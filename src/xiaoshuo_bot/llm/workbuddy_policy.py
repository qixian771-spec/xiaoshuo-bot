from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .base import LLMConfigurationError, LLMRequestError, SHANGHAI_TZ, parse_model_reasoning_suffix

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


@dataclass(frozen=True)
class ModelSwitchEvent:
    """Structured record emitted whenever RoleModelRouter rotates to the next whitelisted model."""

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

    def to_dict(self) -> Dict[str, Any]:
        return {
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


def classify_workbuddy_quota_exhausted(exc: LLMRequestError) -> Tuple[bool, str]:
    """Determine whether an LLMRequestError represents verified WorkBuddy/CPA quota exhaustion.

    Evidence (read-only audit of ygq-future/cpa-workbuddy @ 2a1a608fb0ff28c51996393822a7f36df4367d5b
    and router-for-me/CLIProxyAPI v8.0.69):
    1. HTTP 429 (status_code == 429):
       - cpa-workbuddy/internal/workbuddy/chat_errors.go:107-108 -> code="rate_limit_exceeded", HTTPStatus=429
       - cpa-workbuddy/internal/workbuddy/chat_faults_test.go:25,29 -> HTTP 429
       - CLIProxyAPI v8.0.69 internal/pluginhost/rpc_client.go:51-106,455-460 &
         sdk/api/handlers/handlers_errors.go:108-122 -> preserves HTTP 429 to client
    2. WorkBuddy credit package exhausted (HTTP 4xx + upstream code 14018 / credit_package_exhausted):
       - cpa-workbuddy/internal/workbuddy/chat_errors.go:95-96 -> matches upstream code "14018" or
         "credits exhausted" / "额度已用尽", emits code="credit_package_exhausted",
         message="WorkBuddy requires an active credit package with available balance (upstream code 14018)",
         preserving upstream HTTPStatus (chat_faults_test.go:21 shows HTTP 400!)
       - cpa-workbuddy/internal/workbuddy/chat_faults_test.go:28 confirms strict numeric boundary
         (code 140185 MUST NOT match 14018).

    Non-quota errors MUST NEVER trigger model rotation:
    - Network / transport / DNS / timeout errors (status_code is None or 408)
    - Upstream 5xx (500, 502, 503, 504 including client_version_rejected 10085, upstream_stream_error)
    - Non-quota 400 (11128 channel_rejected, 11153 tool_continuation_rejected,
      11159 thinking_signature_rejected, context_length_exceeded, 140185)
    - 401 authentication errors (invalid_token, invalid_grant, 12153)
    - 403 account restriction / permission denied (11140 account_restricted, model_permission_denied)
    """
    status = exc.status_code
    if status is None:
        return False, ""
    if status == 408 or status >= 500:
        return False, ""

    error_code = (exc.error_code or "").strip().lower()
    upstream_code = (exc.upstream_code or "").strip()
    combined_raw = f"{exc}\n{exc.response_body}"
    combined_lower = combined_raw.lower()
    json_codes = _extract_json_code_tokens(exc.response_body)
    json_codes_lower = {c.lower() for c in json_codes}

    if status == 429:
        if (
            error_code == "credit_package_exhausted"
            or upstream_code == "14018"
            or "credit_package_exhausted" in json_codes_lower
            or "14018" in json_codes
            or _UPSTREAM_CODE_PHRASE_14018_RE.search(combined_raw)
            or "credits exhausted" in combined_lower
            or "额度已用尽" in combined_raw
        ):
            return True, "HTTP 429 · WorkBuddy 积分包额度已用尽 (credit_package_exhausted / 14018)"
        return True, "HTTP 429 · WorkBuddy 模型限流/额度用尽 (rate_limit_exceeded)"

    if 400 <= status < 500:
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
        if is_credit_exhausted:
            return (
                True,
                f"HTTP {status} · WorkBuddy 积分包额度已用尽 (credit_package_exhausted / upstream code 14018)",
            )

    return False, ""


def now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


__all__ = [
    "ModelSwitchEvent",
    "WORKBUDDY_MANUAL_ONLY_SLOTS",
    "WORKBUDDY_PLANNER_ROTATION_SLOTS",
    "WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH",
    "WORKBUDDY_SLOT_DISPLAY_NAMES",
    "WORKBUDDY_SLOT_HY4",
    "WORKBUDDY_SLOT_HY4_FLASH",
    "WORKBUDDY_SLOT_MIMO_V2_6_PRO",
    "WORKBUDDY_WHITELIST_SLOTS",
    "WORKBUDDY_WRITER_ROTATION_SLOTS",
    "classify_workbuddy_quota_exhausted",
    "find_forbidden_model_reason",
    "now_shanghai_iso",
    "validate_workbuddy_model_allowed",
    "validate_workbuddy_rotation_chain",
]
