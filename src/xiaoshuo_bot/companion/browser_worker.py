from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from datetime import datetime
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.parse
import urllib.request

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..contracts import (
    CompanionCaptchaReport,
    CompanionLeaseResponse,
    CompanionMetricsRequest,
    CompanionReceiptRequest,
    CompanionResumeRequest,
    CompanionTaskType,
    StockpileStatus,
)
from ..evolution import MetricsNotSettledError
from ..exporter import strip_chapter_title_prefix
from ..models import SHANGHAI_TZ
from .client import CompanionHttpClient
from .font_decoder import Woff2FontDecoder

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)


_ACTIVE_PROFILE_LOCKS: set[Path] = set()
_ACTIVE_PROFILE_LOCKS_GUARD = threading.Lock()

_SAFE_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CHAPTER_NO_TEXT_RE = re.compile(r"第\s*(\d+)\s*章")
_UNSETTLED_TOKENS = frozenset(
    {"", "--", "-", "N/A", "NA", "NOT_SETTLED", "UNSETTLED", "PENDING", "未结算", "结算中"}
)

_LOGIN_URL_PATH_RE = re.compile(
    r"(?:^|/)(?:login|signin|auth|passport|captcha|verify|secsdk)(?:$|/|\?|\.)",
    re.IGNORECASE,
)
_LOGIN_URL_HOST_RE = re.compile(r"(?:^|\.)(passport|sso|login|captcha)\.", re.IGNORECASE)

_CAPTCHA_DOM_SELECTORS: Tuple[str, ...] = (
    ".secsdk-captcha",
    '[class*="secsdk-captcha"]',
    "#captcha_container",
    ".captcha-modal",
    '[data-testid="secsdk-captcha"]',
    'iframe[src*="captcha"]',
    ".login-expired-modal",
    ".auth-expired-modal",
    '[data-testid="login-modal"]',
)

_CAPTCHA_TEXT_MARKERS: Tuple[str, ...] = (
    "请完成安全验证",
    "滑块验证",
    "登录已过期",
    "扫码登录",
    "账号验证",
)

# User-authored / editable regions whose text must never trigger captcha text markers
# (e.g. a chapter body that happens to contain "登录已过期").
_CAPTCHA_TEXT_EXCLUDED_SELECTOR = (
    ".ProseMirror, textarea, input, select, [contenteditable], script, style, noscript, template"
)

# Only first-party document / XHR / fetch auth failures count toward the HTTP sentinel.
_AUTH_HTTP_RESOURCE_TYPES = frozenset({"document", "xhr", "fetch"})

# Minimum lead time for scheduled publishing; anything sooner is rejected as "in the past".
_MIN_SCHEDULE_LEAD_SEC = 5 * 60

# Cover upload hardening (ROTATE_PACKAGING).
_COVER_ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_COVER_MAX_BYTES = 5 * 1024 * 1024

# Hotlist WOFF2 download cap.
_WOFF2_MAX_BYTES = 2 * 1024 * 1024


class CompanionWorkerError(RuntimeError):
    """Base error for Playwright local companion worker."""


class ProfileLockError(CompanionWorkerError):
    """Raised when an account user_data_dir lock (.companion.lock) is already held."""


class AccountBookMismatchError(CompanionWorkerError):
    """Raised when page author_name, book_id, or book_title does not match the task verbatim."""


class EditorWordCountMismatchError(CompanionWorkerError):
    """Raised when live .ProseMirror editor word count deviates > 5% from expected body length."""


class MetricsSettlementWindowError(MetricsNotSettledError, CompanionWorkerError):
    """Raised when T-1 metrics collection is attempted before 12:30 CST (Asia/Shanghai)."""


class CaptchaAuthPauseError(CompanionWorkerError):
    """Raised at 0ms when any passive login/captcha sentinel triggers."""

    def __init__(
        self,
        message: str,
        *,
        report: CompanionCaptchaReport,
        screenshot_path: Optional[Path] = None,
    ) -> None:
        super().__init__(message)
        self.report = report
        self.screenshot_path = screenshot_path


@dataclass(frozen=True)
class BrowserLaunchConfig:
    profile_root: Path
    channel: Optional[str] = "chrome"
    headless: bool = False
    slow_mo_ms: int = 0
    timeout_ms: int = 15000
    review_pause_sec: float = 0.8
    screenshot_dir: Optional[Path] = None
    cover_root: Optional[Path] = None

    def __post_init__(self) -> None:
        if not str(self.profile_root).strip():
            raise ValueError("profile_root cannot be empty")
        if self.review_pause_sec < 0:
            raise ValueError("review_pause_sec must be >= 0")


@dataclass(frozen=True)
class TomatoDomSelectors:
    author_name: str = '[data-testid="author-name"], .author-name, #author-name'
    book_id: str = '[data-testid="book-id"], #book-id'
    book_title: str = '[data-testid="book-title"], .book-title, #book-title'
    chapter_list_item: str = '[data-testid="chapter-item"], .chapter-list-item, .chapter-row'
    chapter_number_input: str = 'input[name="chapter_no"], input[data-testid="chapter-number-input"], #chapter-number-input'
    chapter_title_input: str = 'input[name="chapter_title"], input[data-testid="chapter-title-input"], #chapter-title-input'
    editor_prosemirror: str = ".ProseMirror"
    editor_word_count: str = '[data-testid="editor-word-count"], .editor-word-count, #editor-word-count'
    author_note_input: str = 'textarea[name="author_note"], textarea[data-testid="author-note-input"], #author-note-input'
    schedule_time_input: str = 'input[name="scheduled_publish_at"], input[data-testid="schedule-time-input"], #schedule-time-input'
    save_draft_btn: str = 'button[data-testid="save-draft-btn"], #save-draft-btn'
    publish_submit_btn: str = 'button[data-testid="publish-submit-btn"], #publish-submit-btn'
    remote_chapter_id_attr: str = "data-remote-chapter-id"
    star_card_name_input: str = 'input[name="star_card_name"], [data-testid="star-card-name"], #star-card-name'
    star_card_role_input: str = 'input[name="star_card_role"], [data-testid="star-card-role"], #star-card-role'
    star_card_tagline_input: str = 'input[name="star_card_tagline"], [data-testid="star-card-tagline"], #star-card-tagline'
    star_card_personality_input: str = 'textarea[name="star_card_personality"], [data-testid="star-card-personality"], #star-card-personality'
    star_card_submit_btn: str = 'button[data-testid="star-card-submit"], #star-card-submit'
    signing_core_hook_input: str = 'textarea[name="core_hook"], [data-testid="signing-core-hook"], #signing-core-hook'
    signing_protagonist_input: str = 'textarea[name="protagonist"], [data-testid="signing-protagonist"], #signing-protagonist'
    signing_golden_finger_input: str = 'textarea[name="golden_finger"], [data-testid="signing-golden-finger"], #signing-golden-finger'
    signing_outline_input: str = 'textarea[name="outline"], [data-testid="signing-outline"], #signing-outline'
    signing_highlights_input: str = 'textarea[name="highlights"], [data-testid="signing-highlights"], #signing-highlights'
    signing_submit_btn: str = 'button[data-testid="signing-submit"], #signing-submit'
    rotate_title_input: str = 'input[name="new_title"], [data-testid="rotate-new-title"], #rotate-new-title'
    rotate_blurb_input: str = 'textarea[name="new_blurb"], [data-testid="rotate-new-blurb"], #rotate-new-blurb'
    rotate_cover_input: str = 'input[name="cover_file"], [data-testid="rotate-cover-input"], #rotate-cover-input'
    rotate_submit_btn: str = 'button[data-testid="rotate-submit"], #rotate-submit'
    metrics_settlement_status: str = '[data-testid="metrics-settlement-status"], #metrics-settlement-status'
    metrics_date: str = '[data-testid="metrics-date"], #metrics-date'
    metric_ch2_read_through: str = '[data-testid="metric-ch2-read-through"], #metric-ch2-read-through'
    metric_word100k_completion: str = '[data-testid="metric-word100k-completion"], #metric-word100k-completion'
    metric_ctr: str = '[data-testid="metric-ctr"], #metric-ctr'
    metric_bookshelf_rate: str = '[data-testid="metric-bookshelf-rate"], #metric-bookshelf-rate'
    metric_impressions: str = '[data-testid="metric-impressions"], #metric-impressions'
    metric_daily_revenue: str = '[data-testid="metric-daily-revenue"], #metric-daily-revenue'
    hotlist_item: str = '[data-testid="hotlist-item"], .hotlist-item'


