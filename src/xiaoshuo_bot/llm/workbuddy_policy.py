from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .base import LLMConfigurationError, LLMError, LLMRequestError, SHANGHAI_TZ, parse_model_reasoning_suffix

# ---------------------------------------------------------------------------
# Immutable WorkBuddy Whitelist & Rotation Policy (Owner Decisions)
# ---------------------------------------------------------------------------

WORKBUDDY_SLOT_HY4_FLASH = "hy4_flash"
WORKBUDDY_SLOT_HY4 = "hy4"
WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH = "deepseek_v4_1_flash"
WORKBUDDY_SLOT_MIMO_V2_6_PRO = "mimo_v2_6_pro"

WORKBUDDY_WHITELIST_SLOTS: Tuple[str, ...] = (
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_HY4,
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
)

WORKBUDDY_SLOT_DISPLAY_NAMES: Dict[str, str] = {
    WORKBUDDY_SLOT_HY4_FLASH: "Hy4 flash",
    WORKBUDDY_SLOT_HY4: "Hy4",
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: "DeepSeek-V4.1-Flash",
    WORKBUDDY_SLOT_MIMO_V2_6_PRO: "Xiaomi MiMo-V2.6-Pro",
}

# Fixed automatic rotation chains (Hy4 MUST NEVER enter automatic rotation)
WORKBUDDY_WRITER_ROTATION_SLOTS: Tuple[str, ...] = (
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
)
WORKBUDDY_PLANNER_ROTATION_SLOTS: Tuple[str, ...] = (
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
)
WORKBUDDY_MANUAL_ONLY_SLOTS: Tuple[str, ...] = (WORKBUDDY_SLOT_HY4,)

# Explicit environment variable names (zero hardcoded guessed upstream model IDs!)
WORKBUDDY_MODEL_ENV_KEYS: Dict[str, str] = {
    WORKBUDDY_SLOT_HY4_FLASH: "WORKBUDDY_MODEL_HY4_FLASH",
    WORKBUDDY_SLOT_HY4: "WORKBUDDY_MODEL_HY4",
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: "WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH",
    WORKBUDDY_SLOT_MIMO_V2_6_PRO: "WORKBUDDY_MODEL_MIMO_V2_6_PRO",
}

# Configurable rate-limit backoff & exhausted-model cooldown defaults (owner review fixes):
# - HTTP 429 rate_limit_exceeded retries on the SAME model with backoff before switching.
# - Exhausted models cool down for 1 hour (3600s) by default (not 60s).
DEFAULT_RATE_LIMIT_BACKOFF_SEC: Tuple[float, ...] = (30.0, 60.0, 120.0)
DEFAULT_MODEL_QUOTA_COOLDOWN_SEC: float = 3600.0
# WorkBuddy models think before answering and the hidden reasoning counts against max_tokens.
# Measured 2026-11-19 on wb/hy4flash-free (only 'high' effort offered): one 2,324-char chapter
# used 27,728 reasoning + ~1,800 visible tokens. Every WorkBuddy call therefore gets this much
# extra output headroom on top of the caller's visible-text budget. Billing follows actual tokens.
WORKBUDDY_REASONING_HEADROOM_TOKENS: int = 32768
WORKBUDDY_RATE_LIMIT_BACKOFF_ENV = "WORKBUDDY_RATE_LIMIT_BACKOFF_SEC"
WORKBUDDY_MODEL_COOLDOWN_ENV = "WORKBUDDY_MODEL_COOLDOWN_SEC"

# Forbidden model patterns:
# - DeepSeek-V4-Pro (and prefix/separator variants)
# - Auto-routing modes: auto / auto-* / 快速 / 均衡 / 极致
_FORBIDDEN_DEEPSEEK_V4_PRO_RE = re.compile(
    r"(?:^|[/:_\-])deepseek[-_.]?v4[-_.]?pro(?:$|[/:_\-])",
    re.IGNORECASE,
)
_FORBIDDEN_AUTO_ROUTE_RE = re.compile(
    r"^(?:auto)(?:$|[-_./:].*)$",
    re.IGNORECASE,
)
_FORBIDDEN_CHINESE_AUTO_TIER_TOKENS: Tuple[str, ...] = ("快速", "均衡", "极致")

