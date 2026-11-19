from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import hmac
import json
import secrets
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    MutableMapping,
    MutableSet,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from .models import AUTHOR_LEVELS, SHANGHAI_TZ


# ---------------------------------------------------------------------------
# Frozen REST API Path Constants (Consumed by T11, T12, T13, T14, T15)
# ---------------------------------------------------------------------------
API_HEALTH = "/api/health"
API_GENRES = "/api/genres"

API_BOOKS = "/api/books"
API_BOOKS_AUTO_CREATE = "/api/books/auto-create"
API_BOOK_STATUS = "/api/books/{book_id}/status"
API_BOOK_CHAPTERS = "/api/books/{book_id}/chapters"
API_BOOK_CHAPTER_DETAIL = "/api/books/{book_id}/chapters/{chapter}"
API_BOOK_CHAPTER_LOCK = "/api/books/{book_id}/chapters/{chapter}/lock"
API_BOOK_EXPORT = "/api/books/{book_id}/export"
API_BOOK_COVER = "/api/books/{book_id}/cover"
API_BOOK_CUT = "/api/books/{book_id}/cut"
API_BOOK_PROMOTE = "/api/books/{book_id}/promote"
API_BOOK_EXTEND = "/api/books/{book_id}/extend"
API_BOOK_RESUME = "/api/books/{book_id}/resume"

API_CPA_AUTH_FILES = "/api/cpa/auth-files"
API_CPA_AUTH_FILE_DETAIL = "/api/cpa/auth-files/{name}"
API_CPA_MODELS = "/api/cpa/models"
API_CPA_USAGE = "/api/cpa/usage"
API_CPA_OAUTH = "/api/cpa/oauth"
API_CPA_BINDINGS = "/api/cpa/bindings"
API_MODEL_SWITCHES = "/api/model-switches"

API_COMPANION_POLL = "/api/companion/poll"
API_COMPANION_LEASE = "/api/companion/lease"
API_COMPANION_RECEIPT = "/api/companion/receipt"
API_COMPANION_METRICS = "/api/companion/metrics"
API_COMPANION_CAPTCHA = "/api/companion/captcha"
API_COMPANION_RESUME = "/api/companion/resume"

API_EVENTS_STREAM = "/api/events/stream"

# Public read-only studio (no auth; owner Web UI grill-me Q6/Q8/Q10)
API_PUBLIC_BOOKS = "/api/public/books"
API_PUBLIC_BOOK = "/api/public/books/{book_id}"
API_PUBLIC_CHAPTER = "/api/public/books/{book_id}/chapters/{chapter}"
API_PUBLIC_EVENTS_STREAM = "/api/public/events/stream"

# Admin new-book wizard and production KPI (admin auth)
API_WIZARD_SESSIONS = "/api/wizard/sessions"
API_WIZARD_SESSION = "/api/wizard/sessions/{session_id}"
API_WIZARD_ANSWER = "/api/wizard/sessions/{session_id}/answer"
API_WIZARD_CONFIRM = "/api/wizard/sessions/{session_id}/confirm"
API_PRODUCTION_KPI = "/api/production/kpi"

ALL_API_ROUTES: Tuple[str, ...] = (
    API_HEALTH,
    API_GENRES,
    API_BOOKS,
    API_BOOKS_AUTO_CREATE,
    API_BOOK_STATUS,
    API_BOOK_CHAPTERS,
    API_BOOK_CHAPTER_DETAIL,
    API_BOOK_CHAPTER_LOCK,
    API_BOOK_EXPORT,
    API_BOOK_COVER,
    API_BOOK_CUT,
    API_BOOK_PROMOTE,
    API_BOOK_EXTEND,
    API_BOOK_RESUME,
    API_CPA_AUTH_FILES,
    API_CPA_AUTH_FILE_DETAIL,
    API_CPA_MODELS,
    API_CPA_USAGE,
    API_CPA_OAUTH,
    API_CPA_BINDINGS,
    API_MODEL_SWITCHES,
    API_COMPANION_POLL,
    API_COMPANION_LEASE,
    API_COMPANION_RECEIPT,
    API_COMPANION_METRICS,
    API_COMPANION_CAPTCHA,
    API_COMPANION_RESUME,
    API_EVENTS_STREAM,
    API_PUBLIC_BOOKS,
    API_PUBLIC_BOOK,
    API_PUBLIC_CHAPTER,
    API_PUBLIC_EVENTS_STREAM,
    API_WIZARD_SESSIONS,
    API_WIZARD_SESSION,
    API_WIZARD_ANSWER,
    API_WIZARD_CONFIRM,
    API_PRODUCTION_KPI,
)


