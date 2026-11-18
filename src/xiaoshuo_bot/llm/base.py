from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta, timezone
import re
from typing import Any, Dict, Iterable, Optional, Tuple
import urllib.parse

SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_MODEL_REASONING_RE = re.compile(r"^(?P<model>.+?)\((?P<effort>low|medium|high)\)$", re.IGNORECASE)
_BEARER_HEADER_RE = re.compile(r"(Authorization\s*:\s*Bearer\s+)\S+", re.IGNORECASE)
_BEARER_TOKEN_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._\-+/=]{4,}", re.IGNORECASE)
_API_KEY_HEADER_RE = re.compile(
    r"((?:x-api-key|x-goog-api-key|x-management-key)\s*:\s*)\S+",
    re.IGNORECASE,
)
_KNOWN_KEY_PREFIX_RE = re.compile(
    r"\b(?:sk|rk|cpa)-[A-Za-z0-9_\-]{6,}\b|\bAIza[A-Za-z0-9_\-]{6,}\b"
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|token|secret|management_key)=)[^&\s]+",
    re.IGNORECASE,
)


class LLMError(RuntimeError):
    """Base exception for all LLM gateway, router, and management errors."""


class LLMConfigurationError(LLMError, ValueError):
    """Raised when required provider, model, endpoint, or credential configuration is missing."""


class LLMStreamTruncatedError(LLMError):
    """Raised when an SSE stream disconnects prematurely or ends with finish_reason != 'stop'."""

    def __init__(
        self,
        message: str = "",
        *,
        usage: Optional[Dict[str, Any]] = None,
        partial_content: str = "",
        model: str = "",
    ) -> None:
        super().__init__(message)
        # Upstream-reported (or estimated) token usage for the paid-but-truncated completion.
        self.usage: Dict[str, Any] = dict(usage or {})
        self.partial_content: str = partial_content or ""
        self.model: str = model or ""


def estimate_usage_from_text(
    content: str, *, prompt_text: str = "", usage: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Return upstream usage if present, else a coarse char-based token estimate (1 char ~ 1 token)."""
    if usage:
        return dict(usage)
    return {
        "prompt_tokens": len(prompt_text or ""),
        "completion_tokens": len(content or ""),
        "estimated": True,
    }


class LLMRequestError(LLMError):
    """Raised on upstream HTTP/network errors with secrets redacted."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class QuotaExhaustedError(LLMError):
    """Raised when all credentials in a role pool are rate-limited (429); never downgrades model."""


class BudgetCircuitBreakerError(LLMError):
    """Raised when daily USD budget or daily chapter generation limit is reached/exceeded."""


class ImageGenerationError(LLMError):
    """Raised when an image generation request fails or yields no valid image payload."""


class CPAManagementError(LLMError):
    """Raised when a CPA /v0/management API request fails."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Mask API keys, Bearer tokens, and explicit secret values from logs or error messages."""
    if not text:
        return ""
    masked = text
    for raw_secret in extra_secrets:
        if not raw_secret:
            continue
        secret = str(raw_secret).strip()
        if len(secret) >= 4:
            masked = masked.replace(secret, "[REDACTED]")
            quoted = urllib.parse.quote(secret, safe="")
            if quoted and quoted != secret:
                masked = masked.replace(quoted, "[REDACTED]")
    masked = _BEARER_HEADER_RE.sub(r"\1[REDACTED]", masked)
    masked = _BEARER_TOKEN_RE.sub(r"\1[REDACTED]", masked)
    masked = _API_KEY_HEADER_RE.sub(r"\1[REDACTED]", masked)
    masked = _KNOWN_KEY_PREFIX_RE.sub("[REDACTED_KEY]", masked)
    masked = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", masked)
    return masked


def parse_model_reasoning_suffix(model: str) -> Tuple[str, Optional[str]]:
    """Parse '<model>(medium|high|low)' suffix into (base_model, effort)."""
    cleaned = (model or "").strip()
    match = _MODEL_REASONING_RE.match(cleaned)
    if not match:
        return cleaned, None
    return match.group("model").strip(), match.group("effort").lower()


@dataclass
class LLMRequest:
    request_id: str
    chapter: int
    attempt: int
    purpose: str  # "draft" | "rewrite" | "plan" | "critic" | "polish"
    system_prompt: str
    user_prompt: str
    temperature: float = 0.7
    max_tokens: int = 6000
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    request_id: str
    content: str
    model: str
    provider: str
    latency_sec: float = 0.0
    usage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageGenerationRequest:
    prompt: str
    request_id: str = "img"
    model: Optional[str] = None
    size: str = "768x1024"
    aspect_ratio: str = "3:4"
    n: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageGenerationResponse:
    request_id: str
    image_bytes: bytes
    mime_type: str
    model: str
    provider: str
    protocol: str  # "gemini_native" | "openai_images"
    text: str = ""
    latency_sec: float = 0.0
    usage: Dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """Abstract interface for LLM inference backends."""

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def close(self) -> None:
        """Optional lifecycle cleanup hook."""
