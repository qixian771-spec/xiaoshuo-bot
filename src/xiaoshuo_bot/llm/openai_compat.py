from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import LLMProvider, LLMRequest, LLMResponse


class OpenAICompatibleProvider(LLMProvider):
    """Standard OpenAI Chat Completions provider (supports OpenAI, DeepSeek, OpenRouter, Qwen, vLLM, etc.)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 240,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("XIAOSHUO_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        raw_base = (
            base_url
            or os.environ.get("XIAOSHUO_API_BASE")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        self.base_url = raw_base.rstrip("/")
        self.model = model or os.environ.get("XIAOSHUO_MODEL") or "deepseek-chat"
        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "Missing API key for OpenAICompatibleProvider. "
                "Set XIAOSHUO_API_KEY or OPENAI_API_KEY, or pass --api-key."
            )

    def generate(self, request: LLMRequest) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        t0 = time.monotonic()
        last_err: Optional[Exception] = None
        for retry in range(3):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                content = resp_data["choices"][0]["message"]["content"]
                return LLMResponse(
                    request_id=request.request_id,
                    content=content,
                    model=resp_data.get("model", self.model),
                    provider="openai_compatible",
                    latency_sec=round(time.monotonic() - t0, 3),
                    usage=resp_data.get("usage", {}),
                )
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as exc:
                last_err = exc
                if retry < 2:
                    time.sleep(2 ** retry)
        raise RuntimeError(f"OpenAI-compatible request failed after 3 attempts: {last_err}")