# Verified quota-exhausted markers from ygq-future/cpa-workbuddy @ 2a1a608fb0ff28c51996393822a7f36df4367d5b
#   - internal/workbuddy/chat_errors.go:95-96  -> upstream code "14018", "credits exhausted", "额度已用尽",
#                                                 plugin Code "credit_package_exhausted",
#                                                 plugin Message "WorkBuddy requires an active credit package with available balance (upstream code 14018)"
#   - internal/workbuddy/chat_errors.go:107-108 -> HTTP 429, plugin Code "rate_limit_exceeded",
#                                                  plugin Message "WorkBuddy rate limit exceeded"
#   - internal/workbuddy/chat_faults_test.go:21,25,28,29 -> credit_package_exhausted can carry HTTP 400;
#                                                           140185 MUST NOT match 14018 (strict numeric boundary).
_UPSTREAM_14018_EXACT_RE = re.compile(r"(?<!\d)14018(?!\d)")
_JSON_CODE_14018_RE = re.compile(
    r'"(?:upstream_code|internal_code|code)"\s*:\s*"?14018"?(?=\s*[,}])',
    re.IGNORECASE,
)
_UPSTREAM_CODE_PHRASE_14018_RE = re.compile(
    r"upstream\s+code\s+14018(?!\d)",
    re.IGNORECASE,
)


def parse_rate_limit_backoff_sec(raw: Any) -> Tuple[float, ...]:
    """Parse configurable 429 rate-limit backoff delays (CSV string or sequence of non-negative numbers)."""
    if raw is None:
        return DEFAULT_RATE_LIMIT_BACKOFF_SEC
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return ()
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    elif isinstance(raw, Sequence):
        parts = list(raw)
    else:
        raise LLMConfigurationError(
            f"rate_limit_backoff_sec must be a sequence or CSV string of non-negative seconds, got {type(raw).__name__}."
        )
    delays: list[float] = []
    for item in parts:
        try:
            val = float(item)
        except (TypeError, ValueError) as exc:
            raise LLMConfigurationError(
                f"Invalid rate_limit_backoff_sec entry {item!r}: {exc}"
            ) from None
        if val < 0:
            raise LLMConfigurationError(
                f"rate_limit_backoff_sec entries must be >= 0, got {val}."
            )
        delays.append(val)
    return tuple(delays)


@dataclass(frozen=True)
class ModelSwitchEvent:
    """Structured record emitted whenever RoleModelRouter switches or restores a whitelisted model."""

    role: str
    from_model: str
    to_model: str
    reason: str
    timestamp: str
    book_id: str = ""
    chapter: int = 0
    from_label: str = ""
    to_label: str = ""
    status_code: Optional[int] = None
    upstream_code: str = ""
    event_type: str = "model_switched"  # "model_switched" | "model_restored"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "role": self.role,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "from_label": self.from_label or self.from_model,
            "to_label": self.to_label or self.to_model,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "book_id": self.book_id,
            "chapter": int(self.chapter),
            "status_code": self.status_code,
            "upstream_code": self.upstream_code,
        }


@dataclass(frozen=True)
class RateLimitRetryEvent:
    """Structured record emitted on each same-model backoff retry after HTTP 429 rate_limit_exceeded."""

    role: str
    model: str
    attempt: int
    max_retries: int
    wait_seconds: float
    reason: str
    timestamp: str
    book_id: str = ""
    chapter: int = 0
    model_label: str = ""
    status_code: int = 429

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": "rate_limit_retry",
            "role": self.role,
            "model": self.model,
            "model_label": self.model_label or self.model,
            "attempt": int(self.attempt),
            "max_retries": int(self.max_retries),
            "wait_seconds": float(self.wait_seconds),
            "reason": self.reason,
            "timestamp": self.timestamp,
            "book_id": self.book_id,
            "chapter": int(self.chapter),
            "status_code": int(self.status_code),
        }


@dataclass(frozen=True)
class ModelQuotaSnapshot:
    """WorkBuddy usage.quota snapshot captured from an upstream chat completion response."""

    model: str
    raw_model: str
    model_label: str
    percent: float
    reset_at: str
    reset_at_raw: str = ""
    shared_models: Tuple[str, ...] = ()
    role: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "raw_model": self.raw_model,
            "model_label": self.model_label or self.model,
            "percent": float(self.percent),
            "reset_at": self.reset_at,
            "reset_at_raw": self.reset_at_raw,
            "shared_models": list(self.shared_models),
            "role": self.role,
            "updated_at": self.updated_at,
        }


