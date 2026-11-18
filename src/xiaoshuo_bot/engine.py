from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from .checker import AITicChecker
from .compiler import PromptCompiler
from .contracts import (
    SSE_CHAPTER_ATTEMPT,
    SSE_CHAPTER_STARTED,
    SSE_CHAPTER_STOCKPILED,
    SSE_CRITIC_SCORED,
    SSE_GATE_FAILED,
    SSE_GATE_PASSED,
    SSE_SURGICAL_POLISHED,
)
from .critic_polisher import (
    AuthorNoteViolationError,
    CriticEvaluation,
    CriticResponseParseError,
    SurgicalPolishGateError,
    SurgicalPolishResult,
    TomatoCriticPolisher,
)
from .llm.base import LLMError, LLMProvider, LLMRequest, LLMStreamTruncatedError
from .llm.router import RoleModelRouter
from .memory import MemoryPyramid
from .models import (
    AmbiguousChapterArtifactError,
    ChapterArtifact,
    NovelProject,
    ProductionIncident,
    SHANGHAI_TZ,
    ScanReport,
    SceneOutline,
    Violation,
    format_chapter_filename,
    resolve_chapter_path,
)
from .reporter import generate_html_dashboard, generate_markdown_report
from .state import (
    ForeshadowEvidenceMissingError,
    LockedChapterTamperedError,
    WorldStateError,
    WorldStateManager,
)

ProgressCallback = Callable[[str, Dict[str, Any]], None]

_CHAPTER_TXT_RE = re.compile(r"^chapter_(\d+)\.txt$")


class ProductionGateFailure(RuntimeError):
    """Raised when a chapter still fails AI-TIC-CHECK after exhausting rewrite retries."""


class MissingPredecessorSnapshotError(RuntimeError):
    """Raised when a partial --chapters N (N > 1) run lacks chapter N-1 world-state snapshot."""


class LockedChapterError(RuntimeError):
    """Raised when attempting to overwrite a locked chapter without explicit unlock=True."""


def _now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def _now_shanghai_stamp() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")


def _compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _archive_file(src: Path, output_dir: Path, stamp: str) -> Optional[Path]:
    if not src.exists():
        return None
    try:
        rel = src.resolve().relative_to(output_dir.resolve())
    except ValueError:
        rel = Path(src.name)
    dest = output_dir / "_archive" / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def _write_text_atomic(
    path: Path,
    content: str,
    *,
    output_dir: Optional[Path] = None,
    archive_stamp: Optional[str] = None,
    require_nonempty: bool = False,
) -> None:
    if require_nonempty and not content.strip():
        raise ValueError(f"Refusing to write empty text artifact to '{path}'")
    path.parent.mkdir(parents=True, exist_ok=True)
    if archive_stamp and output_dir is not None and path.exists():
        _archive_file(path, output_dir, archive_stamp)
    # Unique temp file in the same directory + fsync so a crash never leaves a torn target
    # and concurrent writers never collide on a shared '<name>.tmp'.
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
        tmp_path = fh.name
    os.replace(tmp_path, path)


def _write_json_atomic(
    path: Path,
    payload: Any,
    *,
    output_dir: Optional[Path] = None,
    archive_stamp: Optional[str] = None,
) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # Validate round-trip JSON before replacing target
    json.loads(serialized)
    _write_text_atomic(
        path,
        serialized,
        output_dir=output_dir,
        archive_stamp=archive_stamp,
        require_nonempty=True,
    )


def _resolve_snapshot_path(snapshots_dir: Path, chapter: int) -> Optional[Path]:
    if chapter == 0:
        p4 = snapshots_dir / "chapter_0000_initial.json"
        p2 = snapshots_dir / "chapter_00_initial.json"
        if p4.exists() and p2.exists():
            raise AmbiguousChapterArtifactError(
                f"Ambiguous initial snapshot: both '{p4}' and '{p2}' exist"
            )
        if p4.exists():
            return p4
        if p2.exists():
            return p2
        return None

    p4 = snapshots_dir / f"chapter_{chapter:04d}_after.json"
    p2 = snapshots_dir / f"chapter_{chapter:02d}_after.json"
    if p4 != p2 and p4.exists() and p2.exists():
        raise AmbiguousChapterArtifactError(
            f"Ambiguous snapshot artifacts for chapter {chapter}: both '{p4}' and '{p2}' exist"
        )
    if p4.exists():
        return p4
    if p2.exists():
        return p2
    return None


def _resolve_memory_path(snapshots_dir: Path, chapter: int) -> Path:
    if chapter == 0:
        return snapshots_dir / "memory_0000_initial.json"
    return snapshots_dir / f"memory_{chapter:04d}_after.json"


def _chapter_meta_path(chapters_dir: Path, chapter: int) -> Path:
    return chapters_dir / f"chapter_{chapter:04d}.meta.json"


