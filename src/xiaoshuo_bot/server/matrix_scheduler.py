from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import difflib
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ..contracts import (
    AutoCreateBookRequest,
    BookRunStatus,
    BookSummaryResponse,
    CPABindingConfig,
    ChapterStatusItem,
    CompanionCaptchaReport,
    MatrixControlPort,
    MatrixDailyDigest,
    SSEEventPayload,
    SSE_BOOK_ISOLATED,
    SSE_CHAPTER_STOCKPILED,
    SSE_COMPANION_CAPTCHA,
    SSE_COMPANION_RESUMED,
    SSE_CPA_QUOTA_ALERT,
    SSE_RACING_DECISION,
    SSE_SAFETY_CUSHION_ALERT,
)
from ..cover import CoverGenerationResult, generate_cover_for_project
from ..engine import NovelGenerationEngine, ProductionGateFailure, ProgressCallback
from ..evolution import MotifWeightStore, OutlineAdjustmentDirective
from ..exporter import export_project_bundle
from ..genres import get_genre
from ..llm.base import (
    BudgetCircuitBreakerError,
    LLMConfigurationError,
    LLMProvider,
    LLMRequestError,
    LLMStreamTruncatedError,
    QuotaExhaustedError,
)
from ..llm.cpa_manager import CPAManagementClient
from ..llm.router import RoleEndpointConfig, RoleModelRouter, VALID_ROLES
from ..loader import load_project
from ..models import (
    ChapterArtifact,
    NovelProject,
    ProjectValidationError,
    SHANGHAI_TZ,
    WorldStateSnapshot,
    resolve_chapter_path,
)
from ..planner import AutoNovelPlanner, FinaleVolumeResult, VolumeExtensionResult
from .stockpile_db import (
    SAFETY_CUSHION_CRITICAL,
    SAFETY_CUSHION_TARGET,
    StockpileDB,
    shanghai_now,
)

logger = logging.getLogger(__name__)


class SchedulerConfigurationError(RuntimeError):
    """Raised when a required router/provider/planner/CPA dependency is not configured."""


_SNAPSHOT_FILENAME_RE = re.compile(r"^chapter_(\d+)_after\.json$")


