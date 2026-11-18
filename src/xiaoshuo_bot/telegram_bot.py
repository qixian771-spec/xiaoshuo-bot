from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import html
import http.client
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
import traceback
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .contracts import (
    AutoCreateBookRequest,
    BookRunStatus,
    BookSummaryResponse,
    CompanionCaptchaReport,
    MatrixControlPort,
    MatrixDailyDigest,
)
from .genres import UnknownGenreError, get_genre, list_genres
from .llm.base import redact_secrets
from .models import SHANGHAI_TZ

LOGGER = logging.getLogger(__name__)

DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT_SEC = 30
DEFAULT_READ_TIMEOUT_SEC = 45
MAX_CALLBACK_DATA_BYTES = 64
MAX_HTML_MESSAGE_CHARS = 3800
MAX_PHOTO_CAPTION_CHARS = 1024

_BOT_TOKEN_PATH_RE = re.compile(r"(/bot)[^/\s?]+", re.IGNORECASE)


class TelegramBotError(RuntimeError):
    """Base error for Telegram Bot configuration, API transport, and state-machine failures."""


class TelegramConfigError(TelegramBotError, ValueError):
    """Raised when required Telegram bot credentials, owner ID, or timeouts are missing/invalid."""


class TelegramAPIError(TelegramBotError):
    """Raised when Telegram Bot API returns an error or transport fails (secrets redacted)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class CallbackDataError(TelegramBotError, ValueError):
    """Raised when callback_data violates pure ASCII or <= 64 UTF-8 bytes constraint."""


def escape_html(text: Any) -> str:
    """Escape &, <, >, and " for Telegram HTML parse_mode."""
    raw = "" if text is None else str(text)
    return html.escape(raw, quote=False).replace('"', "&quot;")


def validate_callback_data(data: str) -> str:
    """Enforce pure ASCII non-empty callback_data within Telegram's 64-byte UTF-8 limit."""
    if not isinstance(data, str) or not data:
        raise CallbackDataError("callback_data must be a non-empty ASCII string")
    if any(ch.isspace() for ch in data):
        raise CallbackDataError(f"callback_data must not contain whitespace: {data!r}")
    if not data.isascii():
        raise CallbackDataError(f"callback_data must be pure ASCII: {data!r}")
    encoded_len = len(data.encode("utf-8"))
    if encoded_len > MAX_CALLBACK_DATA_BYTES:
        raise CallbackDataError(
            f"callback_data exceeds {MAX_CALLBACK_DATA_BYTES} bytes ({encoded_len} bytes): {data!r}"
        )
    return data


def make_inline_button(text: str, callback_data: str) -> Dict[str, str]:
    """Construct a Telegram inline keyboard button with validated callback_data."""
    label = str(text or "").strip()
    if not label:
        raise ValueError("Inline button text cannot be empty")
    return {
        "text": label,
        "callback_data": validate_callback_data(callback_data),
    }


def _redact_bot_secrets(text: str, bot_token: str) -> str:
    masked = redact_secrets(text, extra_secrets=[bot_token] if bot_token else ())
    masked = _BOT_TOKEN_PATH_RE.sub(r"\1[REDACTED]", masked)
    return masked


def _parse_owner_user_id(value: Any) -> int:
    if isinstance(value, bool):
        raise TelegramConfigError("owner_user_id must be a positive integer, not bool")
    if isinstance(value, int):
        uid = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise TelegramConfigError(
                "Missing owner_user_id: provide owner_user_id explicitly or set TELEGRAM_OWNER_USER_ID"
            )
        try:
            uid = int(stripped, 10)
        except ValueError as exc:
            raise TelegramConfigError(f"Invalid owner_user_id {stripped!r}: must be an integer") from exc
    else:
        raise TelegramConfigError(
            "Missing owner_user_id: provide owner_user_id explicitly or set TELEGRAM_OWNER_USER_ID"
        )
    if uid <= 0:
        raise TelegramConfigError(f"owner_user_id must be a positive integer, got {uid}")
    return uid


def _parse_bot_token(value: Optional[str]) -> str:
    raw = "" if value is None else str(value).strip()
    if not raw:
        raise TelegramConfigError(
            "Missing bot_token: provide bot_token explicitly or set TELEGRAM_BOT_TOKEN"
        )
    if any(ch.isspace() for ch in raw):
        raise TelegramConfigError("Invalid bot_token: token must not contain whitespace")
    return raw


def _parse_api_base_url(value: Optional[str]) -> str:
    if value is None:
        env_val = os.environ.get("TELEGRAM_API_BASE_URL", "").strip()
        raw = env_val if env_val else DEFAULT_TELEGRAM_API_BASE_URL
    else:
        raw = str(value).strip()
        if not raw:
            raise TelegramConfigError("api_base_url cannot be empty when explicitly provided")
    cleaned = raw.rstrip("/")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TelegramConfigError(f"Invalid Telegram api_base_url: {cleaned!r}")
    return cleaned


@dataclass(frozen=True)
class TelegramBotConfig:
    """Validated runtime configuration for TelegramControlBot."""

    bot_token: str = field(repr=False)
    owner_user_id: int
    api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL
    poll_timeout: int = DEFAULT_POLL_TIMEOUT_SEC
    read_timeout: int = DEFAULT_READ_TIMEOUT_SEC
    state_path: Optional[Path] = None
    max_message_chars: int = MAX_HTML_MESSAGE_CHARS

    def __repr__(self) -> str:
        return (
            f"TelegramBotConfig(bot_token='[REDACTED]', owner_user_id={self.owner_user_id!r}, "
            f"api_base_url={self.api_base_url!r}, poll_timeout={self.poll_timeout!r}, "
            f"read_timeout={self.read_timeout!r}, state_path={self.state_path!r}, "
            f"max_message_chars={self.max_message_chars!r})"
        )

    def __post_init__(self) -> None:
        token = _parse_bot_token(self.bot_token)
        uid = _parse_owner_user_id(self.owner_user_id)
        base_url = _parse_api_base_url(self.api_base_url)
        if isinstance(self.poll_timeout, bool) or not isinstance(self.poll_timeout, int) or self.poll_timeout < 0:
            raise TelegramConfigError(f"poll_timeout must be >= 0 integer, got {self.poll_timeout!r}")
        if isinstance(self.read_timeout, bool) or not isinstance(self.read_timeout, int) or self.read_timeout <= 0:
            raise TelegramConfigError(f"read_timeout must be > 0 integer, got {self.read_timeout!r}")
        if self.poll_timeout > 0 and self.read_timeout <= self.poll_timeout:
            raise TelegramConfigError(
                f"read_timeout ({self.read_timeout}s) must exceed poll_timeout ({self.poll_timeout}s)"
            )
        if self.max_message_chars < 200 or self.max_message_chars > 4096:
            raise TelegramConfigError(
                f"max_message_chars must be in [200, 4096], got {self.max_message_chars!r}"
            )
        state_p = Path(self.state_path) if self.state_path is not None else None
        object.__setattr__(self, "bot_token", token)
        object.__setattr__(self, "owner_user_id", uid)
        object.__setattr__(self, "api_base_url", base_url)
        object.__setattr__(self, "state_path", state_p)

    @classmethod
    def from_env(
        cls,
        *,
        bot_token: Optional[str] = None,
        owner_user_id: Optional[int | str] = None,
        api_base_url: Optional[str] = None,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT_SEC,
        read_timeout: int = DEFAULT_READ_TIMEOUT_SEC,
        state_path: Optional[Path | str] = None,
        max_message_chars: int = MAX_HTML_MESSAGE_CHARS,
    ) -> TelegramBotConfig:
        eff_token = bot_token if bot_token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        eff_owner: Any = (
            owner_user_id
            if owner_user_id is not None
            else os.environ.get("TELEGRAM_OWNER_USER_ID")
        )
        eff_base = _parse_api_base_url(api_base_url)
        return cls(
            bot_token=_parse_bot_token(eff_token),
            owner_user_id=_parse_owner_user_id(eff_owner),
            api_base_url=eff_base,
            poll_timeout=poll_timeout,
            read_timeout=read_timeout,
            state_path=Path(state_path) if state_path is not None else None,
            max_message_chars=max_message_chars,
        )


