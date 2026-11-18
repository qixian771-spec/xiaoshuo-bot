from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hmac
import os
import threading
import time
from typing import Callable, Dict, Mapping, Optional, Tuple

from ..contracts import (
    HMACReplayError,
    HMACSignatureError,
    HMACTimestampError,
    HMACVerificationError,
    HMAC_MAX_SKEW_SECONDS,
    verify_hmac_headers,
)
from ..models import SHANGHAI_TZ

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MIN_ADMIN_TOKEN_LENGTH = 24
DEFAULT_MAX_AUTH_FAILURES = 5
DEFAULT_LOCKOUT_SECONDS = 30 * 60


class AuthError(RuntimeError):
    """Base authentication/authorization error for the matrix server."""


class InsecureBindError(AuthError):
    """Raised when binding to a non-loopback interface without a strong >=24-char admin token."""


class AdminUnauthorizedError(AuthError):
    """Raised when Bearer token authentication fails."""


class AuthLockoutError(AuthError):
    """Raised when a client IP is temporarily banned after consecutive auth failures (HTTP 429)."""

    def __init__(
        self,
        message: str,
        *,
        client_ip: str,
        retry_after_seconds: int,
        locked_until_shanghai: str,
    ) -> None:
        super().__init__(message)
        self.client_ip = client_ip
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self.locked_until_shanghai = locked_until_shanghai


class CompanionUnauthorizedError(AuthError):
    """Raised when cloud-edge HMAC authentication fails or secret is unconfigured."""

    def __init__(self, message: str, *, error_code: str = "HMAC_UNAUTHORIZED") -> None:
        super().__init__(message)
        self.error_code = error_code


def normalize_bind_host(host: str) -> str:
    cleaned = (host or "").strip().lower()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return cleaned


def is_loopback_host(host: str) -> bool:
    return normalize_bind_host(host) in LOOPBACK_HOSTS


def resolve_admin_token(admin_token: Optional[str] = None) -> str:
    if admin_token is not None:
        return str(admin_token).strip()
    return (os.environ.get("XIAOSHUO_ADMIN_TOKEN") or "").strip()


def resolve_companion_secret(companion_secret: Optional[str] = None) -> str:
    if companion_secret is not None:
        return str(companion_secret).strip()
    return (os.environ.get("XIAOSHUO_COMPANION_SECRET") or "").strip()


def validate_bind_security(
    host: str,
    admin_token: Optional[str] = None,
    *,
    min_token_length: int = MIN_ADMIN_TOKEN_LENGTH,
) -> str:
    """Validate that non-loopback binds enforce a strong >=24-char XIAOSHUO_ADMIN_TOKEN."""
    norm_host = normalize_bind_host(host)
    if not norm_host:
        raise InsecureBindError("Bind host cannot be empty.")

    token = resolve_admin_token(admin_token)
    if norm_host not in LOOPBACK_HOSTS:
        if not token:
            raise InsecureBindError(
                f"Refusing to bind non-loopback host {host!r} without XIAOSHUO_ADMIN_TOKEN "
                f"(minimum {min_token_length} characters)."
            )
        if len(token) < min_token_length:
            raise InsecureBindError(
                f"Refusing to bind non-loopback host {host!r}: XIAOSHUO_ADMIN_TOKEN length "
                f"{len(token)} is below required minimum {min_token_length} characters."
            )
    return token


def _lookup_header_ci(headers: Mapping[str, str], target: str) -> str:
    if target in headers:
        return str(headers[target]).strip()
    target_lower = target.lower()
    for k, v in headers.items():
        if str(k).lower() == target_lower:
            return str(v).strip()
    return ""


@dataclass
class _IPFailureRecord:
    consecutive_failures: int = 0
    locked_until_mono: float = 0.0
    locked_until_shanghai: str = ""


