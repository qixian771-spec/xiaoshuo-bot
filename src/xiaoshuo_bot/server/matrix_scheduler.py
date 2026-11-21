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
    SSE_PIPELINE_CHAPTER_LOCKED,
    SSE_PIPELINE_GATE_REPORT,
    SSE_PIPELINE_MODEL_EVENT,
    SSE_PIPELINE_STAGE,
    SSE_PIPELINE_TOKEN_USAGE,
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
from ..llm.workbuddy_policy import (
    ModelQuotaSnapshot,
    ModelSwitchEvent,
    ModelUsageEvent,
    RateLimitRetryEvent,
    WORKBUDDY_MANUAL_ONLY_SLOTS,
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_DISPLAY_NAMES,
    WORKBUDDY_SLOT_HY4,
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
    WORKBUDDY_WRITER_ROTATION_SLOTS,
    _strip_site_prefix,
)
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
from .public_view import PublicPipelineTracker
from .stockpile_db import (
    BookNotFoundError,
    SAFETY_CUSHION_CRITICAL,
    SAFETY_CUSHION_TARGET,
    StockpileDB,
    shanghai_now,
)

logger = logging.getLogger(__name__)

# Server-side bounded autonomous self-healing policy (zero gate weakening).
AUTO_HEAL_COOLDOWN_SECS: Tuple[int, ...] = (120, 300, 900)
AUTO_HEAL_PLANNER_MAX_ATTEMPTS = 2
_RECOVERABLE_PROSE_RULE_PREFIXES: Tuple[str, ...] = (
    "LEN_",
    "RHYTHM_",
    "TIC_",
    "AI_TIC_",
    "HOOK_",
    "STYLE_",
)
_NON_RECOVERABLE_RULE_PREFIXES: Tuple[str, ...] = (
    "STATE_",
    "FORESHADOW",
    "SAFE_",
    "POV_",
    "PANEL_",
)
_RECOVERABLE_ERROR_TYPES = frozenset(
    {
        "LLMRequestError",
        "LLMStreamTruncatedError",
        "TimeoutError",
        "ConnectionError",
    }
)


def _parse_iso_dt(raw: Any) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    return dt