def format_api_route(template: str, **params: Any) -> str:
    """Format a frozen API route template with validated non-empty parameters."""
    clean_params: Dict[str, str] = {}
    for k, v in params.items():
        val = str(v).strip()
        if not val:
            raise ValueError(f"Route parameter '{k}' for '{template}' cannot be empty")
        clean_params[k] = val
    return template.format(**clean_params)


# ---------------------------------------------------------------------------
# Frozen Stockpile, Book & Task Enums
# ---------------------------------------------------------------------------
class StockpileStatus(str, Enum):
    STOCKPILED = "STOCKPILED"
    LEASED = "LEASED"
    DRAFT_SAVED = "DRAFT_SAVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    PAUSED_AUTH_CAPTCHA = "PAUSED_AUTH_CAPTCHA"
    FAILED_HARD_MISMATCH = "FAILED_HARD_MISMATCH"


VALID_STOCKPILE_TRANSITIONS: Dict[StockpileStatus, FrozenSet[StockpileStatus]] = {
    StockpileStatus.STOCKPILED: frozenset({StockpileStatus.LEASED}),
    StockpileStatus.LEASED: frozenset(
        {
            StockpileStatus.STOCKPILED,
            StockpileStatus.DRAFT_SAVED,
            StockpileStatus.SCHEDULED,
            StockpileStatus.PUBLISHED,
            StockpileStatus.PAUSED_AUTH_CAPTCHA,
            StockpileStatus.FAILED_HARD_MISMATCH,
        }
    ),
    StockpileStatus.DRAFT_SAVED: frozenset(
        {
            StockpileStatus.SCHEDULED,
            StockpileStatus.PUBLISHED,
            StockpileStatus.PAUSED_AUTH_CAPTCHA,
            StockpileStatus.FAILED_HARD_MISMATCH,
        }
    ),
    StockpileStatus.SCHEDULED: frozenset(
        {
            StockpileStatus.PUBLISHED,
            StockpileStatus.PAUSED_AUTH_CAPTCHA,
        }
    ),
    StockpileStatus.PUBLISHED: frozenset(),
    StockpileStatus.PAUSED_AUTH_CAPTCHA: frozenset(
        {
            StockpileStatus.STOCKPILED,
            StockpileStatus.LEASED,
        }
    ),
    StockpileStatus.FAILED_HARD_MISMATCH: frozenset(
        {
            StockpileStatus.STOCKPILED,
        }
    ),
}


class BookRunStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ISOLATED_GATE_FAILURE = "ISOLATED_GATE_FAILURE"
    PAUSED_AUTH_CAPTCHA = "PAUSED_AUTH_CAPTCHA"
    PAUSED_QUOTA_EXHAUSTED = "PAUSED_QUOTA_EXHAUSTED"
    PAUSED_MANUAL = "PAUSED_MANUAL"
    CUT_LOSS = "CUT_LOSS"
    COMPLETED = "COMPLETED"


class CompanionTaskType(str, Enum):
    PUBLISH_CHAPTER = "PUBLISH_CHAPTER"
    COLLECT_METRICS = "COLLECT_METRICS"
    ROTATE_PACKAGING = "ROTATE_PACKAGING"
    CREATE_STAR_CARDS = "CREATE_STAR_CARDS"
    SUBMIT_SIGNING_DRAFT = "SUBMIT_SIGNING_DRAFT"


# ---------------------------------------------------------------------------
# SSE Event Constants & Framing
# ---------------------------------------------------------------------------
SSE_HEARTBEAT = "heartbeat"
SSE_CHAPTER_STARTED = "chapter_started"
SSE_CHAPTER_ATTEMPT = "chapter_attempt"
SSE_GATE_FAILED = "gate_failed"
SSE_GATE_PASSED = "gate_passed"
SSE_CRITIC_SCORED = "critic_scored"
SSE_SURGICAL_POLISHED = "surgical_polished"
SSE_CHAPTER_STOCKPILED = "chapter_stockpiled"
SSE_BOOK_ISOLATED = "book_isolated"
SSE_SAFETY_CUSHION_ALERT = "safety_cushion_alert"
SSE_COMPANION_CAPTCHA = "companion_captcha"
SSE_COMPANION_RESUMED = "companion_resumed"
SSE_METRICS_COLLECTED = "metrics_collected"
SSE_RACING_DECISION = "racing_decision"
SSE_CPA_QUOTA_ALERT = "cpa_quota_alert"