def _scan_completed_chapters(chapters_dir: Path) -> List[Tuple[int, Path]]:
    if not chapters_dir.is_dir():
        return []
    chapter_nums: set[int] = set()
    for p in chapters_dir.iterdir():
        if not p.is_file():
            continue
        m = _CHAPTER_TXT_RE.match(p.name)
        if m:
            chapter_nums.add(int(m.group(1)))
    resolved: List[Tuple[int, Path]] = []
    for ch_no in sorted(chapter_nums):
        if ch_no <= 0:
            continue
        p = resolve_chapter_path(chapters_dir, ch_no)
        if p is not None:
            resolved.append((ch_no, p))
    return resolved


class NovelGenerationEngine:
    """End-to-end Asset-Driven Novel Generation, Gate, Surgical Polish, and State Ledger Engine 2.0."""

    def __init__(
        self,
        project: NovelProject,
        provider: LLMProvider | RoleModelRouter,
        output_dir: str | Path,
        checker: Optional[AITicChecker] = None,
        *,
        critic_polisher: Optional[TomatoCriticPolisher] = None,
        enable_critic_polish: Optional[bool] = None,
        memory_pyramid: Optional[MemoryPyramid] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> None:
        self.project = project
        self.provider = provider
        if isinstance(provider, RoleModelRouter):
            self.router: Optional[RoleModelRouter] = provider
            self.writer_provider: LLMProvider = provider.as_provider("writer")
        else:
            self.router = None
            self.writer_provider = provider

        self.output_dir = Path(output_dir).resolve()
        self.checker = checker or AITicChecker(
            pov_mode=project.meta.pov_mode,
            panel_policy=project.meta.panel_policy,
            contract_track=project.meta.contract_track,
        )
        self.compiler = PromptCompiler(project)
        self.state = WorldStateManager(project)
        self.memory = memory_pyramid or MemoryPyramid()
        for cid, ch_obj in project.characters.items():
            self.memory.register_entity_name(cid, ch_obj.name, "character")
        for aid, asset_obj in project.assets.items():
            self.memory.register_entity_name(aid, asset_obj.name, "asset")

        if critic_polisher is not None:
            self.critic_polisher: Optional[TomatoCriticPolisher] = critic_polisher
        elif enable_critic_polish is True:
            self.critic_polisher = TomatoCriticPolisher(provider, checker=self.checker)
        elif (
            enable_critic_polish is None
            and self.router is not None
            and self.router.has_role("critic")
            and self.router.has_role("polisher")
        ):
            self.critic_polisher = TomatoCriticPolisher(self.router, checker=self.checker)
        else:
            self.critic_polisher = None

        self._default_on_progress = on_progress
        self.incidents: List[ProductionIncident] = []

    def _emit(
        self,
        callback: Optional[ProgressCallback],
        event: str,
        payload: Dict[str, Any],
    ) -> None:
        cb = callback or self._default_on_progress
        if cb is not None:
            enriched = {
                "project_id": self.project.meta.project_id,
                "timestamp": _now_shanghai_iso(),
                **payload,
            }
            cb(event, enriched)

    def _entity_names_map(self) -> Dict[str, str]:
        names: Dict[str, str] = {cid: c.name for cid, c in self.project.characters.items()}
        for aid, a in self.project.assets.items():
            names[aid] = a.name
        return names

    @staticmethod
    def _writer_max_tokens(scene: SceneOutline) -> int:
        """Size the writer output budget from the scene char ceiling (CJK ~1+ token/char + headroom)."""
        max_chars = int(getattr(scene, "max_chars", 0) or 0)
        return max(6000, int(max_chars * 2.5) + 2000)

    def _validate_state_gate(self, scene: SceneOutline, text: str) -> Optional[Violation]:
        """Run WorldStateManager.validate_chapter without mutating state.

        Text-dependent failures (missing foreshadow/expectation evidence, locked-chapter hash
        mismatch) are returned as a CRITICAL Violation so the rewrite loop can fix them.
        Outline-level WorldStateErrors propagate (they can't be fixed by rewriting).
        """
        try:
            self.state.validate_chapter(scene, text)
        except (ForeshadowEvidenceMissingError, LockedChapterTamperedError) as exc:
            rule = (
                "STATE_LOCKED_CHAPTER_TAMPERED"
                if isinstance(exc, LockedChapterTamperedError)
                else "STATE_EVIDENCE_MISSING"
            )
            return Violation(
                rule_id=rule,
                category="STATE_CONSISTENCY_VIOLATION",
                severity="CRITICAL",
                matched_text=str(exc)[:300],
                line_number=1,
                context_snippet=str(exc)[:300],
                fix_hint=(
                    "世界状态校验未通过：本章必须在正文中自然写出要求的物理关键词作为伏笔/期待证据"
                    f"（{exc}）"
                ),
            )
        return None

    def _record_degradation(
        self,
        scene: SceneOutline,
        attempt: int,
        *,
        stage: str,
        exc: BaseException,
        action_taken: str,
        incidents_jsonl: Path,
    ) -> ProductionIncident:
        incident = ProductionIncident(
            incident_id=f"INC-CH{scene.chapter:04d}-A{attempt}-{stage}",
            chapter=scene.chapter,
            scene_id=scene.scene_id,
            attempt=attempt,
            timestamp=_now_shanghai_iso(),
            violations=[
                Violation(
                    rule_id=stage,
                    category="PIPELINE_DEGRADED",
                    severity="WARNING",
                    matched_text=f"{type(exc).__name__}: {str(exc)[:300]}",
                    line_number=0,
                    context_snippet="",
                    fix_hint=action_taken,
                )
            ],
            action_taken=action_taken,
        )
        self.incidents.append(incident)
        incidents_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with incidents_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(incident.to_dict(), ensure_ascii=False) + "\n")
        print(
            f"[Engine] DEGRADED {incident.incident_id}: {type(exc).__name__}: {str(exc)[:200]} -> {action_taken}",
            flush=True,
        )
        return incident

    def _load_chapter_meta(self, chapters_dir: Path, chapter: int) -> Dict[str, Any]:
        meta_path = _chapter_meta_path(chapters_dir, chapter)
        if not meta_path.is_file():
            return {}
        return dict(json.loads(meta_path.read_text(encoding="utf-8")))

    def _is_chapter_locked(
        self,
        scene: SceneOutline,
        chapters_dir: Path,
        existing_ch_file: Optional[Path],
    ) -> bool:
        meta = self._load_chapter_meta(chapters_dir, scene.chapter)
        if bool(meta.get("locked", False)):
            return True
        if scene.chapter in self.state.locked_chapters:
            return True
        if scene.locked and existing_ch_file is not None and existing_ch_file.exists():
            return True
        if (
            self.project.meta.max_published_chapter > 0
            and scene.chapter <= self.project.meta.max_published_chapter
            and existing_ch_file is not None
            and existing_ch_file.exists()
        ):
            return True
        return False

    def _restore_predecessor_state(
        self,
        predecessor_chapter: int,
        chapters_dir: Path,
        snapshots_dir: Path,
    ) -> None:
        snap_path = _resolve_snapshot_path(snapshots_dir, predecessor_chapter)
        if snap_path is None:
            raise MissingPredecessorSnapshotError(
                f"Cannot run chapter {predecessor_chapter + 1}: missing predecessor snapshot "
                f"for chapter {predecessor_chapter} in '{snapshots_dir}'."
            )
        self.state.load_snapshot(snap_path, chapters_dir=chapters_dir)
        # Normalize current_chapter for legacy v1 snapshots lacking current_chapter field
        if self.state.current_chapter == 0 and self.state.completed_chapters:
            self.state.current_chapter = max(self.state.completed_chapters)

        mem_path = _resolve_memory_path(snapshots_dir, predecessor_chapter)
        if mem_path.is_file():
            self.memory = MemoryPyramid.load(mem_path)
            return

        # Reconstruct pre-chapter MemoryPyramid deterministically for legacy projects without memory_*.json
        rebuilt = MemoryPyramid()
        names_map = self._entity_names_map()
        for eid, ename in names_map.items():
            etype = "character" if eid in self.project.characters else "asset"
            rebuilt.register_entity_name(eid, ename, etype)
        scene_map = {s.chapter: s for s in self.project.scenes}
        for ch_no in sorted(c for c in self.state.completed_chapters if 1 <= c <= predecessor_chapter):
            ch_file = resolve_chapter_path(chapters_dir, ch_no)
            if ch_file is not None and ch_file.is_file():
                body = ch_file.read_text(encoding="utf-8").rstrip()
                rebuilt.record_chapter(
                    ch_no,
                    body,
                    scene=scene_map.get(ch_no),
                    entity_names=names_map,
                )
        self.memory = rebuilt

    def _sync_existing_chapter_into_state(
        self,
        scene: SceneOutline,
        existing_text: str,
        chapters_dir: Path,
        snapshots_dir: Path,
    ) -> Dict[str, Any]:
        """Advance in-memory state/memory for a resumed existing chapter without overwriting files."""
        meta = self._load_chapter_meta(chapters_dir, scene.chapter)
        if bool(meta.get("locked", False)) or scene.locked:
            expected_sha = str(meta.get("sha256") or "")
            if expected_sha and _compute_sha256(existing_text) != expected_sha:
                raise LockedChapterTamperedError(
                    f"Locked chapter {scene.chapter} content mismatches recorded SHA256 in '{_chapter_meta_path(chapters_dir, scene.chapter)}'"
                )
            if scene.chapter not in self.state.locked_chapters:
                self.state.chapter_hashes[scene.chapter] = _compute_sha256(existing_text)
                self.state.locked_chapters.append(scene.chapter)

        state_diff = self.state.advance_after_chapter(scene, existing_text)
        self.memory.record_chapter(
            scene.chapter,
            existing_text,
            scene=scene,
            entity_names=self._entity_names_map(),
        )
        return state_diff

    def run(
        self,
        target_chapters: Optional[List[int]] = None,
        resume: bool = False,
        *,
        unlock: bool = False,
        on_progress: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        chapters_dir = self.output_dir / "chapters"
        audit_dir = self.output_dir / "audit"
        snapshots_dir = self.output_dir / "snapshots"
        for d in (chapters_dir, audit_dir, snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)

        run_stamp = _now_shanghai_stamp()
        is_partial_run = target_chapters is not None

        incidents_jsonl = audit_dir / "incidents.jsonl"
        if not resume and not is_partial_run and incidents_jsonl.exists():
            _archive_file(incidents_jsonl, self.output_dir, run_stamp)

        # Save initial state snapshot (Chapter 0000 baseline) without clobbering on partial/resume runs
        initial_snap_path = _resolve_snapshot_path(snapshots_dir, 0)
        if not resume and not is_partial_run:
            if initial_snap_path is not None and initial_snap_path.exists():
                _archive_file(initial_snap_path, self.output_dir, run_stamp)
            target_init_snap = snapshots_dir / "chapter_0000_initial.json"
            self.state.save_snapshot(target_init_snap)
            self.memory.save(_resolve_memory_path(snapshots_dir, 0))
        elif initial_snap_path is None:
            target_init_snap = snapshots_dir / "chapter_0000_initial.json"
            self.state.save_snapshot(target_init_snap)
            mem0 = _resolve_memory_path(snapshots_dir, 0)
            if not mem0.exists():
                self.memory.save(mem0)

        target_set = set(target_chapters) if target_chapters is not None else None
        scenes = sorted(
            [s for s in self.project.scenes if target_set is None or s.chapter in target_set],
            key=lambda s: s.chapter,
        )
        if target_chapters is not None and not scenes:
            raise ValueError(
                f"No matching scenes found for target_chapters={target_chapters!r}"
            )

        chapter_summaries: List[Dict[str, Any]] = []
        started_at = _now_shanghai_iso()
        scene_map_all = {s.chapter: s for s in self.project.scenes}

        try:
            for scene in scenes:
                # Enforce N-1 world-state snapshot continuity whenever starting at or jumping to N > 1
                if scene.chapter > 1 and (
                    self.state.current_chapter < scene.chapter - 1
                    or (scene.chapter - 1) not in self.state.completed_chapters
                ):
                    self._restore_predecessor_state(
                        scene.chapter - 1,
                        chapters_dir=chapters_dir,
                        snapshots_dir=snapshots_dir,
                    )

                existing_ch_file = resolve_chapter_path(chapters_dir, scene.chapter)
                is_locked = self._is_chapter_locked(scene, chapters_dir, existing_ch_file)

                if resume and existing_ch_file is not None and existing_ch_file.exists():
                    existing_text = existing_ch_file.read_text(encoding="utf-8").rstrip()
                    due_fs = self.state.get_due_foreshadows(scene.chapter)
                    plant_fs = self.state.get_plant_foreshadows(scene.chapter)
                    chk = self.checker.scan(
                        text=existing_text,
                        scene=scene,
                        characters=self.project.characters,
                        due_foreshadows=due_fs,
                        plant_foreshadows=plant_fs,
                        project_meta=self.project.meta,
                        world_state=self.state.to_snapshot(),
                        chapter=scene.chapter,
                    )
                    if chk.passed and self._validate_state_gate(scene, existing_text) is not None:
                        print(
                            f"[Engine] Chapter {scene.chapter:04d} checkpoint fails world-state evidence "
                            f"gate; regenerating instead of resuming",
                            flush=True,
                        )
                        chk.passed = False
                    if chk.passed:
                        state_diff = self._sync_existing_chapter_into_state(
                            scene, existing_text, chapters_dir, snapshots_dir
                        )
                        existing_meta = self._load_chapter_meta(chapters_dir, scene.chapter)
                        public_title = str(
                            existing_meta.get("public_title")
                            or scene.public_title
                            or scene.internal_title
                        )
                        author_note = str(existing_meta.get("author_note") or "")
                        sha256_val = str(
                            existing_meta.get("sha256") or _compute_sha256(existing_text)
                        )
                        print(
                            f"[Engine] Chapter {scene.chapter:04d} resumed from verified checkpoint "
                            f"({chk.char_count} chars)",
                            flush=True,
                        )
                        chapter_summaries.append(
                            {
                                "chapter": scene.chapter,
                                "volume": scene.volume,
                                "scene_id": scene.scene_id,
                                "internal_title": scene.internal_title,
                                "public_title": public_title,
                                "pov_character": self.project.characters[scene.pov_character].name,
                                "attempts_used": 1,
                                "rewrites_triggered": 0,
                                "char_count": chk.char_count,
                                "paragraph_count": chk.paragraph_count,
                                "first_char": existing_text[0] if existing_text else "",
                                "opening_preview": existing_text[:68].replace("\n", " "),
                                "ending_preview": existing_text[-68:].replace("\n", " "),
                                "model": str(existing_meta.get("model") or "checkpoint"),
                                "latency_sec": 0.0,
                                "chapter_file": str(existing_ch_file),
                                "meta_file": str(_chapter_meta_path(chapters_dir, scene.chapter)),
                                "author_note": author_note,
                                "sha256": sha256_val,
                                "locked": is_locked,
                                "state_transitions": state_diff,
                            }
                        )
                        continue
                    if is_locked and not unlock:
                        raise LockedChapterError(
                            f"Chapter {scene.chapter} is locked and failed resume verification; "
                            f"pass unlock=True (--unlock) to overwrite."
                        )

                if is_locked and not unlock:
                    raise LockedChapterError(
                        f"Chapter {scene.chapter} is locked and cannot be overwritten without "
                        f"unlock=True (--unlock)."
                    )

                if unlock and is_locked:
                    if scene.chapter in self.state.locked_chapters:
                        self.state.locked_chapters = [
                            c for c in self.state.locked_chapters if c != scene.chapter
                        ]
                    self.state.chapter_hashes.pop(scene.chapter, None)

                ch_result = self._run_single_chapter(
                    scene=scene,
                    chapters_dir=chapters_dir,
                    audit_dir=audit_dir,
                    snapshots_dir=snapshots_dir,
                    incidents_jsonl=incidents_jsonl,
                    run_stamp=run_stamp,
                    on_progress=on_progress,
                )
                chapter_summaries.append(ch_result)
        finally:
            if hasattr(self.writer_provider, "close"):
                self.writer_provider.close()
            if self.provider is not self.writer_provider and hasattr(self.provider, "close"):
                self.provider.close()

        finished_at = _now_shanghai_iso()

        # Assemble full manuscript across ALL completed chapters on disk sorted by chapter number
        all_on_disk = _scan_completed_chapters(chapters_dir)
        pure_parts: List[str] = []
        reader_parts: List[str] = [
            f"# 《{self.project.meta.title}》\n\n> {self.project.meta.synopsis}\n"
        ]
        for ch_no, ch_file in all_on_disk:
            body = ch_file.read_text(encoding="utf-8")
            pure_parts.append(body)
            meta = self._load_chapter_meta(chapters_dir, ch_no)
            sc = scene_map_all.get(ch_no)
            title_part = str(
                meta.get("public_title")
                or (sc.public_title if sc else "")
                or (sc.internal_title if sc else "")
            ).strip()
            heading = f"## 第 {ch_no} 章 {title_part}".strip()
            reader_parts.append(f"{heading}\n\n{body}\n")

        manuscript_pure_path = self.output_dir / "manuscript_pure.txt"
        _write_text_atomic(
            manuscript_pure_path,
            ("\n\n".join(pure_parts) + "\n") if pure_parts else "",
        )

        manuscript_reader_path = self.output_dir / "manuscript_reader.md"
        _write_text_atomic(
            manuscript_reader_path,
            "\n\n".join(reader_parts),
        )

        total_chars = sum(c["char_count"] for c in chapter_summaries)
        manifest: Dict[str, Any] = {
            "project": self.project.meta.to_dict(),
            "started_at": started_at,
            "finished_at": finished_at,
            "timezone": "Asia/Shanghai",
            "chapter_count": len(chapter_summaries),
            "all_completed_chapters_on_disk": [ch_no for ch_no, _ in all_on_disk],
            "total_chars": total_chars,
            "total_incidents_detected": len(self.incidents),
            "all_gates_passed": True,
            "chapters": chapter_summaries,
            "incidents": [inc.to_dict() for inc in self.incidents],
            "final_world_state": self.state.snapshot_dict(),
            "artifacts": {
                "manuscript_pure": str(manuscript_pure_path),
                "manuscript_reader": str(manuscript_reader_path),
            },
        }

        report_md_path = self.output_dir / "SMOKE_TEST_REPORT.md"
        _write_text_atomic(report_md_path, generate_markdown_report(manifest))

        dashboard_html_path = self.output_dir / "smoke_test_dashboard.html"
        _write_text_atomic(
            dashboard_html_path,
            generate_html_dashboard(manifest, chapters_dir, output_dir=self.output_dir),
        )

        manifest["artifacts"]["smoke_test_report"] = str(report_md_path)
        manifest["artifacts"]["dashboard_html"] = str(dashboard_html_path)
        manifest_path = self.output_dir / "run_manifest.json"
        _write_json_atomic(manifest_path, manifest)
        return manifest

    def _run_single_chapter(
        self,
        scene: SceneOutline,
        chapters_dir: Path,
        audit_dir: Path,
        snapshots_dir: Path,
        incidents_jsonl: Path,
        *,
        run_stamp: str,
        on_progress: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        max_attempts = self.project.meta.max_rewrite_retries + 1
        due_fs = self.state.get_due_foreshadows(scene.chapter)
        plant_fs = self.state.get_plant_foreshadows(scene.chapter)

        # Outline-only preflight (deceased on stage, ledger underflow / volume cap, foreshadow
        # keyword config, asset refs): fail with a clear configuration error BEFORE spending tokens.
        try:
            self.state.preflight_scene(scene)
        except WorldStateError as exc:
            print(
                f"[Engine] Chapter {scene.chapter:04d} ({scene.scene_id}) outline preflight FAILED "
                f"before any LLM call: {exc}",
                flush=True,
            )
            raise

        self._emit(
            on_progress,
            SSE_CHAPTER_STARTED,
            {"chapter": scene.chapter, "scene_id": scene.scene_id, "volume": scene.volume},
        )

        sys_prompt, user_prompt = self.compiler.compile_scene_prompt(
            scene, self.state, memory_pyramid=self.memory
        )
        _write_text_atomic(
            audit_dir / f"ch{scene.chapter:04d}_compiled_prompt.md",
            f"{sys_prompt}\n\n{user_prompt}\n",
        )

        last_report: Optional[ScanReport] = None
        last_draft: str = ""

        for attempt in range(1, max_attempts + 1):
            purpose = "draft" if attempt == 1 else "rewrite"
            if attempt > 1 and last_report is not None:
                sys_prompt, user_prompt = self.compiler.compile_rewrite_prompt(
                    scene=scene,
                    state=self.state,
                    draft_text=last_draft,
                    report=last_report,
                    memory_pyramid=self.memory,
                )
                _write_text_atomic(
                    audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_rewrite_prompt.md",
                    f"{sys_prompt}\n\n{user_prompt}\n",
                )

            req_id = f"ch{scene.chapter:04d}_attempt{attempt}_{purpose}"
            llm_req = LLMRequest(
                request_id=req_id,
                chapter=scene.chapter,
                attempt=attempt,
                purpose=purpose,
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                temperature=0.65 if attempt == 1 else 0.45,
                max_tokens=self._writer_max_tokens(scene),
            )

            print(
                f"[Engine] Chapter {scene.chapter:04d} ({scene.scene_id}) | "
                f"Attempt {attempt}/{max_attempts} ({purpose})",
                flush=True,
            )
            self._emit(
                on_progress,
                SSE_CHAPTER_ATTEMPT,
                {
                    "chapter": scene.chapter,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "purpose": purpose,
                },
            )
            try:
                llm_resp = self.writer_provider.generate(llm_req)
            except LLMStreamTruncatedError as exc:
                # One in-attempt retry with a larger output budget (usage already billed upstream).
                self._record_degradation(
                    scene,
                    attempt,
                    stage="WRITER_TRUNCATED",
                    exc=exc,
                    action_taken="Writer stream truncated -> single retry with larger max_tokens",
                    incidents_jsonl=incidents_jsonl,
                )
                llm_req = LLMRequest(
                    request_id=f"{req_id}_truncretry",
                    chapter=llm_req.chapter,
                    attempt=llm_req.attempt,
                    purpose=llm_req.purpose,
                    system_prompt=llm_req.system_prompt,
                    user_prompt=llm_req.user_prompt,
                    temperature=llm_req.temperature,
                    max_tokens=int(llm_req.max_tokens * 1.5),
                    metadata=dict(llm_req.metadata),
                )
                llm_resp = self.writer_provider.generate(llm_req)
            candidate_text = llm_resp.content.rstrip()

            report = self.checker.scan(
                text=candidate_text,
                scene=scene,
                characters=self.project.characters,
                due_foreshadows=due_fs,
                plant_foreshadows=plant_fs,
                project_meta=self.project.meta,
                world_state=self.state.to_snapshot(),
                chapter=scene.chapter,
            )
            if report.passed:
                # World-state gate (stricter evidence rules than the checker) must pass BEFORE any
                # chapter artifact is written, otherwise we'd leave an orphan chapter on disk.
                state_violation = self._validate_state_gate(scene, candidate_text)
                if state_violation is not None:
                    report.violations.append(state_violation)
                    report.passed = False
            _write_json_atomic(
                audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_scan.json",
                report.to_dict(),
            )

            if report.passed:
                self._emit(
                    on_progress,
                    SSE_GATE_PASSED,
                    {
                        "chapter": scene.chapter,
                        "attempt": attempt,
                        "char_count": report.char_count,
                    },
                )

                critic_eval: Optional[CriticEvaluation] = None
                polish_res: Optional[SurgicalPolishResult] = None
                author_note = ""

                if self.critic_polisher is not None:
                    # Critic / polish / author-note are post-gate enhancements: an upstream LLM error
                    # or malformed critic output must degrade gracefully, never discard a gated chapter.
                    try:
                        critic_eval = self.critic_polisher.evaluate_chapter(
                            candidate_text,
                            scene=scene,
                            project_meta=self.project.meta,
                            scan_report=report,
                            characters=self.project.characters,
                            world_state=self.state.to_snapshot(),
                            chapter=scene.chapter,
                        )
                    except (LLMError, CriticResponseParseError) as exc:
                        critic_eval = None
                        self._record_degradation(
                            scene,
                            attempt,
                            stage="CRITIC_FAILED",
                            exc=exc,
                            action_taken="Critic evaluation failed -> keeping gated text without critic score/polish",
                            incidents_jsonl=incidents_jsonl,
                        )
                if self.critic_polisher is not None and critic_eval is not None:
                    _write_json_atomic(
                        audit_dir / f"ch{scene.chapter:04d}_critic_eval.json",
                        critic_eval.to_dict(),
                    )
                    self._emit(
                        on_progress,
                        SSE_CRITIC_SCORED,
                        {
                            "chapter": scene.chapter,
                            "overall_score": critic_eval.overall_score,
                            "weak_paragraphs": list(critic_eval.weak_paragraphs),
                        },
                    )
                    if critic_eval.weak_paragraphs:
                        try:
                            polish_res = self.critic_polisher.surgical_polish(
                                candidate_text,
                                critic_eval,
                                scene=scene,
                                project_meta=self.project.meta,
                                characters=self.project.characters,
                                due_foreshadows=due_fs,
                                plant_foreshadows=plant_fs,
                                world_state=self.state.to_snapshot(),
                                chapter=scene.chapter,
                            )
                        except SurgicalPolishGateError as exc:
                            polish_report = exc.report or report
                            incident = ProductionIncident(
                                incident_id=f"INC-CH{scene.chapter:04d}-A{attempt}-POLISH",
                                chapter=scene.chapter,
                                scene_id=scene.scene_id,
                                attempt=attempt,
                                timestamp=_now_shanghai_iso(),
                                violations=polish_report.violations,
                                action_taken=(
                                    f"Surgical polish broke gate -> rewrite attempt {attempt + 1}"
                                    if attempt < max_attempts
                                    else "Surgical polish broke gate — exhausted retries"
                                ),
                            )
                            self.incidents.append(incident)
                            with incidents_jsonl.open("a", encoding="utf-8") as f:
                                f.write(json.dumps(incident.to_dict(), ensure_ascii=False) + "\n")
                            _write_text_atomic(
                                audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_rejected.txt",
                                exc.polished_text or candidate_text,
                            )
                            self._emit(
                                on_progress,
                                SSE_GATE_FAILED,
                                {
                                    "chapter": scene.chapter,
                                    "attempt": attempt,
                                    "incident_id": incident.incident_id,
                                    "violation_count": len(polish_report.violations),
                                },
                            )
                            last_report = polish_report
                            last_draft = exc.polished_text or candidate_text
                            continue
                        except (LLMError, CriticResponseParseError) as exc:
                            polish_res = None
                            self._record_degradation(
                                scene,
                                attempt,
                                stage="POLISH_FAILED",
                                exc=exc,
                                action_taken="Surgical polish failed -> keeping unpolished gated text",
                                incidents_jsonl=incidents_jsonl,
                            )

                        if polish_res is not None:
                            polish_state_violation = self._validate_state_gate(
                                scene, polish_res.polished_text
                            )
                            if polish_state_violation is not None:
                                self._record_degradation(
                                    scene,
                                    attempt,
                                    stage="POLISH_STATE_GATE",
                                    exc=RuntimeError(polish_state_violation.matched_text),
                                    action_taken="Polished text failed world-state gate -> keeping unpolished gated text",
                                    incidents_jsonl=incidents_jsonl,
                                )
                                polish_res = None

                        if polish_res is not None:
                            candidate_text = polish_res.polished_text
                            report = polish_res.scan_report
                            _write_json_atomic(
                                audit_dir / f"ch{scene.chapter:04d}_polish.json",
                                polish_res.to_dict(),
                            )
                            self._emit(
                                on_progress,
                                SSE_SURGICAL_POLISHED,
                                {
                                    "chapter": scene.chapter,
                                    "replaced_indices": list(polish_res.replaced_indices),
                                },
                            )

                if self.critic_polisher is not None:
                    try:
                        author_note = self.critic_polisher.generate_author_note(
                            candidate_text,
                            scene=scene,
                            project_meta=self.project.meta,
                            chapter=scene.chapter,
                        )
                    except (LLMError, CriticResponseParseError, AuthorNoteViolationError) as exc:
                        author_note = ""
                        self._record_degradation(
                            scene,
                            attempt,
                            stage="AUTHOR_NOTE_FAILED",
                            exc=exc,
                            action_taken="Author note generation failed -> empty author_note",
                            incidents_jsonl=incidents_jsonl,
                        )

                print(
                    f"[Engine] Chapter {scene.chapter:04d} PASSED AI-TIC-CHECK "
                    f"({report.char_count} chars, {report.paragraph_count} paragraphs, attempt {attempt})",
                    flush=True,
                )

                ch_filename = format_chapter_filename(scene.chapter)
                ch_path = chapters_dir / ch_filename
                legacy_path = chapters_dir / f"chapter_{scene.chapter:02d}.txt"
                if legacy_path != ch_path and legacy_path.exists():
                    _archive_file(legacy_path, self.output_dir, run_stamp)

                _write_text_atomic(
                    ch_path,
                    candidate_text,
                    output_dir=self.output_dir,
                    archive_stamp=run_stamp,
                    require_nonempty=True,
                )

                public_title = (scene.public_title or scene.internal_title).strip()
                artifact = ChapterArtifact(
                    chapter=scene.chapter,
                    public_title=public_title,
                    body=candidate_text,
                    author_note=author_note,
                    sha256=_compute_sha256(candidate_text),
                    locked=scene.locked,
                    scene_id=scene.scene_id,
                )
                meta_payload = {
                    **artifact.to_dict(),
                    "model": llm_resp.model,
                    "updated_at": _now_shanghai_iso(),
                }
                if critic_eval is not None:
                    meta_payload["critic_score"] = critic_eval.overall_score
                meta_path = _chapter_meta_path(chapters_dir, scene.chapter)
                _write_json_atomic(
                    meta_path,
                    meta_payload,
                    output_dir=self.output_dir,
                    archive_stamp=run_stamp,
                )

                state_diff = self.state.advance_after_chapter(scene, candidate_text)
                self.memory.record_chapter(
                    scene.chapter,
                    candidate_text,
                    scene=scene,
                    entity_names=self._entity_names_map(),
                )

                snap_path = snapshots_dir / f"chapter_{scene.chapter:04d}_after.json"
                legacy_snap = snapshots_dir / f"chapter_{scene.chapter:02d}_after.json"
                if legacy_snap != snap_path and legacy_snap.exists():
                    _archive_file(legacy_snap, self.output_dir, run_stamp)
                if snap_path.exists():
                    _archive_file(snap_path, self.output_dir, run_stamp)
                self.state.save_snapshot(snap_path)

                mem_path = _resolve_memory_path(snapshots_dir, scene.chapter)
                if mem_path.exists():
                    _archive_file(mem_path, self.output_dir, run_stamp)
                self.memory.save(mem_path)

                self._emit(
                    on_progress,
                    SSE_CHAPTER_STOCKPILED,
                    {
                        "chapter": scene.chapter,
                        "char_count": report.char_count,
                        "sha256": artifact.sha256,
                        "locked": artifact.locked,
                    },
                )

                return {
                    "chapter": scene.chapter,
                    "volume": scene.volume,
                    "scene_id": scene.scene_id,
                    "internal_title": scene.internal_title,
                    "public_title": public_title,
                    "pov_character": self.project.characters[scene.pov_character].name,
                    "attempts_used": attempt,
                    "rewrites_triggered": attempt - 1,
                    "char_count": report.char_count,
                    "paragraph_count": report.paragraph_count,
                    "first_char": candidate_text[0],
                    "opening_preview": candidate_text[:68].replace("\n", " "),
                    "ending_preview": candidate_text[-68:].replace("\n", " "),
                    "model": llm_resp.model,
                    "latency_sec": llm_resp.latency_sec,
                    "chapter_file": str(ch_path),
                    "meta_file": str(meta_path),
                    "author_note": author_note,
                    "sha256": artifact.sha256,
                    "locked": artifact.locked,
                    "critic_evaluation": critic_eval.to_dict() if critic_eval is not None else None,
                    "polish_summary": (
                        {
                            "replaced_indices": list(polish_res.replaced_indices),
                            "model": polish_res.model,
                        }
                        if polish_res is not None
                        else None
                    ),
                    "state_transitions": state_diff,
                }

            # Record Production Incident & trigger automatic rewrite
            incident = ProductionIncident(
                incident_id=f"INC-CH{scene.chapter:04d}-A{attempt}",
                chapter=scene.chapter,
                scene_id=scene.scene_id,
                attempt=attempt,
                timestamp=_now_shanghai_iso(),
                violations=report.violations,
                action_taken=(
                    f"Triggered automatic rewrite attempt {attempt + 1}"
                    if attempt < max_attempts
                    else "Exhausted retries — aborting pipeline"
                ),
            )
            self.incidents.append(incident)
            with incidents_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(incident.to_dict(), ensure_ascii=False) + "\n")

            _write_text_atomic(
                audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_rejected.txt",
                candidate_text,
            )
            print(
                f"[Engine] PRODUCTION INCIDENT {incident.incident_id}: "
                f"{len(report.violations)} violation(s) -> {incident.action_taken}",
                flush=True,
            )
            self._emit(
                on_progress,
                SSE_GATE_FAILED,
                {
                    "chapter": scene.chapter,
                    "attempt": attempt,
                    "incident_id": incident.incident_id,
                    "violation_count": len(report.violations),
                },
            )
            last_report = report
            last_draft = candidate_text

        raise ProductionGateFailure(
            f"Chapter {scene.chapter} failed AI-TIC-CHECK after {max_attempts} attempts. "
            f"Last violations: {[v.to_dict() for v in (last_report.violations if last_report else [])]}"
        )
