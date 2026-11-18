from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import time

from .base import LLMProvider, LLMRequest, LLMResponse, SHANGHAI_TZ


def _archive_file(src: Path, archive_root: Path) -> Path:
    """Move stale bridge files into _archive/<timestamp>/ instead of permanent deletion."""
    stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d_%H%M%S_%f")
    dest_dir = archive_root / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}_{time.monotonic_ns()}{src.suffix}"
    shutil.move(str(src), str(dest))
    return dest


class FileBridgeProvider(LLMProvider):
    """File-spool IPC bridge allowing stateless external/agentic LLM workers to service requests."""

    def __init__(
        self,
        bridge_dir: str | Path,
        poll_interval: float = 0.5,
        timeout_sec: int = 900,
        reuse_existing: bool = False,
    ) -> None:
        self.root = Path(bridge_dir).resolve()
        self.requests_dir = self.root / "requests"
        self.responses_dir = self.root / "responses"
        self.archive_dir = self.root / "_archive"
        self.done_sentinel = self.root / "DONE"
        self.poll_interval = poll_interval
        self.timeout_sec = timeout_sec
        self.reuse_existing = reuse_existing

        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        if self.done_sentinel.exists():
            _archive_file(self.done_sentinel, self.archive_dir)

    def generate(self, request: LLMRequest) -> LLMResponse:
        req_json_path = self.requests_dir / f"{request.request_id}.json"
        req_md_path = self.requests_dir / f"{request.request_id}.md"
        resp_txt_path = self.responses_dir / f"{request.request_id}.txt"

        if not self.reuse_existing and resp_txt_path.exists():
            _archive_file(resp_txt_path, self.archive_dir)

        if not (self.reuse_existing and req_md_path.exists()):
            envelope = {
                "request_id": request.request_id,
                "chapter": request.chapter,
                "attempt": request.attempt,
                "purpose": request.purpose,
                "system_prompt": request.system_prompt,
                "user_prompt": request.user_prompt,
                "response_path": str(resp_txt_path),
            }
            tmp_json = req_json_path.with_suffix(".json.tmp")
            tmp_json.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_json.replace(req_json_path)

            md_doc = (
                f"<!-- REQUEST_ID: {request.request_id} | CHAPTER: {request.chapter} | "
                f"ATTEMPT: {request.attempt} | PURPOSE: {request.purpose} -->\n"
                f"<!-- RESPONSE_TARGET: {resp_txt_path} -->\n\n"
                f"{request.system_prompt}\n\n"
                f"{request.user_prompt}\n"
            )
            tmp_md = req_md_path.with_suffix(".md.tmp")
            tmp_md.write_text(md_doc, encoding="utf-8")
            tmp_md.replace(req_md_path)

        print(
            f"[BridgeProvider] Dispatched/awaiting {request.request_id} ({request.purpose}) -> {resp_txt_path.name}",
            flush=True,
        )

        t0 = time.monotonic()
        while (time.monotonic() - t0) < self.timeout_sec:
            if resp_txt_path.exists():
                size_1 = resp_txt_path.stat().st_size
                if size_1 > 0:
                    time.sleep(0.2)
                    size_2 = resp_txt_path.stat().st_size
                    if size_2 == size_1:
                        content = resp_txt_path.read_text(encoding="utf-8")
                        return LLMResponse(
                            request_id=request.request_id,
                            content=content,
                            model="agent-bridge-llm",
                            provider="bridge",
                            latency_sec=round(time.monotonic() - t0, 3),
                        )
            time.sleep(self.poll_interval)

        raise TimeoutError(
            f"BridgeProvider timed out after {self.timeout_sec}s waiting for {resp_txt_path}"
        )

    def close(self) -> None:
        self.done_sentinel.write_text("COMPLETED\n", encoding="utf-8")
