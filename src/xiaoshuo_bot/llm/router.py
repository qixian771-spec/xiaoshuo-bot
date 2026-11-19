from __future__ import annotations

import dataclasses

import base64
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.error
import urllib.parse
import urllib.request

from .anthropic_client import AnthropicProvider
from .base import (
    BudgetCircuitBreakerError,
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerationResponse,
    LLMConfigurationError,
    LLMProvider,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMStreamTruncatedError,
    QuotaExhaustedError,
    SHANGHAI_TZ,
    parse_model_reasoning_suffix,
    redact_secrets,
)
from .openai_compat import OpenAICompatibleProvider
from .workbuddy_policy import (
    DEFAULT_MODEL_QUOTA_COOLDOWN_SEC,
    DEFAULT_RATE_LIMIT_BACKOFF_SEC,
    ModelQuotaSnapshot,
    ModelSwitchEvent,
    RateLimitRetryEvent,
    WorkBuddyModelCatalog,
    classify_workbuddy_failure_kind,
    classify_workbuddy_quota_exhausted,
    extract_workbuddy_usage_quota,
    WORKBUDDY_REASONING_HEADROOM_TOKENS,
    parse_rate_limit_backoff_sec,
    validate_workbuddy_model_allowed,
    validate_workbuddy_rotation_chain,
)

VALID_ROLES: Tuple[str, ...] = ("planner", "writer", "critic", "polisher", "image")


def _extract_tokens(usage: Mapping[str, Any]) -> Tuple[int, int]:
    """Extract (input_tokens, output_tokens) across OpenAI, Anthropic, and Gemini schemas."""
    if not usage:
        return 0, 0
    in_tok = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("promptTokenCount")
        or 0
    )
    out_tok = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("candidatesTokenCount")
        or 0
    )
    return max(0, int(in_tok)), max(0, int(out_tok))


