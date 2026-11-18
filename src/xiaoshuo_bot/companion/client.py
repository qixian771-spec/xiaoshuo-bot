from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import ssl
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request

from ..contracts import (
    API_COMPANION_CAPTCHA,
    API_COMPANION_LEASE,
    API_COMPANION_METRICS,
    API_COMPANION_POLL,
    API_COMPANION_RECEIPT,
    API_COMPANION_RESUME,
    API_HEALTH,
    CompanionCaptchaReport,
    CompanionLeaseRequest,
    CompanionLeaseResponse,
    CompanionMetricsRequest,
    CompanionPollRequest,
    CompanionReceiptRequest,
    CompanionResumeRequest,
    compute_hmac_headers,
)
from ..models import SHANGHAI_TZ


class CompanionClientError(RuntimeError):
    """Base error for companion outbound HTTPS client and outbox operations."""


class CompanionConfigError(CompanionClientError, ValueError):
    """Raised when required companion client configuration (URL, HMAC secret, client_id) is missing."""


class CompanionAuthError(CompanionClientError):
    """Raised when cloud server rejects HMAC authentication or returns 401/403."""


class CompanionTransportError(CompanionClientError):
    """Raised on network or HTTP transport failure during outbound cloud RPC."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _shanghai_now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class OutboxItem:
    id: int
    idempotency_key: str
    endpoint_path: str
    payload_json: str
    status: str
    attempts: int
    last_error: str
    created_at: str
    updated_at: str
    sent_at: str

    def payload_dict(self) -> Dict[str, Any]:
        return dict(json.loads(self.payload_json))


@dataclass(frozen=True)
class OutboxDeliveryResult:
    outbox_id: int
    endpoint_path: str
    delivered: bool
    queued_in_outbox: bool
    status_code: Optional[int] = None
    response_data: Optional[Dict[str, Any]] = None
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutboxFlushResult:
    attempted: int
    sent: int
    failed: int
    remaining_pending: int
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OutboxStore:
    """Durable SQLite WAL outbox guaranteeing receipts, metrics & captcha reports survive disconnects."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=FULL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS companion_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    endpoint_path TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_companion_outbox_status_id "
                "ON companion_outbox(status, id);"
            )
            self._conn.commit()

    def journal_mode(self) -> str:
        with self._lock:
            row = self._conn.execute("PRAGMA journal_mode;").fetchone()
            return str(row[0]).lower() if row else ""

    def enqueue(
        self,
        endpoint_path: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> int:
        if not endpoint_path or not endpoint_path.startswith("/"):
            raise ValueError(f"Invalid endpoint_path: {endpoint_path!r}")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be empty")
        now_iso = _shanghai_now_iso()
        serialized = _canonical_json_bytes(payload).decode("utf-8")

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM companion_outbox WHERE idempotency_key = ?",
                (idempotency_key.strip(),),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cur = self._conn.execute(
                """
                INSERT INTO companion_outbox (
                    idempotency_key, endpoint_path, payload_json, status,
                    attempts, last_error, created_at, updated_at, sent_at
                ) VALUES (?, ?, ?, 'PENDING', 0, '', ?, ?, '')
                """,
                (idempotency_key.strip(), endpoint_path, serialized, now_iso, now_iso),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_pending(self, *, limit: int = 100) -> List[OutboxItem]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, idempotency_key, endpoint_path, payload_json, status,
                       attempts, last_error, created_at, updated_at, sent_at
                FROM companion_outbox
                WHERE status = 'PENDING'
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [
            OutboxItem(
                id=int(r["id"]),
                idempotency_key=str(r["idempotency_key"]),
                endpoint_path=str(r["endpoint_path"]),
                payload_json=str(r["payload_json"]),
                status=str(r["status"]),
                attempts=int(r["attempts"]),
                last_error=str(r["last_error"]),
                created_at=str(r["created_at"]),
                updated_at=str(r["updated_at"]),
                sent_at=str(r["sent_at"]),
            )
            for r in rows
        ]

    def list_all(self) -> List[OutboxItem]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, idempotency_key, endpoint_path, payload_json, status,
                       attempts, last_error, created_at, updated_at, sent_at
                FROM companion_outbox
                ORDER BY id ASC
                """
            ).fetchall()
        return [
            OutboxItem(
                id=int(r["id"]),
                idempotency_key=str(r["idempotency_key"]),
                endpoint_path=str(r["endpoint_path"]),
                payload_json=str(r["payload_json"]),
                status=str(r["status"]),
                attempts=int(r["attempts"]),
                last_error=str(r["last_error"]),
                created_at=str(r["created_at"]),
                updated_at=str(r["updated_at"]),
                sent_at=str(r["sent_at"]),
            )
            for r in rows
        ]

    def mark_sent(self, item_id: int) -> None:
        now_iso = _shanghai_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE companion_outbox
                SET status = 'SENT',
                    attempts = attempts + 1,
                    last_error = '',
                    updated_at = ?,
                    sent_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, int(item_id)),
            )
            self._conn.commit()

    def mark_attempt_failed(self, item_id: int, error_message: str) -> None:
        now_iso = _shanghai_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE companion_outbox
                SET attempts = attempts + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(error_message)[:500], now_iso, int(item_id)),
            )
            self._conn.commit()

    def count_pending(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM companion_outbox WHERE status = 'PENDING'"
            ).fetchone()
            return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class CompanionHttpClient:
    """Pure outbound HTTPS/HTTP long-poll client with HMAC signing and SQLite WAL outbox."""

    def __init__(
        self,
        base_url: str,
        hmac_secret: str | bytes,
        *,
        client_id: str = "companion-default",
        outbox_db_path: str | Path,
        timeout_sec: float = 15.0,
        poll_timeout_sec: int = 25,
        ca_file: Optional[str | Path] = None,
    ) -> None:
        cleaned_url = (base_url or "").strip().rstrip("/")
        parsed = urllib.parse.urlparse(cleaned_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise CompanionConfigError(
                f"Companion server base_url must be a valid http/https URL, got {base_url!r}"
            )
        secret_bytes = hmac_secret.encode("utf-8") if isinstance(hmac_secret, str) else bytes(hmac_secret)
        if not secret_bytes:
            raise CompanionConfigError("Companion hmac_secret cannot be empty")
        cleaned_client_id = (client_id or "").strip()
        if not cleaned_client_id:
            raise CompanionConfigError("Companion client_id cannot be empty")

        self.base_url = cleaned_url
        self._hmac_secret = secret_bytes
        self.client_id = cleaned_client_id
        self.timeout_sec = float(timeout_sec)
        self.poll_timeout_sec = int(poll_timeout_sec)
        self.outbox = OutboxStore(outbox_db_path)

        self._ssl_context: Optional[ssl.SSLContext] = None
        if ca_file is not None:
            ca_path = Path(ca_file).resolve()
            if not ca_path.is_file():
                raise CompanionConfigError(f"CA bundle file does not exist: '{ca_path}'")
            self._ssl_context = ssl.create_default_context(cafile=str(ca_path))

    def close(self) -> None:
        self.outbox.close()

    def __enter__(self) -> CompanionHttpClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        sign_hmac: bool = True,
    ) -> Tuple[int, Dict[str, Any]]:
        upper_method = method.strip().upper()
        norm_path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{norm_path}"
        body_bytes = _canonical_json_bytes(payload) if payload is not None else b""

        headers: Dict[str, str] = {
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        if sign_hmac:
            hmac_headers = compute_hmac_headers(
                self._hmac_secret,
                upper_method,
                norm_path,
                body=body_bytes,
                client_id=self.client_id,
            )
            headers.update(hmac_headers)

        req = urllib.request.Request(
            url=url,
            data=body_bytes if upper_method in ("POST", "PUT", "PATCH") else None,
            headers=headers,
            method=upper_method,
        )
        eff_timeout = float(timeout if timeout is not None else self.timeout_sec)
        try:
            with urllib.request.urlopen(req, timeout=eff_timeout, context=self._ssl_context) as resp:
                status_code = int(getattr(resp, "status", 200))
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                err_body = ""
            if exc.code in (401, 403):
                raise CompanionAuthError(
                    f"Cloud rejected companion request {upper_method} {norm_path} "
                    f"(HTTP {exc.code}): {err_body}"
                ) from exc
            raise CompanionTransportError(
                f"Cloud returned HTTP {exc.code} for {upper_method} {norm_path}: {err_body}",
                status_code=int(exc.code),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CompanionTransportError(
                f"Network error calling {upper_method} {norm_path}: {exc}",
                status_code=None,
            ) from exc

        if not raw or not raw.strip():
            return status_code, {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompanionTransportError(
                f"Invalid JSON response from {upper_method} {norm_path}: {exc}",
                status_code=status_code,
            ) from exc
        if isinstance(parsed, dict):
            return status_code, parsed
        if isinstance(parsed, list):
            return status_code, {"tasks": parsed}
        return status_code, {"data": parsed}

    def check_health(self) -> Dict[str, Any]:
        _, data = self._request_json("GET", API_HEALTH, payload=None, sign_hmac=False)
        return data

    def poll_tasks(
        self,
        account_id: str,
        *,
        author_name: str = "",
        book_ids: Optional[Sequence[str]] = None,
        timeout_sec: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Long-poll cloud server for queued companion tasks (zero inbound ports)."""
        eff_poll_timeout = int(timeout_sec if timeout_sec is not None else self.poll_timeout_sec)
        req = CompanionPollRequest(
            account_id=account_id,
            author_name=author_name,
            book_ids=list(book_ids or ()),
            timeout_sec=eff_poll_timeout,
        )
        http_timeout = max(float(eff_poll_timeout) + 15.0, self.timeout_sec)
        status_code, data = self._request_json(
            "POST",
            API_COMPANION_POLL,
            req.to_dict(),
            timeout=http_timeout,
            sign_hmac=True,
        )
        if status_code == 204 or not data:
            return []
        if "tasks" in data and isinstance(data["tasks"], list):
            return [dict(item) for item in data["tasks"] if isinstance(item, Mapping)]
        if "task" in data:
            task_obj = data["task"]
            return [dict(task_obj)] if isinstance(task_obj, Mapping) else []
        if "task_id" in data:
            return [dict(data)]
        return []

    def lease_task(self, request: CompanionLeaseRequest) -> CompanionLeaseResponse:
        """Atomically claim a queued task lease from cloud server."""
        _, data = self._request_json(
            "POST",
            API_COMPANION_LEASE,
            request.to_dict(),
            sign_hmac=True,
        )
        payload = data.get("lease") or data.get("task") or data
        if not isinstance(payload, Mapping) or "task_id" not in payload:
            raise CompanionTransportError(
                f"Malformed lease response from {API_COMPANION_LEASE}: {data!r}"
            )
        return CompanionLeaseResponse.from_dict(payload)

    def _enqueue_and_deliver(
        self,
        endpoint_path: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        flush_pending_first: bool = True,
    ) -> OutboxDeliveryResult:
        item_id = self.outbox.enqueue(
            endpoint_path,
            payload,
            idempotency_key=idempotency_key,
        )
        if flush_pending_first:
            self.flush_outbox()
            # Check if item was already delivered during flush
            pending_ids = {item.id for item in self.outbox.list_pending(limit=500)}
            if item_id not in pending_ids:
                return OutboxDeliveryResult(
                    outbox_id=item_id,
                    endpoint_path=endpoint_path,
                    delivered=True,
                    queued_in_outbox=True,
                    status_code=200,
                )

        try:
            status_code, resp_data = self._request_json(
                "POST",
                endpoint_path,
                payload,
                sign_hmac=True,
            )
            self.outbox.mark_sent(item_id)
            return OutboxDeliveryResult(
                outbox_id=item_id,
                endpoint_path=endpoint_path,
                delivered=True,
                queued_in_outbox=True,
                status_code=status_code,
                response_data=resp_data,
            )
        except CompanionAuthError:
            self.outbox.mark_attempt_failed(item_id, "Authentication rejected (401/403)")
            raise
        except CompanionTransportError as exc:
            self.outbox.mark_attempt_failed(item_id, str(exc))
            return OutboxDeliveryResult(
                outbox_id=item_id,
                endpoint_path=endpoint_path,
                delivered=False,
                queued_in_outbox=True,
                status_code=exc.status_code,
                error_message=str(exc),
            )

    def send_receipt(
        self,
        receipt: CompanionReceiptRequest,
        *,
        flush_pending_first: bool = True,
    ) -> OutboxDeliveryResult:
        """Persist receipt to local SQLite WAL outbox first, then deliver with HMAC signing."""
        idem_key = (
            f"receipt:{receipt.task_id}:{receipt.book_id}:{receipt.chapter}:{receipt.status}"
        )
        return self._enqueue_and_deliver(
            API_COMPANION_RECEIPT,
            receipt.to_dict(),
            idempotency_key=idem_key,
            flush_pending_first=flush_pending_first,
        )

    def send_metrics(
        self,
        metrics: CompanionMetricsRequest,
        *,
        flush_pending_first: bool = True,
    ) -> OutboxDeliveryResult:
        """Persist T-1 metrics & decoded hotlist to outbox first, then deliver with HMAC signing."""
        idem_key = (
            f"metrics:{metrics.book_id}:{metrics.account_id}:"
            f"{metrics.metrics_date or metrics.collected_at}"
        )
        return self._enqueue_and_deliver(
            API_COMPANION_METRICS,
            metrics.to_dict(),
            idempotency_key=idem_key,
            flush_pending_first=flush_pending_first,
        )

    def send_captcha_report(
        self,
        report: CompanionCaptchaReport,
        *,
        flush_pending_first: bool = False,
    ) -> OutboxDeliveryResult:
        """Persist captcha/login pause screenshot report to outbox first, then deliver immediately."""
        idem_key = (
            f"captcha:{report.account_id}:{report.book_id}:{report.chapter}:{report.detected_at}"
        )
        return self._enqueue_and_deliver(
            API_COMPANION_CAPTCHA,
            report.to_dict(),
            idempotency_key=idem_key,
            flush_pending_first=flush_pending_first,
        )

    def notify_resume(self, request: CompanionResumeRequest) -> Dict[str, Any]:
        """Notify cloud server that an account has resumed after manual verification."""
        _, data = self._request_json(
            "POST",
            API_COMPANION_RESUME,
            request.to_dict(),
            sign_hmac=True,
        )
        return data

    def flush_outbox(self, *, limit: int = 100) -> OutboxFlushResult:
        """Flush pending outbox items in FIFO order on reconnect."""
        pending = self.outbox.list_pending(limit=limit)
        sent = 0
        failed = 0
        errors: List[str] = []

        for item in pending:
            try:
                payload = item.payload_dict()
            except json.JSONDecodeError as exc:
                err_msg = f"Corrupt outbox JSON id={item.id}: {exc}"
                self.outbox.mark_attempt_failed(item.id, err_msg)
                failed += 1
                errors.append(err_msg)
                continue

            try:
                self._request_json(
                    "POST",
                    item.endpoint_path,
                    payload,
                    sign_hmac=True,
                )
                self.outbox.mark_sent(item.id)
                sent += 1
            except (CompanionTransportError, CompanionAuthError) as exc:
                self.outbox.mark_attempt_failed(item.id, str(exc))
                failed += 1
                errors.append(f"id={item.id} ({item.endpoint_path}): {exc}")

        return OutboxFlushResult(
            attempted=len(pending),
            sent=sent,
            failed=failed,
            remaining_pending=self.outbox.count_pending(),
            errors=errors,
        )


__all__ = [
    "CompanionAuthError",
    "CompanionClientError",
    "CompanionConfigError",
    "CompanionHttpClient",
    "CompanionTransportError",
    "OutboxDeliveryResult",
    "OutboxFlushResult",
    "OutboxItem",
    "OutboxStore",
]