_ALNUM_ONLY_RE = re.compile(r"[^a-z0-9]+")
_NUMERIC_EPOCH_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def _normalize_quota_reset_at(raw_value: Any) -> Tuple[str, str]:
    """Normalize WorkBuddy usage.quota.reset_at (unix epoch sec/ms or ISO string) into Asia/Shanghai ISO-8601."""
    if raw_value is None or isinstance(raw_value, bool):
        return "", ""
    if isinstance(raw_value, (int, float)):
        if math.isnan(raw_value) or math.isinf(raw_value):
            return "", ""
        raw_str = str(int(raw_value)) if float(raw_value).is_integer() else str(raw_value)
        ts_val = float(raw_value)
        if abs(ts_val) >= 1e11:
            ts_val /= 1000.0
        try:
            return datetime.fromtimestamp(ts_val, tz=SHANGHAI_TZ).isoformat(), raw_str
        except (OSError, OverflowError, ValueError):
            return raw_str, raw_str

    raw_str = str(raw_value).strip()
    if not raw_str:
        return "", ""
    if _NUMERIC_EPOCH_RE.match(raw_str):
        ts_val = float(raw_str)
        if abs(ts_val) >= 1e11:
            ts_val /= 1000.0
        try:
            return datetime.fromtimestamp(ts_val, tz=SHANGHAI_TZ).isoformat(), raw_str
        except (OSError, OverflowError, ValueError):
            return raw_str, raw_str

    iso_candidate = raw_str[:-1] + "+00:00" if raw_str.endswith("Z") else raw_str
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
        else:
            parsed = parsed.astimezone(SHANGHAI_TZ)
        return parsed.isoformat(), raw_str
    except ValueError:
        return raw_str, raw_str


def _match_model_id_in_whitelist(
    candidate: str,
    whitelist: Sequence[str],
) -> Optional[str]:
    cleaned = (candidate or "").strip()
    if not cleaned:
        return None
    for item in whitelist:
        if item == cleaned:
            return item
    cand_bare = _strip_site_prefix(cleaned).lower()
    for item in whitelist:
        if _strip_site_prefix(item).lower() == cand_bare:
            return item
    cand_norm = _ALNUM_ONLY_RE.sub("", cand_bare)
    if cand_norm:
        for item in whitelist:
            item_norm = _ALNUM_ONLY_RE.sub("", _strip_site_prefix(item).lower())
            if item_norm == cand_norm or (
                len(cand_norm) >= 4
                and (item_norm.startswith(cand_norm) or cand_norm.startswith(item_norm))
            ):
                return item
    return None


def extract_workbuddy_usage_quota(
    usage: Optional[Mapping[str, Any]],
    *,
    requested_model: str = "",
    catalog: Optional["WorkBuddyModelCatalog"] = None,
    model_labels: Optional[Mapping[str, str]] = None,
    role: str = "",
    updated_at: str = "",
) -> Optional[ModelQuotaSnapshot]:
    """Extract WorkBuddy usage.quota ({model, percent, reset_at, shared_models}) from a response usage mapping.

    Returns None when usage.quota is absent or lacks a valid numeric percent (never fabricates 0%).
    """
    if not isinstance(usage, Mapping):
        return None
    raw_quota = usage.get("quota")
    if not isinstance(raw_quota, Mapping) or not raw_quota:
        return None

    raw_percent = raw_quota.get("percent")
    if raw_percent is None or isinstance(raw_percent, bool):
        return None
    try:
        percent_val = float(raw_percent)
    except (TypeError, ValueError):
        return None
    if math.isnan(percent_val) or math.isinf(percent_val):
        return None

    raw_model = str(raw_quota.get("model") or "").strip()
    req_model = (requested_model or "").strip()

    labels_map: Dict[str, str] = {}
    whitelist: Tuple[str, ...] = ()
    if catalog is not None:
        whitelist = catalog.whitelist()
        labels_map.update(catalog.labels())
    if model_labels:
        for k, v in model_labels.items():
            if str(k).strip() and str(v).strip():
                labels_map[str(k).strip()] = str(v).strip()

    canonical_model = ""
    if req_model:
        matched_req = _match_model_id_in_whitelist(req_model, whitelist) if whitelist else None
        canonical_model = matched_req or req_model
    elif raw_model:
        matched_raw = _match_model_id_in_whitelist(raw_model, whitelist) if whitelist else None
        canonical_model = matched_raw or raw_model

    if not canonical_model:
        return None

    display_label = labels_map.get(canonical_model, "").strip() or canonical_model
    reset_at_iso, reset_at_raw = _normalize_quota_reset_at(raw_quota.get("reset_at"))

    shared_raw = raw_quota.get("shared_models")
    shared_models: Tuple[str, ...] = ()
    if isinstance(shared_raw, Sequence) and not isinstance(shared_raw, (str, bytes)):
        shared_models = tuple(str(x).strip() for x in shared_raw if str(x).strip())

    ts = (updated_at or "").strip() or now_shanghai_iso()
    return ModelQuotaSnapshot(
        model=canonical_model,
        raw_model=raw_model or canonical_model,
        model_label=display_label,
        percent=round(percent_val, 2),
        reset_at=reset_at_iso,
        reset_at_raw=reset_at_raw,
        shared_models=shared_models,
        role=(role or "").strip(),
        updated_at=ts,
    )


