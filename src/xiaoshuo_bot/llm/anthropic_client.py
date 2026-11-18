from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import LLMProvider, LLMRequest, LLMResponse


class AnthropicProvider(LLMProvider):
    """Native Anthropic Messages API provider."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 240,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        raw_base = base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        self.base_url = raw_base.rstrip("/")
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514"
        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "Missing API key for AnthropicProvider. Set ANTHROPIC_API_KEY or pass --api-key."
            )

    def generate(self, request: LLMRequest) -> LLMResponse:
        url = f"{self.base_url}/v1/messages"
        payload = {
            "model": self.model,
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
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
        blocks = resp_data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return LLMResponse(
            request_id=request.request_id,
            content=text,
            model=resp_data.get("model", self.model),
            provider="anthropic",
            latency_sec=round(time.monotonic() - t0, 3),
            usage=resp_data.get("usage", {}),
        )
