from __future__ import annotations

from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy_default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import mimetypes
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
import urllib.parse
import zipfile

from ..contracts import (
    ALL_API_ROUTES,
    API_BOOKS,
    API_BOOKS_AUTO_CREATE,
    API_COMPANION_CAPTCHA,
    API_COMPANION_LEASE,
    API_COMPANION_METRICS,
    API_COMPANION_POLL,
    API_COMPANION_RECEIPT,
    API_COMPANION_RESUME,
    API_CPA_AUTH_FILES,
    API_CPA_BINDINGS,
    API_CPA_MODELS,
    API_CPA_OAUTH,
    API_CPA_USAGE,
    API_EVENTS_STREAM,
    API_GENRES,
    API_HEALTH,
    API_MODEL_QUOTAS,
    API_MODEL_SWITCHES,
    API_PRODUCTION_KPI,
    API_PUBLIC_BOOKS,
    API_PUBLIC_EVENTS_STREAM,
    API_PUBLIC_PIPELINE,
    API_WIZARD_SESSIONS,
    AutoCreateBookRequest,
    BookRunStatus,
    ChapterLockRequest,
    CompanionCaptchaReport,
    CompanionLeaseRequest,
    CompanionMetricsRequest,
    CompanionPollRequest,
    CompanionReceiptRequest,
    CompanionResumeRequest,
    CompanionTaskType,
    CutBookRequest,
    ErrorPayload,
    HealthResponse,
    SSEEventPayload,
    SSE_HEARTBEAT,
    SSE_METRICS_COLLECTED,
    SSE_RACING_DECISION,
    StockpileStatus,
    format_sse_frame,
)
from ..cover import CoverError, ImageProviderNotConfiguredError
from ..evolution import EvolutionError, PublishedChapterImmutableError
from ..exporter import ExportError
from ..genres import UnknownGenreError, list_genres
from ..llm.base import CPAManagementError, LLMConfigurationError, LLMRequestError, QuotaExhaustedError
from ..llm.cpa_manager import CPAManagementClient
from ..llm.router import RoleModelRouter
from ..llm.workbuddy_policy import verify_router_models_against_cpa
from ..models import ProjectValidationError, SHANGHAI_TZ
from .auth import (
    AdminAuthManager,
    AdminUnauthorizedError,
    AuthLockoutError,
    CompanionAuthManager,
    CompanionUnauthorizedError,
    is_loopback_host,
    resolve_companion_secret,
    validate_bind_security,
    validate_companion_secret,
)
from .matrix_scheduler import MatrixScheduler, SchedulerConfigurationError
from .public_view import get_public_book, get_public_chapter, list_public_books, public_event
from .wizard import NewBookWizard, WizardConflictError, WizardError
from .stockpile_db import (
    BookNotFoundError,
    ChapterNotFoundError,
    CutConfirmationConflictError,
    DailyQuotaExceededError,
    InvalidStatusTransitionError,
    SequentialLeaseLockError,
    StockpileDB,
    shanghai_now,
)

logger = logging.getLogger(__name__)

DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
MAX_POLL_TIMEOUT_SECONDS = 25

_RE_BOOK_STATUS = re.compile(r"^/api/books/(?P<book_id>[^/]+)/status$")
_RE_BOOK_CHAPTERS = re.compile(r"^/api/books/(?P<book_id>[^/]+)/chapters$")
_RE_BOOK_CHAPTER_DETAIL = re.compile(r"^/api/books/(?P<book_id>[^/]+)/chapters/(?P<chapter>\d+)$")
_RE_BOOK_CHAPTER_LOCK = re.compile(r"^/api/books/(?P<book_id>[^/]+)/chapters/(?P<chapter>\d+)/lock$")
_RE_BOOK_EXPORT = re.compile(r"^/api/books/(?P<book_id>[^/]+)/export$")
_RE_BOOK_COVER = re.compile(r"^/api/books/(?P<book_id>[^/]+)/cover$")
_RE_BOOK_CUT = re.compile(r"^/api/books/(?P<book_id>[^/]+)/cut$")
_RE_BOOK_PROMOTE = re.compile(r"^/api/books/(?P<book_id>[^/]+)/promote$")
_RE_BOOK_EXTEND = re.compile(r"^/api/books/(?P<book_id>[^/]+)/extend$")
_RE_BOOK_RESUME = re.compile(r"^/api/books/(?P<book_id>[^/]+)/resume$")
_RE_CPA_AUTH_FILE_DETAIL = re.compile(r"^/api/cpa/auth-files/(?P<name>[^/]+)$")
_RE_PUBLIC_BOOK = re.compile(r"^/api/public/books/(?P<book_id>[^/]+)$")
_RE_PUBLIC_CHAPTER = re.compile(r"^/api/public/books/(?P<book_id>[^/]+)/chapters/(?P<chapter>\d+)$")
_RE_WIZARD_SESSION = re.compile(r"^/api/wizard/sessions/(?P<session_id>[0-9a-f]{32})$")
_RE_WIZARD_ACTION = re.compile(r"^/api/wizard/sessions/(?P<session_id>[0-9a-f]{32})/(?P<action>answer|confirm)$")

# Static page routing: "/" is the public studio, "/admin/" the owner console.
_STATIC_PAGE_ROUTES = {
    "/": "public.html",
    "/admin": "index.html",
    "/admin/": "index.html",
    "/admin/wizard": "wizard.html",
}


def _extract_multipart_auth_file(content_type: str, body: bytes) -> Tuple[str, bytes]:
    raw_msg = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + body
    )
    msg = BytesParser(policy=email_policy_default).parsebytes(raw_msg)
    if not msg.is_multipart():
        raise ValueError("Expected multipart/form-data payload")
    for part in msg.iter_parts():
        filename = part.get_filename()
        if filename:
            payload = part.get_payload(decode=True)
            if payload is None:
                raw_content = part.get_content()
                payload = (
                    raw_content.encode("utf-8")
                    if isinstance(raw_content, str)
                    else bytes(raw_content)
                )
            return Path(filename).name, payload
    raise ValueError("No uploaded file part found in multipart/form-data request")