def _strip_site_prefix(model_id: str) -> str:
    base, _ = parse_model_reasoning_suffix(model_id)
    cleaned = base.strip()
    if "/" in cleaned:
        prefix, rest = cleaned.split("/", 1)
        if prefix.strip().lower() in {"wb", "wb-ai", "workbuddy", "workbuddy-ai", "cn", "intl"}:
            return rest.strip()
    return cleaned


def find_forbidden_model_reason(model_id: str) -> Optional[str]:
    """Return a human-readable rejection reason if model_id matches a forbidden model or auto-routing mode."""
    raw = (model_id or "").strip()
    if not raw:
        return "模型 ID 不能为空"
    base, _ = parse_model_reasoning_suffix(raw)
    bare = _strip_site_prefix(base)

    if _FORBIDDEN_DEEPSEEK_V4_PRO_RE.search(base) or _FORBIDDEN_DEEPSEEK_V4_PRO_RE.search(bare):
        return f"禁止使用 DeepSeek-V4-Pro（收到 {raw!r}）"

    if _FORBIDDEN_AUTO_ROUTE_RE.match(bare) or _FORBIDDEN_AUTO_ROUTE_RE.match(base):
        return f"禁止使用自动路由模型（收到 {raw!r}）"

    for token in _FORBIDDEN_CHINESE_AUTO_TIER_TOKENS:
        if token in raw:
            return f"禁止使用自动路由档位「{token}」（收到 {raw!r}）"

    return None


def validate_workbuddy_model_allowed(
    model_id: str,
    whitelist: Sequence[str],
    *,
    context: str = "",
) -> str:
    """Enforce forbidden-model hard block + default-deny whitelist check."""
    cleaned = (model_id or "").strip()
    prefix = f"[{context}] " if context else ""
    if not cleaned:
        raise LLMConfigurationError(f"{prefix}模型 ID 不能为空。")

    forbidden_reason = find_forbidden_model_reason(cleaned)
    if forbidden_reason:
        raise LLMConfigurationError(f"{prefix}{forbidden_reason}。")

    allowed = tuple(str(m).strip() for m in whitelist if str(m).strip())
    if not allowed:
        raise LLMConfigurationError(f"{prefix}白名单不能为空。")

    if cleaned not in allowed:
        raise LLMConfigurationError(
            f"{prefix}模型 {cleaned!r} 不在允许的白名单内（允许列表：{', '.join(allowed)}）。"
        )
    return cleaned