class TelegramHTTPSClient:
    """Native stdlib urllib HTTPS/HTTP client for Telegram Bot API (zero third-party deps)."""

    def __init__(
        self,
        bot_token: str,
        *,
        api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL,
        default_timeout: int = 15,
        read_timeout: int = DEFAULT_READ_TIMEOUT_SEC,
    ) -> None:
        self._bot_token = _parse_bot_token(bot_token)
        self.api_base_url = _parse_api_base_url(api_base_url)
        self.default_timeout = int(default_timeout)
        self.read_timeout = int(read_timeout)
        if self.default_timeout <= 0:
            raise TelegramConfigError("default_timeout must be > 0")
        if self.read_timeout <= 0:
            raise TelegramConfigError("read_timeout must be > 0")

    def _endpoint(self, method: str) -> str:
        clean = method.strip().lstrip("/")
        if not clean:
            raise ValueError("Telegram API method cannot be empty")
        return f"{self.api_base_url}/bot{self._bot_token}/{clean}"

    def _redact(self, text: str) -> str:
        return _redact_bot_secrets(text, self._bot_token)

    def _execute(
        self,
        method: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout: int,
    ) -> Any:
        url = self._endpoint(method)
        req = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            err_body = ""
            if exc.fp is not None:
                try:
                    err_body = exc.fp.read(2048).decode("utf-8", errors="replace")
                except (OSError, ValueError):
                    err_body = ""
            description = err_body.strip() or str(exc.reason or "HTTP error")
            err_code: Optional[int] = None
            if err_body.strip().startswith("{"):
                try:
                    parsed_err = json.loads(err_body)
                    if isinstance(parsed_err, dict):
                        description = str(parsed_err.get("description") or description)
                        if parsed_err.get("error_code") is not None:
                            err_code = int(parsed_err["error_code"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            safe_msg = self._redact(description)[:500]
            raise TelegramAPIError(
                f"Telegram API {method} failed with HTTP {exc.code}: {safe_msg}",
                status_code=exc.code,
                error_code=err_code,
            ) from None
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValueError,
        ) as exc:
            safe_err = self._redact(str(exc))
            raise TelegramAPIError(f"Telegram API {method} transport error: {safe_err}") from None

        if not raw:
            raise TelegramAPIError(f"Telegram API {method} returned empty response")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramAPIError(
                f"Telegram API {method} returned invalid JSON: {self._redact(str(exc))}"
            ) from None

        if not isinstance(payload, dict):
            raise TelegramAPIError(f"Telegram API {method} returned non-object JSON payload")
        if not payload.get("ok"):
            desc = self._redact(str(payload.get("description") or "unknown error"))
            raw_code = payload.get("error_code")
            err_code = int(raw_code) if isinstance(raw_code, int) else None
            raise TelegramAPIError(
                f"Telegram API {method} rejected request: {desc}",
                error_code=err_code,
            )
        return payload.get("result")

    def call_json(
        self,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[int] = None,
    ) -> Any:
        encoded = json.dumps(dict(payload or {}), ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        return self._execute(
            method,
            encoded,
            headers,
            timeout=int(timeout) if timeout is not None else self.default_timeout,
        )

    def call_multipart(
        self,
        method: str,
        fields: Mapping[str, str],
        files: Sequence[Tuple[str, str, bytes, str]],
        *,
        timeout: Optional[int] = None,
    ) -> Any:
        boundary = f"----XiaoshuoBotTG{uuid.uuid4().hex}"
        chunks: List[bytes] = []
        for key, value in fields.items():
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        for field_name, filename, content_bytes, mime_type in files:
            safe_name = filename.replace('"', "_")
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'
                    f"Content-Type: {mime_type}\r\n\r\n"
                ).encode("utf-8")
            )
            chunks.append(content_bytes)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        headers = {
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        return self._execute(
            method,
            body,
            headers,
            timeout=int(timeout) if timeout is not None else self.default_timeout,
        )

    def get_updates(
        self,
        *,
        offset: Optional[int] = None,
        timeout: int = DEFAULT_POLL_TIMEOUT_SEC,
        allowed_updates: Sequence[str] = ("message", "callback_query"),
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "timeout": int(timeout),
            "allowed_updates": list(allowed_updates),
        }
        if offset is not None:
            payload["offset"] = int(offset)
        effective_read_timeout = max(self.read_timeout, int(timeout) + 5)
        result = self.call_json("getUpdates", payload, timeout=effective_read_timeout)
        if not isinstance(result, list):
            raise TelegramAPIError("Telegram API getUpdates result must be a list")
        return [item for item in result if isinstance(item, dict)]

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> Any:
        payload: Dict[str, Any] = {
            "callback_query_id": str(callback_query_id),
            "show_alert": bool(show_alert),
        }
        if text:
            payload["text"] = str(text)[:200]
        return self.call_json("answerCallbackQuery", payload)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: Optional[Mapping[str, Any]] = None,
        disable_web_page_preview: bool = True,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": str(text),
            "parse_mode": parse_mode,
            "disable_web_page_preview": bool(disable_web_page_preview),
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        result = self.call_json("sendMessage", payload)
        return dict(result) if isinstance(result, dict) else {"result": result}

    def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        *,
        reply_markup: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        payload: Dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "reply_markup": dict(reply_markup) if reply_markup is not None else {"inline_keyboard": []},
        }
        return self.call_json("editMessageReplyMarkup", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        payload: Dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": str(text),
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return self.call_json("editMessageText", payload)

    def send_photo(
        self,
        chat_id: int,
        photo_bytes: bytes,
        *,
        filename: str = "captcha.png",
        mime_type: str = "image/png",
        caption: Optional[str] = None,
        parse_mode: str = "HTML",
        reply_markup: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not photo_bytes:
            raise ValueError("photo_bytes cannot be empty for sendPhoto")
        fields: Dict[str, str] = {
            "chat_id": str(int(chat_id)),
            "parse_mode": parse_mode,
        }
        if caption is not None:
            raw_caption = str(caption)
            if str(parse_mode or "").strip().upper() == "HTML":
                fields["caption"] = _clamp_html_safe(raw_caption, MAX_PHOTO_CAPTION_CHARS)
            else:
                fields["caption"] = raw_caption[:MAX_PHOTO_CAPTION_CHARS]
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(dict(reply_markup), ensure_ascii=False)
        result = self.call_multipart(
            "sendPhoto",
            fields,
            [("photo", filename, photo_bytes, mime_type)],
        )
        return dict(result) if isinstance(result, dict) else {"result": result}


_HTML_TOKEN_RE = re.compile(
    r"(?P<close></[a-zA-Z][a-zA-Z0-9-]*\s*>)"
    r"|(?P<open><[a-zA-Z][a-zA-Z0-9-]*(?:\s+[^>]*)?>)"
    r"|(?P<entity>&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);)"
    r"|(?P<newline>\n)"
)
_TAG_NAME_RE = re.compile(r"^</?([a-zA-Z][a-zA-Z0-9-]*)")


def _tokenize_html(text: str) -> List[Tuple[str, str, str]]:
    """Tokenize HTML into (kind, tag_name, raw) keeping tags and entities atomic."""
    tokens: List[Tuple[str, str, str]] = []
    pos = 0
    for match in _HTML_TOKEN_RE.finditer(text):
        start, end = match.span()
        if start > pos:
            for ch in text[pos:start]:
                tokens.append(("atom", "", ch))
        raw = match.group(0)
        if match.group("close") is not None:
            name_m = _TAG_NAME_RE.match(raw)
            tag_name = name_m.group(1).lower() if name_m else ""
            tokens.append(("close", tag_name, raw))
        elif match.group("open") is not None:
            if raw.rstrip().endswith("/>"):
                tokens.append(("atom", "", raw))
            else:
                name_m = _TAG_NAME_RE.match(raw)
                tag_name = name_m.group(1).lower() if name_m else ""
                tokens.append(("open", tag_name, raw))
        elif match.group("entity") is not None:
            tokens.append(("atom", "", raw))
        else:
            tokens.append(("newline", "", "\n"))
        pos = end
    if pos < len(text):
        for ch in text[pos:]:
            tokens.append(("atom", "", ch))
    return tokens


def _apply_html_tag(
    stack: Sequence[Tuple[str, str]],
    kind: str,
    tag_name: str,
    raw: str,
) -> List[Tuple[str, str]]:
    new_stack = list(stack)
    if kind == "open":
        new_stack.append((tag_name, raw))
    elif kind == "close":
        for idx in range(len(new_stack) - 1, -1, -1):
            if new_stack[idx][0] == tag_name:
                del new_stack[idx:]
                break
    return new_stack


def _closing_suffix(stack: Sequence[Tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _closing_suffix_len(stack: Sequence[Tuple[str, str]]) -> int:
    return sum(len(name) + 3 for name, _ in stack)


def _opening_prefix(stack: Sequence[Tuple[str, str]]) -> str:
    return "".join(raw for _, raw in stack)


def _clamp_html_safe(text: str, limit: int) -> str:
    """Truncate HTML text to <= limit chars without breaking tags/entities, closing open tags."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    tokens = _tokenize_html(text)
    out: List[str] = []
    out_len = 0
    stack: List[Tuple[str, str]] = []
    for kind, tag_name, raw in tokens:
        next_stack = _apply_html_tag(stack, kind, tag_name, raw)
        needed = out_len + len(raw) + _closing_suffix_len(next_stack)
        if needed > limit:
            break
        out.append(raw)
        out_len += len(raw)
        stack = next_stack
    return ("".join(out) + _closing_suffix(stack)).rstrip()


def _escape_and_clamp_raw(value: Any, max_escaped_len: int) -> str:
    """Escape text for HTML and clamp cleanly at entity boundaries to <= max_escaped_len."""
    escaped = escape_html(value)
    if max_escaped_len <= 0:
        return ""
    if len(escaped) <= max_escaped_len:
        return escaped
    if max_escaped_len <= 3:
        return _clamp_html_safe(escaped, max_escaped_len)
    head = _clamp_html_safe(escaped, max_escaped_len - 3)
    return f"{head}..."


def _split_html_safe(text: str, limit: int) -> List[str]:
    """Split HTML into <= limit chunks on newline/safe boundaries with balanced tags & entities."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    tokens = _tokenize_html(text)
    parts: List[str] = []
    open_stack: List[Tuple[str, str]] = []
    current_stack: List[Tuple[str, str]] = []
    chunk_prefix = ""
    chunk_tokens: List[str] = []
    chunk_body_len = 0
    chunk_has_content = False
    # (chunk_tokens_len_before_nl, stack_at_nl, next_token_idx_after_nl)
    last_nl: Optional[Tuple[int, List[Tuple[str, str]], int]] = None

    idx = 0
    n_tokens = len(tokens)
    while idx < n_tokens:
        kind, tag_name, raw = tokens[idx]
        next_stack = _apply_html_tag(current_stack, kind, tag_name, raw)
        projected_len = len(chunk_prefix) + chunk_body_len + len(raw) + _closing_suffix_len(next_stack)

        if projected_len > limit:
            if chunk_has_content:
                if last_nl is not None:
                    nl_tok_len, nl_stack, next_idx = last_nl
                    body = "".join(chunk_tokens[:nl_tok_len]).rstrip("\n")
                    rendered = (chunk_prefix + body + _closing_suffix(nl_stack)).strip()
                    if rendered:
                        parts.append(rendered)
                    open_stack = list(nl_stack)
                    current_stack = list(nl_stack)
                    chunk_prefix = _opening_prefix(open_stack)
                    chunk_tokens = []
                    chunk_body_len = 0
                    chunk_has_content = False
                    last_nl = None
                    idx = next_idx
                    continue

                rendered = (chunk_prefix + "".join(chunk_tokens) + _closing_suffix(current_stack)).rstrip("\n")
                if rendered.strip():
                    parts.append(rendered)
                open_stack = list(current_stack)
                chunk_prefix = _opening_prefix(open_stack)
                chunk_tokens = []
                chunk_body_len = 0
                chunk_has_content = False
                last_nl = None
                continue

            # Empty content in current chunk but even a single token overflows limit (pathological tag/prefix)
            if current_stack:
                open_stack = []
                current_stack = []
                chunk_prefix = ""
                chunk_tokens = []
                chunk_body_len = 0
                last_nl = None
                continue

            # Single non-tag atom longer than limit
            if len(raw) > limit:
                start = 0
                while start < len(raw):
                    parts.append(raw[start : start + limit])
                    start += limit
                idx += 1
                continue

        if kind == "newline" and chunk_has_content:
            last_nl = (len(chunk_tokens), list(next_stack), idx + 1)

        chunk_tokens.append(raw)
        chunk_body_len += len(raw)
        current_stack = next_stack
        if kind == "atom" and raw.strip():
            chunk_has_content = True
        idx += 1

    if chunk_tokens:
        tail = (chunk_prefix + "".join(chunk_tokens) + _closing_suffix(current_stack)).rstrip("\n")
        if tail.strip():
            parts.append(tail)
    return [p for p in parts if p]


def _split_oversized_text(text: str, limit: int) -> List[str]:
    """Split an oversized block cleanly without breaking HTML tags, entities, or <= limit."""
    return _split_html_safe(text, limit)


def paginate_html_chunks(
    sections: Sequence[str],
    *,
    header: str = "",
    footer: str = "",
    max_chars: int = MAX_HTML_MESSAGE_CHARS,
) -> List[str]:
    """Pack HTML sections into <= max_chars messages cleanly with page labels when >1 page."""
    if max_chars < 200:
        raise ValueError(f"max_chars must be >= 200, got {max_chars}")

    page_badge_reserve = 32
    effective_body_limit = max_chars - page_badge_reserve
    header_clean = header.strip()
    footer_clean = footer.strip()
    clean_sections = [s for s in sections if s and s.strip()]

    if not clean_sections:
        single = "\n".join(p for p in (header_clean, footer_clean) if p).strip()
        if not single:
            return []
        if len(single) <= max_chars:
            return [single]
        pages = _split_html_safe(single, effective_body_limit)
    else:
        expanded: List[str] = []
        for sec in clean_sections:
            expanded.extend(_split_html_safe(sec, effective_body_limit))

        pages = []
        current_parts: List[str] = []
        current_prefix = ""

        if header_clean:
            header_parts = _split_html_safe(header_clean, effective_body_limit)
            if len(header_parts) > 1:
                pages.extend(header_parts[:-1])
            tail_header = header_parts[-1] if header_parts else ""
            if tail_header:
                candidate_prefix = tail_header + "\n\n"
                if len(candidate_prefix) <= effective_body_limit:
                    current_prefix = candidate_prefix
                else:
                    pages.append(tail_header)

        def _current_len() -> int:
            body_len = sum(len(x) for x in current_parts) + max(0, len(current_parts) - 1) * 2
            return len(current_prefix) + body_len

        for part in expanded:
            sep = 2 if current_parts else 0
            if _current_len() + sep + len(part) > effective_body_limit:
                if current_parts:
                    pages.append((current_prefix + "\n\n".join(current_parts)).strip())
                    current_parts = [part]
                    current_prefix = ""
                else:
                    if current_prefix.strip():
                        pages.append(current_prefix.strip())
                    current_prefix = ""
                    current_parts = [part]
            else:
                current_parts.append(part)

        footer_consumed = False
        if current_parts:
            candidate = (current_prefix + "\n\n".join(current_parts)).strip()
            if footer_clean:
                footer_suffix = "\n\n" + footer_clean
                if len(candidate) + len(footer_suffix) <= effective_body_limit:
                    candidate = (candidate + footer_suffix).strip()
                    footer_consumed = True
            pages.append(candidate)
        elif current_prefix.strip():
            pages.append(current_prefix.strip())

        if footer_clean and not footer_consumed:
            pages.extend(_split_html_safe(footer_clean, effective_body_limit))

    if len(pages) <= 1:
        if not pages:
            return []
        if len(pages[0]) <= max_chars:
            return pages
        pages = _split_html_safe(pages[0], effective_body_limit)

    total = len(pages)
    labeled: List[str] = []
    for idx, page_body in enumerate(pages, start=1):
        badge = f"\n\n<i>📄 第 {idx}/{total} 页</i>"
        if len(page_body) + len(badge) <= max_chars:
            labeled.append(page_body + badge)
        else:
            allowed = max(0, max_chars - len(badge))
            clamped = _clamp_html_safe(page_body, allowed).rstrip()
            labeled.append(clamped + badge)
    return labeled


def _decode_base64_image(raw: str) -> bytes:
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("Empty screenshot_base64 payload")
    if cleaned.startswith("data:") and "," in cleaned:
        _, _, cleaned = cleaned.partition(",")
    try:
        data = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 screenshot payload: {exc}") from exc
    if not data:
        raise ValueError("Decoded screenshot bytes cannot be empty")
    return data


def _mime_to_extension(mime_type: str) -> str:
    lower = (mime_type or "").strip().lower()
    if "jpeg" in lower or "jpg" in lower:
        return "jpg"
    if "webp" in lower:
        return "webp"
    return "png"


class TelegramControlBot:
    """Native HTTPS long-polling Telegram Bot controlling MatrixControlPort."""

    def __init__(
        self,
        control_port: MatrixControlPort,
        config: Optional[TelegramBotConfig] = None,
        *,
        bot_token: Optional[str] = None,
        owner_user_id: Optional[int | str] = None,
        api_base_url: Optional[str] = None,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT_SEC,
        read_timeout: int = DEFAULT_READ_TIMEOUT_SEC,
        state_path: Optional[Path | str] = None,
        max_message_chars: int = MAX_HTML_MESSAGE_CHARS,
        client: Optional[TelegramHTTPSClient] = None,
    ) -> None:
        if not isinstance(control_port, MatrixControlPort):
            raise TypeError(
                "control_port must implement MatrixControlPort protocol "
                "(get_daily_digest, list_books, get_book_status, auto_create_book, ...)"
            )
        if config is None:
            self.config = TelegramBotConfig.from_env(
                bot_token=bot_token,
                owner_user_id=owner_user_id,
                api_base_url=api_base_url,
                poll_timeout=poll_timeout,
                read_timeout=read_timeout,
                state_path=state_path,
                max_message_chars=max_message_chars,
            )
        else:
            self.config = config

        self.control_port = control_port
        self.client = client or TelegramHTTPSClient(
            self.config.bot_token,
            api_base_url=self.config.api_base_url,
            read_timeout=self.config.read_timeout,
        )

        self._lock = threading.RLock()
        self._last_update_id: Optional[int] = None
        self._ref_to_id: Dict[str, str] = {}
        self._id_to_ref: Dict[str, str] = {}
        # cut_cas[book_id] = {"version": int, "status": "pending"|"confirmed"|"cancelled", "reason": str}
        self._cut_cas: Dict[str, Dict[str, Any]] = {}
        # promo_cas[book_id] = {"version": int, "consumed": bool}
        self._promo_cas: Dict[str, Dict[str, Any]] = {}
        # captcha_events[ev_id] = {"account_id": str, "book_id": str, "consumed": bool}
        self._captcha_events: Dict[str, Dict[str, Any]] = {}

        self._stop_event: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

        self._load_state()

    @property
    def last_update_id(self) -> Optional[int]:
        with self._lock:
            return self._last_update_id

    def next_offset(self) -> Optional[int]:
        with self._lock:
            return None if self._last_update_id is None else self._last_update_id + 1

    def _load_state(self) -> None:
        path = self.config.state_path
        if path is None or not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise TelegramBotError(
                f"Failed to load Telegram bot state from '{path}': {self._redact(str(exc))}"
            ) from exc
        if not isinstance(data, dict):
            raise TelegramBotError(f"Corrupted Telegram bot state file '{path}': expected JSON object")

        with self._lock:
            uid = data.get("last_update_id")
            if uid is not None:
                if isinstance(uid, bool) or not isinstance(uid, int):
                    raise TelegramBotError(f"Invalid last_update_id in '{path}': {uid!r}")
                self._last_update_id = uid
            refs = data.get("refs")
            if isinstance(refs, dict):
                for ref, full_id in refs.items():
                    r_str = str(ref)
                    f_str = str(full_id)
                    self._ref_to_id[r_str] = f_str
                    self._id_to_ref[f_str] = r_str
            cut_cas = data.get("cut_cas")
            if isinstance(cut_cas, dict):
                self._cut_cas = {str(k): dict(v) for k, v in cut_cas.items() if isinstance(v, dict)}
            promo_cas = data.get("promo_cas")
            if isinstance(promo_cas, dict):
                self._promo_cas = {str(k): dict(v) for k, v in promo_cas.items() if isinstance(v, dict)}
            captcha_events = data.get("captcha_events")
            if isinstance(captcha_events, dict):
                self._captcha_events = {
                    str(k): dict(v) for k, v in captcha_events.items() if isinstance(v, dict)
                }

    def _save_state(self) -> None:
        path = self.config.state_path
        if path is None:
            return
        with self._lock:
            snapshot = {
                "last_update_id": self._last_update_id,
                "updated_at": datetime.now(SHANGHAI_TZ).isoformat(),
                "refs": dict(self._ref_to_id),
                "cut_cas": {k: dict(v) for k, v in self._cut_cas.items()},
                "promo_cas": {k: dict(v) for k, v in self._promo_cas.items()},
                "captcha_events": {k: dict(v) for k, v in self._captcha_events.items()},
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)

    def _redact(self, text: str) -> str:
        return _redact_bot_secrets(text, self.config.bot_token)

    def _compact_ref(self, entity_id: str) -> str:
        raw = str(entity_id).strip()
        if not raw:
            raise ValueError("entity_id cannot be empty for callback ref")
        with self._lock:
            existing = self._id_to_ref.get(raw)
            if existing:
                return existing
            if raw.isascii() and len(raw) <= 24 and ":" not in raw and not any(c.isspace() for c in raw):
                ref = raw
            else:
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
                ref = f"r_{digest}"
            self._id_to_ref[raw] = ref
            self._ref_to_id[ref] = raw
            self._save_state()
            return ref

    def _resolve_ref(self, ref: str) -> str:
        with self._lock:
            return self._ref_to_id.get(ref, ref)

    def _is_owner_user(self, user_obj: Any) -> bool:
        if not isinstance(user_obj, Mapping):
            return False
        uid = user_obj.get("id")
        if isinstance(uid, bool) or not isinstance(uid, int):
            return False
        return uid == self.config.owner_user_id

    def _send_html_chunks(
        self,
        sections: Sequence[str],
        *,
        header: str = "",
        footer: str = "",
        reply_markup: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        chunks = paginate_html_chunks(
            sections,
            header=header,
            footer=footer,
            max_chars=self.config.max_message_chars,
        )
        if not chunks:
            return []
        sent: List[Dict[str, Any]] = []
        last_idx = len(chunks) - 1
        for idx, chunk in enumerate(chunks):
            markup = reply_markup if idx == last_idx else None
            res = self.client.send_message(
                self.config.owner_user_id,
                chunk,
                parse_mode="HTML",
                reply_markup=markup,
            )
            sent.append(res)
        return sent

    # -----------------------------------------------------------------------
    # Formatting & Proactive Push APIs (consumed by T15 & command handlers)
    # -----------------------------------------------------------------------
    def _format_book_section(self, b: BookSummaryResponse) -> str:
        cushion_flag = " ⚠️低水位" if b.low_cushion_alert else ""
        paused_note = f"\n  原因：<code>{escape_html(b.paused_reason)}</code>" if b.paused_reason else ""
        return (
            f"• <b>{escape_html(b.title)}</b> (<code>{escape_html(b.book_id)}</code>)\n"
            f"  状态：<code>{escape_html(b.status)}</code> | 轨道：{escape_html(b.contract_track)} ({escape_html(b.author_level)})\n"
            f"  存稿：<b>{b.stockpiled_count}/{b.safety_cushion_target}</b> 章{cushion_flag} | "
            f"已发至第 {b.max_published_chapter}/{b.total_chapters} 章 | 日更目标 {b.daily_target_chapters} 章"
            f"{paused_note}"
        )

    def send_daily_digest(self, digest: Optional[MatrixDailyDigest] = None) -> List[Dict[str, Any]]:
        data = digest if digest is not None else self.control_port.get_daily_digest()
        header = (
            f"<b>📊 矩阵日报</b>（<code>{escape_html(data.generated_at)}</code>）\n"
            f"在册书目：<b>{data.total_books}</b> | 运行中：<b>{data.active_books}</b> | "
            f"低安全垫：<b>{data.low_cushion_books}</b> | 异常隔离：<b>{data.isolated_books}</b>\n"
            f"今日产字：<b>{data.daily_chars_generated}</b> 字 | 预估成本：<b>${data.daily_cost_usd:.4f}</b>"
        )
        sections: List[str] = []
        if data.paused_captcha_accounts:
            accts = ", ".join(escape_html(a) for a in data.paused_captcha_accounts)
            sections.append(f"<b>🛑 验证码挂起账号</b>：{accts}")
        if data.alerts:
            alert_lines = ["<b>🚨 告警</b>"] + [f"• {escape_html(a)}" for a in data.alerts]
            sections.append("\n".join(alert_lines))
        for b in data.book_summaries:
            sections.append(self._format_book_section(b))
        if data.pending_cut_recommendations:
            rec_lines = ["<b>🔪 止损切书建议</b>"]
            for rec in data.pending_cut_recommendations:
                bid = str(rec.get("book_id") or "")
                btitle = str(rec.get("title") or bid)
                reason = str(rec.get("reason") or "留存未达阈值")
                rec_lines.append(
                    f"• <b>{escape_html(btitle)}</b> (<code>{escape_html(bid)}</code>)：{escape_html(reason)}"
                )
            sections.append("\n".join(rec_lines))

        keyboard_rows: List[List[Dict[str, str]]] = []
        for rec in data.pending_cut_recommendations[:5]:
            bid = str(rec.get("book_id") or "").strip()
            if bid:
                bref = self._compact_ref(bid)
                btitle = str(rec.get("title") or bid)[:12]
                keyboard_rows.append(
                    [make_inline_button(f"🔪 切书止损 {btitle}", f"cut_ask:{bref}")]
                )
        reply_markup = {"inline_keyboard": keyboard_rows} if keyboard_rows else None
        return self._send_html_chunks(sections, header=header, reply_markup=reply_markup)

    def push_cut_recommendation(
        self,
        book_id: str,
        *,
        title: str = "",
        reason: str = "",
        stats_summary: str = "",
    ) -> Dict[str, Any]:
        bref = self._compact_ref(book_id)
        display_title = title or book_id
        lines = [
            f"<b>⚠️ 建议止损切书</b>",
            f"书名：<b>{escape_html(display_title)}</b> (<code>{escape_html(book_id)}</code>)",
        ]
        if reason:
            lines.append(f"原因：{escape_html(reason)}")
        if stats_summary:
            lines.append(f"数据：{escape_html(stats_summary)}")
        lines.append("点击下方按钮将进入二级确认，不会立即切书。")
        markup = {
            "inline_keyboard": [
                [make_inline_button("🔪 切书止损", f"cut_ask:{bref}")],
            ]
        }
        return self.client.send_message(
            self.config.owner_user_id,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=markup,
        )

    def push_promotion_alert(
        self,
        book_id: str,
        *,
        title: str = "",
        stats_summary: str = "",
    ) -> Dict[str, Any]:
        bref = self._compact_ref(book_id)
        with self._lock:
            prev = int(self._promo_cas.get(book_id, {}).get("version", 0))
            ver = prev + 1
            self._promo_cas[book_id] = {"version": ver, "consumed": False}
        self._save_state()

        display_title = title or book_id
        lines = [
            f"<b>🚀 潜力书晋级建议</b>",
            f"书名：<b>{escape_html(display_title)}</b> (<code>{escape_html(book_id)}</code>)",
        ]
        if stats_summary:
            lines.append(f"指标：{escape_html(stats_summary)}")
        markup = {
            "inline_keyboard": [
                [make_inline_button("🚀 批准晋级加更", f"apr:{bref}:{ver}")],
            ]
        }
        return self.client.send_message(
            self.config.owner_user_id,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=markup,
        )

    def push_captcha_alert(self, report: CompanionCaptchaReport) -> Dict[str, Any]:
        ev_id = secrets.token_hex(4)
        with self._lock:
            self._captcha_events[ev_id] = {
                "account_id": report.account_id,
                "book_id": report.book_id,
                "consumed": False,
            }
        self._save_state()

        acct_ref = self._compact_ref(report.account_id)
        markup = {
            "inline_keyboard": [
                [
                    make_inline_button(
                        "✅ 已在电脑完成验证，恢复运行",
                        f"cap_ok:{acct_ref}:{ev_id}",
                    )
                ]
            ]
        }
        safe_acct = _escape_and_clamp_raw(report.account_id, 80)
        safe_book = _escape_and_clamp_raw(report.book_id or "-", 80)
        safe_time = _escape_and_clamp_raw(report.detected_at, 64)

        raw_url = (report.page_url or "").strip()
        safe_url = _escape_and_clamp_raw(raw_url, 180) if raw_url else ""
        url_line = f"\n页面：<code>{safe_url}</code>" if safe_url else ""

        prefix_block = (
            f"<b>🛑 本地发布伴侣已急停挂起</b>\n"
            f"账号：<code>{safe_acct}</code>\n"
            f"书目：<code>{safe_book}</code> | 第 {report.chapter} 章\n"
            f"原因：<code>"
        )
        suffix_block = f"</code>\n时间：<code>{safe_time}</code>"
        reason_budget = MAX_PHOTO_CAPTION_CHARS - len(prefix_block) - len(suffix_block) - len(url_line)
        if reason_budget < 64 and url_line:
            url_line = ""
            reason_budget = MAX_PHOTO_CAPTION_CHARS - len(prefix_block) - len(suffix_block)
        safe_reason = _escape_and_clamp_raw(report.reason, max(16, reason_budget))
        caption = f"{prefix_block}{safe_reason}{suffix_block}{url_line}"
        if len(caption) > MAX_PHOTO_CAPTION_CHARS:
            caption = _clamp_html_safe(caption, MAX_PHOTO_CAPTION_CHARS)

        if report.screenshot_base64 and report.screenshot_base64.strip():
            photo_bytes = _decode_base64_image(report.screenshot_base64)
            ext = _mime_to_extension(report.screenshot_mime)
            return self.client.send_photo(
                self.config.owner_user_id,
                photo_bytes,
                filename=f"captcha_{ev_id}.{ext}",
                mime_type=report.screenshot_mime or "image/png",
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )

        return self.client.send_message(
            self.config.owner_user_id,
            caption,
            parse_mode="HTML",
            reply_markup=markup,
        )

    def push_alert(self, title: str, body: str) -> List[Dict[str, Any]]:
        header = f"<b>{escape_html(title)}</b>"
        return self._send_html_chunks([escape_html(body)], header=header)

    # -----------------------------------------------------------------------
    # Polling & Update Dispatch
    # -----------------------------------------------------------------------
    def poll_once(self) -> int:
        updates = self.client.get_updates(
            offset=self.next_offset(),
            timeout=self.config.poll_timeout,
            allowed_updates=("message", "callback_query"),
        )
        handled_count = 0
        for upd in updates:
            if self.process_update(upd):
                handled_count += 1
        return handled_count

    def process_update(self, update: Mapping[str, Any]) -> bool:
        if not isinstance(update, Mapping):
            return False
        raw_update_id = update.get("update_id")
        if isinstance(raw_update_id, bool) or not isinstance(raw_update_id, int):
            return False

        with self._lock:
            if self._last_update_id is not None and raw_update_id <= self._last_update_id:
                return False

        handled = False
        try:
            if "callback_query" in update and isinstance(update["callback_query"], Mapping):
                handled = self._handle_callback_query(update["callback_query"])
            elif "message" in update and isinstance(update["message"], Mapping):
                handled = self._handle_message(update["message"])
        finally:
            with self._lock:
                if self._last_update_id is None or raw_update_id > self._last_update_id:
                    self._last_update_id = raw_update_id
            self._save_state()

        return handled

    def _handle_message(self, message: Mapping[str, Any]) -> bool:
        # First-line owner authentication: non-owner messages silently dropped with ZERO reply
        if not self._is_owner_user(message.get("from")):
            return False

        text = str(message.get("text") or "").strip()
        if not text:
            return True

        tokens = text.split()
        raw_cmd = tokens[0]
        if not raw_cmd.startswith("/"):
            self._send_help()
            return True

        cmd = raw_cmd.split("@", 1)[0].lower()
        args = tokens[1:]

        if cmd in {"/start", "/help"}:
            self._send_help()
        elif cmd == "/status":
            self._cmd_status(args)
        elif cmd == "/digest":
            self.send_daily_digest()
        elif cmd == "/new":
            self._cmd_new(args)
        elif cmd == "/pause":
            self._cmd_pause(args)
        elif cmd == "/resume":
            self._cmd_resume(args)
        else:
            self._send_html_chunks(
                [f"未知指令：<code>{escape_html(cmd)}</code>\n请发送 /start 查看可用指令。"]
            )
        return True

    def _send_help(self) -> None:
        text = (
            "<b>🤖 番茄爆款矩阵远程控制台</b>\n"
            "• /status [book_id] — 查看矩阵总览或单书详情\n"
            "• /digest — 推送今日矩阵战报（自动分页）\n"
            "• /new [genre_id] [lv0_1|lv2_plus] — 一键立项开新书\n"
            "• /pause &lt;book_id&gt; [原因] — 暂停指定书目生产\n"
            "• /resume &lt;book_id&gt; — 恢复指定书目生产"
        )
        markup = {
            "inline_keyboard": [
                [
                    make_inline_button("📊 今日战报", "cmd:digest"),
                    make_inline_button("📚 矩阵状态", "cmd:status"),
                ],
                [make_inline_button("✨ 一键开新书", "cmd:new")],
            ]
        }
        self.client.send_message(
            self.config.owner_user_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )

    def _cmd_status(self, args: Sequence[str]) -> None:
        if args:
            book_id = args[0].strip()
            try:
                book = self.control_port.get_book_status(book_id)
            except (KeyError, ValueError) as exc:
                self._send_html_chunks(
                    [f"<b>❌ 查询书目失败</b>：<code>{escape_html(str(exc))}</code>"]
                )
                return
            bref = self._compact_ref(book.book_id)
            section = self._format_book_section(book)
            if book.status == BookRunStatus.ACTIVE.value:
                row = [
                    make_inline_button("⏸️ 暂停生产", f"pause:{bref}"),
                    make_inline_button("🔪 切书止损", f"cut_ask:{bref}"),
                ]
            else:
                row = [
                    make_inline_button("▶️ 恢复生产", f"resume:{bref}"),
                    make_inline_button("🔪 切书止损", f"cut_ask:{bref}"),
                ]
            self._send_html_chunks(
                [section],
                header="<b>📘 单书状态详情</b>",
                reply_markup={"inline_keyboard": [row]},
            )
            return

        books = self.control_port.list_books()
        if not books:
            self._send_html_chunks(["当前矩阵暂无在册书目，发送 /new 一键开新书。"])
            return
        sections = [self._format_book_section(b) for b in books]
        self._send_html_chunks(sections, header=f"<b>📚 矩阵状态总览（共 {len(books)} 本）</b>")

    def _send_new_book_menu(self) -> None:
        genres = list_genres()
        rows: List[List[Dict[str, str]]] = []
        current_row: List[Dict[str, str]] = []
        for g in genres:
            tag = "男" if g.channel == "male" else ("女" if g.channel == "female" else "通")
            btn = make_inline_button(f"[{tag}] {g.display_name}", f"new:{g.genre_id}")
            current_row.append(btn)
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)
        self.client.send_message(
            self.config.owner_user_id,
            "<b>✨ 请选择要一键自动立项的爆款赛道：</b>",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": rows},
        )

    def _create_book_for_genre(
        self,
        genre_id: str,
        *,
        author_level: str = "lv0_1",
        account_id: str = "",
    ) -> None:
        try:
            recipe = get_genre(genre_id)
        except UnknownGenreError as exc:
            valid = ", ".join(g.genre_id for g in list_genres())
            self._send_html_chunks(
                [
                    f"<b>❌ 未知赛道</b>：<code>{escape_html(str(exc))}</code>\n"
                    f"可选赛道：<code>{escape_html(valid)}</code>"
                ]
            )
            return

        stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d%H%M%S")
        project_id = f"{recipe.genre_id}_{stamp}_{secrets.token_hex(2)}"
        req = AutoCreateBookRequest(
            project_id=project_id,
            genre_id=recipe.genre_id,
            author_level=author_level,
            account_id=account_id,
        )
        created = self.control_port.auto_create_book(req)
        self._send_html_chunks(
            [self._format_book_section(created)],
            header=f"<b>✅ 新书立项成功（{escape_html(recipe.display_name)}）</b>",
        )

    def _cmd_new(self, args: Sequence[str]) -> None:
        if not args:
            self._send_new_book_menu()
            return
        genre_id = args[0].strip()
        author_level = args[1].strip() if len(args) >= 2 else "lv0_1"
        account_id = args[2].strip() if len(args) >= 3 else ""
        try:
            self._create_book_for_genre(
                genre_id,
                author_level=author_level,
                account_id=account_id,
            )
        except ValueError as exc:
            self._send_html_chunks([f"<b>❌ 立项参数无效</b>：<code>{escape_html(str(exc))}</code>"])

    def _cmd_pause(self, args: Sequence[str]) -> None:
        if not args:
            self._send_html_chunks(["用法：<code>/pause &lt;book_id&gt; [原因]</code>"])
            return
        book_id = args[0].strip()
        reason = " ".join(args[1:]).strip() or "Telegram owner paused"
        try:
            res = self.control_port.pause_book(book_id, reason=reason)
        except (KeyError, ValueError) as exc:
            self._send_html_chunks(
                [f"<b>❌ 暂停书目失败</b>：<code>{escape_html(str(exc))}</code>"]
            )
            return
        self._send_html_chunks(
            [
                f"<b>⏸️ 已暂停书目</b>：<code>{escape_html(book_id)}</code>\n"
                f"结果：<code>{escape_html(json.dumps(res, ensure_ascii=False))}</code>"
            ]
        )

    def _cmd_resume(self, args: Sequence[str]) -> None:
        if not args:
            self._send_html_chunks(["用法：<code>/resume &lt;book_id&gt;</code>"])
            return
        book_id = args[0].strip()
        try:
            res = self.control_port.resume_book(book_id)
        except (KeyError, ValueError) as exc:
            self._send_html_chunks(
                [f"<b>❌ 恢复书目失败</b>：<code>{escape_html(str(exc))}</code>"]
            )
            return
        self._send_html_chunks(
            [
                f"<b>▶️ 已恢复书目</b>：<code>{escape_html(book_id)}</code>\n"
                f"结果：<code>{escape_html(json.dumps(res, ensure_ascii=False))}</code>"
            ]
        )

    # -----------------------------------------------------------------------
    # Callback State Machine (3 Strict Rules)
    # -----------------------------------------------------------------------
    def _handle_callback_query(self, cb: Mapping[str, Any]) -> bool:
        # Rule 0 (First-line auth): silently drop non-owner callback with ZERO outbound calls
        if not self._is_owner_user(cb.get("from")):
            return False

        cb_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "").strip()
        msg = cb.get("message") if isinstance(cb.get("message"), Mapping) else {}
        chat_id = self.config.owner_user_id
        msg_chat = msg.get("chat") if isinstance(msg.get("chat"), Mapping) else {}
        if isinstance(msg_chat.get("id"), int) and not isinstance(msg_chat.get("id"), bool):
            chat_id = int(msg_chat["id"])
        message_id = msg.get("message_id")
        has_message_id = isinstance(message_id, int) and not isinstance(message_id, bool)

        # Rule 2 (Immediate ACK <1s): MUST call answerCallbackQuery BEFORE any heavy work
        ack_text = "已收到指令"
        if data.startswith("cut_ask:"):
            ack_text = "请再次点击确认切书"
        elif data.startswith("cut_do:"):
            ack_text = "正在核验切书确认..."
        elif data.startswith("cut_no:"):
            ack_text = "已取消切书"
        elif data.startswith("cap_ok:"):
            ack_text = "正在恢复本地伴侣..."
        elif data.startswith("apr:"):
            ack_text = "正在执行晋级..."
        self.client.answer_callback_query(cb_id, text=ack_text)

        if not data:
            return True

        parts = data.split(":")
        action = parts[0]

        if action == "cmd" and len(parts) >= 2:
            sub = parts[1]
            if sub == "digest":
                self.send_daily_digest()
            elif sub == "status":
                self._cmd_status([])
            elif sub == "new":
                self._send_new_book_menu()
            return True

        if action == "new" and len(parts) >= 2:
            genre_id = parts[1]
            self._create_book_for_genre(genre_id)
            return True

        if action == "pause" and len(parts) >= 2:
            book_id = self._resolve_ref(parts[1])
            self._cmd_pause([book_id])
            return True

        if action == "resume" and len(parts) >= 2:
            book_id = self._resolve_ref(parts[1])
            self._cmd_resume([book_id])
            return True

        if action == "cut_ask" and len(parts) >= 2:
            self._on_cut_ask(parts[1], chat_id=chat_id, message_id=message_id if has_message_id else None)
            return True

        if action == "cut_do" and len(parts) >= 3:
            self._on_cut_do(
                parts[1],
                parts[2],
                chat_id=chat_id,
                message_id=message_id if has_message_id else None,
            )
            return True

        if action == "cut_no" and len(parts) >= 2:
            self._on_cut_cancel(
                parts[1],
                chat_id=chat_id,
                message_id=message_id if has_message_id else None,
            )
            return True

        if action == "apr" and len(parts) >= 3:
            self._on_approve_promotion(
                parts[1],
                parts[2],
                chat_id=chat_id,
                message_id=message_id if has_message_id else None,
            )
            return True

        if action == "cap_ok" and len(parts) >= 3:
            self._on_captcha_resume(
                parts[1],
                parts[2],
                chat_id=chat_id,
                message_id=message_id if has_message_id else None,
            )
            return True

        self._send_html_chunks([f"未识别的操作指令：<code>{escape_html(data)}</code>"])
        return True

    def _on_cut_ask(
        self,
        book_ref: str,
        *,
        chat_id: int,
        message_id: Optional[int],
    ) -> None:
        book_id = self._resolve_ref(book_ref)
        # First click MUST NOT call confirm_cut_book; request pending version and swap buttons in-place
        req_result = self.control_port.request_cut_book(book_id, reason="Telegram owner requested cut")
        with self._lock:
            current = self._cut_cas.get(book_id)
            local_next = (int(current.get("version", 0)) + 1) if current else 1
            port_ver = None
            if isinstance(req_result, Mapping):
                raw_v = req_result.get("confirmation_version") or req_result.get("version")
                if isinstance(raw_v, int) and not isinstance(raw_v, bool) and raw_v > 0:
                    port_ver = raw_v
            version = max(local_next, port_ver) if port_ver is not None else local_next
            self._cut_cas[book_id] = {
                "version": version,
                "status": "pending",
                "reason": "Telegram owner requested cut",
            }
        self._save_state()

        bref = self._compact_ref(book_id)
        confirm_markup = {
            "inline_keyboard": [
                [
                    make_inline_button("🔥 确认切书", f"cut_do:{bref}:{version}"),
                    make_inline_button("↩️ 取消", f"cut_no:{bref}"),
                ]
            ]
        }
        if message_id is not None:
            self.client.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup=confirm_markup,
            )
        else:
            self.client.send_message(
                chat_id,
                f"<b>⚠️ 二级防误触确认</b>\n确认止损切书 <code>{escape_html(book_id)}</code>？（v{version}）",
                parse_mode="HTML",
                reply_markup=confirm_markup,
            )

    def _on_cut_do(
        self,
        book_ref: str,
        raw_version: str,
        *,
        chat_id: int,
        message_id: Optional[int],
    ) -> None:
        book_id = self._resolve_ref(book_ref)
        try:
            ver = int(raw_version.lstrip("v"))
        except ValueError:
            return

        reason = "Telegram owner confirmed cut"
        should_execute = False
        with self._lock:
            entry = self._cut_cas.get(book_id)
            if (
                entry is not None
                and entry.get("status") == "pending"
                and int(entry.get("version", 0)) == ver
            ):
                entry["status"] = "confirmed"
                reason = str(entry.get("reason") or reason)
                should_execute = True

        if not should_execute:
            # Stale CAS version or duplicate replay click -> idempotent no-op
            return

        self._save_state()
        res = self.control_port.confirm_cut_book(book_id, confirmation_version=ver, reason=reason)
        if message_id is not None:
            self.client.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup={"inline_keyboard": []},
            )
        self.client.send_message(
            chat_id,
            (
                f"<b>🔥 已确认切书止损</b>：<code>{escape_html(book_id)}</code> (v{ver})\n"
                f"结果：<code>{escape_html(json.dumps(res, ensure_ascii=False))}</code>"
            ),
            parse_mode="HTML",
        )

    def _on_cut_cancel(
        self,
        book_ref: str,
        *,
        chat_id: int,
        message_id: Optional[int],
    ) -> None:
        book_id = self._resolve_ref(book_ref)
        should_cancel = False
        with self._lock:
            entry = self._cut_cas.get(book_id)
            if entry is not None and entry.get("status") == "pending":
                entry["status"] = "cancelled"
                should_cancel = True

        if not should_cancel:
            return

        self._save_state()
        self.control_port.cancel_cut_book(book_id)
        bref = self._compact_ref(book_id)
        if message_id is not None:
            self.client.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup={
                    "inline_keyboard": [
                        [make_inline_button("🔪 切书止损", f"cut_ask:{bref}")]
                    ]
                },
            )
        self.client.send_message(
            chat_id,
            f"<b>↩️ 已取消切书止损</b>：<code>{escape_html(book_id)}</code>",
            parse_mode="HTML",
        )

    def _on_approve_promotion(
        self,
        book_ref: str,
        raw_version: str,
        *,
        chat_id: int,
        message_id: Optional[int],
    ) -> None:
        book_id = self._resolve_ref(book_ref)
        try:
            ver = int(raw_version.lstrip("v"))
        except ValueError:
            return

        should_execute = False
        with self._lock:
            entry = self._promo_cas.get(book_id)
            if entry is None:
                # Allow direct one-shot approval if message originated externally, then record consumed
                self._promo_cas[book_id] = {"version": ver, "consumed": True}
                should_execute = True
            elif not entry.get("consumed") and int(entry.get("version", 0)) == ver:
                entry["consumed"] = True
                should_execute = True

        if not should_execute:
            return

        self._save_state()
        res = self.control_port.approve_promotion(book_id)
        if message_id is not None:
            self.client.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup={"inline_keyboard": []},
            )
        self.client.send_message(
            chat_id,
            (
                f"<b>🚀 已批准晋级加更</b>：<code>{escape_html(book_id)}</code>\n"
                f"结果：<code>{escape_html(json.dumps(res, ensure_ascii=False))}</code>"
            ),
            parse_mode="HTML",
        )

    def _on_captcha_resume(
        self,
        acct_ref: str,
        ev_id: str,
        *,
        chat_id: int,
        message_id: Optional[int],
    ) -> None:
        account_id = self._resolve_ref(acct_ref)
        book_id = ""
        should_execute = False
        with self._lock:
            ev = self._captcha_events.get(ev_id)
            if ev is None:
                self._captcha_events[ev_id] = {
                    "account_id": account_id,
                    "book_id": "",
                    "consumed": True,
                }
                should_execute = True
            elif not ev.get("consumed"):
                ev["consumed"] = True
                account_id = str(ev.get("account_id") or account_id)
                book_id = str(ev.get("book_id") or "")
                should_execute = True

        if not should_execute:
            return

        self._save_state()
        res = self.control_port.resume_companion(account_id, book_id=book_id)
        if message_id is not None:
            self.client.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup={"inline_keyboard": []},
            )
        self.client.send_message(
            chat_id,
            (
                f"<b>✅ 已通知本地伴侣恢复运行</b>\n"
                f"账号：<code>{escape_html(account_id)}</code>\n"
                f"结果：<code>{escape_html(json.dumps(res, ensure_ascii=False))}</code>"
            ),
            parse_mode="HTML",
        )

    # -----------------------------------------------------------------------
    # Background Polling Loop (for T15 server thread)
    # -----------------------------------------------------------------------
    def run_forever(
        self,
        *,
        stop_event: Optional[threading.Event] = None,
        error_backoff_sec: float = 2.0,
    ) -> None:
        signal = stop_event or threading.Event()
        while not signal.is_set():
            try:
                self.poll_once()
            except TelegramBotError as exc:
                LOGGER.warning("Telegram polling error: %s", self._redact(str(exc)))
                signal.wait(error_backoff_sec)
            except Exception as exc:
                LOGGER.error(
                    "Unexpected Telegram polling error: %s\n%s",
                    self._redact(str(exc)),
                    self._redact(traceback.format_exc()),
                )
                signal.wait(error_backoff_sec)

    def start_background(self, *, error_backoff_sec: float = 2.0) -> threading.Thread:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop_event = threading.Event()
            stop_ev = self._stop_event
            thread = threading.Thread(
                target=self.run_forever,
                kwargs={"stop_event": stop_ev, "error_backoff_sec": error_backoff_sec},
                name="xiaoshuo-telegram-bot",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return thread

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            stop_ev = self._stop_event
            thread = self._thread
        if stop_ev is not None:
            stop_ev.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None


__all__ = [
    "CallbackDataError",
    "DEFAULT_POLL_TIMEOUT_SEC",
    "DEFAULT_READ_TIMEOUT_SEC",
    "DEFAULT_TELEGRAM_API_BASE_URL",
    "MAX_CALLBACK_DATA_BYTES",
    "MAX_HTML_MESSAGE_CHARS",
    "MAX_PHOTO_CAPTION_CHARS",
    "TelegramAPIError",
    "TelegramBotConfig",
    "TelegramBotError",
    "TelegramConfigError",
    "TelegramControlBot",
    "TelegramHTTPSClient",
    "escape_html",
    "make_inline_button",
    "paginate_html_chunks",
    "validate_callback_data",
]