def _static_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix in (".js", ".mjs"):
        return "application/javascript; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".woff2":
        return "font/woff2"
    if suffix == ".ico":
        return "image/x-icon"
    if suffix == ".txt":
        return "text/plain; charset=utf-8"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class MatrixHTTPServer(ThreadingHTTPServer):
    """Standard-library threaded HTTP/SSE server for the cloud novel matrix."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        *,
        scheduler: MatrixScheduler,
        admin_auth: AdminAuthManager,
        companion_auth: CompanionAuthManager,
        cpa_client: Optional[CPAManagementClient] = None,
        static_dir: Optional[Path | str] = None,
        sse_heartbeat_sec: float = 15.0,
        now_fn: Optional[Callable[[], datetime]] = None,
        telegram_bot: Optional[Any] = None,
    ) -> None:
        self.scheduler = scheduler
        self.db: StockpileDB = scheduler.db
        self.admin_auth = admin_auth
        self.companion_auth = companion_auth
        self.cpa_client = cpa_client or scheduler.cpa_client
        self.static_dir = Path(static_dir).resolve() if static_dir is not None else DEFAULT_STATIC_DIR
        self.sse_heartbeat_sec = float(sse_heartbeat_sec)
        self.now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self.telegram_bot = telegram_bot
        self.stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._serving = False
        self._closed = False
        self.wizard = NewBookWizard(
            db=scheduler.db,
            provider_fn=self._require_llm_provider,
            create_book_fn=scheduler.auto_create_book,
            now_fn=self.now_fn,
        )
        super().__init__(server_address, MatrixRequestHandler)

    def _require_llm_provider(self) -> Any:
        if self.scheduler.provider is None:
            raise SchedulerConfigurationError(
                "LLM provider/RoleModelRouter is not configured; the wizard cannot propose options."
            )
        return self.scheduler.provider

    def start_telegram_bot(self) -> Optional[threading.Thread]:
        if self.telegram_bot is None:
            return None
        return self.telegram_bot.start_background()

    def stop_telegram_bot(self, timeout: float = 5.0) -> None:
        if self.telegram_bot is not None:
            self.telegram_bot.stop(timeout=timeout)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        with self._lifecycle_lock:
            if self.stop_event.is_set() or self._closed:
                return
        self.start_telegram_bot()
        with self._lifecycle_lock:
            if self.stop_event.is_set() or self._closed:
                should_serve = False
            else:
                self._serving = True
                should_serve = True
        try:
            if should_serve:
                super().serve_forever(poll_interval=poll_interval)
        finally:
            with self._lifecycle_lock:
                self._serving = False
            self.stop_telegram_bot()

    def shutdown_and_close(self) -> None:
        with self._lifecycle_lock:
            self.stop_event.set()
            self._closed = True
            should_shutdown = self._serving
        try:
            self.scheduler.event_broker.notify_poll_waiters()
            self.stop_telegram_bot()
            self.scheduler.stop_background_loop()
            if should_shutdown:
                self.shutdown()
        finally:
            try:
                self.server_close()
            finally:
                try:
                    self.wizard.close()
                finally:
                    try:
                        self.scheduler.close()
                    finally:
                        if self.db is not self.scheduler.db:
                            self.db.close()


class MatrixRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: MatrixHTTPServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("HTTP %s - %s", self.client_address[0], format % args)

    def _client_ip(self) -> str:
        peer_ip = str(self.client_address[0])
        if is_loopback_host(peer_ip):
            forwarded = self.headers.get("X-Real-IP") or ""
            if forwarded.strip():
                return forwarded.strip()
        return peer_ip

    def _read_body_bytes(self) -> bytes:
        if getattr(self, "_body_consumed", False):
            return getattr(self, "_cached_body_bytes", b"")
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            self._body_consumed = True
            self._cached_body_bytes = b""
            return b""
        try:
            length = int(raw_len)
        except ValueError as exc:
            self.close_connection = True
            raise ValueError(f"Invalid Content-Length header: {raw_len!r}") from exc
        if length < 0:
            self.close_connection = True
            raise ValueError(f"Negative Content-Length: {length}")
        if length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            raise ValueError(f"Request body exceeds max limit ({MAX_REQUEST_BODY_BYTES} bytes)")
        if length == 0:
            self._body_consumed = True
            self._cached_body_bytes = b""
            return b""
        data = self.rfile.read(length)
        self._body_consumed = True
        self._cached_body_bytes = data
        if len(data) < length:
            self.close_connection = True
        return data

    def _drain_unread_body(self) -> None:
        if self.close_connection or getattr(self, "_body_consumed", False):
            return
        transfer_enc = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_enc:
            self.close_connection = True
            return
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            self._body_consumed = True
            return
        try:
            length = int(raw_len)
        except ValueError:
            self.close_connection = True
            return
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            return
        if length == 0:
            self._body_consumed = True
            return
        try:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    self.close_connection = True
                    return
                remaining -= len(chunk)
            self._body_consumed = True
        except OSError as exc:
            logger.debug("Failed to drain unread request body (%d bytes): %s", length, exc)
            self.close_connection = True

    def _parse_json_body(self, raw: bytes) -> Dict[str, Any]:
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"JSON body must be an object, got {type(parsed).__name__}")
        return parsed

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _send_bytes(
        self,
        status: int,
        data: bytes,
        content_type: str,
        *,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error(
        self,
        status: int,
        error_code: str,
        message: str,
        *,
        detail: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        err = ErrorPayload(
            error_code=error_code,
            message=message,
            detail=detail,
            timestamp=shanghai_now(self.server.now_fn).isoformat(),
        )
        self._send_json(status, err.to_dict(), extra_headers=extra_headers)

    def _require_admin_auth(self) -> bool:
        try:
            self.server.admin_auth.authenticate(self.headers, self._client_ip())
            return True
        except AuthLockoutError as exc:
            self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "TOO_MANY_AUTH_FAILURES",
                str(exc),
                detail={
                    "client_ip": exc.client_ip,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "locked_until": exc.locked_until_shanghai,
                },
                extra_headers={"Retry-After": str(exc.retry_after_seconds)},
            )
            return False
        except AdminUnauthorizedError as exc:
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                "UNAUTHORIZED",
                str(exc),
                extra_headers={"WWW-Authenticate": 'Bearer realm="xiaoshuo-bot"'},
            )
            return False

    def _require_companion_hmac(self, path: str, body: bytes) -> Optional[Tuple[str, int, str]]:
        try:
            return self.server.companion_auth.authenticate(
                self.command,
                path,
                self.headers,
                body,
            )
        except CompanionUnauthorizedError as exc:
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                exc.error_code,
                str(exc),
            )
            return None

    def _require_cpa_client(self) -> CPAManagementClient:
        if self.server.cpa_client is None:
            raise SchedulerConfigurationError(
                "CPA management client is not configured on this server."
            )
        return self.server.cpa_client

    # -----------------------------------------------------------------------
    # HTTP Method Dispatchers
    # -----------------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        self._body_consumed = False
        self._cached_body_bytes = b""
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path or "/"
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)

        try:
            # Static pages: "/" public studio, "/admin/" owner console, other assets by name
            if method in ("GET", "HEAD") and not path.startswith("/api/"):
                self._handle_static(_STATIC_PAGE_ROUTES.get(path, path))
                return

            # Public read-only studio (no auth, GET/HEAD only, sanitized payloads)
            if path.startswith("/api/public/"):
                if method not in ("GET", "HEAD"):
                    self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED", "Public API is read-only")
                    return
                if path == API_PUBLIC_BOOKS:
                    self._send_json(HTTPStatus.OK, {"books": list_public_books(self.server.db)})
                    return
                if path == API_PUBLIC_PIPELINE:
                    self._send_json(
                        HTTPStatus.OK,
                        self.server.scheduler.get_public_pipeline_snapshot(),
                    )
                    return
                if path == API_PUBLIC_EVENTS_STREAM:
                    if method == "HEAD":
                        self._send_bytes(
                            HTTPStatus.OK,
                            b"",
                            "text/event-stream; charset=utf-8",
                            extra_headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                        )
                        return
                    self._handle_events_stream(public=True)
                    return
                m_pc = _RE_PUBLIC_CHAPTER.match(path)
                if m_pc:
                    self._send_json(
                        HTTPStatus.OK,
                        get_public_chapter(self.server.db, m_pc.group("book_id"), int(m_pc.group("chapter"))),
                    )
                    return
                m_pb = _RE_PUBLIC_BOOK.match(path)
                if m_pb:
                    self._send_json(HTTPStatus.OK, get_public_book(self.server.db, m_pb.group("book_id")))
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Unknown route {method} {path}")
                return

            # Health Check (public, zero secrets)
            if method in ("GET", "HEAD") and path == API_HEALTH:
                self._handle_health()
                return

            # Companion Edge Routes (HMAC Authenticated)
            if path.startswith("/api/companion/"):
                body = self._read_body_bytes()
                if path == API_COMPANION_RESUME and method == "POST":
                    # Support either HMAC (companion) or Bearer (admin console)
                    has_hmac = any(
                        k.lower() == "x-xiaoshuo-signature" for k in self.headers.keys()
                    )
                    if has_hmac:
                        if self._require_companion_hmac(path, body) is None:
                            return
                    else:
                        if not self._require_admin_auth():
                            return
                    self._handle_companion_resume(body)
                    return

                if self._require_companion_hmac(path, body) is None:
                    return
                if method == "POST" and path == API_COMPANION_POLL:
                    self._handle_companion_poll(body)
                    return
                if method == "POST" and path == API_COMPANION_LEASE:
                    self._handle_companion_lease(body)
                    return
                if method == "POST" and path == API_COMPANION_RECEIPT:
                    self._handle_companion_receipt(body)
                    return
                if method == "POST" and path == API_COMPANION_METRICS:
                    self._handle_companion_metrics(body)
                    return
                if method == "POST" and path == API_COMPANION_CAPTCHA:
                    self._handle_companion_captcha(body)
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Unknown route {method} {path}")
                return

            # All other /api/* routes require Admin Bearer Auth (when configured)
            if not self._require_admin_auth():
                return

            if method == "GET" and path == API_EVENTS_STREAM:
                self._handle_events_stream()
                return

            if method == "GET" and path == API_PRODUCTION_KPI:
                book_q = (query.get("book_id") or [None])[0]
                self._send_json(HTTPStatus.OK, self.server.db.get_production_kpi(book_q))
                return

            if path == API_WIZARD_SESSIONS and method == "POST":
                data = self._parse_json_body(self._read_body_bytes())
                self._send_json(HTTPStatus.CREATED, self.server.wizard.start(str(data.get("idea") or "")))
                return
            m_ws = _RE_WIZARD_SESSION.match(path)
            if m_ws and method == "GET":
                self._send_json(HTTPStatus.OK, self.server.wizard.get(m_ws.group("session_id")))
                return
            m_wa = _RE_WIZARD_ACTION.match(path)
            if m_wa and method == "POST":
                data = self._parse_json_body(self._read_body_bytes())
                sid = m_wa.group("session_id")
                if m_wa.group("action") == "answer":
                    self._send_json(HTTPStatus.OK, self.server.wizard.answer(sid, data))
                else:
                    self._send_json(
                        HTTPStatus.ACCEPTED,
                        self.server.wizard.confirm_async(sid, pen_name=str(data.get("pen_name") or "")),
                    )
                return

            if method == "GET" and path == API_GENRES:
                channel_list = query.get("channel")
                channel = channel_list[0] if channel_list else None
                recipes = list_genres(channel=channel)
                self._send_json(HTTPStatus.OK, {"genres": [r.to_dict() for r in recipes]})
                return

            if method == "GET" and path == API_MODEL_SWITCHES:
                book_vals = query.get("book_id")
                book_filter = book_vals[0].strip() if book_vals and book_vals[0].strip() else None
                limit_vals = query.get("limit")
                limit = int(limit_vals[0]) if limit_vals and limit_vals[0].strip().isdigit() else 50
                events = self.server.db.list_model_switch_events(book_id=book_filter, limit=limit)
                self._send_json(HTTPStatus.OK, {"events": events})
                return

            if method == "GET" and path == API_MODEL_QUOTAS:
                quotas = self.server.scheduler.get_model_quotas()
                self._send_json(HTTPStatus.OK, {"quotas": quotas})
                return

            if method == "GET" and path == API_BOOKS:
                books = [b.to_dict() for b in self.server.scheduler.list_books()]
                digest = self.server.scheduler.get_daily_digest().to_dict()
                self._send_json(HTTPStatus.OK, {"books": books, "digest": digest})
                return

            if method == "POST" and path == API_BOOKS_AUTO_CREATE:
                body = self._read_body_bytes()
                req = AutoCreateBookRequest.from_dict(self._parse_json_body(body))
                summary = self.server.scheduler.auto_create_book(req)
                self._send_json(HTTPStatus.CREATED, summary.to_dict())
                return

            if path == API_CPA_AUTH_FILES:
                cpa = self._require_cpa_client()
                if method == "GET":
                    files = cpa.list_auth_files()
                    self._send_json(HTTPStatus.OK, {"files": files})
                    return
                if method == "POST":
                    body = self._read_body_bytes()
                    ctype = self.headers.get("Content-Type", "")
                    if "multipart/form-data" in ctype.lower():
                        fname, fbytes = _extract_multipart_auth_file(ctype, body)
                        res = cpa.upload_auth_file(filename=fname, content=fbytes)
                    else:
                        data = self._parse_json_body(body)
                        fname = str(data.get("filename") or "")
                        content_val = data.get("content")
                        if isinstance(content_val, Mapping):
                            content_bytes = json.dumps(dict(content_val), ensure_ascii=False).encode("utf-8")
                        elif isinstance(content_val, str):
                            content_bytes = content_val.encode("utf-8")
                        else:
                            raise ValueError("JSON upload requires 'filename' and 'content'")
                        res = cpa.upload_auth_file(filename=fname, content=content_bytes)
                    self._send_json(HTTPStatus.OK, res)
                    return

            m_cpa_file = _RE_CPA_AUTH_FILE_DETAIL.match(path)
            if m_cpa_file and method == "DELETE":
                cpa = self._require_cpa_client()
                file_name = urllib.parse.unquote(m_cpa_file.group("name"))
                res = cpa.delete_auth_file(file_name)
                self._send_json(HTTPStatus.OK, res)
                return

            if path == API_CPA_MODELS and method == "GET":
                role = (query.get("role") or ["writer"])[0]
                router = getattr(self.server.scheduler, "provider", None)
                if hasattr(router, "list_models"):
                    models = router.list_models(role=role)
                    self._send_json(HTTPStatus.OK, {"role": role, "models": models})
                    return
                raise SchedulerConfigurationError(
                    "RoleModelRouter is not configured to list /v1/models."
                )

            if path == API_CPA_USAGE and method == "GET":
                cpa = self._require_cpa_client()
                cpa_usage = cpa.get_usage()
                router = getattr(self.server.scheduler, "provider", None)
                local_ledger = (
                    router.ledger.get_summary()
                    if router is not None and hasattr(router, "ledger")
                    else {}
                )
                model_quotas = self.server.scheduler.get_model_quotas()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "cpa_usage": cpa_usage,
                        "ledger": local_ledger,
                        "model_quotas": model_quotas,
                    },
                )
                return

            if path == API_CPA_OAUTH:
                cpa = self._require_cpa_client()
                if method == "GET":
                    if "state" in query:
                        res = cpa.get_auth_status(query["state"][0])
                    else:
                        provider_name = (query.get("provider") or [""])[0]
                        proj_id = (query.get("project_id") or [None])[0]
                        res = cpa.get_oauth_url(provider_name, project_id=proj_id)
                    self._send_json(HTTPStatus.OK, res)
                    return
                if method == "POST":
                    data = self._parse_json_body(self._read_body_bytes())
                    res = cpa.get_oauth_url(
                        str(data.get("provider") or ""),
                        project_id=data.get("project_id"),
                        is_webui=bool(data.get("is_webui", True)),
                    )
                    self._send_json(HTTPStatus.OK, res)
                    return

            if path == API_CPA_BINDINGS:
                if method == "GET":
                    bindings = [b.to_dict() for b in self.server.scheduler.get_cpa_bindings()]
                    self._send_json(HTTPStatus.OK, {"bindings": bindings})
                    return
                if method in ("PUT", "POST"):
                    data = self._parse_json_body(self._read_body_bytes())
                    raw_items = data.get("bindings") if "bindings" in data else [data]
                    updated = self.server.scheduler.update_cpa_bindings(raw_items)
                    self._send_json(
                        HTTPStatus.OK, {"bindings": [b.to_dict() for b in updated]}
                    )
                    return

            # Book-specific routes
            if self._dispatch_book_route(method, path, query):
                return

            self._send_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Unknown route {method} {path}")

        except BookNotFoundError as exc:
            self._send_error(HTTPStatus.NOT_FOUND, "BOOK_NOT_FOUND", str(exc))
        except WizardConflictError as exc:
            self._send_error(HTTPStatus.CONFLICT, "WIZARD_PLANNING_BUSY", str(exc))
        except WizardError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "WIZARD_ERROR", str(exc))
        except ChapterNotFoundError as exc:
            self._send_error(HTTPStatus.NOT_FOUND, "CHAPTER_NOT_FOUND", str(exc))
        except SequentialLeaseLockError as exc:
            self._send_error(HTTPStatus.CONFLICT, "SEQUENTIAL_LEASE_LOCKED", str(exc))
        except InvalidStatusTransitionError as exc:
            self._send_error(HTTPStatus.CONFLICT, "INVALID_STATUS_TRANSITION", str(exc))
        except CutConfirmationConflictError as exc:
            self._send_error(HTTPStatus.CONFLICT, "CUT_CONFIRMATION_CONFLICT", str(exc))
        except DailyQuotaExceededError as exc:
            self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "DAILY_QUOTA_DEFERRED",
                str(exc),
                detail={
                    "account_id": exc.account_id,
                    "author_level": exc.author_level,
                    "cst_date": exc.cst_date,
                    "deferred_to_date": exc.deferred_to_date,
                    "used_chars": exc.used_chars,
                    "daily_cap": exc.daily_cap,
                    "requested_chars": exc.requested_chars,
                },
            )
        except PublishedChapterImmutableError as exc:
            self._send_error(HTTPStatus.CONFLICT, "PUBLISHED_CHAPTER_IMMUTABLE", str(exc))
        except (UnknownGenreError, ProjectValidationError, EvolutionError, ExportError, ValueError, KeyError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", str(exc))
        except ImageProviderNotConfiguredError as exc:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "IMAGE_PROVIDER_NOT_CONFIGURED", str(exc))
        except CoverError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "COVER_ERROR", str(exc))
        except (SchedulerConfigurationError, LLMConfigurationError) as exc:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "SERVICE_NOT_CONFIGURED", str(exc))
        except QuotaExhaustedError as exc:
            self._send_error(HTTPStatus.TOO_MANY_REQUESTS, "QUOTA_EXHAUSTED", str(exc))
        except CPAManagementError as exc:
            code = exc.status_code if exc.status_code and 400 <= exc.status_code < 600 else HTTPStatus.BAD_GATEWAY
            self._send_error(code, "CPA_MANAGEMENT_ERROR", str(exc))
        except LLMRequestError as exc:
            code = exc.status_code if exc.status_code and 400 <= exc.status_code < 600 else HTTPStatus.BAD_GATEWAY
            self._send_error(code, "LLM_UPSTREAM_ERROR", str(exc))
        except Exception as exc:
            # Log full details server-side only; never leak internal exception text to clients.
            logger.exception("Unhandled error serving %s %s: %s", method, path, exc)
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "INTERNAL_ERROR",
                "Internal server error; see server logs for details.",
            )
        finally:
            self._drain_unread_body()

    def _dispatch_book_route(
        self, method: str, path: str, query: Dict[str, List[str]]
    ) -> bool:
        m = _RE_BOOK_STATUS.match(path)
        if m:
            bid = urllib.parse.unquote(m.group("book_id"))
            if method == "GET":
                summary = self.server.scheduler.get_book_status(bid)
                book_row = self.server.db.get_book(bid)
                metrics = self.server.db.get_latest_metrics(bid)
                directives = self.server.db.get_outline_directives(bid)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        **summary.to_dict(),
                        "isolation_detail": book_row.get("isolation_detail", {}),
                        "latest_metrics": metrics,
                        "outline_directives": directives,
                    },
                )
                return True
            if method == "POST":
                data = self._parse_json_body(self._read_body_bytes())
                action = str(data.get("action") or "").strip().lower()
                paused = data.get("paused")
                reason = str(data.get("reason") or "")
                if action and action not in ("pause", "resume"):
                    raise ValueError(f"Unsupported book status action: {action!r}")
                if action == "resume" or (paused is False and action != "pause"):
                    res = self.server.scheduler.resume_book(bid)
                else:
                    res = self.server.scheduler.pause_book(bid, reason=reason)
                self._send_json(HTTPStatus.OK, res)
                return True

        m = _RE_BOOK_CHAPTERS.match(path)
        if m and method == "GET":
            bid = urllib.parse.unquote(m.group("book_id"))
            chapters = [c.to_dict() for c in self.server.db.list_chapters(bid)]
            self._send_json(HTTPStatus.OK, {"book_id": bid, "chapters": chapters})
            return True

        m = _RE_BOOK_CHAPTER_DETAIL.match(path)
        if m and method == "GET":
            bid = urllib.parse.unquote(m.group("book_id"))
            ch = int(m.group("chapter"))
            detail = self.server.db.get_chapter(bid, ch)
            self._send_json(HTTPStatus.OK, detail)
            return True

        m = _RE_BOOK_CHAPTER_LOCK.match(path)
        if m and method == "POST":
            bid = urllib.parse.unquote(m.group("book_id"))
            ch = int(m.group("chapter"))
            req = ChapterLockRequest.from_dict(self._parse_json_body(self._read_body_bytes()))
            updated = self.server.scheduler.set_chapter_lock(
                bid, ch, locked=req.locked, reason=req.reason
            )
            self._send_json(HTTPStatus.OK, updated.to_dict())
            return True

        m = _RE_BOOK_EXPORT.match(path)
        if m and method in ("GET", "POST"):
            bid = urllib.parse.unquote(m.group("book_id"))
            body_data = self._parse_json_body(self._read_body_bytes()) if method == "POST" else {}
            body_platform = str(body_data.get("platform") or "").strip() if body_data else ""
            platform = ((query.get("platform") or [body_platform or "tomato"])[0]).strip().lower()
            with tempfile.TemporaryDirectory(prefix="xsb_srv_export_") as tmpdir:
                res = self.server.scheduler.export_book_bundle(
                    bid, platform=platform, output_dir=tmpdir
                )
                zip_path_str = res.get("zip_path") or ""
                if zip_path_str and Path(zip_path_str).is_file():
                    zip_bytes = Path(zip_path_str).read_bytes()
                    dl_name = Path(zip_path_str).name
                else:
                    buf = io.BytesIO()
                    out_dir = Path(res["output_dir"])
                    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                        for fp in sorted(out_dir.rglob("*")):
                            if fp.is_file():
                                zf.write(fp, arcname=fp.relative_to(out_dir).as_posix())
                    zip_bytes = buf.getvalue()
                    dl_name = f"{bid}_{platform}_bundle.zip"
                self._send_bytes(
                    HTTPStatus.OK,
                    zip_bytes,
                    "application/zip",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{dl_name}"'
                    },
                )
            return True

        m = _RE_BOOK_COVER.match(path)
        if m:
            bid = urllib.parse.unquote(m.group("book_id"))
            if method == "GET":
                book = self.server.db.get_book(bid)
                cpath = Path(book["cover_path"]) if book.get("cover_path") else None
                if cpath is not None and cpath.is_file():
                    mime = "image/jpeg" if cpath.suffix.lower() in (".jpg", ".jpeg") else "image/png"
                    self._send_bytes(HTTPStatus.OK, cpath.read_bytes(), mime)
                    return True
                if (query.get("generate") or ["0"])[0] == "1":
                    base_mode = (query.get("base_mode") or ["procedural"])[0]
                    res = self.server.scheduler.generate_book_cover(bid, base_mode=base_mode)
                    self._send_bytes(HTTPStatus.OK, res.output_path.read_bytes(), "image/png")
                    return True
                self._send_error(HTTPStatus.NOT_FOUND, "COVER_NOT_FOUND", f"No cover generated for '{bid}'")
                return True
            if method == "POST":
                data = self._parse_json_body(self._read_body_bytes())
                base_mode = str(data.get("base_mode") or "ai_image")
                fmt = str(data.get("output_format") or "PNG")
                res = self.server.scheduler.generate_book_cover(
                    bid, base_mode=base_mode, output_format=fmt
                )
                self._send_json(HTTPStatus.OK, res.to_dict())
                return True

        m = _RE_BOOK_CUT.match(path)
        if m:
            bid = urllib.parse.unquote(m.group("book_id"))
            if method == "DELETE":
                self._send_json(HTTPStatus.OK, self.server.scheduler.cancel_cut_book(bid))
                return True
            if method == "POST":
                req = CutBookRequest.from_dict(self._parse_json_body(self._read_body_bytes()))
                if req.confirm:
                    res = self.server.scheduler.confirm_cut_book(
                        bid, req.confirmation_version, reason=req.reason
                    )
                else:
                    res = self.server.scheduler.request_cut_book(bid, reason=req.reason)
                self._send_json(HTTPStatus.OK, res)
                return True

        m = _RE_BOOK_PROMOTE.match(path)
        if m and method == "POST":
            bid = urllib.parse.unquote(m.group("book_id"))
            self._parse_json_body(self._read_body_bytes())
            self._send_json(HTTPStatus.OK, self.server.scheduler.approve_promotion(bid))
            return True

        m = _RE_BOOK_EXTEND.match(path)
        if m and method == "POST":
            bid = urllib.parse.unquote(m.group("book_id"))
            data = self._parse_json_body(self._read_body_bytes())
            res = self.server.scheduler.extend_book_volume(
                bid,
                batch_size=int(data.get("batch_size", 15)),
                finale=bool(data.get("finale", False)),
                num_chapters=int(data.get("num_chapters", 10)),
            )
            self._send_json(HTTPStatus.OK, res)
            return True

        m = _RE_BOOK_RESUME.match(path)
        if m and method == "POST":
            bid = urllib.parse.unquote(m.group("book_id"))
            self._parse_json_body(self._read_body_bytes())
            self._send_json(HTTPStatus.OK, self.server.scheduler.resume_book(bid))
            return True

        return False

    # -----------------------------------------------------------------------
    # Endpoint Implementations
    # -----------------------------------------------------------------------
    def _handle_static(self, path: str) -> None:
        static_root = self.server.static_dir
        if not static_root.is_dir():
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "STATIC_NOT_FOUND",
                f"Static web directory not present at '{static_root}'",
            )
            return
        rel = path.lstrip("/")
        if not rel or rel.endswith("/"):
            rel = f"{rel}index.html"
        elif rel.startswith("static/"):
            rel = rel[len("static/") :]
            if not rel or rel.endswith("/"):
                rel = f"{rel}index.html"
        candidate = (static_root / rel).resolve()
        try:
            candidate.relative_to(static_root)
        except ValueError:
            self._send_error(HTTPStatus.FORBIDDEN, "FORBIDDEN", "Invalid static asset path")
            return
        if not candidate.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "STATIC_NOT_FOUND", f"Asset '{rel}' not found")
            return
        self._send_bytes(
            HTTPStatus.OK,
            candidate.read_bytes(),
            _static_mime_type(candidate),
            extra_headers={
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _handle_health(self) -> None:
        cpa_ok = False
        if self.server.cpa_client is not None:
            try:
                self.server.cpa_client.list_auth_files()
                cpa_ok = True
            except CPAManagementError:
                cpa_ok = False
        digest = self.server.scheduler.get_daily_digest()
        resp = HealthResponse(
            status="ok",
            service="xiaoshuo-bot",
            version="2.0.0",
            shanghai_time=shanghai_now(self.server.now_fn).isoformat(),
            cpa_reachable=cpa_ok,
            active_books=digest.active_books,
        )
        payload = resp.to_dict()
        payload["admin_auth_required"] = bool(self.server.admin_auth.auth_enabled)
        self._send_json(HTTPStatus.OK, payload)

    def _handle_companion_poll(self, body: bytes) -> None:
        req = CompanionPollRequest.from_dict(self._parse_json_body(body))
        timeout_sec = max(0, min(int(req.timeout_sec), MAX_POLL_TIMEOUT_SECONDS))
        tasks = self.server.db.poll_available_tasks(
            req.account_id, book_ids=req.book_ids or None
        )
        if not tasks and timeout_sec > 0 and not self.server.stop_event.is_set():
            deadline = time.monotonic() + float(timeout_sec)
            while not self.server.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.server.scheduler.event_broker.wait_for_poll_wakeup(
                    remaining, stop_event=self.server.stop_event
                )
                if self.server.stop_event.is_set():
                    break
                tasks = self.server.db.poll_available_tasks(
                    req.account_id, book_ids=req.book_ids or None
                )
                if tasks:
                    break

        self._send_json(
            HTTPStatus.OK,
            {
                "account_id": req.account_id,
                "tasks": tasks,
                "server_time": shanghai_now(self.server.now_fn).isoformat(),
            },
        )

    def _handle_companion_lease(self, body: bytes) -> None:
        req = CompanionLeaseRequest.from_dict(self._parse_json_body(body))
        resp = self.server.db.lease_chapter(req)
        self._send_json(HTTPStatus.OK, resp.to_dict())

    _NON_CHAPTER_TASK_TYPES = frozenset(
        {
            CompanionTaskType.CREATE_STAR_CARDS.value,
            CompanionTaskType.SUBMIT_SIGNING_DRAFT.value,
            CompanionTaskType.ROTATE_PACKAGING.value,
        }
    )

    def _try_record_non_chapter_receipt(
        self, req: CompanionReceiptRequest
    ) -> Optional[Dict[str, Any]]:
        db = self.server.db
        now_iso = shanghai_now(self.server.now_fn).isoformat()
        with db._lock:
            db.get_book(req.book_id)
            row = db._conn.execute(
                "SELECT * FROM companion_tasks WHERE task_id = ? AND book_id = ?",
                (req.task_id, req.book_id),
            ).fetchone()
            if row is None:
                return None
            task_type = str(row["task_type"])
            if task_type not in self._NON_CHAPTER_TASK_TYPES:
                return None
            current_status = str(row["status"])
            if current_status == req.status and req.status in (
                StockpileStatus.DRAFT_SAVED.value,
                StockpileStatus.PUBLISHED.value,
                StockpileStatus.PAUSED_AUTH_CAPTCHA.value,
            ):
                return {
                    "book_id": req.book_id,
                    "task_id": req.task_id,
                    "task_type": task_type,
                    "chapter": int(req.chapter),
                    "status": req.status,
                    "idempotent_replay": True,
                }
            db._conn.execute(
                "UPDATE companion_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (req.status, now_iso, req.task_id),
            )
            if req.status == StockpileStatus.PAUSED_AUTH_CAPTCHA.value:
                db.update_book_status(
                    req.book_id,
                    BookRunStatus.PAUSED_AUTH_CAPTCHA,
                    paused_reason=req.error_message or "PAUSED_AUTH_CAPTCHA",
                )
            return {
                "book_id": req.book_id,
                "task_id": req.task_id,
                "task_type": task_type,
                "chapter": int(req.chapter),
                "status": req.status,
                "idempotent_replay": False,
            }

    def _handle_companion_receipt(self, body: bytes) -> None:
        req = CompanionReceiptRequest.from_dict(self._parse_json_body(body))
        if req.chapter <= 0:
            non_ch_res = self._try_record_non_chapter_receipt(req)
            if non_ch_res is not None:
                self._send_json(HTTPStatus.OK, non_ch_res)
                return
        res = self.server.db.record_receipt(req)
        self._send_json(HTTPStatus.OK, res)

    def _handle_companion_metrics(self, body: bytes) -> None:
        req = CompanionMetricsRequest.from_dict(self._parse_json_body(body))
        res = self.server.db.record_metrics(
            req, motif_store=self.server.scheduler.motif_store
        )
        self.server.scheduler.event_broker.publish(
            SSEEventPayload(
                event=SSE_METRICS_COLLECTED,
                book_id=req.book_id,
                message=f"Collected T-1 metrics for '{req.book_id}' ({res['metrics_date']})",
                data={
                    "metrics_date": res["metrics_date"],
                    "settled": res["settled"],
                    "hotlist_count": res["hotlist_count"],
                },
                timestamp=shanghai_now(self.server.now_fn).isoformat(),
            )
        )
        rd = res.get("racing_decision") or {}
        if rd.get("action"):
            self.server.scheduler.event_broker.publish(
                SSEEventPayload(
                    event=SSE_RACING_DECISION,
                    book_id=req.book_id,
                    message=str(rd.get("reason") or rd.get("action")),
                    data=rd,
                    timestamp=shanghai_now(self.server.now_fn).isoformat(),
                )
            )
        self._send_json(HTTPStatus.OK, res)

    def _handle_companion_captcha(self, body: bytes) -> None:
        report = CompanionCaptchaReport.from_dict(self._parse_json_body(body))
        res = self.server.scheduler.report_captcha_pause(report)
        self._send_json(HTTPStatus.OK, res)

    def _handle_companion_resume(self, body: bytes) -> None:
        req = CompanionResumeRequest.from_dict(self._parse_json_body(body))
        res = self.server.scheduler.resume_companion(
            account_id=req.account_id, book_id=req.book_id
        )
        self._send_json(HTTPStatus.OK, res)

    def _handle_events_stream(self, public: bool = False) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        broker = self.server.scheduler.event_broker
        sub_id, q = broker.subscribe()
        heartbeat_interval = max(0.5, self.server.sse_heartbeat_sec)
        last_heartbeat = time.monotonic()

        try:
            init_frame = format_sse_frame(
                SSE_HEARTBEAT,
                {"status": "connected", "timestamp": shanghai_now(self.server.now_fn).isoformat()},
                event_id="init",
                retry_ms=3000,
            )
            self.wfile.write(init_frame.encode("utf-8"))
            self.wfile.flush()

            while not self.server.stop_event.is_set():
                try:
                    evt = q.get(timeout=0.5)
                    if self.server.stop_event.is_set():
                        break
                    if public:
                        titles = {b["book_id"]: b["title"] for b in self.server.db.list_books()}
                        is_replay = bool(
                            getattr(
                                getattr(self.server.scheduler, "pipeline_tracker", None),
                                "replay_mode",
                                False,
                            )
                        )
                        pub = public_event(evt, titles, replay=is_replay)
                        if pub is None:
                            continue
                        frame = format_sse_frame(evt.event, pub, event_id=evt.event_id or None)
                    else:
                        frame = evt.to_sse_frame()
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                except queue.Empty:
                    now_m = time.monotonic()
                    if now_m - last_heartbeat >= heartbeat_interval:
                        hb = format_sse_frame(
                            SSE_HEARTBEAT,
                            {"timestamp": shanghai_now(self.server.now_fn).isoformat()},
                        )
                        self.wfile.write(hb.encode("utf-8"))
                        self.wfile.flush()
                        last_heartbeat = now_m
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            logger.debug("SSE subscriber %s disconnected", sub_id)
        finally:
            broker.unsubscribe(sub_id)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    db_path: str | Path,
    projects_root: str | Path,
    admin_token: Optional[str] = None,
    companion_secret: Optional[str] = None,
    provider: Optional[Any] = None,
    cpa_client: Optional[CPAManagementClient] = None,
    motif_store: Optional[Any] = None,
    static_dir: Optional[Path | str] = None,
    scheduler: Optional[MatrixScheduler] = None,
    sse_heartbeat_sec: float = 15.0,
    now_fn: Optional[Callable[[], datetime]] = None,
    monotonic_fn: Optional[Callable[[], float]] = None,
    telegram_bot: Optional[Any] = None,
    allow_insecure_loopback_no_auth: Optional[bool] = None,
    export_only: bool = False,
    verify_cpa_models: Optional[bool] = None,
    planner_max_consecutive_same_location: Optional[int] = None,
) -> MatrixHTTPServer:
    """Create and validate a configured MatrixHTTPServer (enforces InsecureBindError & CPA model check)."""
    resolved_token = validate_bind_security(
        host,
        admin_token,
        allow_insecure_loopback_no_auth=allow_insecure_loopback_no_auth,
    )
    resolved_secret = validate_companion_secret(resolve_companion_secret(companion_secret))

    router_obj: Optional[RoleModelRouter] = None
    if isinstance(provider, RoleModelRouter):
        router_obj = provider
    elif scheduler is not None and isinstance(scheduler.provider, RoleModelRouter):
        router_obj = scheduler.provider

    should_verify_models = (
        bool(verify_cpa_models)
        if verify_cpa_models is not None
        else (
            router_obj is not None
            and (
                router_obj.workbuddy_catalog is not None
                or any(
                    router_obj.has_role(r)
                    and bool(
                        router_obj.get_role_config(r).model_whitelist
                        or router_obj.get_role_config(r).rotation_models
                    )
                    for r in ("planner", "writer", "critic", "polisher")
                )
            )
        )
    )
    if should_verify_models:
        if router_obj is None:
            raise LLMConfigurationError(
                "启动自检拒绝启动：已要求核对 CPA /v1/models，但未配置 RoleModelRouter。"
            )
        verify_router_models_against_cpa(router_obj, catalog=router_obj.workbuddy_catalog)

    created_db: Optional[StockpileDB] = None
    try:
        if scheduler is None:
            created_db = StockpileDB(db_path, now_fn=now_fn)
            scheduler = MatrixScheduler(
                db=created_db,
                projects_root=projects_root,
                provider=provider,
                cpa_client=cpa_client,
                motif_store=motif_store,
                now_fn=now_fn,
                export_only=export_only,
                planner_max_consecutive_same_location=planner_max_consecutive_same_location,
            )

        admin_auth = AdminAuthManager(
            admin_token=resolved_token,
            monotonic_fn=monotonic_fn,
            now_fn=now_fn,
        )
        companion_auth = CompanionAuthManager(
            companion_secret=resolved_secret,
            now_fn=now_fn,
        )

        return MatrixHTTPServer(
            (host, int(port)),
            scheduler=scheduler,
            admin_auth=admin_auth,
            companion_auth=companion_auth,
            cpa_client=cpa_client or scheduler.cpa_client,
            static_dir=static_dir,
            sse_heartbeat_sec=sse_heartbeat_sec,
            now_fn=now_fn,
            telegram_bot=telegram_bot,
        )
    except Exception:
        if created_db is not None:
            created_db.close()
        raise


__all__ = [
    "ALL_API_ROUTES",
    "DEFAULT_STATIC_DIR",
    "MAX_POLL_TIMEOUT_SECONDS",
    "MatrixHTTPServer",
    "MatrixRequestHandler",
    "create_server",
]
