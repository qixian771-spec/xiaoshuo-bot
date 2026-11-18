from __future__ import annotations

import json
import os
import time
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request

from .base import (
    LLMConfigurationError,
    LLMProvider,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMStreamTruncatedError,
    redact_secrets,
)


class AnthropicProvider(LLMProvider):
    """Native Anthropic Messages API provider (zero silent defaults)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        raw_key = api_key if api_key is not None else (os.environ.get("ANTHROPIC_API_KEY") or "")
        raw_base = base_url if base_url is not None else (os.environ.get("ANTHROPIC_BASE_URL") or "")
        raw_model = model if model is not None else (os.environ.get("ANTHROPIC_MODEL") or "")

        self.api_key = raw_key.strip()
        self.base_url = raw_base.strip().rstrip("/")
        self.model = raw_model.strip()
        self.timeout = int(timeout)

        if not self.api_key:
            raise LLMConfigurationError(
                "Missing API key for AnthropicProvider. "
                "Provide api_key explicitly or set ANTHROPIC_API_KEY."
            )
        if not self.base_url:
            raise LLMConfigurationError(
                "Missing base_url for AnthropicProvider. "
                "Provide base_url explicitly or set ANTHROPIC_BASE_URL."
            )
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LLMConfigurationError(
                f"Invalid base_url for AnthropicProvider: {redact_secrets(self.base_url, [self.api_key])!r}"
            )
        if not self.model:
            raise LLMConfigurationError(
                "Missing model for AnthropicProvider. "
                "Provide model explicitly or set ANTHROPIC_MODEL."
            )
        if self.timeout <= 0:
            raise LLMConfigurationError("timeout must be a positive integer.")

    def _messages_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/messages"
        return f"{self.base_url}/v1/messages"

    def _redact(self, text: str) -> str:
        return redact_secrets(text, extra_secrets=[self.api_key])

    def generate(self, request: LLMRequest) -> LLMResponse:
        url = self._messages_url()
        payload = {
            "model": str(request.metadata.get("model") or self.model).strip(),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.user_prompt}],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        t0 = time.monotonic()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raw_err = ""
            if exc.fp is not None:
                try:
                    raw_err = exc.fp.read(1024).decode("utf-8", errors="replace")
                except (OSError, ValueError):
                    raw_err = ""
            msg = self._redact(raw_err.strip() or str(exc.reason or exc))[:500]
            raise LLMRequestError(
                f"Anthropic HTTP {exc.code}: {msg}",
                status_code=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise LLMRequestError(
                f"Anthropic request error: {self._redact(str(exc))}"
            ) from None

        stop_reason = resp_data.get("stop_reason")
        if stop_reason == "max_tokens":
            raise LLMStreamTruncatedError(
                f"Anthropic response truncated (stop_reason='max_tokens') for {request.request_id}."
            )

        blocks = resp_data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        return LLMResponse(
            request_id=request.request_id,
            content=text,
            model=str(resp_data.get("model", self.model)),
            provider="anthropic",
            latency_sec=round(time.monotonic() - t0, 3),
            usage=dict(resp_data.get("usage") or {}),
        )