def _select_latest_snapshot_file(snapshots_dir: Path) -> Optional[Path]:
    candidates: List[Tuple[int, int, str, Path]] = []
    for path in snapshots_dir.glob("chapter_*_after.json"):
        match = _SNAPSHOT_FILENAME_RE.match(path.name)
        if match:
            num_str = match.group(1)
            candidates.append((int(num_str), len(num_str), path.name, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[-1][3]


class SSEEventBroker:
    """Thread-safe in-memory pub/sub broker for live SSEEventPayload streaming."""

    def __init__(self, *, history_limit: int = 200) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._subscribers: Dict[str, queue.Queue[SSEEventPayload]] = {}
        self._history: Deque[SSEEventPayload] = deque(maxlen=max(1, int(history_limit)))
        self._poll_condition = threading.Condition()
        self._wake_seq = 0

    def subscribe(self, max_queue_size: int = 256) -> Tuple[str, queue.Queue[SSEEventPayload]]:
        with self._lock:
            self._seq += 1
            sub_id = f"sub-{self._seq}"
            q: queue.Queue[SSEEventPayload] = queue.Queue(maxsize=max_queue_size)
            self._subscribers[sub_id] = q
            return sub_id, q

    def unsubscribe(self, sub_id: str) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, payload: SSEEventPayload) -> SSEEventPayload:
        with self._lock:
            self._seq += 1
            if not payload.event_id:
                payload.event_id = f"evt-{self._seq}"
            self._history.append(payload)
            subs = list(self._subscribers.values())

        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    logger.warning("Dropping SSE event %s for slow subscriber", payload.event_id)

        if payload.event in (
            SSE_CHAPTER_STOCKPILED,
            SSE_COMPANION_RESUMED,
            SSE_RACING_DECISION,
        ):
            self.notify_poll_waiters()
        return payload

    def notify_poll_waiters(self) -> None:
        with self._poll_condition:
            self._wake_seq += 1
            self._poll_condition.notify_all()

    def wait_for_poll_wakeup(self, timeout_sec: float, stop_event: Optional[threading.Event] = None) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self._poll_condition:
            start_seq = self._wake_seq
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                if self._wake_seq != start_seq:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._poll_condition.wait(timeout=min(remaining, 0.5))

    def get_recent_events(self, limit: int = 50) -> List[SSEEventPayload]:
        with self._lock:
            items = list(self._history)
        if limit <= 0:
            return []
        return items[-limit:]


@dataclass(frozen=True)
class QueuedProductionItem:
    book_id: str
    title: str
    stockpiled_count: int
    priority_tier: int  # 0 = URGENT (<6), 1 = NORMAL (6..9)
    urgent: bool


EngineFactory = Callable[[NovelProject, Path, ProgressCallback], Any]


class CPABindingOriginChangeError(ValueError):
    """Raised when a CPA binding update would send existing API keys to a different origin (HTTP 400)."""


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalized_origin(base_url: str) -> Tuple[str, str, int]:
    """Return (scheme, host, port) for an http(s) base_url or raise CPABindingOriginChangeError."""
    raw = str(base_url or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise CPABindingOriginChangeError(f"Invalid base_url {raw!r}: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS:
        raise CPABindingOriginChangeError(
            f"base_url {raw!r} must use http or https scheme."
        )
    if not host:
        raise CPABindingOriginChangeError(f"base_url {raw!r} has no host.")
    return scheme, host, int(port if port is not None else _DEFAULT_PORTS[scheme])


class MatrixScheduler:
    """Cloud matrix orchestrator implementing MatrixControlPort, 10-ch cushion, and fault isolation."""

    def __init__(
        self,
        *,
        db: StockpileDB,
        projects_root: str | Path,
        provider: Optional[RoleModelRouter | LLMProvider] = None,
        engine_factory: Optional[EngineFactory] = None,
        planner_factory: Optional[Callable[[], AutoNovelPlanner]] = None,
        cpa_client: Optional[CPAManagementClient] = None,
        motif_store: Optional[MotifWeightStore] = None,
        event_broker: Optional[SSEEventBroker] = None,
        safety_cushion_target: int = SAFETY_CUSHION_TARGET,
        safety_cushion_critical: int = SAFETY_CUSHION_CRITICAL,
        now_fn: Optional[Callable[[], datetime]] = None,
        export_only: bool = False,
    ) -> None:
        self.db = db
        self.projects_root = Path(projects_root).resolve()
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self._engine_factory = engine_factory
        self._planner_factory = planner_factory
        self.cpa_client = cpa_client
        self.motif_store = motif_store
        self.event_broker = event_broker or SSEEventBroker()
        self.safety_cushion_target = int(safety_cushion_target)
        self.safety_cushion_critical = int(safety_cushion_critical)
        self._now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        # Export-only mode (owner decision 23A): no publishing companion consumes the stockpile,
        # so books are written straight through their outline instead of stopping at the cushion.
        self.export_only = bool(export_only)

        self._prod_lock = threading.Lock()
        self._bg_stop = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None

    def _emit(
        self,
        event: str,
        *,
        book_id: str = "",
        chapter: int = 0,
        message: str = "",
        data: Optional[Mapping[str, Any]] = None,
    ) -> SSEEventPayload:
        payload = SSEEventPayload(
            event=event,
            book_id=book_id,
            chapter=int(chapter),
            message=message,
            data=dict(data or {}),
            timestamp=shanghai_now(self._now_fn).isoformat(),
        )
        return self.event_broker.publish(payload)

    def _get_router(self) -> Optional[RoleModelRouter]:
        if isinstance(self.provider, RoleModelRouter):
            return self.provider
        return None

    def _get_planner(self) -> AutoNovelPlanner:
        if self._planner_factory is not None:
            return self._planner_factory()
        if self.provider is None:
            raise SchedulerConfigurationError(
                "LLM provider/RoleModelRouter is not configured for AutoNovelPlanner."
            )
        return AutoNovelPlanner(self.provider)

    # -----------------------------------------------------------------------
    # MatrixControlPort Implementation
    # -----------------------------------------------------------------------
    def get_daily_digest(self) -> MatrixDailyDigest:
        summaries = self.list_books()
        active_count = sum(1 for b in summaries if b.status == BookRunStatus.ACTIVE.value)
        low_cushion_count = sum(1 for b in summaries if b.low_cushion_alert)
        isolated_count = sum(
            1 for b in summaries if b.status == BookRunStatus.ISOLATED_GATE_FAILURE.value
        )
        paused_accounts = self.db.get_paused_captcha_accounts()
        daily_chars = self.db.get_today_generated_chars()
        daily_cost = 0.0
        router = self._get_router()
        if router is not None:
            daily_cost = float(router.ledger.get_summary().get("estimated_cost_usd", 0.0))

        pending_cuts = self.db.get_pending_cut_recommendations()
        alerts: List[str] = []
        for b in summaries:
            if b.status == BookRunStatus.ACTIVE.value and b.stockpiled_count < self.safety_cushion_critical:
                alerts.append(
                    f"[CRITICAL CUSHION] 《{b.title}》({b.book_id}) stockpiled={b.stockpiled_count} < {self.safety_cushion_critical}"
                )
            elif b.status == BookRunStatus.ISOLATED_GATE_FAILURE.value:
                alerts.append(f"[ISOLATED] 《{b.title}》({b.book_id}) gate failure: {b.paused_reason}")
        for acct in paused_accounts:
            alerts.append(f"[CAPTCHA PAUSED] Account '{acct}' awaiting manual captcha resolution")

        return MatrixDailyDigest(
            generated_at=shanghai_now(self._now_fn).isoformat(),
            total_books=len(summaries),
            active_books=active_count,
            low_cushion_books=low_cushion_count,
            isolated_books=isolated_count,
            paused_captcha_accounts=paused_accounts,
            daily_chars_generated=daily_chars,
            daily_cost_usd=round(daily_cost, 6),
            book_summaries=summaries,
            pending_cut_recommendations=pending_cuts,
            alerts=alerts,
        )

    def list_books(self) -> List[BookSummaryResponse]:
        return self.db.list_book_summaries()

    def get_book_status(self, book_id: str) -> BookSummaryResponse:
        return self.db.get_book_summary(book_id)

    def auto_create_book(
        self,
        request: AutoCreateBookRequest,
        *,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> BookSummaryResponse:
        get_genre(request.genre_id)
        pid = (request.project_id or "").strip()
        if not pid or "/" in pid or "\\" in pid or ".." in pid:
            raise ProjectValidationError(
                f"Invalid project_id {request.project_id!r}: path separators and '..' are forbidden"
            )
        projects_root_resolved = self.projects_root.resolve()
        project_dir = (projects_root_resolved / pid).resolve()
        try:
            rel = project_dir.relative_to(projects_root_resolved)
        except ValueError as exc:
            raise ProjectValidationError(
                f"project_id {request.project_id!r} resolves outside projects_root"
            ) from exc
        if rel == Path("."):
            raise ProjectValidationError(
                f"Invalid project_id {request.project_id!r}: cannot point to projects_root"
            )
        planner = self._get_planner()

        result = planner.plan_new_project(
            project_id=pid,
            genre_id=request.genre_id,
            output_dir=project_dir,
            author_level=request.author_level,
            projects_root=self.projects_root,
            pen_name=request.pen_name or "砚山行",
            hot_keywords=request.hot_keywords,
            motif_store=self.motif_store,
            benchmark_chapters=request.benchmark_chapters or None,
            target_chapters=request.target_chapters,
            creative_brief=request.creative_brief,
            on_progress=on_progress,
        )
        proj = result.project
        self.db.register_book(
            book_id=proj.meta.project_id,
            project_dir=project_dir,
            title=proj.meta.title,
            genre_id=proj.meta.genre_id or request.genre_id,
            author_level=proj.meta.author_level,
            contract_track=proj.meta.contract_track,
            status=BookRunStatus.ACTIVE,
            account_id=request.account_id,
            author_name=proj.meta.author_name or proj.meta.pen_name or request.pen_name,
            fanqie_book_id=proj.meta.book_id,
            daily_target_chapters=2,
            max_published_chapter=proj.meta.max_published_chapter,
            total_chapters=len(proj.scenes),
            signed=proj.meta.signed,
            alternate_titles=proj.meta.alternate_titles,
            alternate_blurbs=proj.meta.alternate_blurbs,
            opening_motif=result.opening_motif,
            golden_finger_type=result.golden_finger_type,
        )
        self.event_broker.notify_poll_waiters()
        return self.db.get_book_summary(proj.meta.project_id)

    def register_project(
        self,
        project_dir: str | Path,
        *,
        book_id: Optional[str] = None,
        account_id: str = "",
        fanqie_book_id: str = "",
        daily_target_chapters: int = 2,
        import_existing_chapters: bool = False,
    ) -> BookSummaryResponse:
        root = Path(project_dir).resolve()
        proj = load_project(root)
        bid = (book_id or proj.meta.project_id).strip()
        self.db.register_book(
            book_id=bid,
            project_dir=root,
            title=proj.meta.title,
            genre_id=proj.meta.genre_id,
            author_level=proj.meta.author_level,
            contract_track=proj.meta.contract_track,
            status=BookRunStatus.ACTIVE,
            account_id=account_id,
            author_name=proj.meta.author_name or proj.meta.pen_name,
            fanqie_book_id=fanqie_book_id or proj.meta.book_id,
            daily_target_chapters=daily_target_chapters,
            max_published_chapter=proj.meta.max_published_chapter,
            total_chapters=len(proj.scenes),
            signed=proj.meta.signed,
            alternate_titles=proj.meta.alternate_titles,
            alternate_blurbs=proj.meta.alternate_blurbs,
        )
        if import_existing_chapters:
            chapters_dir = root / "output" / "chapters"
            if chapters_dir.is_dir():
                for sc in sorted(proj.scenes, key=lambda s: s.chapter):
                    ch_path = resolve_chapter_path(chapters_dir, sc.chapter)
                    if ch_path is not None and ch_path.is_file():
                        body = ch_path.read_text(encoding="utf-8").rstrip()
                        meta_path = chapters_dir / f"chapter_{sc.chapter:04d}.meta.json"
                        meta_data: Dict[str, Any] = {}
                        if meta_path.is_file():
                            meta_data = dict(json.loads(meta_path.read_text(encoding="utf-8")))
                        pub_title = str(
                            meta_data.get("public_title") or sc.public_title or sc.internal_title
                        )
                        self.db.add_stockpiled_chapter(
                            bid,
                            sc.chapter,
                            pub_title,
                            body,
                            author_note=str(meta_data.get("author_note", "")),
                            sha256=str(meta_data.get("sha256", "")),
                            locked=bool(meta_data.get("locked", sc.locked)),
                            overwrite_if_stockpiled=False,
                        )
        return self.db.get_book_summary(bid)

    def approve_promotion(self, book_id: str) -> Dict[str, Any]:
        book = self.db.get_book(book_id)
        prev_daily = int(book["daily_target_chapters"])
        new_daily = min(5, max(3, prev_daily + 1)) if prev_daily < 5 else prev_daily
        self.db.update_book_daily_target(book_id, new_daily)
        return {
            "status": "approved",
            "book_id": book_id,
            "previous_daily_chapters": prev_daily,
            "daily_target_chapters": new_daily,
        }

    def request_cut_book(self, book_id: str, reason: str = "") -> Dict[str, Any]:
        return self.db.request_cut_book(book_id, reason=reason)

    def confirm_cut_book(
        self, book_id: str, confirmation_version: int, reason: str = ""
    ) -> Dict[str, Any]:
        return self.db.confirm_cut_book(
            book_id, confirmation_version=confirmation_version, reason=reason
        )

    def cancel_cut_book(self, book_id: str) -> Dict[str, Any]:
        return self.db.cancel_cut_book(book_id)

    def report_captcha_pause(self, report: CompanionCaptchaReport) -> Dict[str, Any]:
        res = self.db.record_captcha_pause(
            account_id=report.account_id,
            book_id=report.book_id,
            chapter=report.chapter,
            reason=report.reason,
            page_url=report.page_url,
            screenshot_base64=report.screenshot_base64,
            screenshot_mime=report.screenshot_mime,
            detected_at=report.detected_at,
        )
        self._emit(
            SSE_COMPANION_CAPTCHA,
            book_id=report.book_id,
            chapter=report.chapter,
            message=f"Companion paused on captcha/auth for account '{report.account_id}': {report.reason}",
            data={
                "account_id": report.account_id,
                "reason": report.reason,
                "page_url": report.page_url,
                "affected_books": res["affected_books"],
                "has_screenshot": bool(report.screenshot_base64),
            },
        )
        return res

    def resume_companion(self, account_id: str, book_id: str = "") -> Dict[str, Any]:
        res = self.db.resume_companion(account_id=account_id, book_id=book_id)
        self._emit(
            SSE_COMPANION_RESUMED,
            book_id=book_id,
            message=f"Companion resumed for account '{account_id}'",
            data=res,
        )
        return res

    def pause_book(self, book_id: str, reason: str = "") -> Dict[str, Any]:
        updated = self.db.update_book_status(
            book_id, BookRunStatus.PAUSED_MANUAL, paused_reason=reason or "Manually paused"
        )
        return {"book_id": book_id, "status": updated["status"], "reason": updated["paused_reason"]}

    def resume_book(self, book_id: str) -> Dict[str, Any]:
        updated = self.db.update_book_status(
            book_id, BookRunStatus.ACTIVE, paused_reason="", isolation_detail={}
        )
        self.event_broker.notify_poll_waiters()
        return {"book_id": book_id, "status": updated["status"]}

    # -----------------------------------------------------------------------
    # Safety Cushion Inspection & Priority Queue
    # -----------------------------------------------------------------------
    def inspect_safety_cushions(self, *, emit_alerts: bool = True) -> List[QueuedProductionItem]:
        """Inspect active books' STOCKPILED counts; queue <10 and prioritize <6 to top."""
        queue_items: List[QueuedProductionItem] = []
        for book in self.db.list_books():
            if book["status"] != BookRunStatus.ACTIVE.value:
                continue
            bid = book["book_id"]
            stockpiled = self.db.count_stockpiled(bid)
            if self.export_only:
                queue_items.append(
                    QueuedProductionItem(
                        book_id=bid,
                        title=book["title"],
                        stockpiled_count=stockpiled,
                        priority_tier=1,
                        urgent=False,
                    )
                )
                continue
            if stockpiled < self.safety_cushion_target:
                urgent = stockpiled < self.safety_cushion_critical
                tier = 0 if urgent else 1
                queue_items.append(
                    QueuedProductionItem(
                        book_id=bid,
                        title=book["title"],
                        stockpiled_count=stockpiled,
                        priority_tier=tier,
                        urgent=urgent,
                    )
                )
                if urgent and emit_alerts:
                    self._emit(
                        SSE_SAFETY_CUSHION_ALERT,
                        book_id=bid,
                        message=(
                            f"Safety cushion critical for 《{book['title']}》: "
                            f"{stockpiled} < {self.safety_cushion_critical} chapters"
                        ),
                        data={
                            "stockpiled_count": stockpiled,
                            "critical_threshold": self.safety_cushion_critical,
                            "target_threshold": self.safety_cushion_target,
                            "priority": "URGENT",
                        },
                    )
        queue_items.sort(key=lambda item: (item.priority_tier, item.stockpiled_count, item.book_id))
        return queue_items

    def _collect_isolation_details(
        self,
        book_id: str,
        project_dir: Path,
        chapter: int,
        exc: ProductionGateFailure,
    ) -> Dict[str, Any]:
        audit_dir = project_dir / "output" / "audit"
        rejected_texts: List[Tuple[int, str]] = []
        scan_violations: List[Dict[str, Any]] = []

        if audit_dir.is_dir():
            for attempt_idx in range(1, 10):
                rej_file = audit_dir / f"ch{chapter:04d}_attempt{attempt_idx}_rejected.txt"
                scan_file = audit_dir / f"ch{chapter:04d}_attempt{attempt_idx}_scan.json"
                if rej_file.is_file():
                    rejected_texts.append((attempt_idx, rej_file.read_text(encoding="utf-8")))
                if scan_file.is_file():
                    scan_data = json.loads(scan_file.read_text(encoding="utf-8"))
                    scan_violations = list(scan_data.get("violations", []))

        diff_lines: List[str] = []
        if len(rejected_texts) >= 2:
            a_idx, a_txt = rejected_texts[-2]
            b_idx, b_txt = rejected_texts[-1]
            diff_lines = list(
                difflib.unified_diff(
                    a_txt.splitlines(),
                    b_txt.splitlines(),
                    fromfile=f"attempt_{a_idx}",
                    tofile=f"attempt_{b_idx}",
                    lineterm="",
                )
            )[:120]
            if not diff_lines:
                diff_lines = [
                    f"--- attempt_{b_idx} (identical to attempt_{a_idx}, rejected) ---",
                    *b_txt.splitlines()[:40],
                ]
        elif len(rejected_texts) == 1:
            a_idx, a_txt = rejected_texts[0]
            diff_lines = [f"--- attempt_{a_idx} (rejected) ---", *a_txt.splitlines()[:40]]

        return {
            "book_id": book_id,
            "chapter": chapter,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "violations": scan_violations,
            "diff": "\n".join(diff_lines),
            "isolated_at": shanghai_now(self._now_fn).isoformat(),
        }

    def _isolate_on_error(self, book_id: str, chapter: int, what: str, exc: BaseException) -> Dict[str, Any]:
        """Stop one book visibly (circuit breaker) instead of letting the loop retry forever."""
        detail = {"error_type": type(exc).__name__, "error": f"{what}：{exc}", "chapter": int(chapter)}
        self.db.update_book_status(
            book_id,
            BookRunStatus.ISOLATED_GATE_FAILURE,
            paused_reason=f"Chapter {chapter} {what}: {exc}",
            isolation_detail=detail,
        )
        self._emit(
            SSE_BOOK_ISOLATED,
            book_id=book_id,
            chapter=chapter,
            message=f"Book '{book_id}' isolated: {what}",
            data=detail,
        )
        return {"book_id": book_id, "status": BookRunStatus.ISOLATED_GATE_FAILURE.value, "detail": detail}

    def produce_next_chapter(self, book_id: str) -> Dict[str, Any]:
        """Generate & stockpile the next unproduced chapter for one book with fault isolation."""
        book = self.db.get_book(book_id)
        if book["status"] != BookRunStatus.ACTIVE.value:
            return {"book_id": book_id, "status": "SKIPPED", "reason": f"Book status is {book['status']}"}

        project_dir = Path(book["project_dir"]).resolve()
        project = load_project(project_dir)
        self.db.update_book_total_chapters(book_id, len(project.scenes))

        existing_chs = self.db.get_existing_chapter_numbers(book_id)
        sorted_scenes = sorted(project.scenes, key=lambda s: s.chapter)
        next_scene = next((s for s in sorted_scenes if s.chapter not in existing_chs), None)
        if next_scene is None:
            if project.meta.is_final_volume:
                self.db.update_book_status(book_id, BookRunStatus.COMPLETED, paused_reason="All chapters completed")
                return {"book_id": book_id, "status": "COMPLETED"}
            if not self.export_only:
                return {"book_id": book_id, "status": "NO_OUTLINED_CHAPTERS"}
            if project.meta.contract_track == "complete_150k":
                # lv0_1 books are planned as one complete outline; finishing it finishes the book.
                self.db.update_book_status(book_id, BookRunStatus.COMPLETED, paused_reason="Complete outline written")
                return {"book_id": book_id, "status": "COMPLETED"}
            try:
                ext = self.extend_book_volume(book_id)
            except Exception as exc:
                return self._isolate_on_error(book_id, len(existing_chs) + 1, "续写下一卷大纲失败", exc)
            return {"book_id": book_id, "status": "EXTENDED", "detail": ext}

        output_dir = project_dir / "output"

        def _forward_progress(event_name: str, payload: Dict[str, Any]) -> None:
            ch_no = int(payload.get("chapter", next_scene.chapter))
            self._emit(
                event_name,
                book_id=book_id,
                chapter=ch_no,
                message=f"[{book_id}] ch{ch_no:04d} {event_name}",
                data=payload,
            )

        if self._engine_factory is not None:
            engine = self._engine_factory(project, output_dir, _forward_progress)
        else:
            if self.provider is None:
                raise SchedulerConfigurationError(
                    "LLM provider/RoleModelRouter is not configured for chapter production."
                )
            engine = NovelGenerationEngine(
                project=project,
                provider=self.provider,
                output_dir=output_dir,
                on_progress=_forward_progress,
            )

        started_monotonic = time.monotonic()
        try:
            manifest = engine.run(target_chapters=[next_scene.chapter], resume=False, on_progress=_forward_progress)
        except ProductionGateFailure as exc:
            iso_detail = self._collect_isolation_details(book_id, project_dir, next_scene.chapter, exc)
            self.db.update_book_status(
                book_id,
                BookRunStatus.ISOLATED_GATE_FAILURE,
                paused_reason=f"Chapter {next_scene.chapter} gate failure: {exc}",
                isolation_detail=iso_detail,
            )
            self._emit(
                SSE_BOOK_ISOLATED,
                book_id=book_id,
                chapter=next_scene.chapter,
                message=f"Book '{book_id}' isolated on ch{next_scene.chapter:04d} gate failure",
                data=iso_detail,
            )
            return {"book_id": book_id, "status": BookRunStatus.ISOLATED_GATE_FAILURE.value, "detail": iso_detail}
        except (LLMRequestError, LLMStreamTruncatedError) as exc:
            return self._isolate_on_error(book_id, next_scene.chapter, "上游模型请求失败", exc)
        except (QuotaExhaustedError, BudgetCircuitBreakerError) as exc:
            self.db.update_book_status(
                book_id,
                BookRunStatus.PAUSED_QUOTA_EXHAUSTED,
                paused_reason=str(exc),
            )
            self._emit(
                SSE_CPA_QUOTA_ALERT,
                book_id=book_id,
                chapter=next_scene.chapter,
                message=str(exc),
                data={
                    "error_type": type(exc).__name__,
                    "kind": "quota_exhausted",
                    "reason": str(exc),
                },
            )
            return {"book_id": book_id, "status": BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value, "error": str(exc)}

        chapters_dir = output_dir / "chapters"
        ch_path = resolve_chapter_path(chapters_dir, next_scene.chapter)
        if ch_path is None or not ch_path.is_file():
            raise FileNotFoundError(
                f"Engine finished chapter {next_scene.chapter} for '{book_id}' but chapter file is missing in {chapters_dir}"
            )
        body = ch_path.read_text(encoding="utf-8").rstrip()
        ch_summary = (manifest.get("chapters") or [{}])[0]
        public_title = str(
            ch_summary.get("public_title")
            or next_scene.public_title
            or next_scene.internal_title
        )
        author_note = str(ch_summary.get("author_note") or "")
        sha256_val = str(ch_summary.get("sha256") or "")
        locked_val = bool(ch_summary.get("locked", next_scene.locked))

        item = self.db.add_stockpiled_chapter(
            book_id,
            next_scene.chapter,
            public_title,
            body,
            author_note=author_note,
            sha256=sha256_val,
            locked=locked_val,
        )
        self.db.record_production_stat(
            book_id,
            item.chapter,
            duration_sec=time.monotonic() - started_monotonic,
            attempts_used=int(ch_summary["attempts_used"]),
            rewrites_triggered=int(ch_summary["rewrites_triggered"]),
            char_count=item.char_count,
        )
        self.event_broker.notify_poll_waiters()
        return {
            "book_id": book_id,
            "status": "PRODUCED",
            "chapter": item.chapter,
            "char_count": item.char_count,
            "stockpiled_count": self.db.count_stockpiled(book_id),
        }

    def run_production_cycle(self, *, max_chapters_per_cycle: Optional[int] = None) -> Dict[str, Any]:
        """Inspect cushion levels and produce chapters in priority order (<6 first) with fault isolation."""
        with self._prod_lock:
            queue_items = self.inspect_safety_cushions(emit_alerts=True)
            results: List[Dict[str, Any]] = []
            produced_count = 0

            for item in queue_items:
                if max_chapters_per_cycle is not None and produced_count >= max_chapters_per_cycle:
                    break
                res = self.produce_next_chapter(item.book_id)
                results.append(res)
                if res.get("status") == "PRODUCED":
                    produced_count += 1

            return {
                "queued_books": [q.book_id for q in queue_items],
                "urgent_books": [q.book_id for q in queue_items if q.urgent],
                "produced_count": produced_count,
                "results": results,
            }

    def start_background_loop(self, poll_interval_sec: float = 10.0) -> None:
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        self._bg_stop.clear()

        def _loop() -> None:
            while not self._bg_stop.is_set():
                try:
                    self.run_production_cycle(max_chapters_per_cycle=5)
                except SchedulerConfigurationError as exc:
                    logger.warning("Scheduler cycle skipped (unconfigured): %s", exc)
                except Exception as exc:
                    logger.exception("Unexpected error in MatrixScheduler background cycle: %s", exc)
                self._bg_stop.wait(timeout=max(0.5, float(poll_interval_sec)))

        self._bg_thread = threading.Thread(target=_loop, name="xiaoshuo-matrix-scheduler", daemon=True)
        self._bg_thread.start()

    def stop_background_loop(self, timeout_sec: float = 5.0) -> None:
        self._bg_stop.set()
        if self._bg_thread is not None and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=timeout_sec)
        self._bg_thread = None

    # -----------------------------------------------------------------------
    # Helpers for Chapter Lock, Extend, Export, Cover & CPA Bindings
    # -----------------------------------------------------------------------
    def set_chapter_lock(
        self, book_id: str, chapter: int, locked: bool, reason: str = ""
    ) -> ChapterStatusItem:
        item = self.db.set_chapter_lock(book_id, chapter, locked=locked, reason=reason)
        book = self.db.get_book(book_id)
        meta_path = Path(book["project_dir"]) / "output" / "chapters" / f"chapter_{chapter:04d}.meta.json"
        if meta_path.is_file():
            meta_obj = dict(json.loads(meta_path.read_text(encoding="utf-8")))
            meta_obj["locked"] = bool(locked)
            meta_obj["lock_reason"] = reason
            meta_obj["updated_at"] = shanghai_now(self._now_fn).isoformat()
            tmp_path = meta_path.with_name(f"{meta_path.name}.tmp")
            tmp_path.write_text(json.dumps(meta_obj, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, meta_path)
        return item

    def extend_book_volume(
        self,
        book_id: str,
        *,
        batch_size: int = 15,
        finale: bool = False,
        num_chapters: int = 10,
    ) -> Dict[str, Any]:
        book = self.db.get_book(book_id)
        project_dir = Path(book["project_dir"]).resolve()
        project = load_project(project_dir)
        planner = self._get_planner()

        snapshot: Optional[WorldStateSnapshot] = None
        snapshots_dir = project_dir / "output" / "snapshots"
        if snapshots_dir.is_dir():
            latest_snap = _select_latest_snapshot_file(snapshots_dir)
            if latest_snap is not None:
                snapshot = WorldStateSnapshot.from_dict(
                    json.loads(latest_snap.read_text(encoding="utf-8"))
                )

        if finale:
            res = planner.plan_finale_volume(
                project_dir,
                snapshot=snapshot,
                num_chapters=num_chapters,
            )
        else:
            effective_max_published = max(
                int(book["max_published_chapter"]),
                int(project.meta.max_published_chapter),
            )
            raw_dirs = self.db.get_outline_directives(book_id)
            directives: List[OutlineAdjustmentDirective] = []
            for d in raw_dirs:
                target_ch = int(d["target_chapter"])
                if target_ch <= effective_max_published:
                    continue
                directives.append(
                    OutlineAdjustmentDirective(
                        directive_id=str(d["directive_id"]),
                        book_id=str(d["book_id"]),
                        target_chapter=target_ch,
                        max_published_chapter=effective_max_published,
                        signal_type=str(d["signal_type"]),
                        priority=int(d["priority"]),
                        trigger_keywords=list(d.get("trigger_keywords", [])),
                        mention_count=int(d.get("mention_count", 1)),
                        instruction=str(d["instruction"]),
                        mentioned_entities=list(d.get("mentioned_entities", [])),
                    )
                )
            res = planner.extend_volume(
                project_dir,
                snapshot=snapshot,
                directives=directives or None,
                batch_size=batch_size,
            )

        self.db.update_book_total_chapters(book_id, len(res.project.scenes))
        if isinstance(res, FinaleVolumeResult):
            return {
                "book_id": book_id,
                "project_id": res.project.meta.project_id,
                "title": res.project.meta.title,
                "project_dir": str(res.project_dir),
                "finale": True,
                "is_final_volume": True,
                "volume_index": int(res.volume_index),
                "start_chapter": int(res.start_chapter),
                "end_chapter": int(res.end_chapter),
                "total_chapters": len(res.project.scenes),
                "resolved_foreshadow_ids": list(res.resolved_foreshadow_ids),
                "fulfilled_expectation_ids": list(res.fulfilled_expectation_ids),
                "volume_file_path": str(res.volume_file_path),
                "attempts_used": int(res.attempts_used),
            }
        if isinstance(res, VolumeExtensionResult):
            return {
                "book_id": book_id,
                "project_id": res.project.meta.project_id,
                "title": res.project.meta.title,
                "project_dir": str(res.project_dir),
                "finale": False,
                "is_final_volume": False,
                "volume_index": int(res.volume_index),
                "start_chapter": int(res.start_chapter),
                "end_chapter": int(res.end_chapter),
                "total_chapters": len(res.project.scenes),
                "triggered_map_change": bool(res.triggered_map_change),
                "new_map_name": str(res.new_map_name),
                "new_map_location_id": str(res.new_map_location_id),
                "soft_identity_reboot": str(res.soft_identity_reboot),
                "new_volume_cap_locks": [lk.to_dict() for lk in res.new_volume_cap_locks],
                "volume_file_path": str(res.volume_file_path),
                "attempts_used": int(res.attempts_used),
            }
        if hasattr(res, "to_dict"):
            return dict(res.to_dict())
        raise TypeError(f"Unsupported extend_book_volume result type: {type(res).__name__}")

    def export_book_bundle(
        self,
        book_id: str,
        *,
        platform: str = "tomato",
        output_dir: str | Path,
    ) -> Dict[str, Any]:
        book = self.db.get_book(book_id)
        project = load_project(book["project_dir"])
        ch_details = [
            self.db.get_chapter(book_id, item.chapter) for item in self.db.list_chapters(book_id)
        ]
        artifacts = [
            ChapterArtifact(
                chapter=int(c["chapter"]),
                public_title=str(c["public_title"]),
                body=str(c["body"]),
                author_note=str(c["author_note"]),
                sha256=str(c["sha256"]),
                locked=bool(c["locked"]),
            )
            for c in ch_details
        ]
        cover_path = book.get("cover_path") or None
        if cover_path and not Path(cover_path).is_file():
            cover_path = None
        res = export_project_bundle(
            project,
            artifacts,
            output_dir,
            platform=platform,
            cover_path=cover_path,
        )
        return {
            "platform": platform,
            "output_dir": str(res.output_dir),
            "zip_path": str(getattr(res, "zip_path", "")),
            "chapter_count": int(res.chapter_count),
        }

    def generate_book_cover(
        self,
        book_id: str,
        *,
        base_mode: str = "ai_image",
        output_format: str = "PNG",
    ) -> CoverGenerationResult:
        book = self.db.get_book(book_id)
        project = load_project(book["project_dir"])
        out_path = Path(book["project_dir"]) / "output" / f"cover.{output_format.lower()}"
        router = self._get_router()
        res = generate_cover_for_project(
            project,
            out_path,
            base_mode=base_mode,
            router=router,
            output_format=output_format,
        )
        self.db.set_book_cover_path(book_id, res.output_path)
        return res

    def get_cpa_bindings(self) -> List[CPABindingConfig]:
        router = self._get_router()
        if router is None:
            return []
        bindings: List[CPABindingConfig] = []
        for role in VALID_ROLES:
            if router.has_role(role):
                cfg = router.get_role_config(role)
                bindings.append(
                    CPABindingConfig(
                        role=cfg.role,
                        model=cfg.model,
                        base_url=cfg.base_url,
                        provider_type=cfg.provider_type,
                        reasoning_effort=cfg.reasoning_effort,
                    )
                )
        return bindings

    def update_cpa_bindings(self, items: Sequence[CPABindingConfig | Mapping[str, Any]]) -> List[CPABindingConfig]:
        router = self._get_router()
        if router is None:
            raise SchedulerConfigurationError(
                "RoleModelRouter is not configured; cannot update CPA bindings."
            )
        for raw in items:
            binding = raw if isinstance(raw, CPABindingConfig) else CPABindingConfig.from_dict(raw)
            if router.has_role(binding.role):
                existing = router.get_role_config(binding.role)
                # Existing API keys are bound to the original endpoint; never forward them
                # to a different scheme/host/port (credential exfiltration / SSRF).
                new_origin = _normalized_origin(binding.base_url)
                if new_origin != _normalized_origin(existing.base_url):
                    raise CPABindingOriginChangeError(
                        f"Changing base_url host for role {binding.role!r} "
                        f"({existing.base_url!r} -> {binding.base_url!r}) requires re-provisioning "
                        "credentials; existing API keys are not reused across origins."
                    )
                new_cfg = RoleEndpointConfig(
                    role=binding.role,
                    base_url=binding.base_url,
                    model=binding.model,
                    api_keys=list(existing.api_keys),
                    provider_type=binding.provider_type,
                    timeout=existing.timeout,
                    stream=existing.stream,
                    reasoning_effort=binding.reasoning_effort,
                    strip_reasoning_suffix=existing.strip_reasoning_suffix,
                    cooldown_sec=existing.cooldown_sec,
                    input_cost_per_1m=existing.input_cost_per_1m,
                    output_cost_per_1m=existing.output_cost_per_1m,
                    image_cost_per_call=existing.image_cost_per_call,
                )
                router.update_role(new_cfg)
            else:
                raise LLMConfigurationError(
                    f"Cannot bind role {binding.role!r} without existing API credentials pool in RoleModelRouter."
                )
        return self.get_cpa_bindings()


__all__ = [
    "CPABindingOriginChangeError",
    "EngineFactory",
    "MatrixScheduler",
    "QueuedProductionItem",
    "SSEEventBroker",
    "SchedulerConfigurationError",
]