@dataclass
class RoleEndpointConfig:
    """Explicit endpoint, model, and same-tier credential pool binding for one role."""

    role: str
    base_url: str
    model: str
    api_keys: List[str] = field(default_factory=list)
    provider_type: str = "openai"  # "openai" | "anthropic" | "gemini_native" | "openai_images" | "auto"
    timeout: int = 300
    stream: bool = True
    reasoning_effort: Optional[str] = None
    strip_reasoning_suffix: bool = False
    cooldown_sec: float = 60.0
    input_cost_per_1m: float = 0.0
    output_cost_per_1m: float = 0.0
    image_cost_per_call: float = 0.0
    model_whitelist: Tuple[str, ...] = ()
    rotation_models: Tuple[str, ...] = ()
    manual_only_models: Tuple[str, ...] = ()
    model_cooldown_sec: Optional[float] = None
    rate_limit_backoff_sec: Optional[Tuple[float, ...]] = None
    model_labels: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.role = (self.role or "").strip()
        if self.role not in VALID_ROLES:
            raise LLMConfigurationError(
                f"Unknown role {self.role!r}; expected one of {VALID_ROLES}."
            )
        self.base_url = (self.base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise LLMConfigurationError(f"Missing base_url for role {self.role!r}.")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LLMConfigurationError(
                f"Invalid base_url for role {self.role!r}: {redact_secrets(self.base_url, self.api_keys)!r}"
            )
        self.model = (self.model or "").strip()
        if not self.model:
            raise LLMConfigurationError(f"Missing model for role {self.role!r}.")

        cleaned_keys = [str(k).strip() for k in self.api_keys if str(k).strip()]
        if not cleaned_keys:
            raise LLMConfigurationError(
                f"Missing api_keys pool for role {self.role!r}; at least one non-empty credential is required."
            )
        self.api_keys = cleaned_keys

        self.provider_type = (self.provider_type or "openai").strip().lower()
        if self.provider_type not in {"openai", "anthropic", "gemini_native", "openai_images", "auto"}:
            raise LLMConfigurationError(
                f"Unsupported provider_type {self.provider_type!r} for role {self.role!r}."
            )
        if self.timeout <= 0:
            raise LLMConfigurationError(f"timeout must be > 0 for role {self.role!r}.")
        if self.cooldown_sec < 0:
            raise LLMConfigurationError(f"cooldown_sec must be >= 0 for role {self.role!r}.")
        if self.model_cooldown_sec is not None:
            self.model_cooldown_sec = float(self.model_cooldown_sec)
            if self.model_cooldown_sec < 0:
                raise LLMConfigurationError(
                    f"model_cooldown_sec must be >= 0 for role {self.role!r}."
                )
        if self.rate_limit_backoff_sec is not None:
            self.rate_limit_backoff_sec = parse_rate_limit_backoff_sec(self.rate_limit_backoff_sec)

        def _normalize_str_tuple(raw_val: Any, field_name: str) -> Tuple[str, ...]:
            if raw_val is None:
                return ()
            if isinstance(raw_val, str) or not isinstance(raw_val, Sequence):
                raise LLMConfigurationError(
                    f"{field_name} for role {self.role!r} must be a sequence of model IDs."
                )
            return tuple(str(x).strip() for x in raw_val if str(x).strip())

        self.model_whitelist = _normalize_str_tuple(self.model_whitelist, "model_whitelist")
        self.rotation_models = _normalize_str_tuple(self.rotation_models, "rotation_models")
        self.manual_only_models = _normalize_str_tuple(self.manual_only_models, "manual_only_models")
        if self.model_labels:
            self.model_labels = {
                str(k).strip(): str(v).strip()
                for k, v in self.model_labels.items()
                if str(k).strip() and str(v).strip()
            }
        else:
            self.model_labels = {}

        if self.model_whitelist or self.rotation_models or self.manual_only_models:
            self.rotation_models = validate_workbuddy_rotation_chain(
                role=self.role,
                primary_model=self.model,
                rotation_models=self.rotation_models,
                whitelist=self.model_whitelist,
                manual_only_models=self.manual_only_models,
            )

    def effective_model_cooldown_sec(self) -> float:
        """Exhausted-model cooldown duration (defaults to 1 hour / 3600s, NOT 60s)."""
        if self.model_cooldown_sec is not None:
            return float(self.model_cooldown_sec)
        return float(DEFAULT_MODEL_QUOTA_COOLDOWN_SEC)

    def effective_rate_limit_backoff_sec(self) -> Tuple[float, ...]:
        """Same-model HTTP 429 retry backoff schedule (defaults to 30s, 60s, 120s)."""
        if self.rate_limit_backoff_sec is not None:
            return tuple(self.rate_limit_backoff_sec)
        return DEFAULT_RATE_LIMIT_BACKOFF_SEC

    def format_model_label(self, model_id: str) -> str:
        cleaned = (model_id or "").strip()
        label = self.model_labels.get(cleaned, "").strip()
        if not label or label == cleaned:
            return cleaned
        return f"{label} ({cleaned})"

    @classmethod
    def from_mapping(cls, role: str, data: Mapping[str, Any]) -> "RoleEndpointConfig":
        raw_keys: List[str] = []
        if "api_keys" in data and data["api_keys"] is not None:
            keys_val = data["api_keys"]
            if isinstance(keys_val, str):
                raw_keys = [keys_val]
            elif isinstance(keys_val, Sequence):
                raw_keys = [str(x) for x in keys_val]
            else:
                raise LLMConfigurationError(
                    f"api_keys for role {role!r} must be a list of strings."
                )
        elif "api_key" in data and data["api_key"] is not None:
            raw_keys = [str(data["api_key"])]

        raw_labels = data.get("model_labels")
        model_labels: Dict[str, str] = {}
        if isinstance(raw_labels, Mapping):
            model_labels = {str(k): str(v) for k, v in raw_labels.items()}

        return cls(
            role=role,
            base_url=str(data.get("base_url") or ""),
            model=str(data.get("model") or ""),
            api_keys=raw_keys,
            provider_type=str(data.get("provider_type") or ("auto" if role == "image" else "openai")),
            timeout=int(data.get("timeout", 300)),
            stream=bool(data.get("stream", True)),
            reasoning_effort=data.get("reasoning_effort"),
            strip_reasoning_suffix=bool(data.get("strip_reasoning_suffix", False)),
            cooldown_sec=float(data.get("cooldown_sec", 60.0)),
            input_cost_per_1m=float(data.get("input_cost_per_1m", 0.0)),
            output_cost_per_1m=float(data.get("output_cost_per_1m", 0.0)),
            image_cost_per_call=float(data.get("image_cost_per_call", 0.0)),
            model_whitelist=tuple(data.get("model_whitelist") or ()),
            rotation_models=tuple(data.get("rotation_models") or ()),
            manual_only_models=tuple(data.get("manual_only_models") or ()),
            model_cooldown_sec=(
                float(data["model_cooldown_sec"])
                if data.get("model_cooldown_sec") is not None
                else None
            ),
            rate_limit_backoff_sec=(
                parse_rate_limit_backoff_sec(data["rate_limit_backoff_sec"])
                if data.get("rate_limit_backoff_sec") is not None
                else None
            ),
            model_labels=model_labels,
        )


class UsageBudgetLedger:
    """Thread-safe Shanghai-timezone (UTC+8) token, cost, and chapter circuit-breaker ledger."""

    def __init__(
        self,
        daily_budget_limit: Optional[float] = None,
        daily_chapter_limit: Optional[int] = None,
        state_path: Optional[str | Path] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.daily_budget_limit = (
            float(daily_budget_limit) if daily_budget_limit is not None else None
        )
        self.daily_chapter_limit = (
            int(daily_chapter_limit) if daily_chapter_limit is not None else None
        )
        self.state_path = Path(state_path).resolve() if state_path else None
        self._now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self._lock = threading.Lock()
        self._daily: Dict[str, Dict[str, Any]] = {}

        if self.state_path and self.state_path.exists():
            self._load_unlocked()

    def _today_key(self) -> str:
        dt = self._now_fn()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        else:
            dt = dt.astimezone(SHANGHAI_TZ)
        return dt.strftime("%Y-%m-%d")

    def _ensure_bucket(self, date_key: str) -> Dict[str, Any]:
        bucket = self._daily.get(date_key)
        if bucket is None:
            bucket = {
                "date": date_key,
                "request_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
                "chapters": [],
                "by_role": {},
            }
            self._daily[date_key] = bucket
        return bucket

    def _load_unlocked(self) -> None:
        raw = self.state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("daily"), dict):
            self._daily = dict(data["daily"])

    def _save_unlocked(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timezone": "Asia/Shanghai",
            "daily_budget_limit": self.daily_budget_limit,
            "daily_chapter_limit": self.daily_chapter_limit,
            "daily": self._daily,
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.state_path.parent),
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
        )
        # Unique temp name per write (no fixed '.tmp' collisions); project policy forbids unlink in llm/.
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, self.state_path)

    def check_can_proceed(self, chapter: Optional[int] = None) -> None:
        """Raise BudgetCircuitBreakerError if daily budget or chapter cap has been reached."""
        with self._lock:
            date_key = self._today_key()
            bucket = self._ensure_bucket(date_key)
            if self.daily_budget_limit is not None:
                if self.daily_budget_limit <= 0 or bucket["estimated_cost_usd"] >= self.daily_budget_limit - 1e-12:
                    raise BudgetCircuitBreakerError(
                        f"Daily budget circuit breaker tripped for {date_key}: "
                        f"spent=${bucket['estimated_cost_usd']:.6f} >= limit=${self.daily_budget_limit:.6f}"
                    )
            if self.daily_chapter_limit is not None:
                if self.daily_chapter_limit <= 0:
                    raise BudgetCircuitBreakerError(
                        f"Daily chapter circuit breaker tripped for {date_key}: limit={self.daily_chapter_limit}"
                    )
                chapters = bucket["chapters"]
                if chapter is not None and chapter > 0:
                    if chapter not in chapters and len(chapters) >= self.daily_chapter_limit:
                        raise BudgetCircuitBreakerError(
                            f"Daily chapter limit reached for {date_key}: "
                            f"{len(chapters)} >= {self.daily_chapter_limit} (blocked chapter {chapter})"
                        )
                elif len(chapters) >= self.daily_chapter_limit:
                    raise BudgetCircuitBreakerError(
                        f"Daily chapter limit reached for {date_key}: "
                        f"{len(chapters)} >= {self.daily_chapter_limit}"
                    )

    def record_usage(
        self,
        *,
        role: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: Optional[float] = None,
        chapter: Optional[int] = None,
        input_cost_per_1m: float = 0.0,
        output_cost_per_1m: float = 0.0,
        fixed_cost_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """Record one LLM/image request usage and enforce circuit breakers."""
        in_tok = max(0, int(input_tokens))
        out_tok = max(0, int(output_tokens))
        if cost_usd is None:
            computed_cost = (
                (in_tok * float(input_cost_per_1m) + out_tok * float(output_cost_per_1m)) / 1_000_000.0
                + float(fixed_cost_usd)
            )
        else:
            computed_cost = float(cost_usd)

        with self._lock:
            date_key = self._today_key()
            bucket = self._ensure_bucket(date_key)
            bucket["request_count"] += 1
            bucket["input_tokens"] += in_tok
            bucket["output_tokens"] += out_tok
            bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + computed_cost, 8)

            if chapter is not None and chapter > 0 and chapter not in bucket["chapters"]:
                bucket["chapters"].append(int(chapter))
                bucket["chapters"].sort()

            role_stats = bucket["by_role"].setdefault(
                role,
                {
                    "model": model,
                    "request_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_usd": 0.0,
                },
            )
            role_stats["model"] = model
            role_stats["request_count"] += 1
            role_stats["input_tokens"] += in_tok
            role_stats["output_tokens"] += out_tok
            role_stats["estimated_cost_usd"] = round(
                role_stats["estimated_cost_usd"] + computed_cost, 8
            )

            # Usage for an already-paid upstream call must never be discarded by raising here:
            # record it, mark the breaker as tripped, and let the NEXT check_can_proceed() block.
            tripped_reasons: List[str] = []
            if (
                self.daily_budget_limit is not None
                and bucket["estimated_cost_usd"] > self.daily_budget_limit + 1e-12
            ):
                tripped_reasons.append(
                    f"Daily budget exceeded for {date_key}: "
                    f"spent=${bucket['estimated_cost_usd']:.6f} > limit=${self.daily_budget_limit:.6f}"
                )
            if (
                self.daily_chapter_limit is not None
                and len(bucket["chapters"]) > self.daily_chapter_limit
            ):
                tripped_reasons.append(
                    f"Daily chapter limit exceeded for {date_key}: "
                    f"{len(bucket['chapters'])} > {self.daily_chapter_limit}"
                )
            if tripped_reasons:
                bucket["tripped"] = True
                bucket["tripped_reason"] = "; ".join(tripped_reasons)

            self._save_unlocked()

            return json.loads(json.dumps(bucket))

    def record_chapter(self, chapter: int) -> Dict[str, Any]:
        """Explicitly mark a chapter as generated today and enforce daily_chapter_limit."""
        if chapter <= 0:
            raise ValueError("chapter must be positive.")
        with self._lock:
            date_key = self._today_key()
            bucket = self._ensure_bucket(date_key)
            if chapter not in bucket["chapters"]:
                if (
                    self.daily_chapter_limit is not None
                    and len(bucket["chapters"]) >= self.daily_chapter_limit
                ):
                    raise BudgetCircuitBreakerError(
                        f"Daily chapter limit reached for {date_key}: "
                        f"{len(bucket['chapters'])} >= {self.daily_chapter_limit}"
                    )
                bucket["chapters"].append(int(chapter))
                bucket["chapters"].sort()
                self._save_unlocked()
            return json.loads(json.dumps(bucket))

    def get_summary(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            key = date_str or self._today_key()
            bucket = self._ensure_bucket(key)
            return json.loads(json.dumps(bucket))

    def get_total_summary(self) -> Dict[str, Any]:
        with self._lock:
            total_reqs = sum(b["request_count"] for b in self._daily.values())
            total_in = sum(b["input_tokens"] for b in self._daily.values())
            total_out = sum(b["output_tokens"] for b in self._daily.values())
            total_cost = round(sum(b["estimated_cost_usd"] for b in self._daily.values()), 8)
            return {
                "days": len(self._daily),
                "request_count": total_reqs,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "estimated_cost_usd": total_cost,
            }


class _RoleProviderAdapter(LLMProvider):
    """LLMProvider wrapper bound to a specific role on RoleModelRouter."""

    def __init__(self, router: "RoleModelRouter", role: str) -> None:
        self._router = router
        self._role = role

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self._router.generate(self._role, request)


class RoleModelRouter:
    """5-role dynamic model router with same-tier 429 key-pool rotation and explicit whitelisted failover."""

    def __init__(
        self,
        roles: Mapping[str, RoleEndpointConfig | Mapping[str, Any]],
        ledger: Optional[UsageBudgetLedger] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
        on_model_switched: Optional[Callable[[ModelSwitchEvent], None]] = None,
        on_rate_limit_retry: Optional[Callable[[RateLimitRetryEvent], None]] = None,
        on_model_quota: Optional[Callable[[ModelQuotaSnapshot], None]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        workbuddy_catalog: Optional[WorkBuddyModelCatalog] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._roles: Dict[str, RoleEndpointConfig] = {}
        self._cursors: Dict[str, int] = {}
        self._cooldowns: Dict[str, Dict[int, float]] = {}
        self._active_models: Dict[str, str] = {}
        self._model_cooldowns: Dict[str, Dict[str, float]] = {}
        self._model_key_cursors: Dict[str, Dict[str, int]] = {}
        self._model_key_cooldowns: Dict[str, Dict[str, Dict[int, float]]] = {}
        self._last_chapter_by_role: Dict[str, Optional[int]] = {}
        self._switch_listeners: List[Callable[[ModelSwitchEvent], None]] = []
        self._switch_history: List[ModelSwitchEvent] = []
        self._retry_listeners: List[Callable[[RateLimitRetryEvent], None]] = []
        self._retry_history: List[RateLimitRetryEvent] = []
        self._quota_listeners: List[Callable[[ModelQuotaSnapshot], None]] = []
        self._model_quotas: Dict[str, ModelQuotaSnapshot] = {}
        self.ledger = ledger or UsageBudgetLedger()
        self._monotonic = monotonic_fn or time.monotonic
        self._now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self._sleep = sleep_fn or time.sleep
        self.workbuddy_catalog: Optional[WorkBuddyModelCatalog] = workbuddy_catalog
        if on_model_switched is not None:
            self._switch_listeners.append(on_model_switched)
        if on_rate_limit_retry is not None:
            self._retry_listeners.append(on_rate_limit_retry)
        if on_model_quota is not None:
            self._quota_listeners.append(on_model_quota)

        for role_name, raw_cfg in roles.items():
            cfg = (
                raw_cfg
                if isinstance(raw_cfg, RoleEndpointConfig)
                else RoleEndpointConfig.from_mapping(role_name, raw_cfg)
            )
            if cfg.role != role_name:
                raise LLMConfigurationError(
                    f"Mismatched role key {role_name!r} vs config.role {cfg.role!r}."
                )
            self._roles[cfg.role] = cfg
            self._cursors[cfg.role] = 0
            self._cooldowns[cfg.role] = {}
            self._active_models[cfg.role] = cfg.model
            self._model_cooldowns[cfg.role] = {}
            self._model_key_cursors[cfg.role] = {}
            self._model_key_cooldowns[cfg.role] = {}
            self._last_chapter_by_role[cfg.role] = None

    @classmethod
    def from_workbuddy_catalog(
        cls,
        catalog: WorkBuddyModelCatalog,
        *,
        base_url: str,
        api_keys: Sequence[str],
        role_model_overrides: Optional[Mapping[str, Optional[str]]] = None,
        role_base_url_overrides: Optional[Mapping[str, Optional[str]]] = None,
        role_api_key_overrides: Optional[Mapping[str, Sequence[str]]] = None,
        image_role: Optional[RoleEndpointConfig] = None,
        ledger: Optional[UsageBudgetLedger] = None,
        on_model_switched: Optional[Callable[[ModelSwitchEvent], None]] = None,
        on_rate_limit_retry: Optional[Callable[[RateLimitRetryEvent], None]] = None,
        on_model_quota: Optional[Callable[[ModelQuotaSnapshot], None]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        timeout: int = 300,
        stream: bool = True,
        cooldown_sec: float = 60.0,
        model_cooldown_sec: Optional[float] = None,
        rate_limit_backoff_sec: Optional[Sequence[float]] = None,
    ) -> "RoleModelRouter":
        overrides = role_model_overrides or {}
        url_ov = role_base_url_overrides or {}
        key_ov = role_api_key_overrides or {}
        parsed_backoff = (
            parse_rate_limit_backoff_sec(rate_limit_backoff_sec)
            if rate_limit_backoff_sec is not None
            else None
        )
        roles_cfg: Dict[str, RoleEndpointConfig] = {}
        for role in ("planner", "writer", "critic", "polisher"):
            primary_model, rotation_chain = catalog.default_role_model_and_rotation(
                role,
                override_model=overrides.get(role),
            )
            eff_url = (url_ov.get(role) or base_url or "").strip()
            eff_keys = list(key_ov.get(role) or api_keys or ())
            roles_cfg[role] = RoleEndpointConfig(
                role=role,
                base_url=eff_url,
                model=primary_model,
                api_keys=eff_keys,
                provider_type="openai",
                timeout=timeout,
                stream=stream,
                cooldown_sec=cooldown_sec,
                model_whitelist=catalog.whitelist(),
                rotation_models=rotation_chain,
                manual_only_models=catalog.manual_only(),
                model_cooldown_sec=model_cooldown_sec,
                rate_limit_backoff_sec=parsed_backoff,
                model_labels=catalog.labels(),
            )
        if image_role is not None:
            roles_cfg["image"] = image_role
        return cls(
            roles=roles_cfg,
            ledger=ledger,
            monotonic_fn=monotonic_fn,
            on_model_switched=on_model_switched,
            on_rate_limit_retry=on_rate_limit_retry,
            on_model_quota=on_model_quota,
            sleep_fn=sleep_fn,
            workbuddy_catalog=catalog,
        )

    @classmethod
    def from_dict(
        cls,
        config: Mapping[str, Any],
        ledger: Optional[UsageBudgetLedger] = None,
        on_model_switched: Optional[Callable[[ModelSwitchEvent], None]] = None,
        on_rate_limit_retry: Optional[Callable[[RateLimitRetryEvent], None]] = None,
        on_model_quota: Optional[Callable[[ModelQuotaSnapshot], None]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> "RoleModelRouter":
        catalog: Optional[WorkBuddyModelCatalog] = None
        if "workbuddy_models" in config and config["workbuddy_models"] is not None:
            catalog = WorkBuddyModelCatalog.from_mapping(config["workbuddy_models"])
            if "roles" not in config:
                raw_keys = config.get("api_keys")
                if raw_keys is None and config.get("api_key") is not None:
                    raw_keys = [str(config["api_key"])]
                elif isinstance(raw_keys, str):
                    raw_keys = [raw_keys]
                return cls.from_workbuddy_catalog(
                    catalog,
                    base_url=str(config.get("base_url") or ""),
                    api_keys=list(raw_keys or ()),
                    role_model_overrides=config.get("role_model_overrides")
                    if isinstance(config.get("role_model_overrides"), Mapping)
                    else None,
                    ledger=ledger,
                    on_model_switched=on_model_switched,
                    on_rate_limit_retry=on_rate_limit_retry,
                    on_model_quota=on_model_quota,
                    monotonic_fn=monotonic_fn,
                    sleep_fn=sleep_fn,
                    timeout=int(config.get("timeout", 300)),
                    stream=bool(config.get("stream", True)),
                    cooldown_sec=float(config.get("cooldown_sec", 60.0)),
                    model_cooldown_sec=(
                        float(config["model_cooldown_sec"])
                        if config.get("model_cooldown_sec") is not None
                        else None
                    ),
                    rate_limit_backoff_sec=(
                        parse_rate_limit_backoff_sec(config["rate_limit_backoff_sec"])
                        if config.get("rate_limit_backoff_sec") is not None
                        else None
                    ),
                )

        roles_raw = config.get("roles") if "roles" in config else config
        if not isinstance(roles_raw, Mapping):
            raise LLMConfigurationError("Router config must be a mapping of role -> endpoint config.")

        if catalog is None:
            return cls(
                roles=roles_raw,
                ledger=ledger,
                monotonic_fn=monotonic_fn,
                on_model_switched=on_model_switched,
                on_rate_limit_retry=on_rate_limit_retry,
                on_model_quota=on_model_quota,
                sleep_fn=sleep_fn,
            )

        enriched_roles: Dict[str, RoleEndpointConfig] = {}
        for role_name, raw_cfg in roles_raw.items():
            if not isinstance(raw_cfg, Mapping):
                raise LLMConfigurationError(f"Role config for {role_name!r} must be a mapping.")
            merged = dict(raw_cfg)
            if role_name != "image":
                explicit_model = str(merged.get("model") or "").strip() or None
                primary, default_rotation = catalog.default_role_model_and_rotation(
                    role_name, override_model=explicit_model
                )
                merged["model"] = primary
                merged.setdefault("model_whitelist", list(catalog.whitelist()))
                merged.setdefault("manual_only_models", list(catalog.manual_only()))
                merged.setdefault("model_labels", dict(catalog.labels()))
                if "model_cooldown_sec" not in merged and config.get("model_cooldown_sec") is not None:
                    merged["model_cooldown_sec"] = config["model_cooldown_sec"]
                if "rate_limit_backoff_sec" not in merged and config.get("rate_limit_backoff_sec") is not None:
                    merged["rate_limit_backoff_sec"] = config["rate_limit_backoff_sec"]
                if "rotation_models" not in merged or merged["rotation_models"] is None:
                    merged["rotation_models"] = list(default_rotation)
            enriched_roles[role_name] = RoleEndpointConfig.from_mapping(role_name, merged)

        return cls(
            roles=enriched_roles,
            ledger=ledger,
            monotonic_fn=monotonic_fn,
            on_model_switched=on_model_switched,
            on_rate_limit_retry=on_rate_limit_retry,
            on_model_quota=on_model_quota,
            sleep_fn=sleep_fn,
            workbuddy_catalog=catalog,
        )

    def add_model_switch_listener(self, callback: Callable[[ModelSwitchEvent], None]) -> None:
        with self._lock:
            if callback not in self._switch_listeners:
                self._switch_listeners.append(callback)

    def add_rate_limit_retry_listener(self, callback: Callable[[RateLimitRetryEvent], None]) -> None:
        with self._lock:
            if callback not in self._retry_listeners:
                self._retry_listeners.append(callback)

    def add_model_quota_listener(self, callback: Callable[[ModelQuotaSnapshot], None]) -> None:
        with self._lock:
            if callback not in self._quota_listeners:
                self._quota_listeners.append(callback)

    def get_model_quotas(self) -> Dict[str, ModelQuotaSnapshot]:
        with self._lock:
            return dict(self._model_quotas)

    def get_active_model(self, role: str) -> str:
        cfg = self.get_role_config(role)
        with self._lock:
            return self._active_models.get(role, cfg.model)

    def get_model_switch_history(self) -> List[ModelSwitchEvent]:
        with self._lock:
            return list(self._switch_history)

    def get_rate_limit_retry_history(self) -> List[RateLimitRetryEvent]:
        with self._lock:
            return list(self._retry_history)

    def recover_preferred_models(
        self,
        *,
        roles: Optional[Sequence[str]] = None,
        book_id: str = "",
        chapter: int = 0,
    ) -> List[ModelSwitchEvent]:
        """Before writing a new chapter, restore the earliest cooled-off model in each role's rotation chain."""
        with self._lock:
            target_roles = (
                [r for r in roles if r in self._roles]
                if roles is not None
                else [r for r in ("planner", "writer", "critic", "polisher") if r in self._roles]
            )
        restored_events: List[ModelSwitchEvent] = []
        for role in target_roles:
            cfg = self.get_role_config(role)
            if not cfg.rotation_models:
                continue
            with self._lock:
                current_model = self._active_models.get(role, cfg.rotation_models[0])
            try:
                cur_idx = cfg.rotation_models.index(current_model)
            except ValueError:
                continue
            if cur_idx <= 0:
                continue
            for earlier_model in cfg.rotation_models[:cur_idx]:
                if self._is_model_in_cooldown(role, earlier_model):
                    continue
                with self._lock:
                    self._active_models[role] = earlier_model
                    self._model_key_cooldowns.setdefault(role, {}).pop(earlier_model, None)
                cooldown_min = round(cfg.effective_model_cooldown_sec() / 60.0, 1)
                cooldown_desc = (
                    f"{int(cfg.effective_model_cooldown_sec() // 3600)} 小时"
                    if cfg.effective_model_cooldown_sec() >= 3600 and cfg.effective_model_cooldown_sec() % 3600 == 0
                    else (f"{cooldown_min:g} 分钟" if cfg.effective_model_cooldown_sec() >= 60 else f"{int(cfg.effective_model_cooldown_sec())} 秒")
                )
                ch_prefix = f"第 {chapter} 章开写前，" if chapter > 0 else ""
                earlier_idx = cfg.rotation_models.index(earlier_model)
                target_desc = "首选模型" if earlier_idx == 0 else "更靠前可用模型"
                event = ModelSwitchEvent(
                    role=role,
                    from_model=current_model,
                    to_model=earlier_model,
                    from_label=cfg.format_model_label(current_model),
                    to_label=cfg.format_model_label(earlier_model),
                    reason=f"{ch_prefix}{cfg.format_model_label(earlier_model)} 冷却期（{cooldown_desc}）已过，已自动切回{target_desc}",
                    timestamp=self._now_fn().isoformat(),
                    book_id=str(book_id or ""),
                    chapter=int(chapter or 0),
                    event_type="model_restored",
                )
                self._emit_model_switch(event)
                restored_events.append(event)
                break
        return restored_events

    def reset_model_rotation(self, role: Optional[str] = None) -> None:
        with self._lock:
            targets = [role] if role is not None else list(self._roles.keys())
            for r in targets:
                cfg = self._roles.get(r)
                if cfg is None:
                    continue
                self._active_models[r] = cfg.model
                self._model_cooldowns[r] = {}
                self._model_key_cursors[r] = {}
                self._model_key_cooldowns[r] = {}
                self._last_chapter_by_role[r] = None
                self._cooldowns[r] = {}
                self._cursors[r] = 0

    def has_role(self, role: str) -> bool:
        with self._lock:
            return role in self._roles

    def get_role_config(self, role: str) -> RoleEndpointConfig:
        if role not in VALID_ROLES:
            raise LLMConfigurationError(
                f"Unknown role {role!r}; expected one of {VALID_ROLES}."
            )
        with self._lock:
            cfg = self._roles.get(role)
        if cfg is None:
            raise LLMConfigurationError(
                f"Role {role!r} is not configured in RoleModelRouter."
            )
        return cfg

    def update_role(self, config: RoleEndpointConfig | Mapping[str, Any], role: Optional[str] = None) -> None:
        if isinstance(config, RoleEndpointConfig):
            cfg = config
        else:
            target_role = role or str(config.get("role") or "")
            cfg = RoleEndpointConfig.from_mapping(target_role, config)
        with self._lock:
            self._roles[cfg.role] = cfg
            self._cursors[cfg.role] = 0
            self._cooldowns[cfg.role] = {}
            self._active_models[cfg.role] = cfg.model
            self._model_cooldowns[cfg.role] = {}
            self._model_key_cursors[cfg.role] = {}
            self._model_key_cooldowns[cfg.role] = {}

    def as_provider(self, role: str) -> LLMProvider:
        self.get_role_config(role)
        return _RoleProviderAdapter(self, role)

    def _candidate_key_indices(self, role: str, cfg: RoleEndpointConfig) -> List[int]:
        now = self._monotonic()
        with self._lock:
            total = len(cfg.api_keys)
            start = self._cursors.get(role, 0) % total
            cooldown_map = self._cooldowns.setdefault(role, {})
            candidates: List[int] = []
            for offset in range(total):
                idx = (start + offset) % total
                until = cooldown_map.get(idx, 0.0)
                if until <= now:
                    if idx in cooldown_map:
                        del cooldown_map[idx]
                    candidates.append(idx)
            return candidates

    def _mark_key_429(self, role: str, key_idx: int, cfg: RoleEndpointConfig) -> None:
        now = self._monotonic()
        with self._lock:
            cooldown_map = self._cooldowns.setdefault(role, {})
            cooldown_map[key_idx] = now + cfg.cooldown_sec
            self._cursors[role] = (key_idx + 1) % len(cfg.api_keys)

    def _mark_key_success(self, role: str, key_idx: int, cfg: RoleEndpointConfig) -> None:
        with self._lock:
            self._cursors[role] = (key_idx + 1) % len(cfg.api_keys)
            cooldown_map = self._cooldowns.get(role, {})
            cooldown_map.pop(key_idx, None)

    def _candidate_key_indices_for_model(
        self, role: str, model_id: str, cfg: RoleEndpointConfig
    ) -> List[int]:
        if not cfg.rotation_models:
            return self._candidate_key_indices(role, cfg)
        now = self._monotonic()
        with self._lock:
            total = len(cfg.api_keys)
            cursors_by_model = self._model_key_cursors.setdefault(role, {})
            start = cursors_by_model.get(model_id, 0) % total
            cooldowns_by_model = self._model_key_cooldowns.setdefault(role, {})
            cooldown_map = cooldowns_by_model.setdefault(model_id, {})
            candidates: List[int] = []
            for offset in range(total):
                idx = (start + offset) % total
                until = cooldown_map.get(idx, 0.0)
                if until <= now:
                    if idx in cooldown_map:
                        del cooldown_map[idx]
                    candidates.append(idx)
            return candidates

    def _mark_key_quota_for_model(
        self, role: str, model_id: str, key_idx: int, cfg: RoleEndpointConfig
    ) -> None:
        if not cfg.rotation_models:
            self._mark_key_429(role, key_idx, cfg)
            return
        now = self._monotonic()
        with self._lock:
            cooldowns_by_model = self._model_key_cooldowns.setdefault(role, {})
            cooldown_map = cooldowns_by_model.setdefault(model_id, {})
            cooldown_map[key_idx] = now + cfg.cooldown_sec
            cursors_by_model = self._model_key_cursors.setdefault(role, {})
            cursors_by_model[model_id] = (key_idx + 1) % len(cfg.api_keys)

    def _mark_key_success_for_model(
        self, role: str, model_id: str, key_idx: int, cfg: RoleEndpointConfig
    ) -> None:
        if not cfg.rotation_models:
            self._mark_key_success(role, key_idx, cfg)
            return
        with self._lock:
            cursors_by_model = self._model_key_cursors.setdefault(role, {})
            cursors_by_model[model_id] = (key_idx + 1) % len(cfg.api_keys)
            cooldown_map = self._model_key_cooldowns.get(role, {}).get(model_id, {})
            cooldown_map.pop(key_idx, None)

    def _is_model_in_cooldown(self, role: str, model_id: str) -> bool:
        now = self._monotonic()
        with self._lock:
            model_map = self._model_cooldowns.setdefault(role, {})
            until = model_map.get(model_id, 0.0)
            if until <= now:
                model_map.pop(model_id, None)
                return False
            return True

    def _mark_model_quota_exhausted(
        self, role: str, model_id: str, cfg: RoleEndpointConfig
    ) -> None:
        now = self._monotonic()
        with self._lock:
            model_map = self._model_cooldowns.setdefault(role, {})
            model_map[model_id] = now + cfg.effective_model_cooldown_sec()

    def _emit_model_switch(self, event: ModelSwitchEvent) -> None:
        with self._lock:
            self._switch_history.append(event)
            listeners = list(self._switch_listeners)
        for listener in listeners:
            listener(event)

    def _emit_rate_limit_retry(self, event: RateLimitRetryEvent) -> None:
        with self._lock:
            self._retry_history.append(event)
            listeners = list(self._retry_listeners)
        for listener in listeners:
            listener(event)

    def _capture_usage_quota(
        self,
        role: str,
        cfg: RoleEndpointConfig,
        requested_model: str,
        usage: Optional[Mapping[str, Any]],
    ) -> Optional[ModelQuotaSnapshot]:
        snap = extract_workbuddy_usage_quota(
            usage,
            requested_model=requested_model,
            catalog=self.workbuddy_catalog,
            model_labels=cfg.model_labels,
            role=role,
            updated_at=self._now_fn().isoformat(),
        )
        if snap is None:
            return None
        with self._lock:
            self._model_quotas[snap.model] = snap
            listeners = list(self._quota_listeners)
        for listener in listeners:
            listener(snap)
        return snap

    def _clear_key_cooldowns_for_model(self, role: str, model_id: str, cfg: RoleEndpointConfig) -> None:
        with self._lock:
            if cfg.rotation_models:
                self._model_key_cooldowns.setdefault(role, {}).pop(model_id, None)
            else:
                self._cooldowns[role] = {}

    def _build_text_provider(
        self,
        cfg: RoleEndpointConfig,
        api_key: str,
        *,
        model: Optional[str] = None,
    ) -> LLMProvider:
        effective_model = (model or cfg.model).strip()
        if cfg.provider_type == "anthropic":
            return AnthropicProvider(
                api_key=api_key,
                base_url=cfg.base_url,
                model=effective_model,
                timeout=cfg.timeout,
            )
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=cfg.base_url,
            model=effective_model,
            timeout=cfg.timeout,
            stream=cfg.stream,
            reasoning_effort=cfg.reasoning_effort,
            strip_reasoning_suffix=cfg.strip_reasoning_suffix,
        )

    def list_models(self, role: str = "writer") -> List[str]:
        cfg = self.get_role_config(role)
        candidates = self._candidate_key_indices(role, cfg)
        if not candidates:
            raise QuotaExhaustedError(
                f"All {len(cfg.api_keys)} credentials for role '{role}' (model '{cfg.model}') "
                "are in 429 cooldown. Refusing to downgrade model."
            )
        for key_idx in candidates:
            api_key = cfg.api_keys[key_idx]
            provider = OpenAICompatibleProvider(
                api_key=api_key,
                base_url=cfg.base_url,
                model=cfg.model,
                timeout=cfg.timeout,
                stream=cfg.stream,
            )
            try:
                models = provider.list_models()
                self._mark_key_success(role, key_idx, cfg)
                return models
            except LLMRequestError as exc:
                if exc.status_code == 429:
                    self._mark_key_429(role, key_idx, cfg)
                    continue
                raise LLMRequestError(
                    redact_secrets(str(exc), cfg.api_keys),
                    status_code=exc.status_code,
                    error_code=exc.error_code,
                    upstream_code=exc.upstream_code,
                    response_body=redact_secrets(exc.response_body, cfg.api_keys),
                ) from None
        raise QuotaExhaustedError(
            f"All {len(cfg.api_keys)} credentials for role '{role}' (model '{cfg.model}') "
            "exhausted quota (HTTP 429). Refusing to downgrade model."
        )

    def generate(self, role: str, request: LLMRequest) -> LLMResponse:
        cfg = self.get_role_config(role)
        if role == "image":
            raise LLMConfigurationError(
                "Role 'image' requires generate_image(ImageGenerationRequest), not text generate()."
            )

        chapter_no = request.chapter if request.chapter > 0 else None
        self.ledger.check_can_proceed(chapter=chapter_no)

        explicit_req_model = str(request.metadata.get("model") or "").strip()
        if explicit_req_model and cfg.model_whitelist:
            validate_workbuddy_model_allowed(
                explicit_req_model,
                cfg.model_whitelist,
                context=f"role={role} request.metadata.model",
            )

        # Bug 2 fix: before writing a new chapter, check whether an earlier (higher-priority)
        # model in the rotation chain has finished cooling down and restore it automatically.
        if cfg.rotation_models and chapter_no is not None:
            with self._lock:
                prev_chapter = self._last_chapter_by_role.get(role)
                chapter_changed = prev_chapter != chapter_no
            if chapter_changed:
                self.recover_preferred_models(
                    roles=(role,),
                    book_id=str(request.metadata.get("book_id") or ""),
                    chapter=chapter_no,
                )
                with self._lock:
                    self._last_chapter_by_role[role] = chapter_no

        if cfg.rotation_models:
            with self._lock:
                active_model = self._active_models.get(role, cfg.rotation_models[0])
            try:
                start_idx = cfg.rotation_models.index(active_model)
            except ValueError:
                start_idx = 0
                active_model = cfg.rotation_models[0]
            model_chain: Tuple[str, ...] = cfg.rotation_models[start_idx:]
        else:
            model_chain = (cfg.model,)

        policy_active = bool(cfg.rotation_models or cfg.model_whitelist)
        backoff_delays = cfg.effective_rate_limit_backoff_sec() if policy_active else ()
        last_quota_reason = "HTTP 429"
        last_status_code: Optional[int] = None
        last_upstream_code = ""

        for chain_pos, current_model in enumerate(model_chain):
            if cfg.rotation_models and self._is_model_in_cooldown(role, current_model):
                last_quota_reason = f"模型 {current_model} 仍在额度冷却期内"
                continue

            req_for_model = request
            if explicit_req_model and explicit_req_model != current_model:
                cleaned_meta = {k: v for k, v in request.metadata.items() if k != "model"}
                req_for_model = LLMRequest(
                    request_id=request.request_id,
                    chapter=request.chapter,
                    attempt=request.attempt,
                    purpose=request.purpose,
                    system_prompt=request.system_prompt,
                    user_prompt=request.user_prompt,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    metadata=cleaned_meta,
                )

            if policy_active:
                # WorkBuddy models spend part of max_tokens on hidden reasoning; add headroom so the
                # visible text budget the caller asked for is not eaten by thinking.
                req_for_model = dataclasses.replace(
                    req_for_model,
                    max_tokens=int(req_for_model.max_tokens) + WORKBUDDY_REASONING_HEADROOM_TOKENS,
                )

            # Bug 1 fix: HTTP 429 rate_limit_exceeded retries on the SAME model using configured
            # backoff delays (default 30s, 60s, 120s = 3 retries) before judging quota exhausted.
            # Explicit credit_package_exhausted / upstream code 14018 rotates immediately.
            max_same_model_attempts = (len(backoff_delays) + 1) if policy_active else 1
            for retry_idx in range(max_same_model_attempts):
                candidates = self._candidate_key_indices_for_model(role, current_model, cfg)
                if not candidates:
                    if not cfg.rotation_models:
                        raise QuotaExhaustedError(
                            f"All {len(cfg.api_keys)} credentials for role '{role}' (model '{current_model}') "
                            "are currently in 429 cooldown. Refusing to downgrade model."
                        )
                    break

                saw_rate_limit = False
                credit_exhausted_immediately = False

                for key_idx in candidates:
                    api_key = cfg.api_keys[key_idx]
                    provider = self._build_text_provider(cfg, api_key, model=current_model)
                    try:
                        resp = provider.generate(req_for_model)
                        self._mark_key_success_for_model(role, current_model, key_idx, cfg)
                        resolved_model = (resp.model or current_model).strip()
                        self._capture_usage_quota(role, cfg, current_model, resp.usage)
                        in_tok, out_tok = _extract_tokens(resp.usage)
                        self.ledger.record_usage(
                            role=role,
                            model=resolved_model,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            chapter=chapter_no,
                            input_cost_per_1m=cfg.input_cost_per_1m,
                            output_cost_per_1m=cfg.output_cost_per_1m,
                        )
                        return resp
                    except LLMStreamTruncatedError as exc:
                        # Upstream already generated (and billed) tokens; record them and NEVER switch models.
                        self._mark_key_success_for_model(role, current_model, key_idx, cfg)
                        trunc_usage = getattr(exc, "usage", None) or {}
                        self._capture_usage_quota(role, cfg, current_model, trunc_usage)
                        in_tok, out_tok = _extract_tokens(trunc_usage)
                        self.ledger.record_usage(
                            role=role,
                            model=getattr(exc, "model", "") or current_model,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            chapter=chapter_no,
                            input_cost_per_1m=cfg.input_cost_per_1m,
                            output_cost_per_1m=cfg.output_cost_per_1m,
                        )
                        raise
                    except LLMRequestError as exc:
                        if policy_active:
                            fail_kind, quota_reason = classify_workbuddy_failure_kind(exc)
                        else:
                            fail_kind = "rate_limit" if exc.status_code == 429 else "non_quota"
                            quota_reason = "HTTP 429"
                        if fail_kind != "non_quota":
                            self._mark_key_quota_for_model(role, current_model, key_idx, cfg)
                            last_quota_reason = quota_reason
                            last_status_code = exc.status_code
                            last_upstream_code = exc.upstream_code or ""
                            if fail_kind == "credit_exhausted":
                                credit_exhausted_immediately = True
                            else:
                                saw_rate_limit = True
                            continue
                        raise LLMRequestError(
                            redact_secrets(str(exc), cfg.api_keys),
                            status_code=exc.status_code,
                            error_code=exc.error_code,
                            upstream_code=exc.upstream_code,
                            response_body=redact_secrets(exc.response_body, cfg.api_keys),
                        ) from None

                if (
                    policy_active
                    and saw_rate_limit
                    and not credit_exhausted_immediately
                    and retry_idx < len(backoff_delays)
                ):
                    wait_sec = float(backoff_delays[retry_idx])
                    retry_event = RateLimitRetryEvent(
                        role=role,
                        model=current_model,
                        model_label=cfg.format_model_label(current_model),
                        attempt=retry_idx + 1,
                        max_retries=len(backoff_delays),
                        wait_seconds=wait_sec,
                        reason=last_quota_reason,
                        timestamp=self._now_fn().isoformat(),
                        book_id=str(request.metadata.get("book_id") or ""),
                        chapter=int(request.chapter or 0),
                        status_code=429,
                    )
                    self._emit_rate_limit_retry(retry_event)
                    self._sleep(wait_sec)
                    self._clear_key_cooldowns_for_model(role, current_model, cfg)
                    continue

                if (
                    policy_active
                    and saw_rate_limit
                    and not credit_exhausted_immediately
                    and len(backoff_delays) > 0
                ):
                    last_quota_reason = (
                        f"HTTP 429 · 连续 {len(backoff_delays)} 次退避重试后仍被限流，"
                        "判定该模型额度用尽 (rate_limit_exceeded)"
                    )
                break

            # All candidate keys (and any rate-limit backoff retries) for current_model exhausted quota.
            if cfg.rotation_models:
                self._mark_model_quota_exhausted(role, current_model, cfg)
                next_model: Optional[str] = None
                for upcoming in model_chain[chain_pos + 1 :]:
                    if not self._is_model_in_cooldown(role, upcoming):
                        next_model = upcoming
                        break
                if next_model is not None:
                    with self._lock:
                        self._active_models[role] = next_model
                    event = ModelSwitchEvent(
                        role=role,
                        from_model=current_model,
                        to_model=next_model,
                        from_label=cfg.format_model_label(current_model),
                        to_label=cfg.format_model_label(next_model),
                        reason=last_quota_reason,
                        timestamp=self._now_fn().isoformat(),
                        book_id=str(request.metadata.get("book_id") or ""),
                        chapter=int(request.chapter or 0),
                        status_code=last_status_code,
                        upstream_code=last_upstream_code,
                        event_type="model_switched",
                    )
                    self._emit_model_switch(event)
                    continue
                break

        if cfg.rotation_models:
            chain_display = " → ".join(cfg.format_model_label(m) for m in cfg.rotation_models)
            raise QuotaExhaustedError(
                f"角色 '{role}' 白名单轮换链全部模型（{chain_display}）均已耗尽额度"
                f"（最后原因：{last_quota_reason}），已触发熔断停机，不准再切换其他模型。"
            )

        raise QuotaExhaustedError(
            f"All {len(cfg.api_keys)} credentials for role '{role}' (model '{cfg.model}') "
            f"exhausted quota ({last_quota_reason}). Refusing to downgrade model."
        )

    def _resolve_image_protocol(self, cfg: RoleEndpointConfig, model: str) -> str:
        if cfg.provider_type in {"gemini_native", "openai_images"}:
            return cfg.provider_type
        lowered_model = model.lower()
        if "gemini" in lowered_model or cfg.base_url.lower().endswith("/v1beta"):
            return "gemini_native"
        return "openai_images"

    def _gemini_generate_content_url(self, base_url: str, model: str) -> str:
        clean_model, _ = parse_model_reasoning_suffix(model)
        root = base_url.rstrip("/")
        if root.endswith("/v1beta"):
            root = root[:-7]
        elif root.endswith("/v1"):
            root = root[:-3]
        encoded_model = urllib.parse.quote(clean_model, safe="-_.")
        return f"{root}/v1beta/models/{encoded_model}:generateContent"

    def _openai_images_url(self, base_url: str) -> str:
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            return f"{root}/images/generations"
        if root.endswith("/v1beta"):
            return f"{root[:-7]}/v1/images/generations"
        parsed = urllib.parse.urlparse(root)
        if not parsed.path or parsed.path == "/":
            return f"{root}/v1/images/generations"
        return f"{root}/images/generations"

    def _call_gemini_image(
        self,
        cfg: RoleEndpointConfig,
        api_key: str,
        req: ImageGenerationRequest,
        model: str,
        t0: float,
    ) -> ImageGenerationResponse:
        url = self._gemini_generate_content_url(cfg.base_url, model)
        gen_cfg: Dict[str, Any] = {
            "responseModalities": ["TEXT", "IMAGE"],
        }
        if req.aspect_ratio:
            gen_cfg["imageConfig"] = {"aspectRatio": req.aspect_ratio}

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": req.prompt}],
                }
            ],
            "generationConfig": gen_cfg,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "x-goog-api-key": api_key,
        }
        http_req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(http_req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            err_text = _read_http_error_redacted(exc, cfg.api_keys)
            raise LLMRequestError(
                f"Gemini image generation HTTP {exc.code}: {err_text}",
                status_code=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ImageGenerationError(
                f"Gemini image generation failed: {redact_secrets(str(exc), cfg.api_keys)}"
            ) from None

        text_parts: List[str] = []
        image_bytes: Optional[bytes] = None
        mime_type = "image/png"
        for cand in data.get("candidates") or []:
            content = cand.get("content") or {}
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict) and inline.get("data") and image_bytes is None:
                    mime_type = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
                    try:
                        image_bytes = base64.b64decode(str(inline["data"]), validate=True)
                    except (ValueError, TypeError) as exc:
                        raise ImageGenerationError(
                            f"Invalid base64 inlineData from Gemini image response: {exc}"
                        ) from None

        if not image_bytes:
            raise ImageGenerationError(
                "Gemini :generateContent response contained no inline image data."
            )

        usage = dict(data.get("usageMetadata") or data.get("usage") or {})
        return ImageGenerationResponse(
            request_id=req.request_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
            model=model,
            provider="cpa_gemini",
            protocol="gemini_native",
            text="\n".join(text_parts).strip(),
            latency_sec=round(time.monotonic() - t0, 3),
            usage=usage,
        )

    def _call_openai_image(
        self,
        cfg: RoleEndpointConfig,
        api_key: str,
        req: ImageGenerationRequest,
        model: str,
        t0: float,
    ) -> ImageGenerationResponse:
        url = self._openai_images_url(cfg.base_url)
        payload = {
            "model": model,
            "prompt": req.prompt,
            "n": req.n,
            "size": req.size,
            "response_format": "b64_json",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        http_req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(http_req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            err_text = _read_http_error_redacted(exc, cfg.api_keys)
            raise LLMRequestError(
                f"OpenAI images generation HTTP {exc.code}: {err_text}",
                status_code=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ImageGenerationError(
                f"OpenAI images generation failed: {redact_secrets(str(exc), cfg.api_keys)}"
            ) from None

        items = data.get("data") or []
        if not items or not isinstance(items[0], dict):
            raise ImageGenerationError("OpenAI /v1/images/generations response contained empty data.")

        first = items[0]
        image_bytes: Optional[bytes] = None
        mime_type = "image/png"
        if first.get("b64_json"):
            raw_b64 = str(first["b64_json"])
            if raw_b64.startswith("data:") and ";base64," in raw_b64:
                header_part, raw_b64 = raw_b64.split(";base64,", 1)
                mime_type = header_part[5:] or mime_type
            try:
                image_bytes = base64.b64decode(raw_b64, validate=True)
            except (ValueError, TypeError) as exc:
                raise ImageGenerationError(f"Invalid b64_json in image response: {exc}") from None
        elif isinstance(first.get("url"), str) and first["url"].startswith("data:"):
            url_val = first["url"]
            if ";base64," not in url_val:
                raise ImageGenerationError("Unsupported non-base64 data URI in image response.")
            header_part, raw_b64 = url_val.split(";base64,", 1)
            mime_type = header_part[5:] or mime_type
            try:
                image_bytes = base64.b64decode(raw_b64, validate=True)
            except (ValueError, TypeError) as exc:
                raise ImageGenerationError(f"Invalid base64 data URI in image response: {exc}") from None

        if not image_bytes:
            raise ImageGenerationError("Image response missing b64_json payload.")

        return ImageGenerationResponse(
            request_id=req.request_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
            model=model,
            provider="openai_compatible",
            protocol="openai_images",
            text=str(first.get("revised_prompt") or ""),
            latency_sec=round(time.monotonic() - t0, 3),
            usage=dict(data.get("usage") or {}),
        )

    def generate_image(self, request: ImageGenerationRequest | str) -> ImageGenerationResponse:
        """Generate an image via CPA Gemini native :generateContent or OpenAI /v1/images/generations."""
        cfg = self.get_role_config("image")
        req = (
            ImageGenerationRequest(prompt=request)
            if isinstance(request, str)
            else request
        )
        if not req.prompt or not req.prompt.strip():
            raise ImageGenerationError("Image generation prompt cannot be empty.")

        self.ledger.check_can_proceed()
        effective_model = (req.model or cfg.model).strip()
        protocol = self._resolve_image_protocol(cfg, effective_model)

        candidates = self._candidate_key_indices("image", cfg)
        if not candidates:
            raise QuotaExhaustedError(
                f"All {len(cfg.api_keys)} credentials for role 'image' (model '{effective_model}') "
                "are currently in 429 cooldown. Refusing to downgrade model."
            )

        t0 = time.monotonic()
        for key_idx in candidates:
            api_key = cfg.api_keys[key_idx]
            try:
                if protocol == "gemini_native":
                    img_resp = self._call_gemini_image(cfg, api_key, req, effective_model, t0)
                else:
                    img_resp = self._call_openai_image(cfg, api_key, req, effective_model, t0)
                self._mark_key_success("image", key_idx, cfg)
                in_tok, out_tok = _extract_tokens(img_resp.usage)
                self.ledger.record_usage(
                    role="image",
                    model=effective_model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    input_cost_per_1m=cfg.input_cost_per_1m,
                    output_cost_per_1m=cfg.output_cost_per_1m,
                    fixed_cost_usd=cfg.image_cost_per_call,
                )
                return img_resp
            except LLMRequestError as exc:
                if exc.status_code == 429:
                    self._mark_key_429("image", key_idx, cfg)
                    continue
                raise LLMRequestError(
                    redact_secrets(str(exc), cfg.api_keys),
                    status_code=exc.status_code,
                ) from None

        raise QuotaExhaustedError(
            f"All {len(cfg.api_keys)} credentials for role 'image' (model '{effective_model}') "
            "exhausted quota (HTTP 429). Refusing to downgrade model."
        )


def _read_http_error_redacted(exc: urllib.error.HTTPError, secrets: Sequence[str]) -> str:
    raw = ""
    if exc.fp is not None:
        try:
            raw = exc.fp.read(1024).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            raw = ""
    text = raw.strip() or str(exc.reason or exc)
    return redact_secrets(text, secrets)[:500]