@dataclass(frozen=True)
class WorkerTaskOutcome:
    task_id: str
    task_type: str
    book_id: str
    account_id: str
    status: str
    receipt: Optional[CompanionReceiptRequest] = None
    metrics: Optional[CompanionMetricsRequest] = None
    captcha_report: Optional[CompanionCaptchaReport] = None
    screenshot_path: Optional[Path] = None
    idempotent_skip: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "book_id": self.book_id,
            "account_id": self.account_id,
            "status": self.status,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "captcha_report": self.captcha_report.to_dict() if self.captcha_report else None,
            "screenshot_path": str(self.screenshot_path) if self.screenshot_path else None,
            "idempotent_skip": self.idempotent_skip,
            "details": dict(self.details),
        }


def resolve_account_user_data_dir(profile_root: str | Path, account_id: str) -> Path:
    """Resolve an isolated root-level per-account user_data_dir under profile_root."""
    cleaned_id = (account_id or "").strip()
    if not cleaned_id or not _SAFE_ACCOUNT_ID_RE.fullmatch(cleaned_id) or cleaned_id in (".", ".."):
        raise ValueError(
            f"account_id must be a safe non-empty identifier (got {account_id!r})"
        )
    root = Path(profile_root).resolve()
    account_dir = (root / cleaned_id).resolve()
    if account_dir.parent != root:
        raise ValueError(
            f"Account user_data_dir '{account_dir}' must be a direct child of '{root}'"
        )
    account_dir.mkdir(parents=True, exist_ok=True)
    return account_dir