def classify_self_heal_eligibility(book: Mapping[str, Any]) -> Tuple[bool, str, int, int]:
    """Classify whether an isolated book can safely undergo bounded server-side self-healing.

    Returns (eligible, reason_code, chapter, max_attempts).
    Strictly rejects manual pauses, deterministic world-state/foreshadow/safety deadlocks,
    and budget/quota stops without reset confirmation.
    """
    status = str(book.get("status") or "")
    if status != BookRunStatus.ISOLATED_GATE_FAILURE.value:
        return (False, f"status={status}", 0, 0)

    detail = book.get("isolation_detail")
    if not isinstance(detail, Mapping):
        detail = {}
    paused_reason = str(book.get("paused_reason") or "")
    if "自愈已达上限" in paused_reason:
        return (False, "heal_exhausted", int(detail.get("chapter") or 0), 0)

    chapter = int(detail.get("chapter") or 0)
    if chapter <= 0:
        m = re.search(r"Chapter\s+(\d+)", paused_reason)
        if m:
            chapter = int(m.group(1))

    err_type = str(detail.get("error_type") or "").strip()
    if err_type in _RECOVERABLE_ERROR_TYPES:
        return (True, f"transient_{err_type}", chapter, len(AUTO_HEAL_COOLDOWN_SECS))

    if err_type == "PlanningValidationError":
        return (True, "transient_planner_validation", chapter, AUTO_HEAL_PLANNER_MAX_ATTEMPTS)

    if err_type == "ProductionGateFailure":
        raw_viols = detail.get("violations")
        crit_rules: List[str] = []
        if isinstance(raw_viols, list):
            for v in raw_viols:
                if not isinstance(v, Mapping):
                    continue
                sev = str(v.get("severity") or "CRITICAL").upper()
                rid = str(v.get("rule_id") or "").strip().upper()
                if sev == "CRITICAL" and rid:
                    crit_rules.append(rid)
        if not crit_rules:
            # Fallback to parsing paused_reason when isolation_detail lacked structured violations
            for m in re.finditer(r"['\"]rule_id['\"]:\s*['\"]([A-Z0-9_]+)['\"]", paused_reason):
                rid = m.group(1).upper()
                if rid not in crit_rules:
                    crit_rules.append(rid)
            # Exclude WARNING-only RHYTHM_BURSTINESS when parsed from string fallback
            crit_rules = [r for r in crit_rules if r != "RHYTHM_BURSTINESS"]

        if not crit_rules:
            return (False, "unknown_gate_violations", chapter, 0)
        for rid in crit_rules:
            if any(rid.startswith(p) for p in _NON_RECOVERABLE_RULE_PREFIXES):
                return (False, f"structural_{rid}", chapter, 0)
            if not any(rid.startswith(p) for p in _RECOVERABLE_PROSE_RULE_PREFIXES):
                return (False, f"unclassified_{rid}", chapter, 0)
        return (True, f"stochastic_prose:{'+'.join(sorted(set(crit_rules)))}", chapter, len(AUTO_HEAL_COOLDOWN_SECS))

    return (False, f"non_recoverable_{err_type or 'unknown'}", chapter, 0)


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
        planner_max_consecutive_same_location: Optional[int] = None,
        auto_heal_enabled: bool = True,
        auto_heal_cooldown_secs: Sequence[int] = AUTO_HEAL_COOLDOWN_SECS,
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
        self.planner_max_consecutive_same_location = (
            int(planner_max_consecutive_same_location)
            if planner_max_consecutive_same_location is not None
            else None
        )
        self.auto_heal_enabled = bool(auto_heal_enabled)
        self.auto_heal_cooldown_secs: Tuple[int, ...] = tuple(
            max(0, int(s)) for s in (auto_heal_cooldown_secs or AUTO_HEAL_COOLDOWN_SECS)
        ) or AUTO_HEAL_COOLDOWN_SECS
        self._heal_attempts: Dict[str, Tuple[int, int]] = {}

        self._prod_lock = threading.Lock()
        self._bg_stop = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None
        self._thread_ctx = threading.local()
        self.pipeline_tracker = PublicPipelineTracker()

        if isinstance(self.provider, RoleModelRouter):
            self.provider.add_model_switch_listener(self._handle_model_switched)
            self.provider.add_rate_limit_retry_listener(self._handle_rate_limit_retry)
            self.provider.add_model_quota_listener(self._handle_model_quota)
            self.provider.add_usage_listener(self._handle_llm_usage)

    def _resolve_event_book_context(
        self,
        event_book_id: str,
        event_chapter: int,
    ) -> Tuple[str, str, int]:
        book_id = event_book_id or str(getattr(self._thread_ctx, "book_id", "") or "")
        chapter = int(event_chapter or getattr(self._thread_ctx, "chapter", 0) or 0)
        book_title = str(getattr(self._thread_ctx, "book_title", "") or "")
        if book_id and not book_title:
            book_rec = self.db.get_book(book_id)
            if book_rec:
                book_title = str(book_rec.get("title") or "")
        return book_id, book_title, chapter

    def _handle_model_switched(self, event: ModelSwitchEvent) -> None:
        book_id, book_title, chapter = self._resolve_event_book_context(
            event.book_id,
            event.chapter,
        )
        from_display = event.from_label or event.from_model
        to_display = event.to_label or event.to_model
        ev_type = (
            event.event_type
            if event.event_type in ("model_switched", "model_restored")
            else "model_switched"
        )
        saved = self.db.record_model_switch(
            role=event.role,
            from_model=from_display,
            to_model=to_display,
            reason=event.reason,
            book_id=book_id,
            book_title=book_title,
            chapter=chapter,
            status_code=event.status_code,
            upstream_code=event.upstream_code,
            event_type=ev_type,
            created_at=event.timestamp,
        )
        if ev_type == "model_restored":
            msg = f"角色 {event.role} 已切回 {to_display}（原模型：{from_display}；{event.reason}）"
        else:
            msg = f"角色 {event.role} 模型已从 {from_display} 切到 {to_display}（原因：{event.reason}）"
        self._emit(
            SSE_PIPELINE_MODEL_EVENT,
            book_id=book_id,
            chapter=chapter,
            message=msg,
            data={
                "kind": ev_type,
                "role": event.role,
                "from_model": event.from_model,
                "to_model": event.to_model,
                "reason": event.reason,
            },
        )
        self._emit(
            SSE_CPA_QUOTA_ALERT,
            book_id=book_id,
            chapter=chapter,
            message=msg,
            data={
                "kind": ev_type,
                "event_type": ev_type,
                "event_id": saved["id"],
                "role": event.role,
                "from_model": from_display,
                "to_model": to_display,
                "from_model_id": event.from_model,
                "to_model_id": event.to_model,
                "reason": event.reason,
                "book_title": book_title,
                "status_code": event.status_code,
                "upstream_code": event.upstream_code,
            },
        )

    def _handle_rate_limit_retry(self, event: RateLimitRetryEvent) -> None:
        book_id, book_title, chapter = self._resolve_event_book_context(
            event.book_id,
            event.chapter,
        )
        model_display = event.model_label or event.model
        wait_val = float(event.wait_seconds)
        wait_str = str(int(wait_val)) if wait_val.is_integer() else f"{wait_val:g}"
        retry_msg = (
            f"{model_display} 被限流，第 {event.attempt} 次等待重试"
            f"（共 {event.max_retries} 次，等待 {wait_str} 秒）"
        )
        saved = self.db.record_model_switch(
            role=event.role,
            from_model=model_display,
            to_model=model_display,
            reason=event.reason,
            book_id=book_id,
            book_title=book_title,
            chapter=chapter,
            status_code=event.status_code,
            upstream_code="rate_limit_exceeded",
            event_type="rate_limit_retry",
            retry_attempt=event.attempt,
            max_retries=event.max_retries,
            wait_seconds=wait_val,
            created_at=event.timestamp,
        )
        self._emit(
            SSE_PIPELINE_MODEL_EVENT,
            book_id=book_id,
            chapter=chapter,
            message=retry_msg,
            data={
                "kind": "rate_limit_retry",
                "role": event.role,
                "model": event.model,
                "retry_attempt": event.attempt,
                "max_retries": event.max_retries,
                "wait_seconds": wait_val,
                "reason": event.reason,
            },
        )
        self._emit(
            SSE_CPA_QUOTA_ALERT,
            book_id=book_id,
            chapter=chapter,
            message=retry_msg,
            data={
                "kind": "rate_limit_retry",
                "event_type": "rate_limit_retry",
                "event_id": saved["id"],
                "role": event.role,
                "model": model_display,
                "model_id": event.model,
                "from_model": model_display,
                "to_model": model_display,
                "retry_attempt": event.attempt,
                "max_retries": event.max_retries,
                "wait_seconds": wait_val,
                "reason": event.reason,
                "book_title": book_title,
                "status_code": event.status_code,
            },
        )

    def _handle_llm_usage(self, event: ModelUsageEvent) -> None:
        book_id, _book_title, chapter = self._resolve_event_book_context(
            event.book_id,
            event.chapter,
        )
        self._emit(
            SSE_PIPELINE_TOKEN_USAGE,
            book_id=book_id,
            chapter=chapter,
            message=(
                f"{event.role} {event.model} usage: "
                f"prompt={event.prompt_tokens} comp={event.completion_tokens} "
                f"reason={event.reasoning_tokens} total={event.total_tokens}"
            ),
            data=event.to_dict(),
        )

    def _handle_model_quota(self, snap: ModelQuotaSnapshot) -> None:
        saved = self.db.upsert_model_quota(
            model=snap.model,
            model_label=snap.model_label,
            raw_model=snap.raw_model,
            percent=snap.percent,
            reset_at=snap.reset_at,
            reset_at_raw=snap.reset_at_raw,
            shared_models=snap.shared_models,
            role=snap.role,
            updated_at=snap.updated_at,
        )
        book_id, _book_title, chapter = self._resolve_event_book_context("", 0)
        self._emit(
            SSE_PIPELINE_MODEL_EVENT,
            book_id=book_id,
            chapter=chapter,
            message=f"{snap.model} quota {snap.percent}%",
            data={
                "kind": "quota_updated",
                "role": snap.role,
                "model": snap.model,
                "quota": {
                    "model": saved["model"],
                    "label": saved["model_label"],
                    "has_quota": True,
                    "percent": saved["percent"],
                    "reset_at": saved["reset_at"],
                    "updated_at": saved["updated_at"],
                },
            },
        )

    def get_model_quotas(self) -> List[Dict[str, Any]]:
        """Return latest WorkBuddy usage.quota snapshots merged with configured whitelist models."""
        persisted = self.db.list_model_quotas()
        by_model: Dict[str, Dict[str, Any]] = {}
        by_bare: Dict[str, Dict[str, Any]] = {}
        for row in persisted:
            m = str(row.get("model") or "").strip()
            if not m:
                continue
            by_model[m] = row
            bare = _strip_site_prefix(m).lower()
            if bare and bare not in by_bare:
                by_bare[bare] = row

        def _find_row(model_id: str) -> Optional[Dict[str, Any]]:
            if model_id in by_model:
                return by_model[model_id]
            bare = _strip_site_prefix(model_id).lower()
            if bare in by_bare:
                return by_bare[bare]
            return None

        items: List[Dict[str, Any]] = []
        seen_models: set[str] = set()
        router = self._get_router()
        catalog = getattr(router, "workbuddy_catalog", None) if router is not None else None

        if catalog is not None:
            display_slots = (
                WORKBUDDY_SLOT_HY4_FLASH,
                WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
                WORKBUDDY_SLOT_MIMO_V2_6_PRO,
                WORKBUDDY_SLOT_HY4,
            )
            slot_map = catalog.slot_to_model()
            for slot in display_slots:
                model_id = slot_map[slot]
                seen_models.add(model_id)
                row = _find_row(model_id)
                if row is not None:
                    seen_models.add(row["model"])
                items.append(
                    {
                        "slot": slot,
                        "model": model_id,
                        "label": WORKBUDDY_SLOT_DISPLAY_NAMES[slot],
                        "in_rotation": slot in WORKBUDDY_WRITER_ROTATION_SLOTS,
                        "manual_only": slot in WORKBUDDY_MANUAL_ONLY_SLOTS,
                        "has_quota": row is not None,
                        "percent": float(row["percent"]) if row is not None else None,
                        "reset_at": str(row["reset_at"]) if row is not None else "",
                        "reset_at_raw": str(row["reset_at_raw"]) if row is not None else "",
                        "raw_model": str(row["raw_model"]) if row is not None else "",
                        "shared_models": list(row["shared_models"]) if row is not None else [],
                        "role": str(row["role"]) if row is not None else "",
                        "updated_at": str(row["updated_at"]) if row is not None else "",
                    }
                )
        elif router is not None:
            ordered_models: List[Tuple[str, str, bool, bool]] = []
            seen_cfg: set[str] = set()
            for role in ("writer", "planner", "critic", "polisher"):
                if not router.has_role(role):
                    continue
                cfg = router.get_role_config(role)
                candidates = list(cfg.model_whitelist) or list(cfg.rotation_models) or [cfg.model]
                for model_id in candidates:
                    if not model_id or model_id in seen_cfg:
                        continue
                    seen_cfg.add(model_id)
                    ordered_models.append(
                        (
                            model_id,
                            cfg.model_labels.get(model_id, "") or model_id,
                            model_id in cfg.rotation_models,
                            model_id in cfg.manual_only_models,
                        )
                    )
            for model_id, label, in_rot, manual_only in ordered_models:
                seen_models.add(model_id)
                row = _find_row(model_id)
                if row is not None:
                    seen_models.add(row["model"])
                items.append(
                    {
                        "slot": "",
                        "model": model_id,
                        "label": label,
                        "in_rotation": bool(in_rot),
                        "manual_only": bool(manual_only),
                        "has_quota": row is not None,
                        "percent": float(row["percent"]) if row is not None else None,
                        "reset_at": str(row["reset_at"]) if row is not None else "",
                        "reset_at_raw": str(row["reset_at_raw"]) if row is not None else "",
                        "raw_model": str(row["raw_model"]) if row is not None else "",
                        "shared_models": list(row["shared_models"]) if row is not None else [],
                        "role": str(row["role"]) if row is not None else "",
                        "updated_at": str(row["updated_at"]) if row is not None else "",
                    }
                )

        for row in persisted:
            m = str(row.get("model") or "").strip()
            if not m or m in seen_models:
                continue
            seen_models.add(m)
            items.append(
                {
                    "slot": "",
                    "model": m,
                    "label": str(row.get("model_label") or m),
                    "in_rotation": False,
                    "manual_only": False,
                    "has_quota": True,
                    "percent": float(row["percent"]),
                    "reset_at": str(row.get("reset_at") or ""),
                    "reset_at_raw": str(row.get("reset_at_raw") or ""),
                    "raw_model": str(row.get("raw_model") or ""),
                    "shared_models": list(row.get("shared_models") or []),
                    "role": str(row.get("role") or ""),
                    "updated_at": str(row.get("updated_at") or ""),
                }
            )
        return items

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
        titles: Dict[str, str] = {}
        if book_id:
            try:
                rec = self.db.get_book(book_id)
                titles[book_id] = str(rec.get("title") or book_id)
            except BookNotFoundError:
                titles[book_id] = book_id
        self.pipeline_tracker.observe(payload, titles)
        return self.event_broker.publish(payload)

    def get_public_pipeline_snapshot(self) -> Dict[str, Any]:
        return self.pipeline_tracker.snapshot(self.db, self)

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
        return AutoNovelPlanner(
            self.provider,
            max_validation_retries=5,
            max_consecutive_same_location=self.planner_max_consecutive_same_location,
        )

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
        self._emit(
            SSE_PIPELINE_STAGE,
            book_id=pid,
            chapter=0,
            message=f"[{pid}] 立项开始",
            data={"stage": "plan", "status": "running", "detail": "三源立项与分卷大纲生成中"},
        )

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
        self._emit(
            SSE_PIPELINE_STAGE,
            book_id=proj.meta.project_id,
            chapter=0,
            message=f"[{proj.meta.project_id}] 立项完成（{len(proj.scenes)} 章）",
            data={
                "stage": "plan",
                "status": "done",
                "detail": f"《{proj.meta.title}》共 {len(proj.scenes)} 章大纲就绪",
            },
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
        self._heal_attempts.pop(book_id, None)
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
        prev_ctx = (
            getattr(self._thread_ctx, "book_id", ""),
            getattr(self._thread_ctx, "book_title", ""),
            getattr(self._thread_ctx, "chapter", 0),
        )
        self._thread_ctx.book_id = book_id
        self._thread_ctx.book_title = str(book.get("title") or "")
        self._thread_ctx.chapter = int(next_scene.chapter)
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
                SSE_PIPELINE_MODEL_EVENT,
                book_id=book_id,
                chapter=next_scene.chapter,
                message=str(exc),
                data={
                    "kind": "quota_exhausted",
                    "reason": str(exc),
                },
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
        finally:
            self._thread_ctx.book_id, self._thread_ctx.book_title, self._thread_ctx.chapter = prev_ctx

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
        actual_model = str(ch_summary.get("model") or "").strip()
        if not actual_model:
            meta_file = chapters_dir / f"chapter_{next_scene.chapter:04d}.meta.json"
            if meta_file.is_file():
                meta_obj = json.loads(meta_file.read_text(encoding="utf-8"))
                if isinstance(meta_obj, dict):
                    actual_model = str(meta_obj.get("model") or "").strip()

        item = self.db.add_stockpiled_chapter(
            book_id,
            next_scene.chapter,
            public_title,
            body,
            author_note=author_note,
            sha256=sha256_val,
            locked=locked_val,
            model=actual_model,
        )
        duration_sec = time.monotonic() - started_monotonic
        self.db.record_production_stat(
            book_id,
            item.chapter,
            duration_sec=duration_sec,
            attempts_used=int(ch_summary["attempts_used"]),
            rewrites_triggered=int(ch_summary["rewrites_triggered"]),
            char_count=item.char_count,
            model=actual_model,
        )
        self._emit(
            SSE_PIPELINE_CHAPTER_LOCKED,
            book_id=book_id,
            chapter=item.chapter,
            message=f"LOCKED ch{item.chapter:04d} sha256:{item.sha256[:12]}",
            data={
                "chapter": item.chapter,
                "title": public_title,
                "char_count": item.char_count,
                "sha256_prefix": item.sha256[:12],
                "model": actual_model,
                "duration_sec": round(duration_sec, 1),
                "attempts_used": int(ch_summary["attempts_used"]),
                "rewrites_triggered": int(ch_summary["rewrites_triggered"]),
            },
        )
        self._emit(
            SSE_PIPELINE_STAGE,
            book_id=book_id,
            chapter=item.chapter,
            message=f"ch{item.chapter:04d} 已入库可导出",
            data={
                "stage": "export",
                "status": "done",
                "char_count": item.char_count,
                "model": actual_model,
                "detail": "已入库 · 可随时导出成书包",
            },
        )
        self._emit(
            SSE_PIPELINE_STAGE,
            book_id=book_id,
            chapter=item.chapter,
            message="待机 / 等待下一章",
            data={
                "stage": "idle",
                "status": "idle",
                "detail": "待机 / 等待下一章",
            },
        )
        self.event_broker.notify_poll_waiters()
        return {
            "book_id": book_id,
            "status": "PRODUCED",
            "chapter": item.chapter,
            "char_count": item.char_count,
            "model": item.model,
            "stockpiled_count": self.db.count_stockpiled(book_id),
        }

    def run_self_healing_pass(self) -> List[Dict[str, Any]]:
        """Autonomously recover transient/stochastic book isolations after exponential cooldown.

        Never weakens any quality gate, never resumes PAUSED_MANUAL or structural STATE_* deadlocks,
        and enforces a strict per-chapter retry ceiling so failed books cannot loop endlessly.
        """
        if not self.auto_heal_enabled:
            return []

        now = shanghai_now(self._now_fn)
        healed: List[Dict[str, Any]] = []
        with self._prod_lock:
            for book in self.db.list_books():
                bid = str(book["book_id"])
                status = str(book["status"])

                # Clear heal counter once the book progresses past the previously failing chapter
                if bid in self._heal_attempts:
                    prev_ch, _ = self._heal_attempts[bid]
                    if status == BookRunStatus.ACTIVE.value:
                        existing = self.db.get_existing_chapter_numbers(bid)
                        if prev_ch in existing or (existing and max(existing) >= prev_ch):
                            self._heal_attempts.pop(bid, None)

                # Also allow PAUSED_QUOTA_EXHAUSTED (not BudgetCircuitBreaker) to self-heal once quota resets
                if status == BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value:
                    paused_reason = str(book.get("paused_reason") or "")
                    if "BudgetCircuitBreaker" in paused_reason or "budget" in paused_reason.lower():
                        continue
                    quotas = self.db.list_model_quotas()
                    reset_times = [
                        _parse_iso_dt(q.get("reset_at"))
                        for q in quotas
                        if q.get("reset_at")
                    ]
                    valid_resets = [dt for dt in reset_times if dt is not None]
                    if valid_resets and any((now - dt).total_seconds() >= 60 for dt in valid_resets):
                        iso_dt = _parse_iso_dt(book.get("updated_at"))
                        if iso_dt is None or (now - iso_dt).total_seconds() >= 60:
                            self.db.update_book_status(bid, BookRunStatus.ACTIVE, paused_reason="")
                            msg = f"[SelfHeal] 免费/轮换模型额度已重置，自动恢复《{book['title']}》生产"
                            logger.info(msg)
                            self._emit(
                                SSE_PIPELINE_MODEL_EVENT,
                                book_id=bid,
                                message=msg,
                                data={"kind": "self_heal", "reason": "quota_reset_elapsed"},
                            )
                            healed.append({"book_id": bid, "chapter": 0, "reason": "quota_reset_elapsed", "attempt": 1})
                    continue

                eligible, reason_code, chapter, cap_max = classify_self_heal_eligibility(book)
                if not eligible:
                    continue

                effective_max = min(cap_max, len(self.auto_heal_cooldown_secs))
                prev_ch, attempts_done = self._heal_attempts.get(bid, (chapter, 0))
                if prev_ch != chapter:
                    attempts_done = 0

                if attempts_done >= effective_max:
                    paused_reason = str(book.get("paused_reason") or "")
                    if "自愈已达上限" not in paused_reason:
                        self.db.update_book_status(
                            bid,
                            BookRunStatus.ISOLATED_GATE_FAILURE,
                            paused_reason=f"[自愈已达上限 {effective_max}/{effective_max}，待人工排查] {paused_reason}",
                        )
                    continue

                detail = book.get("isolation_detail") if isinstance(book.get("isolation_detail"), Mapping) else {}
                iso_dt = _parse_iso_dt(detail.get("isolated_at")) or _parse_iso_dt(book.get("updated_at"))
                required_wait = self.auto_heal_cooldown_secs[min(attempts_done, len(self.auto_heal_cooldown_secs) - 1)]
                elapsed = (now - iso_dt).total_seconds() if iso_dt is not None else required_wait
                if elapsed < required_wait:
                    continue

                next_attempt = attempts_done + 1
                self._heal_attempts[bid] = (chapter, next_attempt)
                self.db.update_book_status(bid, BookRunStatus.ACTIVE, paused_reason="")
                msg = (
                    f"[SelfHeal] 检测到可恢复隔离（{reason_code}），冷却 {int(elapsed)}s 后自动恢复"
                    f"《{book['title']}》第 {chapter} 章（自愈第 {next_attempt}/{effective_max} 轮）"
                )
                logger.info(msg)
                print(msg, flush=True)
                self._emit(
                    SSE_PIPELINE_MODEL_EVENT,
                    book_id=bid,
                    chapter=chapter,
                    message=msg,
                    data={
                        "kind": "self_heal",
                        "reason": reason_code,
                        "heal_attempt": next_attempt,
                        "max_heal_attempts": effective_max,
                        "cooldown_elapsed_sec": round(elapsed, 1),
                    },
                )
                healed.append(
                    {
                        "book_id": bid,
                        "chapter": chapter,
                        "reason": reason_code,
                        "attempt": next_attempt,
                        "max_attempts": effective_max,
                    }
                )
        return healed

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
                    self.run_self_healing_pass()
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
                next_rotation: Tuple[str, ...] = existing.rotation_models
                if existing.model_whitelist:
                    if binding.model in existing.manual_only_models:
                        next_rotation = ()
                    elif binding.model in existing.rotation_models:
                        idx = existing.rotation_models.index(binding.model)
                        next_rotation = existing.rotation_models[idx:]
                    else:
                        next_rotation = ()
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
                    model_whitelist=existing.model_whitelist,
                    rotation_models=next_rotation,
                    manual_only_models=existing.manual_only_models,
                    model_cooldown_sec=existing.model_cooldown_sec,
                    rate_limit_backoff_sec=existing.rate_limit_backoff_sec,
                    model_labels=dict(existing.model_labels),
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