def validate_workbuddy_rotation_chain(
    *,
    role: str,
    primary_model: str,
    rotation_models: Sequence[str],
    whitelist: Sequence[str],
    manual_only_models: Sequence[str] = (),
) -> Tuple[str, ...]:
    """Validate whitelist, forbidden models, manual-only exclusion (Hy4), and rotation order invariants."""
    cleaned_whitelist = tuple(dict.fromkeys(str(m).strip() for m in whitelist if str(m).strip()))
    if not cleaned_whitelist:
        raise LLMConfigurationError(f"role={role!r}: 启用白名单/轮换策略时 model_whitelist 不能为空。")

    for wl_model in cleaned_whitelist:
        reason = find_forbidden_model_reason(wl_model)
        if reason:
            raise LLMConfigurationError(f"role={role!r}: 白名单包含禁止项：{reason}。")

    cleaned_manual_only = tuple(
        dict.fromkeys(str(m).strip() for m in manual_only_models if str(m).strip())
    )
    for manual_model in cleaned_manual_only:
        validate_workbuddy_model_allowed(
            manual_model, cleaned_whitelist, context=f"role={role} manual_only_models"
        )

    cleaned_primary = validate_workbuddy_model_allowed(
        primary_model, cleaned_whitelist, context=f"role={role} primary model"
    )

    cleaned_rotation: list[str] = []
    seen: set[str] = set()
    for item in rotation_models:
        m = str(item).strip()
        if not m:
            continue
        validate_workbuddy_model_allowed(m, cleaned_whitelist, context=f"role={role} rotation_models")
        if m in cleaned_manual_only:
            raise LLMConfigurationError(
                f"role={role!r}: 手动专用模型 {m!r}（Hy4）严禁进入自动轮换链。"
            )
        if m in seen:
            raise LLMConfigurationError(
                f"role={role!r}: rotation_models 包含重复模型 {m!r}。"
            )
        seen.add(m)
        cleaned_rotation.append(m)

    if cleaned_primary in cleaned_manual_only and cleaned_rotation:
        raise LLMConfigurationError(
            f"role={role!r}: 当前角色已手动指定专用模型 {cleaned_primary!r}（Hy4），严禁配置自动轮换链。"
        )

    if cleaned_rotation and cleaned_rotation[0] != cleaned_primary:
        raise LLMConfigurationError(
            f"role={role!r}: rotation_models 首项 {cleaned_rotation[0]!r} 必须与主模型 {cleaned_primary!r} 一致。"
        )

    return tuple(cleaned_rotation)


def _extract_json_code_tokens(raw_text: str) -> Tuple[str, ...]:
    """Best-effort extraction of top-level/nested error code tokens from a JSON error body."""
    text = (raw_text or "").strip()
    if not text or not text.startswith("{"):
        return ()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return ()

    codes: list[str] = []

    def _visit(node: Any, depth: int) -> None:
        if depth > 6 or not isinstance(node, Mapping):
            return
        for key in ("code", "internal_code", "upstream_code", "type"):
            val = node.get(key)
            if isinstance(val, (str, int)) and not isinstance(val, bool):
                token = str(val).strip()
                if token:
                    codes.append(token)
        for child_key in ("error", "data"):
            child = node.get(child_key)
            if isinstance(child, Mapping):
                _visit(child, depth + 1)

    _visit(obj, 0)
    return tuple(codes)


def classify_workbuddy_failure_kind(exc: LLMRequestError) -> Tuple[str, str]:
    """Classify an LLMRequestError into ('credit_exhausted' | 'rate_limit' | 'non_quota', reason).

    - 'credit_exhausted': Explicit credit package exhaustion (cpa-workbuddy chat_errors.go:95-96:
      code='credit_package_exhausted' / upstream code 14018 / 'credits exhausted' / '额度已用尽').
      Rotates immediately without backoff wait.
    - 'rate_limit': HTTP 429 rate_limit_exceeded without explicit credit exhaustion
      (cpa-workbuddy chat_errors.go:107-108). Retries on the SAME model with configured backoff
      (default 30s / 60s / 120s, 3 retries) before judging the model exhausted and rotating.
    - 'non_quota': Network error, timeout (408/504), 5xx, 401 auth error, 403 account_restricted /
      model_permission_denied, non-14018 400 (including boundary code 140185). Never rotates.
    """
    status = exc.status_code
    if status is None:
        return "non_quota", ""
    if status == 408 or status >= 500:
        return "non_quota", ""

    error_code = (exc.error_code or "").strip().lower()
    upstream_code = (exc.upstream_code or "").strip()
    combined_raw = f"{exc}\n{exc.response_body}"
    combined_lower = combined_raw.lower()
    json_codes = _extract_json_code_tokens(exc.response_body)
    json_codes_lower = {c.lower() for c in json_codes}

    is_credit_exhausted = (
        error_code == "credit_package_exhausted"
        or upstream_code == "14018"
        or "credit_package_exhausted" in json_codes_lower
        or "14018" in json_codes
        or "credit_package_exhausted" in combined_lower
        or "workbuddy requires an active credit package with available balance" in combined_lower
        or "credits exhausted" in combined_lower
        or "额度已用尽" in combined_raw
        or bool(_UPSTREAM_CODE_PHRASE_14018_RE.search(combined_raw))
        or bool(_JSON_CODE_14018_RE.search(combined_raw))
    )

    if status == 429:
        if is_credit_exhausted:
            return (
                "credit_exhausted",
                "HTTP 429 · WorkBuddy 积分包额度已用尽 (credit_package_exhausted / 14018)",
            )
        return "rate_limit", "HTTP 429 · WorkBuddy 模型限流 (rate_limit_exceeded)"

    if 400 <= status < 500 and is_credit_exhausted:
        return (
            "credit_exhausted",
            f"HTTP {status} · WorkBuddy 积分包额度已用尽 (credit_package_exhausted / upstream code 14018)",
        )

    return "non_quota", ""