ALL_SSE_EVENTS: Tuple[str, ...] = (
    SSE_HEARTBEAT,
    SSE_CHAPTER_STARTED,
    SSE_CHAPTER_ATTEMPT,
    SSE_GATE_FAILED,
    SSE_GATE_PASSED,
    SSE_CRITIC_SCORED,
    SSE_SURGICAL_POLISHED,
    SSE_CHAPTER_STOCKPILED,
    SSE_BOOK_ISOLATED,
    SSE_SAFETY_CUSHION_ALERT,
    SSE_COMPANION_CAPTCHA,
    SSE_COMPANION_RESUMED,
    SSE_METRICS_COLLECTED,
    SSE_RACING_DECISION,
    SSE_CPA_QUOTA_ALERT,
)


def _shanghai_now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def format_sse_frame(
    event: str,
    data: Mapping[str, Any] | str,
    *,
    event_id: Optional[str] = None,
    retry_ms: Optional[int] = None,
) -> str:
    """Format a compliant Server-Sent Events (SSE) frame."""
    event_name = (event or "").strip()
    if not event_name:
        raise ValueError("SSE event name cannot be empty")
    lines: List[str] = []
    if event_id is not None and str(event_id).strip():
        lines.append(f"id: {str(event_id).strip()}")
    if retry_ms is not None:
        lines.append(f"retry: {int(retry_ms)}")
    lines.append(f"event: {event_name}")
    if isinstance(data, str):
        payload_lines = data.splitlines() or [""]
        for pl in payload_lines:
            lines.append(f"data: {pl}")
    else:
        encoded = json.dumps(dict(data), ensure_ascii=False)
        lines.append(f"data: {encoded}")
    return "\n".join(lines) + "\n\n"


@dataclass
class SSEEventPayload:
    event: str
    book_id: str = ""
    chapter: int = 0
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_shanghai_now_iso)
    event_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_sse_frame(self) -> str:
        return format_sse_frame(
            self.event,
            self.to_dict(),
            event_id=self.event_id or None,
        )


# ---------------------------------------------------------------------------
# Cloud-Edge HMAC Authentication & Anti-Replay Pure Functions
# ---------------------------------------------------------------------------
HMAC_HEADER_TIMESTAMP = "X-Xiaoshuo-Timestamp"
HMAC_HEADER_NONCE = "X-Xiaoshuo-Nonce"
HMAC_HEADER_SIGNATURE = "X-Xiaoshuo-Signature"
HMAC_HEADER_CLIENT_ID = "X-Xiaoshuo-Client-Id"
HMAC_MAX_SKEW_SECONDS = 60


class HMACVerificationError(ValueError):
    """Base error raised when cloud-edge HMAC signature, timestamp, or nonce check fails."""


class HMACTimestampError(HMACVerificationError):
    """Raised when HMAC timestamp header is missing, malformed, or outside ±60s window."""


class HMACReplayError(HMACVerificationError):
    """Raised when HMAC nonce has already been observed within the validity window."""


class HMACSignatureError(HMACVerificationError):
    """Raised when HMAC signature header is missing or does not match expected digest."""