class AccountProfileLock:
    """Exclusive intra-process and inter-process OS file lock (.companion.lock) per account dir."""

    def __init__(self, user_data_dir: Path, account_id: str) -> None:
        self.user_data_dir = Path(user_data_dir).resolve()
        self.account_id = account_id
        self.lock_path = self.user_data_dir / ".companion.lock"
        self._fp: Optional[Any] = None
        self._acquired = False

    def acquire(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        with _ACTIVE_PROFILE_LOCKS_GUARD:
            if self.user_data_dir in _ACTIVE_PROFILE_LOCKS:
                raise ProfileLockError(
                    f"Profile directory '{self.user_data_dir}' for account '{self.account_id}' "
                    f"is already locked in this process"
                )
            _ACTIVE_PROFILE_LOCKS.add(self.user_data_dir)

        try:
            fp = self.lock_path.open("a+", encoding="utf-8")
        except OSError as exc:
            with _ACTIVE_PROFILE_LOCKS_GUARD:
                _ACTIVE_PROFILE_LOCKS.discard(self.user_data_dir)
            raise ProfileLockError(f"Cannot open lock file '{self.lock_path}': {exc}") from exc

        try:
            fp.seek(0)
            if os.name == "nt":
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fp.close()
            with _ACTIVE_PROFILE_LOCKS_GUARD:
                _ACTIVE_PROFILE_LOCKS.discard(self.user_data_dir)
            raise ProfileLockError(
                f"Account profile '{self.account_id}' is already locked by another companion process "
                f"('{self.lock_path}'): {exc}"
            ) from exc

        try:
            fp.seek(0)
            fp.truncate(0)
            fp.write(
                json.dumps(
                    {
                        "account_id": self.account_id,
                        "pid": os.getpid(),
                        "locked_at": datetime.now(SHANGHAI_TZ).isoformat(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fp.flush()
        except OSError as exc:
            fp.seek(0)
            if os.name == "nt":
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            fp.close()
            with _ACTIVE_PROFILE_LOCKS_GUARD:
                _ACTIVE_PROFILE_LOCKS.discard(self.user_data_dir)
            raise ProfileLockError(
                f"Failed writing lock metadata to '{self.lock_path}': {exc}"
            ) from exc

        self._fp = fp
        self._acquired = True

    def release(self) -> None:
        if self._fp is not None:
            try:
                self._fp.seek(0)
                if os.name == "nt":
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
            finally:
                self._fp.close()
                self._fp = None
        if self._acquired:
            with _ACTIVE_PROFILE_LOCKS_GUARD:
                _ACTIVE_PROFILE_LOCKS.discard(self.user_data_dir)
            self._acquired = False

    def __enter__(self) -> AccountProfileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class CaptchaLoginSentinel:
    """3-Layer Read-Only Passive Captcha & Login Sentinel (Zero-Touch Iron Rule).

    Strictly read-only inspection only: URL redirects, HTTP 401/403 responses, and visible
    captcha/verification DOM nodes. Never clicks, drags, or alters browser fingerprint.
    """

    def __init__(self) -> None:
        self.auth_http_errors: List[Tuple[int, str]] = []

    def attach(self, page: Page) -> None:
        self.auth_http_errors.clear()

        def _on_response(resp: Response) -> None:
            if resp.status not in (401, 403):
                return
            try:
                if _is_first_party_auth_response(page, resp):
                    self.auth_http_errors.append((int(resp.status), str(resp.url)))
            except Exception as exc:  # never let the passive hook crash the page event loop
                logger.debug("Sentinel response hook ignored error: %s", exc)

        page.on("response", _on_response)

    def check_passive_signals(self, page: Page) -> Optional[str]:
        """Inspect Layer 1 (URL), Layer 2 (HTTP 401/403), and Layer 3 (DOM) read-only."""
        if self.auth_http_errors:
            status_code, err_url = self.auth_http_errors[-1]
            return f"AUTH_HTTP_{status_code}:{err_url}"

        raw_url = page.url or ""
        parsed_url = urllib.parse.urlparse(raw_url)
        if _LOGIN_URL_HOST_RE.search(parsed_url.netloc or "") or _LOGIN_URL_PATH_RE.search(
            parsed_url.path or ""
        ):
            return f"LOGIN_OR_CAPTCHA_REDIRECT:{raw_url}"

        dom_hit = page.evaluate(
            """({selectors, markers, excluded}) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (!st || st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) {
                        return false;
                    }
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                for (const sel of selectors) {
                    const nodes = document.querySelectorAll(sel);
                    for (const node of nodes) {
                        if (isVisible(node)) {
                            return `DOM_SELECTOR:${sel}`;
                        }
                    }
                }
                // Walk visible text nodes only, skipping editor/form content authored by the user.
                const chunks = [];
                if (document.body) {
                    const visCache = new Map();
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
                        acceptNode: (node) => {
                            const parent = node.parentElement;
                            if (!parent) return NodeFilter.FILTER_REJECT;
                            if (parent.closest(excluded)) return NodeFilter.FILTER_REJECT;
                            let vis = visCache.get(parent);
                            if (vis === undefined) {
                                vis = isVisible(parent);
                                visCache.set(parent, vis);
                            }
                            return vis ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                        },
                    });
                    let n;
                    while ((n = walker.nextNode())) {
                        chunks.push(n.nodeValue || '');
                    }
                }
                const bodyText = chunks.join('');
                for (const marker of markers) {
                    if (bodyText.includes(marker)) {
                        return `DOM_TEXT:${marker}`;
                    }
                }
                return null;
            }""",
            {
                "selectors": list(_CAPTCHA_DOM_SELECTORS),
                "markers": list(_CAPTCHA_TEXT_MARKERS),
                "excluded": _CAPTCHA_TEXT_EXCLUDED_SELECTOR,
            },
        )
        if dom_hit:
            return str(dom_hit)
        return None

    def inspect_or_halt(
        self,
        page: Page,
        *,
        account_id: str,
        book_id: str = "",
        chapter: int = 0,
        screenshot_dir: Optional[Path] = None,
        client: Optional[CompanionHttpClient] = None,
    ) -> None:
        reason = self.check_passive_signals(page)
        if reason is None:
            return

        # 0ms immediate abort of all mouse/keyboard actions: capture screenshot & emit pause report.
        ts_label = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d_%H%M%S_%f")
        saved_path: Optional[Path] = None
        if screenshot_dir is not None:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            saved_path = screenshot_dir / f"captcha_{account_id}_{ts_label}.png"
            png_bytes = page.screenshot(path=str(saved_path), type="png")
        else:
            png_bytes = page.screenshot(type="png")

        b64_png = base64.b64encode(png_bytes).decode("ascii")
        report = CompanionCaptchaReport(
            account_id=account_id,
            book_id=book_id,
            chapter=chapter,
            reason=reason,
            page_url=page.url or "",
            screenshot_base64=b64_png,
            screenshot_mime="image/png",
            detected_at=datetime.now(SHANGHAI_TZ).isoformat(),
        )
        if client is not None:
            try:
                client.send_captcha_report(report)
            except Exception as exc:  # reporting must never swallow the halt
                logger.error(
                    "Failed to send captcha report for account '%s' (%s): %s", account_id, reason, exc
                )

        raise CaptchaAuthPauseError(
            f"0ms passive sentinel halt for account '{account_id}' ({reason})",
            report=report,
            screenshot_path=saved_path,
        )


def _registrable_host(hostname: str) -> str:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def _same_site(url_a: str, url_b: str) -> bool:
    host_a = (urllib.parse.urlparse(url_a or "").hostname or "").lower()
    host_b = (urllib.parse.urlparse(url_b or "").hostname or "").lower()
    if not host_a or not host_b:
        return False
    site_b = _registrable_host(host_b)
    return host_a == site_b or host_a.endswith("." + site_b) or host_a == host_b


def _is_first_party_auth_response(page: Page, resp: Response) -> bool:
    """Only first-party document/xhr/fetch 401/403s count (ignore third-party analytics/ads)."""
    resource_type = str(resp.request.resource_type or "")
    if resource_type not in _AUTH_HTTP_RESOURCE_TYPES:
        return False
    if resource_type == "document" and resp.frame == page.main_frame:
        # Top-level navigation response: page.url may still point at the previous page.
        return True
    return _same_site(str(resp.url), page.url or "")


def _parse_shanghai_schedule(raw_value: str) -> Optional[datetime]:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", cleaned):
        return datetime.fromisoformat(cleaned.replace(" ", "T")).replace(tzinfo=SHANGHAI_TZ)
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        raise ValueError(
            f"scheduled_publish_at '{raw_value}' must carry explicit timezone or YYYY-MM-DD HH:MM CST"
        )
    return dt.astimezone(SHANGHAI_TZ)


def _detect_image_kind(head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def _format_shanghai_schedule(raw_value: str) -> str:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", cleaned):
        dt = datetime.fromisoformat(cleaned.replace(" ", "T")).replace(tzinfo=SHANGHAI_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        raise ValueError(
            f"scheduled_publish_at '{raw_value}' must carry explicit timezone or YYYY-MM-DD HH:MM CST"
        )
    return dt.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")


def _parse_ratio_text(raw_text: str, field_name: str, *, required: bool) -> Optional[float]:
    cleaned = (raw_text or "").strip()
    if cleaned.upper() in _UNSETTLED_TOKENS:
        if required:
            raise MetricsNotSettledError(
                f"Metric '{field_name}' is unsettled ({cleaned!r}); refusing to coerce to 0.0"
            )
        return None
    is_pct = cleaned.endswith("%")
    num_str = cleaned[:-1].strip() if is_pct else cleaned
    try:
        val = float(num_str)
    except ValueError as exc:
        raise MetricsNotSettledError(
            f"Metric '{field_name}' has non-numeric value {cleaned!r}"
        ) from exc
    if is_pct:
        val = val / 100.0
    if val < 0.0 or val > 1.0:
        raise ValueError(f"Metric '{field_name}' ratio out of [0, 1] range: {val}")
    return val


def _parse_int_text(raw_text: str, field_name: str, *, required: bool) -> Optional[int]:
    cleaned = (raw_text or "").strip().replace(",", "")
    if cleaned.upper() in _UNSETTLED_TOKENS:
        if required:
            raise MetricsNotSettledError(
                f"Metric '{field_name}' is unsettled ({cleaned!r}); refusing to coerce to 0"
            )
        return None
    try:
        val = int(float(cleaned))
    except ValueError as exc:
        raise MetricsNotSettledError(
            f"Metric '{field_name}' has non-integer value {cleaned!r}"
        ) from exc
    if val < 0:
        raise ValueError(f"Metric '{field_name}' must be >= 0, got {val}")
    return val


def _parse_float_text(raw_text: str, field_name: str, *, required: bool) -> Optional[float]:
    cleaned = (raw_text or "").strip().replace(",", "").lstrip("¥￥")
    if cleaned.upper() in _UNSETTLED_TOKENS:
        if required:
            raise MetricsNotSettledError(
                f"Metric '{field_name}' is unsettled ({cleaned!r}); refusing to coerce to 0.0"
            )
        return None
    try:
        val = float(cleaned)
    except ValueError as exc:
        raise MetricsNotSettledError(
            f"Metric '{field_name}' has non-numeric value {cleaned!r}"
        ) from exc
    if val < 0:
        raise ValueError(f"Metric '{field_name}' must be >= 0, got {val}")
    return val


class TomatoBrowserWorker:
    """Multi-account physically isolated Playwright worker for Tomato Writer automation."""

    def __init__(
        self,
        account_id: str,
        launch_config: BrowserLaunchConfig,
        *,
        selectors: Optional[TomatoDomSelectors] = None,
        client: Optional[CompanionHttpClient] = None,
        font_decoder: Optional[Woff2FontDecoder] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.account_id = account_id
        self.launch_config = launch_config
        self.selectors = selectors or TomatoDomSelectors()
        self.client = client
        self.font_decoder = font_decoder or Woff2FontDecoder()
        self.now_provider = now_provider or (lambda: datetime.now(SHANGHAI_TZ))
        self.user_data_dir = resolve_account_user_data_dir(launch_config.profile_root, account_id)
        self.screenshot_dir = (
            Path(launch_config.screenshot_dir).resolve()
            if launch_config.screenshot_dir is not None
            else (self.user_data_dir / "screenshots")
        )
        self._lock = AccountProfileLock(self.user_data_dir, account_id)
        self._sentinel = CaptchaLoginSentinel()
        self._playwright: Any = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    def open(self) -> TomatoBrowserWorker:
        self._lock.acquire()
        try:
            self._playwright = sync_playwright().start()
            launch_kwargs: Dict[str, Any] = {
                "user_data_dir": str(self.user_data_dir),
                "headless": bool(self.launch_config.headless),
                "slow_mo": int(self.launch_config.slow_mo_ms),
                "viewport": {"width": 1366, "height": 860},
            }
            if self.launch_config.channel is not None:
                launch_kwargs["channel"] = self.launch_config.channel
            self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
            self._context.set_default_timeout(int(self.launch_config.timeout_ms))
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
            self._sentinel.attach(self._page)
            return self
        except Exception as exc:
            self.close()
            if isinstance(exc, CompanionWorkerError):
                raise
            raise CompanionWorkerError(
                f"Failed to launch persistent browser for account '{self.account_id}' "
                f"(channel={self.launch_config.channel!r}, headless={self.launch_config.headless}): {exc}"
            ) from exc

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            self._page = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                finally:
                    self._playwright = None
            self._lock.release()

    def __enter__(self) -> TomatoBrowserWorker:
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise CompanionWorkerError("Browser worker is not open; call open() or use 'with' context")
        return self._page

    def _navigate_and_guard(
        self,
        url: str,
        *,
        book_id: str = "",
        chapter: int = 0,
    ) -> None:
        page = self.page
        self._sentinel.auth_http_errors.clear()
        page.goto(url, wait_until="domcontentloaded")
        self._sentinel.inspect_or_halt(
            page,
            account_id=self.account_id,
            book_id=book_id,
            chapter=chapter,
            screenshot_dir=self.screenshot_dir,
            client=self.client,
        )

    def _read_text_first(self, selector: str) -> str:
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return ""
        return (loc.inner_text() or "").strip()

    def _read_attr_or_text(self, selector: str, attr_name: str) -> str:
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return ""
        attr_val = loc.get_attribute(attr_name)
        if attr_val is not None and attr_val.strip():
            return attr_val.strip()
        return (loc.inner_text() or "").strip()

    def _verify_identity_and_book(self, task: CompanionLeaseResponse) -> None:
        """Publish Gate 1: strictly verify author_name + book_id + book_title verbatim."""
        page_author = self._read_text_first(self.selectors.author_name)
        page_book_id = self._read_attr_or_text(self.selectors.book_id, "data-book-id")
        page_book_title = self._read_text_first(self.selectors.book_title)

        expected_author = (task.author_name or "").strip()
        expected_title = (task.book_title or "").strip()
        expected_book_id = (task.fanqie_book_id or task.book_id or "").strip()

        if expected_author and page_author != expected_author:
            raise AccountBookMismatchError(
                f"Pen-name mismatch on page: expected {expected_author!r}, got {page_author!r}"
            )
        if expected_book_id and page_book_id != expected_book_id:
            raise AccountBookMismatchError(
                f"Book ID mismatch on page: expected {expected_book_id!r}, got {page_book_id!r}"
            )
        if expected_title and page_book_title != expected_title:
            raise AccountBookMismatchError(
                f"Book title mismatch on page: expected {expected_title!r}, got {page_book_title!r}"
            )

    def _reconcile_remote_chapters(
        self,
        task: CompanionLeaseResponse,
    ) -> Optional[CompanionReceiptRequest]:
        """Publish Gate 2: read-only inspect remote draft box & recent 20 published/scheduled chapters."""
        items = self.page.evaluate(
            """(selector) => {
                const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 20);
                return nodes.map((el) => ({
                    chapter: el.getAttribute('data-chapter') || '',
                    status: el.getAttribute('data-status') || '',
                    remoteId: el.getAttribute('data-remote-chapter-id') || el.getAttribute('data-chapter-id') || '',
                    scheduledAt: el.getAttribute('data-scheduled-at') || '',
                    publishedAt: el.getAttribute('data-published-at') || '',
                    text: (el.innerText || '').trim(),
                }));
            }""",
            self.selectors.chapter_list_item,
        )

        target_ch = int(task.chapter)
        for item in items:
            ch_no: Optional[int] = None
            raw_ch = str(item.get("chapter") or "").strip()
            if raw_ch.isdigit():
                ch_no = int(raw_ch)
            else:
                m = _CHAPTER_NO_TEXT_RE.search(str(item.get("text") or ""))
                if m:
                    ch_no = int(m.group(1))
            if ch_no != target_ch:
                continue

            raw_status = str(item.get("status") or "").strip().upper()
            if raw_status in ("PUBLISHED", "已发布"):
                mapped_status = StockpileStatus.PUBLISHED.value
            elif raw_status in ("SCHEDULED", "定时发布", "待发布"):
                mapped_status = StockpileStatus.SCHEDULED.value
            else:
                mapped_status = StockpileStatus.DRAFT_SAVED.value

            expected_chars = len(re.sub(r"\s+", "", task.body))
            return CompanionReceiptRequest(
                task_id=task.task_id,
                book_id=task.book_id,
                chapter=task.chapter,
                account_id=self.account_id,
                status=mapped_status,
                remote_chapter_id=str(item.get("remoteId") or f"remote-ch-{target_ch}"),
                char_count=expected_chars,
                scheduled_at=str(item.get("scheduledAt") or ""),
                published_at=str(item.get("publishedAt") or ""),
                sha256=task.sha256,
            )
        return None

    def _halt_if_flagged(self, *, book_id: str = "", chapter: int = 0) -> None:
        self._sentinel.inspect_or_halt(
            self.page,
            account_id=self.account_id,
            book_id=book_id,
            chapter=chapter,
            screenshot_dir=self.screenshot_dir,
            client=self.client,
        )

    def _guarded_action(
        self,
        selector: str,
        action: Callable[[Any], None],
        *,
        book_id: str = "",
        chapter: int = 0,
    ) -> None:
        """Run inspect_or_halt BEFORE a write action; convert captcha-induced timeouts into halts."""
        self._halt_if_flagged(book_id=book_id, chapter=chapter)
        loc = self.page.locator(selector).first
        try:
            action(loc)
        except PlaywrightTimeoutError:
            # A captcha/login overlay commonly intercepts pointer events -> timeout.
            # inspect_or_halt raises CaptchaAuthPauseError if so; otherwise re-raise the timeout.
            self._halt_if_flagged(book_id=book_id, chapter=chapter)
            raise

    def _guarded_fill(self, selector: str, value: str, *, book_id: str = "", chapter: int = 0) -> None:
        self._guarded_action(selector, lambda loc: loc.fill(value), book_id=book_id, chapter=chapter)

    def _guarded_click(self, selector: str, *, book_id: str = "", chapter: int = 0) -> None:
        self._guarded_action(selector, lambda loc: loc.click(), book_id=book_id, chapter=chapter)

    def _guarded_set_input_files(
        self,
        selector: str,
        files: Any,
        *,
        book_id: str = "",
        chapter: int = 0,
    ) -> None:
        self._guarded_action(
            selector, lambda loc: loc.set_input_files(files), book_id=book_id, chapter=chapter
        )

    def _has_element(self, selector: str) -> bool:
        return self.page.locator(selector).count() > 0

    def _read_attr_only(self, selector: str, attr_name: str) -> str:
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return ""
        attr_val = loc.get_attribute(attr_name)
        return attr_val.strip() if attr_val is not None else ""

    def _insert_prosemirror_paragraphs(self, body: str, *, book_id: str = "", chapter: int = 0) -> None:
        paragraphs = [line.strip() for line in body.splitlines() if line.strip()]
        if not paragraphs:
            raise CompanionWorkerError("Chapter body cannot be empty")
        self._guarded_click(self.selectors.editor_prosemirror, book_id=book_id, chapter=chapter)
        self.page.evaluate(
            """({selector, paragraphs}) => {
                const el = document.querySelector(selector);
                if (!el) throw new Error('ProseMirror editor element not found');
                el.innerHTML = '';
                for (const pText of paragraphs) {
                    const p = document.createElement('p');
                    p.textContent = pText;
                    el.appendChild(p);
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            {"selector": self.selectors.editor_prosemirror, "paragraphs": paragraphs},
        )

    def _read_live_editor_word_count(self) -> int:
        wc_text = self._read_text_first(self.selectors.editor_word_count)
        digits = re.search(r"\d+", wc_text.replace(",", ""))
        if digits:
            return int(digits.group(0))
        editor_text = self._read_text_first(self.selectors.editor_prosemirror)
        return len(re.sub(r"\s+", "", editor_text))

    def _validated_schedule(self, raw_value: str) -> Optional[datetime]:
        """Parse scheduled_publish_at and require it to be >= 5 minutes in the future."""
        try:
            sched = _parse_shanghai_schedule(raw_value)
        except ValueError as exc:
            raise CompanionWorkerError(f"Invalid scheduled_publish_at {raw_value!r}: {exc}") from exc
        if sched is None:
            return None
        now_raw = self.now_provider()
        if now_raw.tzinfo is None:
            raise ValueError("now_provider() must return a timezone-aware datetime")
        lead_sec = (sched - now_raw).total_seconds()
        if lead_sec < _MIN_SCHEDULE_LEAD_SEC:
            raise CompanionWorkerError(
                f"schedule in the past: scheduled_publish_at={raw_value!r} is not at least "
                f"{_MIN_SCHEDULE_LEAD_SEC // 60} minutes after now={now_raw.isoformat()}"
            )
        return sched

    def publish_chapter(
        self,
        task: CompanionLeaseResponse,
        *,
        target_url: str,
    ) -> WorkerTaskOutcome:
        """Execute Publish 3 Iron Gates with 0ms passive captcha sentinel."""
        self._navigate_and_guard(target_url, book_id=task.book_id, chapter=task.chapter)
        self._verify_identity_and_book(task)

        # Gate 2: Remote pre-write reconciliation (idempotent skip if already exists)
        existing_receipt = self._reconcile_remote_chapters(task)
        if existing_receipt is not None:
            if self.client is not None:
                self.client.send_receipt(existing_receipt)
            return WorkerTaskOutcome(
                task_id=task.task_id,
                task_type=task.task_type,
                book_id=task.book_id,
                account_id=self.account_id,
                status=existing_receipt.status,
                receipt=existing_receipt,
                idempotent_skip=True,
                details={"reconciled": True},
            )

        # Pre-flight (before any write): schedule must be valid and all required controls present.
        schedule_dt = self._validated_schedule(task.scheduled_publish_at or "")
        formatted_schedule = schedule_dt.strftime("%Y-%m-%d %H:%M") if schedule_dt is not None else ""
        if not self._has_element(self.selectors.save_draft_btn):
            raise CompanionWorkerError(
                f"Save-draft button not found for ch.{task.chapter}; refusing to report a draft that was never saved"
            )
        if schedule_dt is not None:
            if not self._has_element(self.selectors.schedule_time_input):
                raise CompanionWorkerError(
                    f"Schedule requested ({formatted_schedule}) but schedule input is missing on page"
                )
            if not self._has_element(self.selectors.publish_submit_btn):
                raise CompanionWorkerError(
                    f"Schedule requested ({formatted_schedule}) but publish button is missing on page"
                )

        # Gate 3: Dual-box title stripping + ProseMirror paste + <=5% word count + draft + note + schedule
        clean_title = strip_chapter_title_prefix(task.public_title, chapter_no=task.chapter)
        guard_kw: Dict[str, Any] = {"book_id": task.book_id, "chapter": task.chapter}

        self._guarded_fill(self.selectors.chapter_number_input, str(task.chapter), **guard_kw)
        self._guarded_fill(self.selectors.chapter_title_input, clean_title, **guard_kw)

        self._insert_prosemirror_paragraphs(task.body, **guard_kw)
        if self.launch_config.review_pause_sec > 0:
            time.sleep(self.launch_config.review_pause_sec)

        self._halt_if_flagged(**guard_kw)

        expected_chars = len(re.sub(r"\s+", "", task.body))
        actual_chars = self._read_live_editor_word_count()
        deviation = abs(actual_chars - expected_chars) / max(1, expected_chars)
        if deviation > 0.05:
            raise EditorWordCountMismatchError(
                f"Editor word count mismatch for ch.{task.chapter}: "
                f"expected={expected_chars}, actual={actual_chars}, deviation={deviation:.2%} (> 5%)"
            )

        if not self._has_element(self.selectors.save_draft_btn):
            raise CompanionWorkerError(f"Save-draft button disappeared before saving ch.{task.chapter}")
        self._guarded_click(self.selectors.save_draft_btn, **guard_kw)
        self._halt_if_flagged(**guard_kw)
        last_clicked_selector = self.selectors.save_draft_btn

        if task.author_note and self._has_element(self.selectors.author_note_input):
            self._guarded_fill(self.selectors.author_note_input, task.author_note.strip(), **guard_kw)

        final_status = StockpileStatus.DRAFT_SAVED.value
        if schedule_dt is not None:
            if not self._has_element(self.selectors.schedule_time_input):
                raise CompanionWorkerError(
                    f"Schedule requested ({formatted_schedule}) but schedule input is missing on page"
                )
            self._guarded_fill(self.selectors.schedule_time_input, formatted_schedule, **guard_kw)
            if not self._has_element(self.selectors.publish_submit_btn):
                raise CompanionWorkerError(
                    f"Schedule requested ({formatted_schedule}) but publish button is missing on page"
                )
            # _guarded_click runs inspect_or_halt immediately before the final submit click.
            self._guarded_click(self.selectors.publish_submit_btn, **guard_kw)
            self._halt_if_flagged(**guard_kw)
            last_clicked_selector = self.selectors.publish_submit_btn
            final_status = StockpileStatus.SCHEDULED.value

        remote_id = self._read_attr_only(last_clicked_selector, self.selectors.remote_chapter_id_attr)
        if not remote_id and last_clicked_selector != self.selectors.publish_submit_btn:
            remote_id = self._read_attr_only(
                self.selectors.publish_submit_btn, self.selectors.remote_chapter_id_attr
            )
        remote_id = remote_id or f"tomato-ch-{task.chapter}"
        scheduled_at = formatted_schedule

        # Post-check: best-effort re-read of the remote chapter list to confirm the write landed.
        if self.launch_config.review_pause_sec > 0:
            self.page.wait_for_timeout(int(self.launch_config.review_pause_sec * 1000))
        post_receipt = self._reconcile_remote_chapters(task)
        post_verify = post_receipt is not None
        if post_receipt is not None:
            remote_id = post_receipt.remote_chapter_id or remote_id
            final_status = post_receipt.status or final_status
            scheduled_at = post_receipt.scheduled_at or scheduled_at

        receipt = CompanionReceiptRequest(
            task_id=task.task_id,
            book_id=task.book_id,
            chapter=task.chapter,
            account_id=self.account_id,
            status=final_status,
            remote_chapter_id=remote_id,
            char_count=actual_chars,
            scheduled_at=scheduled_at,
            published_at=post_receipt.published_at if post_receipt is not None else "",
            sha256=task.sha256,
        )
        if self.client is not None:
            self.client.send_receipt(receipt)

        return WorkerTaskOutcome(
            task_id=task.task_id,
            task_type=task.task_type,
            book_id=task.book_id,
            account_id=self.account_id,
            status=final_status,
            receipt=receipt,
            details={
                "clean_title": clean_title,
                "actual_chars": actual_chars,
                "expected_chars": expected_chars,
                "scheduled_at": scheduled_at,
                "post_verify": post_verify,
            },
        )

    def create_star_cards(
        self,
        task: CompanionLeaseResponse,
        *,
        target_url: str,
    ) -> WorkerTaskOutcome:
        """Create character star-cards in Tomato writer backend."""
        self._navigate_and_guard(target_url, book_id=task.book_id, chapter=task.chapter)
        self._verify_identity_and_book(task)

        cards = list(task.metadata.get("star_cards", []))
        if not cards:
            raise CompanionWorkerError("CREATE_STAR_CARDS task requires metadata['star_cards']")

        guard_kw: Dict[str, Any] = {"book_id": task.book_id}
        created_names: List[str] = []
        for card in cards:
            self._halt_if_flagged(**guard_kw)
            if not self._has_element(self.selectors.star_card_submit_btn):
                raise CompanionWorkerError("Star-card submit button not found on page")
            self._guarded_fill(self.selectors.star_card_name_input, str(card["name"]), **guard_kw)
            self._guarded_fill(self.selectors.star_card_role_input, str(card.get("role", "")), **guard_kw)
            self._guarded_fill(
                self.selectors.star_card_tagline_input, str(card.get("tagline", "")), **guard_kw
            )
            self._guarded_fill(
                self.selectors.star_card_personality_input,
                str(card.get("personality", "")),
                **guard_kw,
            )
            self._guarded_click(self.selectors.star_card_submit_btn, **guard_kw)
            self._halt_if_flagged(**guard_kw)
            created_names.append(str(card["name"]))

        receipt = CompanionReceiptRequest(
            task_id=task.task_id,
            book_id=task.book_id,
            chapter=task.chapter,
            account_id=self.account_id,
            status=StockpileStatus.PUBLISHED.value,
            remote_chapter_id=f"star-cards-{len(created_names)}",
        )
        if self.client is not None:
            self.client.send_receipt(receipt)

        return WorkerTaskOutcome(
            task_id=task.task_id,
            task_type=task.task_type,
            book_id=task.book_id,
            account_id=self.account_id,
            status=StockpileStatus.PUBLISHED.value,
            receipt=receipt,
            details={"created_star_cards": created_names},
        )

    def submit_signing_draft(
        self,
        task: CompanionLeaseResponse,
        *,
        target_url: str,
    ) -> WorkerTaskOutcome:
        """Fill and submit the 20,000-word Lv2+ Tomato editor signing application draft."""
        self._navigate_and_guard(target_url, book_id=task.book_id, chapter=task.chapter)
        self._verify_identity_and_book(task)

        app_data = dict(task.metadata.get("signing_application", {}))
        if not app_data:
            raise CompanionWorkerError(
                "SUBMIT_SIGNING_DRAFT task requires metadata['signing_application']"
            )
        if not self._has_element(self.selectors.signing_submit_btn):
            raise CompanionWorkerError("Signing submit button not found on page")

        guard_kw: Dict[str, Any] = {"book_id": task.book_id}
        self._guarded_fill(
            self.selectors.signing_core_hook_input, str(app_data.get("core_hook", "")), **guard_kw
        )
        self._guarded_fill(
            self.selectors.signing_protagonist_input, str(app_data.get("protagonist", "")), **guard_kw
        )
        self._guarded_fill(
            self.selectors.signing_golden_finger_input, str(app_data.get("golden_finger", "")), **guard_kw
        )
        self._guarded_fill(
            self.selectors.signing_outline_input, str(app_data.get("outline", "")), **guard_kw
        )
        self._guarded_fill(
            self.selectors.signing_highlights_input, str(app_data.get("highlights", "")), **guard_kw
        )
        self._halt_if_flagged(**guard_kw)
        # _guarded_click runs inspect_or_halt immediately before the final submit click.
        self._guarded_click(self.selectors.signing_submit_btn, **guard_kw)
        self._halt_if_flagged(**guard_kw)

        receipt = CompanionReceiptRequest(
            task_id=task.task_id,
            book_id=task.book_id,
            chapter=task.chapter,
            account_id=self.account_id,
            status=StockpileStatus.DRAFT_SAVED.value,
            remote_chapter_id="signing-draft",
        )
        if self.client is not None:
            self.client.send_receipt(receipt)

        return WorkerTaskOutcome(
            task_id=task.task_id,
            task_type=task.task_type,
            book_id=task.book_id,
            account_id=self.account_id,
            status=StockpileStatus.DRAFT_SAVED.value,
            receipt=receipt,
            details={"signing_submitted": True},
        )

    def _validate_cover_path(self, raw_path: str) -> Path:
        """Confine cover uploads to launch_config.cover_root and verify a real PNG/JPEG/WEBP <= 5MB."""
        cover_root_cfg = self.launch_config.cover_root
        if cover_root_cfg is None or not str(cover_root_cfg).strip():
            raise CompanionWorkerError("cover upload disabled: no cover_root configured")
        try:
            cover_root = Path(cover_root_cfg).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CompanionWorkerError(f"cover_root '{cover_root_cfg}' is not accessible: {exc}") from exc
        if not cover_root.is_dir():
            raise CompanionWorkerError(f"cover_root '{cover_root}' is not a directory")

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = cover_root / candidate
        if candidate.is_symlink():
            raise CompanionWorkerError(f"Cover path '{raw_path}' must not be a symlink")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CompanionWorkerError(f"Cover path '{raw_path}' does not exist: {exc}") from exc
        if not resolved.is_relative_to(cover_root):
            raise CompanionWorkerError(
                f"Cover path '{raw_path}' resolves outside cover_root '{cover_root}'"
            )
        if resolved.is_symlink() or not resolved.is_file():
            raise CompanionWorkerError(f"Cover path '{raw_path}' is not a regular file")
        suffix = resolved.suffix.lower()
        if suffix not in _COVER_ALLOWED_SUFFIXES:
            raise CompanionWorkerError(
                f"Cover path '{raw_path}' has disallowed extension {suffix!r} "
                f"(allowed: {sorted(_COVER_ALLOWED_SUFFIXES)})"
            )
        size = resolved.stat().st_size
        if size <= 0 or size > _COVER_MAX_BYTES:
            raise CompanionWorkerError(
                f"Cover path '{raw_path}' size {size} bytes outside (0, {_COVER_MAX_BYTES}]"
            )
        with resolved.open("rb") as fh:
            head = fh.read(16)
        kind = _detect_image_kind(head)
        expected_kind = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}[suffix]
        if kind != expected_kind:
            raise CompanionWorkerError(
                f"Cover path '{raw_path}' content does not match a {expected_kind.upper()} image "
                f"(detected {kind or 'unknown'})"
            )
        return resolved

    def rotate_packaging(
        self,
        task: CompanionLeaseResponse,
        *,
        target_url: str,
    ) -> WorkerTaskOutcome:
        """Submit cold-start alternate title / blurb / cover packaging rotation request."""
        new_title = str(task.metadata.get("new_title") or task.public_title).strip()
        if not (1 <= len(new_title) <= 15) or "《" in new_title or "》" in new_title:
            raise CompanionWorkerError(
                f"Alternate title for ROTATE_PACKAGING must be 1..15 chars without 《》, got {new_title!r}"
            )
        new_blurb = str(task.metadata.get("new_blurb") or "").strip()
        cover_path = str(task.metadata.get("cover_path") or "").strip()
        # Validate the local cover file before touching the browser (no arbitrary local file upload).
        validated_cover: Optional[Path] = self._validate_cover_path(cover_path) if cover_path else None

        self._navigate_and_guard(target_url, book_id=task.book_id, chapter=task.chapter)
        self._verify_identity_and_book(task)

        if not self._has_element(self.selectors.rotate_submit_btn):
            raise CompanionWorkerError("Packaging rotation submit button not found on page")
        if validated_cover is not None and not self._has_element(self.selectors.rotate_cover_input):
            raise CompanionWorkerError("Cover upload requested but cover file input is missing on page")

        guard_kw: Dict[str, Any] = {"book_id": task.book_id}
        self._guarded_fill(self.selectors.rotate_title_input, new_title, **guard_kw)
        blurb_filled = False
        if new_blurb and self._has_element(self.selectors.rotate_blurb_input):
            self._guarded_fill(self.selectors.rotate_blurb_input, new_blurb, **guard_kw)
            blurb_filled = True
        cover_uploaded = False
        if validated_cover is not None:
            self._guarded_set_input_files(
                self.selectors.rotate_cover_input, str(validated_cover), **guard_kw
            )
            cover_uploaded = True

        self._halt_if_flagged(**guard_kw)
        # _guarded_click runs inspect_or_halt immediately before the final submit click.
        self._guarded_click(self.selectors.rotate_submit_btn, **guard_kw)
        self._halt_if_flagged(**guard_kw)

        receipt = CompanionReceiptRequest(
            task_id=task.task_id,
            book_id=task.book_id,
            chapter=task.chapter,
            account_id=self.account_id,
            status=StockpileStatus.PUBLISHED.value,
            remote_chapter_id=f"rotate:{new_title}",
        )
        if self.client is not None:
            self.client.send_receipt(receipt)

        return WorkerTaskOutcome(
            task_id=task.task_id,
            task_type=task.task_type,
            book_id=task.book_id,
            account_id=self.account_id,
            status=StockpileStatus.PUBLISHED.value,
            receipt=receipt,
            details={
                "new_title": new_title,
                "blurb_filled": blurb_filled,
                "cover_uploaded": cover_uploaded,
            },
        )

    def _fetch_woff2_bytes_from_url(self, woff2_url: str) -> bytes:
        page_url = self.page.url or ""
        full_url = urllib.parse.urljoin(page_url, woff2_url)
        parsed = urllib.parse.urlparse(full_url)
        page_parsed = urllib.parse.urlparse(page_url)
        same_origin = (
            parsed.scheme in ("http", "https")
            and parsed.scheme == page_parsed.scheme
            and (parsed.netloc or "").lower() == (page_parsed.netloc or "").lower()
        )
        if parsed.scheme != "https" and not same_origin:
            raise CompanionWorkerError(
                f"Refusing to fetch hotlist WOFF2 from non-https, cross-origin URL {full_url!r}"
            )
        with urllib.request.urlopen(full_url, timeout=15.0) as resp:
            data = bytes(resp.read(_WOFF2_MAX_BYTES + 1))
        if len(data) > _WOFF2_MAX_BYTES:
            raise CompanionWorkerError(
                f"Hotlist WOFF2 at {full_url!r} exceeds {_WOFF2_MAX_BYTES} byte cap"
            )
        return data

    def collect_metrics(
        self,
        task: CompanionLeaseResponse,
        *,
        target_url: str,
        hotlist_woff2_bytes: Optional[bytes] = None,
    ) -> WorkerTaskOutcome:
        """Collect T-1 retention metrics & decoded hotlist after 12:30 CST settlement guard."""
        now_raw = self.now_provider()
        if now_raw.tzinfo is None:
            raise ValueError("now_provider() must return a timezone-aware datetime")
        now_cst = now_raw.astimezone(SHANGHAI_TZ)
        if (now_cst.hour, now_cst.minute) < (12, 30):
            raise MetricsSettlementWindowError(
                f"Refusing T-1 metrics collection before 12:30 CST (Asia/Shanghai); "
                f"current time is {now_cst.strftime('%Y-%m-%d %H:%M:%S %z')}"
            )

        self._navigate_and_guard(target_url, book_id=task.book_id, chapter=task.chapter)
        self._verify_identity_and_book(task)

        raw_settlement = self._read_text_first(self.selectors.metrics_settlement_status)
        if raw_settlement.strip().upper() in _UNSETTLED_TOKENS:
            raise MetricsNotSettledError(
                f"Metrics settlement_status for book '{task.book_id}' is unsettled ({raw_settlement!r})"
            )

        ch2 = _parse_ratio_text(
            self._read_text_first(self.selectors.metric_ch2_read_through),
            "ch2_read_through",
            required=True,
        )
        bookshelf = _parse_ratio_text(
            self._read_text_first(self.selectors.metric_bookshelf_rate),
            "bookshelf_rate",
            required=True,
        )
        word100k = _parse_ratio_text(
            self._read_text_first(self.selectors.metric_word100k_completion),
            "word100k_completion",
            required=False,
        )
        ctr = _parse_ratio_text(
            self._read_text_first(self.selectors.metric_ctr),
            "click_through_rate",
            required=False,
        )
        impressions = _parse_int_text(
            self._read_text_first(self.selectors.metric_impressions),
            "impressions",
            required=False,
        )
        daily_rev = _parse_float_text(
            self._read_text_first(self.selectors.metric_daily_revenue),
            "daily_revenue_cny",
            required=False,
        )
        metrics_date = self._read_text_first(self.selectors.metrics_date) or now_cst.strftime("%Y-%m-%d")

        hotlist_raw = self.page.evaluate(
            """(selector) => {
                const nodes = Array.from(document.querySelectorAll(selector));
                return nodes.map((el) => ({
                    rank: (el.getAttribute('data-rank') || (el.querySelector('.rank')?.innerText || '')).trim(),
                    title: (el.getAttribute('data-title') || (el.querySelector('.title')?.innerText || '')).trim(),
                    author: (el.getAttribute('data-author') || (el.querySelector('.author')?.innerText || '')).trim(),
                    heat: (el.getAttribute('data-heat') || (el.querySelector('.heat')?.innerText || '')).trim(),
                    woff2_url: el.getAttribute('data-woff2-url') || '',
                }));
            }""",
            self.selectors.hotlist_item,
        )

        decoded_hotlist: List[Dict[str, Any]] = []
        if hotlist_raw:
            font_bytes = hotlist_woff2_bytes
            if font_bytes is None:
                woff_url = next((str(x.get("woff2_url") or "") for x in hotlist_raw if x.get("woff2_url")), "")
                if not woff_url:
                    style_url = self.page.evaluate(
                        """() => {
                            const st = document.querySelector('[data-woff2-url]');
                            return st ? (st.getAttribute('data-woff2-url') || '') : '';
                        }"""
                    )
                    woff_url = str(style_url or "")
                if woff_url:
                    font_bytes = self._fetch_woff2_bytes_from_url(woff_url)

            if font_bytes is not None:
                decoded_hotlist = self.font_decoder.decode_hotlist_entries(
                    hotlist_raw,
                    font_bytes,
                    text_fields=("rank", "title", "author", "heat"),
                )
            else:
                decoded_hotlist = list(hotlist_raw)

        metrics_req = CompanionMetricsRequest(
            book_id=task.book_id,
            account_id=self.account_id,
            settled=True,
            settlement_status="SETTLED",
            collected_at=now_cst.isoformat(),
            metrics_date=metrics_date,
            ch2_read_through=ch2,
            word100k_read_through=word100k,
            click_through_rate=ctr,
            bookshelf_rate=bookshelf,
            impressions=impressions,
            daily_revenue_cny=daily_rev,
            decoded_hotlist=decoded_hotlist,
        )
        if self.client is not None:
            self.client.send_metrics(metrics_req)

        return WorkerTaskOutcome(
            task_id=task.task_id,
            task_type=task.task_type,
            book_id=task.book_id,
            account_id=self.account_id,
            status=StockpileStatus.PUBLISHED.value,
            metrics=metrics_req,
            details={"decoded_hotlist_count": len(decoded_hotlist)},
        )

    def execute_task(
        self,
        task: CompanionLeaseResponse,
        *,
        target_url: str,
        hotlist_woff2_bytes: Optional[bytes] = None,
        wait_for_resume: Optional[Callable[[CompanionCaptchaReport], bool]] = None,
    ) -> WorkerTaskOutcome:
        """Dispatch a leased CompanionTaskType task, honoring 0ms captcha halt and mismatch gates."""
        task_type = CompanionTaskType(task.task_type)

        while True:
            try:
                if task_type == CompanionTaskType.PUBLISH_CHAPTER:
                    return self.publish_chapter(task, target_url=target_url)
                if task_type == CompanionTaskType.COLLECT_METRICS:
                    return self.collect_metrics(
                        task,
                        target_url=target_url,
                        hotlist_woff2_bytes=hotlist_woff2_bytes,
                    )
                if task_type == CompanionTaskType.CREATE_STAR_CARDS:
                    return self.create_star_cards(task, target_url=target_url)
                if task_type == CompanionTaskType.SUBMIT_SIGNING_DRAFT:
                    return self.submit_signing_draft(task, target_url=target_url)
                if task_type == CompanionTaskType.ROTATE_PACKAGING:
                    return self.rotate_packaging(task, target_url=target_url)
                raise CompanionWorkerError(f"Unsupported CompanionTaskType: {task.task_type!r}")
            except CaptchaAuthPauseError as exc:
                if wait_for_resume is not None and wait_for_resume(exc.report):
                    if self.client is not None:
                        self.client.notify_resume(
                            CompanionResumeRequest(
                                account_id=self.account_id,
                                book_id=task.book_id,
                                note="Resumed after manual operator verification",
                            )
                        )
                    continue
                if self.client is not None and task.task_id:
                    self.client.send_receipt(
                        CompanionReceiptRequest(
                            task_id=task.task_id,
                            book_id=task.book_id,
                            chapter=task.chapter,
                            account_id=self.account_id,
                            status=StockpileStatus.PAUSED_AUTH_CAPTCHA.value,
                            error_code="PAUSED_AUTH_CAPTCHA",
                            error_message=str(exc),
                            sha256=task.sha256,
                        )
                    )
                return WorkerTaskOutcome(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    book_id=task.book_id,
                    account_id=self.account_id,
                    status=StockpileStatus.PAUSED_AUTH_CAPTCHA.value,
                    captcha_report=exc.report,
                    screenshot_path=exc.screenshot_path,
                )
            except AccountBookMismatchError as exc:
                receipt = CompanionReceiptRequest(
                    task_id=task.task_id,
                    book_id=task.book_id,
                    chapter=task.chapter,
                    account_id=self.account_id,
                    status=StockpileStatus.FAILED_HARD_MISMATCH.value,
                    error_code="ACCOUNT_BOOK_MISMATCH",
                    error_message=str(exc),
                    sha256=task.sha256,
                )
                if self.client is not None and task.task_id:
                    self.client.send_receipt(receipt)
                raise


__all__ = [
    "AccountBookMismatchError",
    "AccountProfileLock",
    "BrowserLaunchConfig",
    "CaptchaAuthPauseError",
    "CaptchaLoginSentinel",
    "CompanionWorkerError",
    "EditorWordCountMismatchError",
    "MetricsSettlementWindowError",
    "ProfileLockError",
    "TomatoBrowserWorker",
    "TomatoDomSelectors",
    "WorkerTaskOutcome",
    "resolve_account_user_data_dir",
]
