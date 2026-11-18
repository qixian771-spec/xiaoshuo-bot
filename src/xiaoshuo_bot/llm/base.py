from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class LLMRequest:
    request_id: str
    chapter: int
    attempt: int
    purpose: str  # "draft" | "rewrite"
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


class LLMProvider(ABC):
    """Abstract interface for LLM inference backends."""

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def close(self) -> None:
        """Optional lifecycle cleanup hook."""
