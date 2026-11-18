from __future__ import annotations

import http.client
import json
import os
import socket
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from .base import (
    LLMConfigurationError,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMStreamTruncatedError,
    parse_model_reasoning_suffix,
    redact_secrets,
)


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible & CPA streaming Chat Completions provider (zero silent defaults)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 300,
        stream: bool = True,
        reasoning_effort: Optional[str] = None,
        strip_reasoning_suffix: bool = False,
        max_retries: int = 2,
    ) -> None:
        raw_key = (
            api_key
            if api_key is not None
            else (
                os.environ.get("XIAOSHUO_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or ""
            )
        )
        raw_base = (
            base_url
            if base_url is not None
            else (
                os.environ.get("XIAOSHUO_API_BASE")
                or os.environ.get("OPENAI_BASE_URL")
                or ""
            )
        )
        raw_model = (
            model
            if model is not None
            else (
                os.environ.get("XIAOSHUO_MODEL")
                or os.environ.get("OPENAI_MODEL")
                or ""
            )
        )

        self.api_key = raw_key.strip()
        self.base_url = raw_base.strip().rstrip("/")
        self.model = raw_model.strip()
        self.timeout = int(timeout)
        self.stream = bool(stream)
        self.reasoning_effort = reasoning_effort.strip().lower() if reasoning_effort else None
        self.strip_reasoning_suffix = bool(strip_reasoning_suffix)
        self.max_retries = max(1, int(max_retries))

        if not self.api_key:
            raise LLMConfigurationError(
                "Missing API key for OpenAICompatibleProvider. "
                "Provide api_key explicitly or set XIAOSHUO_API_KEY / OPENAI_API_KEY."
            )
        if not self.base_url:
            raise LLMConfigurationError(
                "Missing base_url for OpenAICompatibleProvider. "
                "Provide base_url explicitly or set XIAOSHUO_API_BASE / OPENAI_BASE_URL."
            )
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LLMConfigurationError(
                f"Invalid base_url for OpenAICompatibleProvider: {redact_secrets(self.base_url, [self.api_key])!r}"
            )
        if not self.model:
            raise LLMConfigurationError(
                "Missing model for OpenAICompatibleProvider. "
                "Provide model explicitly or set XIAOSHUO_MODEL / OPENAI_MODEL."
            )
        if self.timeout <= 0:
            raise LLMConfigurationError("timeout must be a positive integer.")
        if self.reasoning_effort and self.reasoning_effort not in {"low", "medium", "high"}:
            raise LLMConfigurationError(
                f"Invalid reasoning_effort {self.reasoning_effort!r}; expected low, medium, or high."
            )

    def _v1_url(self, resource: str) -> str:
        parsed = urllib.parse.urlparse(self.base_url)
        clean_resource = resource.lstrip("/")
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/{clean_resource}"
        if not parsed.path or parsed.path == "/":
            return f"{self.base_url}/v1/{clean_resource}"
        return f"{self.base_url}/{clean_resource}"

    def _redact(self, text: str) -> str:
        return redact_secrets(text, extra_secrets=[self.api_key])

    def list_models(self) -> List[str]:
        """Probe available models via GET /v1/models."""
        url = self._v1_url("models")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            err_body = self._read_http_error(exc)
            raise LLMRequestError(
                f"GET /v1/models failed with HTTP {exc.code}: {err_body}",
                status_code=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMRequestError(
                f"GET /v1/models request error: {self._redact(str(exc))}"
            ) from None

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise LLMRequestError(f"GET /v1/models returned invalid JSON: {exc}") from None

        items = payload.get("data")
        if items is None:
            items = payload.get("models", [])
        models: List[str] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
                elif isinstance(item, str) and item.strip():
                    models.append(item.strip())
        return models

    def _read_http_error(self, exc: urllib.error.HTTPError) -> str:
        raw = ""
        if exc.fp is not None:
            try:
                raw = exc.fp.read(1024).decode("utf-8", errors="replace")
            except (OSError, ValueError):
                raw = ""
        text = raw.strip() or str(exc.reason or exc)
        return self._redact(text)[:500]

    def _build_payload(self, request: LLMRequest, use_stream: bool) -> Dict[str, Any]:
        requested_model = str(request.metadata.get("model") or self.model).strip()
        base_model, suffix_effort = parse_model_reasoning_suffix(requested_model)
        effective_effort = (
            request.metadata.get("reasoning_effort")
            or self.reasoning_effort
            or suffix_effort
        )
        if effective_effort is not None:
            effective_effort = str(effective_effort).strip().lower()
            if effective_effort not in {"low", "medium", "high"}:
                raise LLMConfigurationError(
                    f"Invalid reasoning_effort {effective_effort!r}; expected low, medium, or high."
                )

        wire_model = base_model if self.strip_reasoning_suffix else requested_model
        payload: Dict[str, Any] = {
            "model": wire_model,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "stream": use_stream,
        }
        if use_stream:
            payload["stream_options"] = {"include_usage": True}
        if effective_effort:
            payload["reasoning_effort"] = effective_effort
        return payload

    def _consume_sse(
        self,
        resp: Any,
        request: LLMRequest,
        t0: float,
    ) -> LLMResponse:
        content_parts: List[str] = []
        finish_reason: Optional[str] = None
        saw_done = False
        actual_model = self.model
        usage: Dict[str, Any] = {}

        try:
            while True:
                raw_line = resp.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                if line.startswith(":"):
                    # SSE comment / CPA keepalive heartbeat (e.g. ": keepalive")
                    continue
                if line.startswith(("event:", "id:", "retry:")):
                    continue
                if not line.startswith("data:"):
                    continue

                data_str = line[5:].strip()
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    saw_done = True
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError as exc:
                    raise LLMStreamTruncatedError(
                        f"Malformed SSE JSON chunk for {request.request_id}: {self._redact(str(exc))}"
                    ) from None

                if isinstance(chunk, dict) and chunk.get("error"):
                    err_obj = chunk["error"]
                    err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                    err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    status_code = err_code if isinstance(err_code, int) else None
                    raise LLMRequestError(
                        f"Upstream SSE error: {self._redact(str(err_msg))}",
                        status_code=status_code,
                    )

                if chunk.get("model"):
                    actual_model = str(chunk["model"])
                if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                    usage = dict(chunk["usage"])

                choices = chunk.get("choices") or []
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict):
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            content_parts.append(piece)
                    if choice.get("finish_reason") is not None:
                        finish_reason = str(choice["finish_reason"])
        except (
            http.client.IncompleteRead,
            http.client.HTTPException,
            ConnectionError,
            TimeoutError,
            socket.timeout,
            OSError,
        ) as exc:
            raise LLMStreamTruncatedError(
                f"SSE stream interrupted mid-generation for {request.request_id}: {self._redact(str(exc))}"
            ) from None

        if finish_reason == "length":
            raise LLMStreamTruncatedError(
                f"SSE stream truncated (finish_reason='length') for {request.request_id}."
            )
        if finish_reason != "stop":
            raise LLMStreamTruncatedError(
                f"SSE stream ended without finish_reason='stop' (got {finish_reason!r}) for {request.request_id}."
            )
        if not saw_done:
            raise LLMStreamTruncatedError(
                f"SSE stream closed prematurely before [DONE] for {request.request_id}."
            )

        return LLMResponse(
            request_id=request.request_id,
            content="".join(content_parts),
            model=actual_model,
            provider="openai_compatible",
            latency_sec=round(time.monotonic() - t0, 3),
            usage=usage,
        )

    def _consume_json(
        self,
        resp: Any,
        request: LLMRequest,
        t0: float,
    ) -> LLMResponse:
        raw_body = resp.read().decode("utf-8", errors="replace")
        try:
            resp_data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise LLMRequestError(f"Invalid JSON response: {self._redact(str(exc))}") from None

        choices = resp_data.get("choices") or []
        if not choices:
            raise LLMRequestError("OpenAI-compatible response contained no choices.")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise LLMStreamTruncatedError(
                f"Completion truncated (finish_reason='length') for {request.request_id}."
            )
        if finish_reason is not None and finish_reason != "stop":
            raise LLMStreamTruncatedError(
                f"Completion ended abnormally (finish_reason={finish_reason!r}) for {request.request_id}."
            )
        message = choice.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMRequestError("OpenAI-compatible response missing message.content.")

        return LLMResponse(
            request_id=request.request_id,
            content=content,
            model=str(resp_data.get("model", self.model)),
            provider="openai_compatible",
            latency_sec=round(time.monotonic() - t0, 3),
            usage=dict(resp_data.get("usage") or {}),
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        url = self._v1_url("chat/completions")
        use_stream = (
            bool(request.metadata["stream"])
            if "stream" in request.metadata
            else self.stream
        )
        payload = self._build_payload(request, use_stream=use_stream)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if use_stream else "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        t0 = time.monotonic()
        last_err: Optional[Exception] = None
        for attempt_idx in range(self.max_retries):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if use_stream:
                        return self._consume_sse(resp, request, t0)
                    return self._consume_json(resp, request, t0)
            except LLMStreamTruncatedError:
                raise
            except urllib.error.HTTPError as exc:
                err_msg = self._read_http_error(exc)
                err = LLMRequestError(
                    f"OpenAI-compatible HTTP {exc.code}: {err_msg}",
                    status_code=exc.code,
                )
                # Do not retry 4xx (including 429 so caller/router rotates keys immediately)
                if exc.code < 500 or exc.code not in {500, 502, 503, 504}:
                    raise err from None
                last_err = err
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = LLMRequestError(
                    f"OpenAI-compatible transport error: {self._redact(str(exc))}"
                )

            if attempt_idx + 1 < self.max_retries:
                time.sleep(min(2.0, 0.5 * (2 ** attempt_idx)))

        if isinstance(last_err, LLMError):
            raise last_err
        raise LLMRequestError(
            f"OpenAI-compatible request failed: {self._redact(str(last_err))}"
        )