def classify_workbuddy_quota_exhausted(exc: LLMRequestError) -> Tuple[bool, str]:
    """Return (is_quota_or_rate_limit, reason) backed by cpa-workbuddy @ 2a1a608 & CPA v8.0.69."""
    kind, reason = classify_workbuddy_failure_kind(exc)
    if kind == "non_quota":
        return False, ""
    return True, reason


def now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


@dataclass(frozen=True)
class WorkBuddyModelCatalog:
    """Explicit configuration mapping the 4 whitelisted WorkBuddy logical slots to real CPA upstream IDs.

    Zero hardcoded guessed upstream IDs: every slot MUST be explicitly configured by operator.
    """

    hy4_flash: str
    hy4: str
    deepseek_v4_1_flash: str
    mimo_v2_6_pro: str

    def __post_init__(self) -> None:
        slot_values = {
            WORKBUDDY_SLOT_HY4_FLASH: (self.hy4_flash or "").strip(),
            WORKBUDDY_SLOT_HY4: (self.hy4 or "").strip(),
            WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: (self.deepseek_v4_1_flash or "").strip(),
            WORKBUDDY_SLOT_MIMO_V2_6_PRO: (self.mimo_v2_6_pro or "").strip(),
        }
        missing = [
            f"{slot} ({WORKBUDDY_SLOT_DISPLAY_NAMES[slot]} / env {WORKBUDDY_MODEL_ENV_KEYS[slot]})"
            for slot, val in slot_values.items()
            if not val
        ]
        if missing:
            raise LLMConfigurationError(
                "WorkBuddy 白名单模型配置缺失，严禁使用硬编码猜测值，请显式配置："
                + "、".join(missing)
            )
        object.__setattr__(self, "hy4_flash", slot_values[WORKBUDDY_SLOT_HY4_FLASH])
        object.__setattr__(self, "hy4", slot_values[WORKBUDDY_SLOT_HY4])
        object.__setattr__(
            self, "deepseek_v4_1_flash", slot_values[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH]
        )
        object.__setattr__(self, "mimo_v2_6_pro", slot_values[WORKBUDDY_SLOT_MIMO_V2_6_PRO])

        seen_ids: Dict[str, str] = {}
        for slot in WORKBUDDY_WHITELIST_SLOTS:
            model_id = slot_values[slot]
            forbidden = find_forbidden_model_reason(model_id)
            if forbidden:
                raise LLMConfigurationError(
                    f"WorkBuddy 白名单槽位 {slot!r}（{WORKBUDDY_SLOT_DISPLAY_NAMES[slot]}）"
                    f"配置了禁止模型：{forbidden}。"
                )
            if model_id in seen_ids:
                raise LLMConfigurationError(
                    f"WorkBuddy 白名单槽位 {slot!r} 与 {seen_ids[model_id]!r} 重复映射到同一上游模型 ID {model_id!r}。"
                )
            seen_ids[model_id] = slot

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WorkBuddyModelCatalog":
        if not isinstance(data, Mapping):
            raise LLMConfigurationError("workbuddy_models 配置必须是一个字典映射。")
        allowed_keys = set(WORKBUDDY_WHITELIST_SLOTS)
        unknown_keys = sorted(str(k) for k in data.keys() if str(k) not in allowed_keys)
        if unknown_keys:
            raise LLMConfigurationError(
                f"workbuddy_models 包含白名单以外的非法配置项：{', '.join(unknown_keys)}。"
                f"仅允许：{', '.join(WORKBUDDY_WHITELIST_SLOTS)}。"
            )
        return cls(
            hy4_flash=str(data.get(WORKBUDDY_SLOT_HY4_FLASH) or ""),
            hy4=str(data.get(WORKBUDDY_SLOT_HY4) or ""),
            deepseek_v4_1_flash=str(data.get(WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH) or ""),
            mimo_v2_6_pro=str(data.get(WORKBUDDY_SLOT_MIMO_V2_6_PRO) or ""),
        )

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        *,
        overrides: Optional[Mapping[str, Optional[str]]] = None,
    ) -> Optional["WorkBuddyModelCatalog"]:
        env = environ if environ is not None else os.environ
        ov = overrides or {}
        resolved: Dict[str, str] = {}
        any_present = False
        for slot in WORKBUDDY_WHITELIST_SLOTS:
            env_name = WORKBUDDY_MODEL_ENV_KEYS[slot]
            raw = ov.get(slot)
            if raw is None or not str(raw).strip():
                raw = env.get(env_name)
            cleaned = str(raw or "").strip()
            if cleaned:
                any_present = True
            resolved[slot] = cleaned
        if not any_present:
            return None
        return cls.from_mapping(resolved)

    def slot_to_model(self) -> Dict[str, str]:
        return {
            WORKBUDDY_SLOT_HY4_FLASH: self.hy4_flash,
            WORKBUDDY_SLOT_HY4: self.hy4,
            WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: self.deepseek_v4_1_flash,
            WORKBUDDY_SLOT_MIMO_V2_6_PRO: self.mimo_v2_6_pro,
        }

    def whitelist(self) -> Tuple[str, ...]:
        return (
            self.hy4_flash,
            self.hy4,
            self.deepseek_v4_1_flash,
            self.mimo_v2_6_pro,
        )

    def manual_only(self) -> Tuple[str, ...]:
        return (self.hy4,)

    def writer_rotation(self) -> Tuple[str, ...]:
        return (
            self.hy4_flash,
            self.deepseek_v4_1_flash,
            self.mimo_v2_6_pro,
        )

    def planner_rotation(self) -> Tuple[str, ...]:
        return (
            self.deepseek_v4_1_flash,
            self.mimo_v2_6_pro,
        )

    def labels(self) -> Dict[str, str]:
        return {
            self.hy4_flash: WORKBUDDY_SLOT_DISPLAY_NAMES[WORKBUDDY_SLOT_HY4_FLASH],
            self.hy4: WORKBUDDY_SLOT_DISPLAY_NAMES[WORKBUDDY_SLOT_HY4],
            self.deepseek_v4_1_flash: WORKBUDDY_SLOT_DISPLAY_NAMES[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
            self.mimo_v2_6_pro: WORKBUDDY_SLOT_DISPLAY_NAMES[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
        }

    def default_role_model_and_rotation(
        self,
        role: str,
        *,
        override_model: Optional[str] = None,
    ) -> Tuple[str, Tuple[str, ...]]:
        """Resolve (primary_model, rotation_models) for one text role under owner constraints."""
        base_chain = self.planner_rotation() if role == "planner" else self.writer_rotation()
        if not override_model or not str(override_model).strip():
            return base_chain[0], base_chain

        chosen = validate_workbuddy_model_allowed(
            str(override_model).strip(),
            self.whitelist(),
            context=f"role={role}",
        )
        if chosen in self.manual_only():
            return chosen, ()
        if chosen in base_chain:
            idx = base_chain.index(chosen)
            return chosen, base_chain[idx:]
        # e.g. planner explicitly overridden to hy4_flash -> start from hy4_flash forward along full rotation
        full_chain = self.writer_rotation()
        if chosen in full_chain:
            idx = full_chain.index(chosen)
            return chosen, full_chain[idx:]
        return chosen, ()

    def to_dict(self) -> Dict[str, str]:
        return self.slot_to_model()


def verify_router_models_against_cpa(
    router: Any,
    *,
    catalog: Optional[WorkBuddyModelCatalog] = None,
    probe_role: Optional[str] = None,
) -> Dict[str, Any]:
    """Startup self-check: validate configured models against whitelist AND live CPA GET /v1/models.

    Raises LLMConfigurationError (refusing service startup) whenever:
    - Any configured role model or rotation model is forbidden or outside the allowed whitelist;
    - CPA /v1/models cannot be queried or returns no models;
    - Any configured whitelist/role model does not exist in CPA /v1/models (listing exact model IDs).
    """
    text_roles = ("planner", "writer", "critic", "polisher")
    configured_roles = [r for r in text_roles if router.has_role(r)]
    if not configured_roles:
        raise LLMConfigurationError("启动自检失败：RoleModelRouter 未配置任何文本角色。")

    effective_catalog = catalog or getattr(router, "workbuddy_catalog", None)
    required_models: Dict[str, list[str]] = {}

    if effective_catalog is not None:
        whitelist = effective_catalog.whitelist()
        for slot, model_id in effective_catalog.slot_to_model().items():
            required_models.setdefault(model_id, []).append(
                f"白名单槽位 {slot}（{WORKBUDDY_SLOT_DISPLAY_NAMES[slot]}）"
            )
    else:
        whitelist = ()

    for role in configured_roles:
        cfg = router.get_role_config(role)
        role_wl = whitelist or cfg.model_whitelist
        if role_wl:
            validate_workbuddy_model_allowed(
                cfg.model, role_wl, context=f"启动自检 role={role} model"
            )
            for rot_model in cfg.rotation_models:
                validate_workbuddy_model_allowed(
                    rot_model, role_wl, context=f"启动自检 role={role} rotation_models"
                )
        else:
            forbidden = find_forbidden_model_reason(cfg.model)
            if forbidden:
                raise LLMConfigurationError(f"启动自检拒绝启动 [role={role}]：{forbidden}。")

        required_models.setdefault(cfg.model, []).append(f"角色 {role} 主模型")
        for rot_model in cfg.rotation_models:
            required_models.setdefault(rot_model, []).append(f"角色 {role} 轮换链")
        for wl_model in cfg.model_whitelist:
            required_models.setdefault(wl_model, []).append(f"角色 {role} 白名单")

    selected_probe_role = (
        probe_role
        if probe_role and router.has_role(probe_role)
        else ("writer" if router.has_role("writer") else configured_roles[0])
    )
    try:
        available_list = list(router.list_models(role=selected_probe_role))
    except LLMError as exc:
        raise LLMConfigurationError(
            f"启动自检拒绝启动：无法从 CPA /v1/models 获取可用模型清单（role={selected_probe_role}）：{exc}"
        ) from None

    available_set = {str(m).strip() for m in available_list if str(m).strip()}
    if not available_set:
        raise LLMConfigurationError(
            f"启动自检拒绝启动：CPA /v1/models 返回空模型列表（role={selected_probe_role}），请先确认 WorkBuddy 登录状态。"
        )

    missing_details: list[str] = []
    for model_id, usages in required_models.items():
        if model_id not in available_set:
            uniq_usages = "、".join(dict.fromkeys(usages))
            missing_details.append(f"{model_id!r}（用于：{uniq_usages}）")

    if missing_details:
        raise LLMConfigurationError(
            "启动自检拒绝启动：以下已配置模型在 CPA /v1/models 中不存在："
            + "；".join(missing_details)
            + f"。CPA 当前返回 {len(available_set)} 个模型：{', '.join(sorted(available_set)[:12])}"
            + ("..." if len(available_set) > 12 else "")
        )

    return {
        "verified": True,
        "probe_role": selected_probe_role,
        "checked_models": sorted(required_models.keys()),
        "available_count": len(available_set),
    }


__all__ = [
    "DEFAULT_MODEL_QUOTA_COOLDOWN_SEC",
    "WORKBUDDY_REASONING_HEADROOM_TOKENS",
    "DEFAULT_RATE_LIMIT_BACKOFF_SEC",
    "ModelQuotaSnapshot",
    "ModelSwitchEvent",
    "RateLimitRetryEvent",
    "WORKBUDDY_MANUAL_ONLY_SLOTS",
    "WORKBUDDY_MODEL_COOLDOWN_ENV",
    "WORKBUDDY_MODEL_ENV_KEYS",
    "WORKBUDDY_PLANNER_ROTATION_SLOTS",
    "WORKBUDDY_RATE_LIMIT_BACKOFF_ENV",
    "WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH",
    "WORKBUDDY_SLOT_DISPLAY_NAMES",
    "WORKBUDDY_SLOT_HY4",
    "WORKBUDDY_SLOT_HY4_FLASH",
    "WORKBUDDY_SLOT_MIMO_V2_6_PRO",
    "WORKBUDDY_WHITELIST_SLOTS",
    "WORKBUDDY_WRITER_ROTATION_SLOTS",
    "WorkBuddyModelCatalog",
    "classify_workbuddy_failure_kind",
    "classify_workbuddy_quota_exhausted",
    "extract_workbuddy_usage_quota",
    "find_forbidden_model_reason",
    "now_shanghai_iso",
    "parse_rate_limit_backoff_sec",
    "validate_workbuddy_model_allowed",
    "validate_workbuddy_rotation_chain",
    "verify_router_models_against_cpa",
]