def _normalize_body_bytes(body: bytes | str | Mapping[str, Any] | None) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(dict(body), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _canonical_hmac_message(
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    client_id: str,
    body_bytes: bytes,
) -> bytes:
    body_sha256 = hashlib.sha256(body_bytes).hexdigest()
    canonical = (
        f"{method.strip().upper()}\n"
        f"{path.strip()}\n"
        f"{int(timestamp)}\n"
        f"{nonce.strip()}\n"
        f"{client_id.strip()}\n"
        f"{body_sha256}"
    )
    return canonical.encode("utf-8")


def compute_hmac_headers(
    secret: str | bytes,
    method: str,
    path: str,
    body: bytes | str | Mapping[str, Any] | None = b"",
    *,
    client_id: str = "companion-default",
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
) -> Dict[str, str]:
    """Compute cloud-edge HMAC-SHA256 authentication headers using SHANGHAI_TZ timestamp."""
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not secret_bytes:
        raise ValueError("HMAC secret cannot be empty")
    ts = int(datetime.now(SHANGHAI_TZ).timestamp()) if timestamp is None else int(timestamp)
    eff_nonce = (nonce if nonce is not None else secrets.token_hex(16)).strip()
    if not eff_nonce:
        raise ValueError("HMAC nonce cannot be empty")
    eff_client_id = (client_id or "").strip()
    if not eff_client_id:
        raise ValueError("HMAC client_id cannot be empty")

    body_bytes = _normalize_body_bytes(body)
    msg = _canonical_hmac_message(method, path, ts, eff_nonce, eff_client_id, body_bytes)
    sig = hmac.new(secret_bytes, msg, hashlib.sha256).hexdigest()
    return {
        HMAC_HEADER_TIMESTAMP: str(ts),
        HMAC_HEADER_NONCE: eff_nonce,
        HMAC_HEADER_SIGNATURE: sig,
        HMAC_HEADER_CLIENT_ID: eff_client_id,
    }


def _lookup_header_ci(headers: Mapping[str, str], target: str) -> str:
    if target in headers:
        return str(headers[target]).strip()
    target_lower = target.lower()
    for k, v in headers.items():
        if str(k).lower() == target_lower:
            return str(v).strip()
    return ""


def verify_hmac_headers(
    secret: str | bytes,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes | str | Mapping[str, Any] | None = b"",
    *,
    now: Optional[int] = None,
    max_skew_seconds: int = HMAC_MAX_SKEW_SECONDS,
    seen_nonces: Optional[MutableMapping[str, float] | MutableSet[str]] = None,
) -> Tuple[str, int, str]:
    """Verify ±60s timestamp, anti-replay nonce, and constant-time HMAC-SHA256 signature.

    Returns (client_id, timestamp, nonce) on success; raises HMACVerificationError on failure.
    """
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not secret_bytes:
        raise ValueError("HMAC secret cannot be empty")
    if max_skew_seconds <= 0:
        raise ValueError(f"max_skew_seconds must be positive, got {max_skew_seconds}")

    raw_ts = _lookup_header_ci(headers, HMAC_HEADER_TIMESTAMP)
    if not raw_ts:
        raise HMACTimestampError(f"Missing required header '{HMAC_HEADER_TIMESTAMP}'")
    try:
        ts = int(raw_ts)
    except ValueError as exc:
        raise HMACTimestampError(f"Invalid HMAC timestamp {raw_ts!r}") from exc

    now_ts = int(datetime.now(SHANGHAI_TZ).timestamp()) if now is None else int(now)
    skew = abs(now_ts - ts)
    if skew > max_skew_seconds:
        raise HMACTimestampError(
            f"HMAC timestamp expired or skewed by {skew}s (max ±{max_skew_seconds}s)"
        )

    nonce = _lookup_header_ci(headers, HMAC_HEADER_NONCE)
    if not nonce or len(nonce) < 8:
        raise HMACReplayError(f"Missing or weak header '{HMAC_HEADER_NONCE}'")

    client_id = _lookup_header_ci(headers, HMAC_HEADER_CLIENT_ID)
    if not client_id:
        raise HMACSignatureError(f"Missing required header '{HMAC_HEADER_CLIENT_ID}'")

    provided_sig = _lookup_header_ci(headers, HMAC_HEADER_SIGNATURE)
    if not provided_sig:
        raise HMACSignatureError(f"Missing required header '{HMAC_HEADER_SIGNATURE}'")

    body_bytes = _normalize_body_bytes(body)
    msg = _canonical_hmac_message(method, path, ts, nonce, client_id, body_bytes)
    expected_sig = hmac.new(secret_bytes, msg, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(provided_sig.lower(), expected_sig.lower()):
        raise HMACSignatureError("HMAC signature mismatch")

    if seen_nonces is not None:
        nonce_key = f"{client_id}:{nonce}"
        if isinstance(seen_nonces, MutableMapping):
            cutoff = now_ts - max_skew_seconds
            expired = [k for k, v in list(seen_nonces.items()) if v < cutoff]
            for k in expired:
                del seen_nonces[k]
            if nonce_key in seen_nonces:
                raise HMACReplayError(f"Replayed HMAC nonce '{nonce}' for client '{client_id}'")
            seen_nonces[nonce_key] = float(ts)
        else:
            if nonce_key in seen_nonces:
                raise HMACReplayError(f"Replayed HMAC nonce '{nonce}' for client '{client_id}'")
            seen_nonces.add(nonce_key)

    return client_id, ts, nonce


# ---------------------------------------------------------------------------
# Request / Response / Error Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ErrorPayload:
    error_code: str
    message: str
    detail: Optional[Dict[str, Any]] = None
    timestamp: str = field(default_factory=_shanghai_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ErrorPayload:
        return cls(
            error_code=str(data["error_code"]),
            message=str(data["message"]),
            detail=dict(data["detail"]) if data.get("detail") is not None else None,
            timestamp=str(data.get("timestamp") or _shanghai_now_iso()),
        )


@dataclass
class HealthResponse:
    status: str = "ok"
    service: str = "xiaoshuo-bot"
    version: str = "2.0.0"
    shanghai_time: str = field(default_factory=_shanghai_now_iso)
    cpa_reachable: bool = False
    active_books: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HealthResponse:
        return cls(
            status=str(data.get("status", "ok")),
            service=str(data.get("service", "xiaoshuo-bot")),
            version=str(data.get("version", "2.0.0")),
            shanghai_time=str(data.get("shanghai_time") or _shanghai_now_iso()),
            cpa_reachable=bool(data.get("cpa_reachable", False)),
            active_books=int(data.get("active_books", 0)),
        )


@dataclass
class AutoCreateBookRequest:
    project_id: str
    genre_id: str
    author_level: str = "lv0_1"
    account_id: str = ""
    pen_name: str = ""
    hot_keywords: List[str] = field(default_factory=list)
    benchmark_chapters: List[str] = field(default_factory=list)
    target_chapters: Optional[int] = None
    creative_brief: str = ""

    def __post_init__(self) -> None:
        if not self.project_id or not str(self.project_id).strip():
            raise ValueError("project_id cannot be empty")
        if not self.genre_id or not str(self.genre_id).strip():
            raise ValueError("genre_id cannot be empty")
        if self.author_level not in AUTHOR_LEVELS:
            raise ValueError(
                f"Unknown author_level {self.author_level!r}; expected one of {AUTHOR_LEVELS}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AutoCreateBookRequest:
        return cls(
            project_id=str(data["project_id"]),
            genre_id=str(data["genre_id"]),
            author_level=str(data.get("author_level", "lv0_1")),
            account_id=str(data.get("account_id", "")),
            pen_name=str(data.get("pen_name", "")),
            hot_keywords=[str(x) for x in data.get("hot_keywords", [])],
            benchmark_chapters=[str(x) for x in data.get("benchmark_chapters", [])],
            target_chapters=(
                int(data["target_chapters"]) if data.get("target_chapters") is not None else None
            ),
            creative_brief=str(data.get("creative_brief", "")),
        )


@dataclass
class BookSummaryResponse:
    book_id: str
    title: str
    genre_id: str = ""
    author_level: str = "lv0_1"
    contract_track: str = "complete_150k"
    status: str = BookRunStatus.ACTIVE.value
    stockpiled_count: int = 0
    safety_cushion_target: int = 10
    low_cushion_alert: bool = False
    max_published_chapter: int = 0
    total_chapters: int = 0
    daily_target_chapters: int = 2
    account_id: str = ""
    paused_reason: str = ""
    alternate_titles: List[str] = field(default_factory=list)
    cover_url: str = ""
    updated_at: str = field(default_factory=_shanghai_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BookSummaryResponse:
        return cls(
            book_id=str(data["book_id"]),
            title=str(data["title"]),
            genre_id=str(data.get("genre_id", "")),
            author_level=str(data.get("author_level", "lv0_1")),
            contract_track=str(data.get("contract_track", "complete_150k")),
            status=str(data.get("status", BookRunStatus.ACTIVE.value)),
            stockpiled_count=int(data.get("stockpiled_count", 0)),
            safety_cushion_target=int(data.get("safety_cushion_target", 10)),
            low_cushion_alert=bool(data.get("low_cushion_alert", False)),
            max_published_chapter=int(data.get("max_published_chapter", 0)),
            total_chapters=int(data.get("total_chapters", 0)),
            daily_target_chapters=int(data.get("daily_target_chapters", 2)),
            account_id=str(data.get("account_id", "")),
            paused_reason=str(data.get("paused_reason", "")),
            alternate_titles=[str(x) for x in data.get("alternate_titles", [])],
            cover_url=str(data.get("cover_url", "")),
            updated_at=str(data.get("updated_at") or _shanghai_now_iso()),
        )


@dataclass
class ChapterStatusItem:
    chapter: int
    public_title: str
    status: str
    char_count: int = 0
    locked: bool = False
    sha256: str = ""
    author_note: str = ""
    scheduled_at: str = ""
    published_at: str = ""
    error_message: str = ""
    model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChapterStatusItem:
        return cls(
            chapter=int(data["chapter"]),
            public_title=str(data.get("public_title", "")),
            status=str(data["status"]),
            char_count=int(data.get("char_count", 0)),
            locked=bool(data.get("locked", False)),
            sha256=str(data.get("sha256", "")),
            author_note=str(data.get("author_note", "")),
            scheduled_at=str(data.get("scheduled_at", "")),
            published_at=str(data.get("published_at", "")),
            error_message=str(data.get("error_message", "")),
            model=str(data.get("model", "")),
        )


@dataclass
class ChapterLockRequest:
    locked: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChapterLockRequest:
        return cls(
            locked=bool(data["locked"]),
            reason=str(data.get("reason", "")),
        )


@dataclass
class CutBookRequest:
    reason: str = ""
    confirm: bool = False
    confirmation_version: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CutBookRequest:
        return cls(
            reason=str(data.get("reason", "")),
            confirm=bool(data.get("confirm", False)),
            confirmation_version=int(data.get("confirmation_version", 0)),
        )


@dataclass
class CompanionPollRequest:
    account_id: str
    author_name: str = ""
    book_ids: List[str] = field(default_factory=list)
    timeout_sec: int = 25

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompanionPollRequest:
        return cls(
            account_id=str(data["account_id"]),
            author_name=str(data.get("author_name", "")),
            book_ids=[str(x) for x in data.get("book_ids", [])],
            timeout_sec=int(data.get("timeout_sec", 25)),
        )


@dataclass
class CompanionLeaseRequest:
    task_id: str
    book_id: str
    chapter: int
    account_id: str
    lease_seconds: int = 300

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompanionLeaseRequest:
        return cls(
            task_id=str(data["task_id"]),
            book_id=str(data["book_id"]),
            chapter=int(data["chapter"]),
            account_id=str(data["account_id"]),
            lease_seconds=int(data.get("lease_seconds", 300)),
        )


@dataclass
class CompanionLeaseResponse:
    task_id: str
    task_type: str
    book_id: str
    book_title: str
    author_name: str
    fanqie_book_id: str
    chapter: int
    public_title: str
    body: str
    author_note: str = ""
    sha256: str = ""
    status: str = StockpileStatus.LEASED.value
    lease_expires_at: str = ""
    scheduled_publish_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompanionLeaseResponse:
        return cls(
            task_id=str(data["task_id"]),
            task_type=str(data.get("task_type", CompanionTaskType.PUBLISH_CHAPTER.value)),
            book_id=str(data["book_id"]),
            book_title=str(data.get("book_title", "")),
            author_name=str(data.get("author_name", "")),
            fanqie_book_id=str(data.get("fanqie_book_id", "")),
            chapter=int(data.get("chapter", 0)),
            public_title=str(data.get("public_title", "")),
            body=str(data.get("body", "")),
            author_note=str(data.get("author_note", "")),
            sha256=str(data.get("sha256", "")),
            status=str(data.get("status", StockpileStatus.LEASED.value)),
            lease_expires_at=str(data.get("lease_expires_at", "")),
            scheduled_publish_at=str(data.get("scheduled_publish_at", "")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class CompanionReceiptRequest:
    task_id: str
    book_id: str
    chapter: int
    account_id: str
    status: str
    remote_chapter_id: str = ""
    char_count: int = 0
    scheduled_at: str = ""
    published_at: str = ""
    error_code: str = ""
    error_message: str = ""
    sha256: str = ""

    def __post_init__(self) -> None:
        StockpileStatus(self.status)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompanionReceiptRequest:
        return cls(
            task_id=str(data["task_id"]),
            book_id=str(data["book_id"]),
            chapter=int(data["chapter"]),
            account_id=str(data["account_id"]),
            status=str(data["status"]),
            remote_chapter_id=str(data.get("remote_chapter_id", "")),
            char_count=int(data.get("char_count", 0)),
            scheduled_at=str(data.get("scheduled_at", "")),
            published_at=str(data.get("published_at", "")),
            error_code=str(data.get("error_code", "")),
            error_message=str(data.get("error_message", "")),
            sha256=str(data.get("sha256", "")),
        )


@dataclass
class CompanionMetricsRequest:
    book_id: str
    account_id: str
    settled: bool
    settlement_status: str
    collected_at: str = field(default_factory=_shanghai_now_iso)
    metrics_date: str = ""
    ch2_read_through: Optional[float] = None
    word100k_read_through: Optional[float] = None
    click_through_rate: Optional[float] = None
    bookshelf_rate: Optional[float] = None
    impressions: Optional[int] = None
    daily_revenue_cny: Optional[float] = None
    comments: List[Dict[str, Any]] = field(default_factory=list)
    decoded_hotlist: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompanionMetricsRequest:
        return cls(
            book_id=str(data["book_id"]),
            account_id=str(data["account_id"]),
            settled=bool(data["settled"]),
            settlement_status=str(data["settlement_status"]),
            collected_at=str(data.get("collected_at") or _shanghai_now_iso()),
            metrics_date=str(data.get("metrics_date", "")),
            ch2_read_through=(
                float(data["ch2_read_through"]) if data.get("ch2_read_through") is not None else None
            ),
            word100k_read_through=(
                float(data["word100k_read_through"])
                if data.get("word100k_read_through") is not None
                else None
            ),
            click_through_rate=(
                float(data["click_through_rate"])
                if data.get("click_through_rate") is not None
                else None
            ),
            bookshelf_rate=(
                float(data["bookshelf_rate"]) if data.get("bookshelf_rate") is not None else None
            ),
            impressions=int(data["impressions"]) if data.get("impressions") is not None else None,
            daily_revenue_cny=(
                float(data["daily_revenue_cny"]) if data.get("daily_revenue_cny") is not None else None
            ),
            comments=[dict(c) for c in data.get("comments", [])],
            decoded_hotlist=[dict(h) for h in data.get("decoded_hotlist", [])],
        )


@dataclass
class CompanionCaptchaReport:
    account_id: str
    book_id: str = ""
    chapter: int = 0
    reason: str = "CAPTCHA_DETECTED"
    page_url: str = ""
    screenshot_base64: str = ""
    screenshot_mime: str = "image/png"
    detected_at: str = field(default_factory=_shanghai_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompanionCaptchaReport:
        return cls(
            account_id=str(data["account_id"]),
            book_id=str(data.get("book_id", "")),
            chapter=int(data.get("chapter", 0)),
            reason=str(data.get("reason", "CAPTCHA_DETECTED")),
            page_url=str(data.get("page_url", "")),
            screenshot_base64=str(data.get("screenshot_base64", "")),
            screenshot_mime=str(data.get("screenshot_mime", "image/png")),
            detected_at=str(data.get("detected_at") or _shanghai_now_iso()),
        )


@dataclass
class CompanionResumeRequest:
    account_id: str
    book_id: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompanionResumeRequest:
        return cls(
            account_id=str(data["account_id"]),
            book_id=str(data.get("book_id", "")),
            note=str(data.get("note", "")),
        )


@dataclass
class CPABindingConfig:
    role: str
    model: str
    base_url: str
    provider_type: str = "openai"
    reasoning_effort: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CPABindingConfig:
        return cls(
            role=str(data["role"]),
            model=str(data["model"]),
            base_url=str(data["base_url"]),
            provider_type=str(data.get("provider_type", "openai")),
            reasoning_effort=data.get("reasoning_effort"),
        )


@dataclass
class MatrixDailyDigest:
    generated_at: str = field(default_factory=_shanghai_now_iso)
    total_books: int = 0
    active_books: int = 0
    low_cushion_books: int = 0
    isolated_books: int = 0
    paused_captcha_accounts: List[str] = field(default_factory=list)
    daily_chars_generated: int = 0
    daily_cost_usd: float = 0.0
    book_summaries: List[BookSummaryResponse] = field(default_factory=list)
    pending_cut_recommendations: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total_books": self.total_books,
            "active_books": self.active_books,
            "low_cushion_books": self.low_cushion_books,
            "isolated_books": self.isolated_books,
            "paused_captcha_accounts": list(self.paused_captcha_accounts),
            "daily_chars_generated": self.daily_chars_generated,
            "daily_cost_usd": self.daily_cost_usd,
            "book_summaries": [b.to_dict() for b in self.book_summaries],
            "pending_cut_recommendations": [dict(x) for x in self.pending_cut_recommendations],
            "alerts": list(self.alerts),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MatrixDailyDigest:
        return cls(
            generated_at=str(data.get("generated_at") or _shanghai_now_iso()),
            total_books=int(data.get("total_books", 0)),
            active_books=int(data.get("active_books", 0)),
            low_cushion_books=int(data.get("low_cushion_books", 0)),
            isolated_books=int(data.get("isolated_books", 0)),
            paused_captcha_accounts=[str(x) for x in data.get("paused_captcha_accounts", [])],
            daily_chars_generated=int(data.get("daily_chars_generated", 0)),
            daily_cost_usd=float(data.get("daily_cost_usd", 0.0)),
            book_summaries=[
                BookSummaryResponse.from_dict(b) for b in data.get("book_summaries", [])
            ],
            pending_cut_recommendations=[
                dict(x) for x in data.get("pending_cut_recommendations", [])
            ],
            alerts=[str(x) for x in data.get("alerts", [])],
        )


# ---------------------------------------------------------------------------
# MatrixControlPort (Protocol consumed by T13 Telegram Bot & implemented by T11)
# ---------------------------------------------------------------------------
@runtime_checkable
class MatrixControlPort(Protocol):
    """Abstract control port decoupling T13 Telegram Bot from T11 scheduler internals."""

    def get_daily_digest(self) -> MatrixDailyDigest: ...

    def list_books(self) -> List[BookSummaryResponse]: ...

    def get_book_status(self, book_id: str) -> BookSummaryResponse: ...

    def auto_create_book(self, request: AutoCreateBookRequest) -> BookSummaryResponse: ...

    def approve_promotion(self, book_id: str) -> Dict[str, Any]: ...

    def request_cut_book(self, book_id: str, reason: str = "") -> Dict[str, Any]: ...

    def confirm_cut_book(
        self, book_id: str, confirmation_version: int, reason: str = ""
    ) -> Dict[str, Any]: ...

    def cancel_cut_book(self, book_id: str) -> Dict[str, Any]: ...

    def report_captcha_pause(self, report: CompanionCaptchaReport) -> Dict[str, Any]: ...

    def resume_companion(self, account_id: str, book_id: str = "") -> Dict[str, Any]: ...

    def pause_book(self, book_id: str, reason: str = "") -> Dict[str, Any]: ...

    def resume_book(self, book_id: str) -> Dict[str, Any]: ...


__all__ = [
    "ALL_API_ROUTES",
    "ALL_SSE_EVENTS",
    "API_BOOKS",
    "API_BOOKS_AUTO_CREATE",
    "API_BOOK_CHAPTERS",
    "API_BOOK_CHAPTER_DETAIL",
    "API_BOOK_CHAPTER_LOCK",
    "API_BOOK_COVER",
    "API_BOOK_CUT",
    "API_BOOK_EXPORT",
    "API_BOOK_EXTEND",
    "API_BOOK_PROMOTE",
    "API_BOOK_RESUME",
    "API_BOOK_STATUS",
    "API_COMPANION_CAPTCHA",
    "API_COMPANION_LEASE",
    "API_COMPANION_METRICS",
    "API_COMPANION_POLL",
    "API_COMPANION_RECEIPT",
    "API_COMPANION_RESUME",
    "API_CPA_AUTH_FILES",
    "API_CPA_AUTH_FILE_DETAIL",
    "API_CPA_BINDINGS",
    "API_CPA_MODELS",
    "API_CPA_OAUTH",
    "API_CPA_USAGE",
    "API_EVENTS_STREAM",
    "API_MODEL_SWITCHES",
    "API_PRODUCTION_KPI",
    "API_PUBLIC_BOOK",
    "API_PUBLIC_BOOKS",
    "API_PUBLIC_CHAPTER",
    "API_PUBLIC_EVENTS_STREAM",
    "API_WIZARD_ANSWER",
    "API_WIZARD_CONFIRM",
    "API_WIZARD_SESSION",
    "API_WIZARD_SESSIONS",
    "API_GENRES",
    "API_HEALTH",
    "AutoCreateBookRequest",
    "BookRunStatus",
    "BookSummaryResponse",
    "CPABindingConfig",
    "ChapterLockRequest",
    "ChapterStatusItem",
    "CompanionCaptchaReport",
    "CompanionLeaseRequest",
    "CompanionLeaseResponse",
    "CompanionMetricsRequest",
    "CompanionPollRequest",
    "CompanionReceiptRequest",
    "CompanionResumeRequest",
    "CompanionTaskType",
    "CutBookRequest",
    "ErrorPayload",
    "HMACReplayError",
    "HMACSignatureError",
    "HMACTimestampError",
    "HMACVerificationError",
    "HMAC_HEADER_CLIENT_ID",
    "HMAC_HEADER_NONCE",
    "HMAC_HEADER_SIGNATURE",
    "HMAC_HEADER_TIMESTAMP",
    "HMAC_MAX_SKEW_SECONDS",
    "HealthResponse",
    "MatrixControlPort",
    "MatrixDailyDigest",
    "SSEEventPayload",
    "SSE_BOOK_ISOLATED",
    "SSE_CHAPTER_ATTEMPT",
    "SSE_CHAPTER_STARTED",
    "SSE_CHAPTER_STOCKPILED",
    "SSE_COMPANION_CAPTCHA",
    "SSE_COMPANION_RESUMED",
    "SSE_CPA_QUOTA_ALERT",
    "SSE_CRITIC_SCORED",
    "SSE_GATE_FAILED",
    "SSE_GATE_PASSED",
    "SSE_HEARTBEAT",
    "SSE_METRICS_COLLECTED",
    "SSE_RACING_DECISION",
    "SSE_SAFETY_CUSHION_ALERT",
    "SSE_SURGICAL_POLISHED",
    "StockpileStatus",
    "VALID_STOCKPILE_TRANSITIONS",
    "compute_hmac_headers",
    "format_api_route",
    "format_sse_frame",
    "verify_hmac_headers",
]