class AdminAuthManager:
    """Constant-time Bearer token authenticator with per-IP 5-failure 30-min lockout."""

    def __init__(
        self,
        admin_token: str = "",
        *,
        max_failures: int = DEFAULT_MAX_AUTH_FAILURES,
        lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS,
        monotonic_fn: Optional[Callable[[], float]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if max_failures < 1:
            raise ValueError(f"max_failures must be >= 1, got {max_failures}")
        if lockout_seconds < 1:
            raise ValueError(f"lockout_seconds must be >= 1, got {lockout_seconds}")
        self._token = str(admin_token or "").strip()
        self.max_failures = int(max_failures)
        self.lockout_seconds = int(lockout_seconds)
        self._monotonic = monotonic_fn or time.monotonic
        self._now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self._lock = threading.Lock()
        self._records: Dict[str, _IPFailureRecord] = {}

    @property
    def auth_enabled(self) -> bool:
        return bool(self._token)

    def _shanghai_now(self) -> datetime:
        dt = self._now_fn()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(SHANGHAI_TZ)

    def _clear_if_expired_unlocked(self, rec: _IPFailureRecord, now_mono: float) -> None:
        if rec.locked_until_mono > 0.0 and now_mono >= rec.locked_until_mono:
            rec.consecutive_failures = 0
            rec.locked_until_mono = 0.0
            rec.locked_until_shanghai = ""

    def is_ip_locked(self, client_ip: str) -> bool:
        ip = (client_ip or "").strip() or "unknown"
        now_mono = self._monotonic()
        with self._lock:
            rec = self._records.get(ip)
            if rec is None:
                return False
            self._clear_if_expired_unlocked(rec, now_mono)
            return rec.locked_until_mono > now_mono

    def get_failure_count(self, client_ip: str) -> int:
        ip = (client_ip or "").strip() or "unknown"
        now_mono = self._monotonic()
        with self._lock:
            rec = self._records.get(ip)
            if rec is None:
                return 0
            self._clear_if_expired_unlocked(rec, now_mono)
            return rec.consecutive_failures

    def authenticate(self, headers: Mapping[str, str], client_ip: str) -> None:
        """Verify Authorization: Bearer <token> or enforce per-IP brute-force lockout."""
        if not self._token:
            return

        ip = (client_ip or "").strip() or "unknown"
        now_mono = self._monotonic()

        with self._lock:
            rec = self._records.setdefault(ip, _IPFailureRecord())
            self._clear_if_expired_unlocked(rec, now_mono)
            if rec.locked_until_mono > now_mono:
                remaining = int(max(1.0, rec.locked_until_mono - now_mono))
                raise AuthLockoutError(
                    f"IP {ip!r} is locked out until {rec.locked_until_shanghai} "
                    f"({remaining}s remaining) due to {self.max_failures} consecutive auth failures.",
                    client_ip=ip,
                    retry_after_seconds=remaining,
                    locked_until_shanghai=rec.locked_until_shanghai,
                )

        auth_header = _lookup_header_ci(headers, "Authorization")
        provided_token = ""
        if auth_header:
            parts = auth_header.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                provided_token = parts[1].strip()

        is_valid = bool(provided_token) and hmac.compare_digest(
            provided_token.encode("utf-8"),
            self._token.encode("utf-8"),
        )

        with self._lock:
            rec = self._records.setdefault(ip, _IPFailureRecord())
            now_mono = self._monotonic()
            self._clear_if_expired_unlocked(rec, now_mono)
            if is_valid:
                rec.consecutive_failures = 0
                rec.locked_until_mono = 0.0
                rec.locked_until_shanghai = ""
                return

            rec.consecutive_failures += 1
            if rec.consecutive_failures >= self.max_failures:
                rec.locked_until_mono = now_mono + float(self.lockout_seconds)
                until_dt = self._shanghai_now() + timedelta(seconds=self.lockout_seconds)
                rec.locked_until_shanghai = until_dt.isoformat()
                raise AuthLockoutError(
                    f"IP {ip!r} locked out for {self.lockout_seconds}s until "
                    f"{rec.locked_until_shanghai} after {rec.consecutive_failures} consecutive auth failures.",
                    client_ip=ip,
                    retry_after_seconds=self.lockout_seconds,
                    locked_until_shanghai=rec.locked_until_shanghai,
                )

        if not auth_header:
            raise AdminUnauthorizedError("Missing Authorization: Bearer <token> header.")
        if not provided_token:
            raise AdminUnauthorizedError("Malformed Authorization header; expected Bearer <token>.")
        raise AdminUnauthorizedError("Invalid admin Bearer token.")


class CompanionAuthManager:
    """Thread-safe HMAC-SHA256 + ±60s timestamp + anti-replay nonce verifier for companion routes."""

    def __init__(
        self,
        companion_secret: str = "",
        *,
        max_skew_seconds: int = HMAC_MAX_SKEW_SECONDS,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._secret = str(companion_secret or "").strip()
        self.max_skew_seconds = int(max_skew_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self._lock = threading.Lock()
        self._seen_nonces: Dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self._secret)

    def authenticate(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> Tuple[str, int, str]:
        if not self._secret:
            raise CompanionUnauthorizedError(
                "Companion HMAC secret is not configured; refusing unauthenticated edge request.",
                error_code="COMPANION_SECRET_UNCONFIGURED",
            )
        dt = self._now_fn()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        else:
            dt = dt.astimezone(SHANGHAI_TZ)
        now_ts = int(dt.timestamp())

        with self._lock:
            try:
                return verify_hmac_headers(
                    self._secret,
                    method,
                    path,
                    headers,
                    body,
                    now=now_ts,
                    max_skew_seconds=self.max_skew_seconds,
                    seen_nonces=self._seen_nonces,
                )
            except HMACTimestampError as exc:
                raise CompanionUnauthorizedError(str(exc), error_code="HMAC_TIMESTAMP_ERROR") from exc
            except HMACReplayError as exc:
                raise CompanionUnauthorizedError(str(exc), error_code="HMAC_REPLAY_ERROR") from exc
            except HMACSignatureError as exc:
                raise CompanionUnauthorizedError(str(exc), error_code="HMAC_SIGNATURE_ERROR") from exc
            except HMACVerificationError as exc:
                raise CompanionUnauthorizedError(str(exc), error_code="HMAC_VERIFICATION_ERROR") from exc


__all__ = [
    "AdminAuthManager",
    "AdminUnauthorizedError",
    "AuthError",
    "AuthLockoutError",
    "CompanionAuthManager",
    "CompanionUnauthorizedError",
    "DEFAULT_LOCKOUT_SECONDS",
    "DEFAULT_MAX_AUTH_FAILURES",
    "InsecureBindError",
    "LOOPBACK_HOSTS",
    "MIN_ADMIN_TOKEN_LENGTH",
    "is_loopback_host",
    "normalize_bind_host",
    "resolve_admin_token",
    "resolve_companion_secret",
    "validate_bind_security",
]
