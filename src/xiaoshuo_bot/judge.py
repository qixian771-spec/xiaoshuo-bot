"""Cross-model blind pairwise judge and volume-arc reviewer (L2/L3).

Model equivalence / same-family blocking rules enforced in ``PairwiseJudge``:
1. Reuse ``xiaoshuo_bot.llm.workbuddy_policy._strip_site_prefix`` (which strips reasoning-effort
   suffixes like ``(high)`` and site prefixes such as ``wb/``, ``wb-ai/``, ``workbuddy/``,
   ``cn/``, ``intl/``) and ``_ALNUM_ONLY_RE`` to canonicalize bare model IDs (e.g.
   ``wb/deepseek-v4.1-flash`` <-> ``deepseek_v4_1_flash``, ``wb/mimo-v2.6-pro`` <->
   ``mimo_v2_6_pro``, ``wb/hy4`` <-> ``hy4``), plus stripping trailing tier/release tags such as
   ``-free`` / ``-latest``.
2. Map WorkBuddy aliases onto the 4 whitelisted slots (``hy4_flash``, ``hy4``,
   ``deepseek_v4_1_flash``, ``mimo_v2_6_pro``) so e.g. ``wb/hy4flash-free`` and ``hy4_flash``
   resolve to the same slot ``hy4_flash``.
3. Enforce cross-family isolation: models sharing a model family prefix (``hunyuan`` covering
   both ``hy4`` and ``hy4_flash`` / ``wb/hy4flash-free``, ``deepseek``, ``mimo``, ``gemini``,
   ``claude``, ``qwen``, ``glm``, ``gpt``, ``doubao``, ``kimi``, ``ernie``, ``llama``,
   ``mistral``) are blocked together with ``SameModelJudgeError`` so e.g. ``hy4`` cannot judge
   ``hy4_flash`` prose and ``deepseek-*`` cannot judge another ``deepseek-*`` model.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC, Sequence as SequenceABC
from dataclasses import asdict, dataclass, field
import json
import logging
import random
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse
from .llm.workbuddy_policy import (
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_HY4,
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
    WORKBUDDY_WHITELIST_SLOTS,
    _ALNUM_ONLY_RE,
    _match_model_id_in_whitelist,
    _strip_site_prefix,
)

logger = logging.getLogger(__name__)

MAX_EMPTY_RESPONSE_RETRIES: int = 1
VALID_PAIRWISE_RAW_WINNERS = frozenset({"A", "B", "TIE"})
VALID_ARC_VERDICTS = frozenset({"PASS", "FAIL"})
VALID_ARC_PROBLEM_TYPES = frozenset({"同质化", "原地踏步", "爽感疲劳", "工具人", "跑偏"})
VALID_VOLUME_HEALTH_CHECK_IDS: Tuple[str, ...] = (
    "VH_01_NO_ROUTINE_REPEAT",
    "VH_02_FORESHADOW_AND_REWARD",
    "VH_03_CHARACTER_AND_ARC",
    "VH_04_CORE_FLAVOR_QUOTA",
    "VH_05_THREE_ACT_AND_HOOK",
    "VH_06_LEDGER_AND_DEBT_MATH",
)
VALID_VOLUME_HEALTH_STATUSES = frozenset({"PASS", "FAIL"})

_TIER_SUFFIX_RE = re.compile(r"(?:[-_./]?(?:free|latest))$", re.IGNORECASE)

_EXPLICIT_SLOT_ALIASES: Dict[str, str] = {
    "hy4flash": WORKBUDDY_SLOT_HY4_FLASH,
    "hy4flashfree": WORKBUDDY_SLOT_HY4_FLASH,
    "hy4": WORKBUDDY_SLOT_HY4,
    "hy4free": WORKBUDDY_SLOT_HY4,
    "deepseekv41flash": WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    "mimov26pro": WORKBUDDY_SLOT_MIMO_V2_6_PRO,
    "xiaomimimov26pro": WORKBUDDY_SLOT_MIMO_V2_6_PRO,
}

# Known cross-vendor / cross-architecture model families.
# Note: hy4 and hy4_flash (including wb/hy4flash-free) are intentionally grouped into the
# same "hunyuan" family so cross-family judging requires an independent family (e.g. deepseek/mimo).
_FAMILY_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:hy\d+(?:flash|pro|lite|mini|free)?|hy(?=$|[-_./])|hunyuan)", re.IGNORECASE), "hunyuan"),
    (re.compile(r"^deepseek", re.IGNORECASE), "deepseek"),
    (re.compile(r"^(?:xiaomi[-_./]*)?mimo", re.IGNORECASE), "mimo"),
    (re.compile(r"^gemini", re.IGNORECASE), "gemini"),
    (re.compile(r"^claude", re.IGNORECASE), "claude"),
    (re.compile(r"^qwen", re.IGNORECASE), "qwen"),
    (re.compile(r"^(?:chat)?glm", re.IGNORECASE), "glm"),
    (re.compile(r"^gpt", re.IGNORECASE), "gpt"),
    (re.compile(r"^doubao", re.IGNORECASE), "doubao"),
    (re.compile(r"^(?:kimi|moonshot)", re.IGNORECASE), "kimi"),
    (re.compile(r"^ernie", re.IGNORECASE), "ernie"),
    (re.compile(r"^llama", re.IGNORECASE), "llama"),
    (re.compile(r"^(?:mistral|mixtral)", re.IGNORECASE), "mistral"),
)


class SameModelJudgeError(RuntimeError):
    """Raised when ``judge_model`` is identical to, in the same slot as, or same family as ``writer_model``."""


class JudgeResponseError(RuntimeError):
    """Raised when judge LLM output is empty, non-JSON, missing fields, or violates enum constraints."""


@dataclass
class PairwiseResult:
    """Result of a 2-round order-swapped blind pairwise comparison."""

    winner: str  # "candidate" | "reference" | "tie"
    reason: str
    drop_risk_point: str
    round_1_winner: str  # "candidate" | "reference" | "tie"
    round_2_winner: str  # "candidate" | "reference" | "tie"
    raw_responses: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArcVerdict:
    """Volume/arc-level diagnostic verdict from ``PairwiseJudge.arc_review``."""

    verdict: str  # "PASS" | "FAIL"
    problems: List[Dict[str, Any]]
    summary: str
    raw_response: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VolumeHealthJudgeResult:
    """End-of-volume 6-point semantic health verdict from ``PairwiseJudge.volume_health_review``."""

    verdict: str  # "PASS" | "FAIL"
    checks: List[Dict[str, Any]]
    summary: str
    raw_response: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _canonicalize_model_identity(model_id: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    """Return ``(bare_lower, alnum_norm, slot_or_none, family_or_none)`` for a model ID."""
    bare = _strip_site_prefix(model_id).strip().lower()
    bare_no_tier = _TIER_SUFFIX_RE.sub("", bare).strip()
    alnum_full = _ALNUM_ONLY_RE.sub("", bare)
    alnum_core = _ALNUM_ONLY_RE.sub("", bare_no_tier) or alnum_full

    slot: Optional[str] = _EXPLICIT_SLOT_ALIASES.get(alnum_full) or _EXPLICIT_SLOT_ALIASES.get(alnum_core)
    if slot is None:
        matched_slot = _match_model_id_in_whitelist(bare_no_tier or bare, WORKBUDDY_WHITELIST_SLOTS)
        if matched_slot is not None:
            slot = matched_slot

    family: Optional[str] = None
    for pat, fam in _FAMILY_PATTERNS:
        if pat.search(bare_no_tier) or pat.search(bare):
            family = fam
            break

    return bare_no_tier or bare, alnum_core, slot, family


def _check_same_or_same_family_models(judge_model: str, writer_model: str) -> None:
    j_bare, j_norm, j_slot, j_family = _canonicalize_model_identity(judge_model)
    w_bare, w_norm, w_slot, w_family = _canonicalize_model_identity(writer_model)

    if j_bare == w_bare or (j_norm and j_norm == w_norm):
        raise SameModelJudgeError(
            f"禁止同模型自审：judge_model={judge_model!r} 与 writer_model={writer_model!r} "
            f"规范化后相同 ({j_norm!r})。"
        )

    if j_slot is not None and w_slot is not None and j_slot == w_slot:
        raise SameModelJudgeError(
            f"禁止同槽位模型自审：judge_model={judge_model!r} 与 writer_model={writer_model!r} "
            f"同属 WorkBuddy 槽位 {j_slot!r}。"
        )

    if j_family is not None and w_family is not None and j_family == w_family:
        raise SameModelJudgeError(
            f"禁止同族模型自审：judge_model={judge_model!r} 与 writer_model={writer_model!r} "
            f"同属 {j_family!r} 模型族。"
        )

    if (
        len(j_norm) >= 4
        and len(w_norm) >= 4
        and (j_norm.startswith(w_norm) or w_norm.startswith(j_norm))
    ):
        raise SameModelJudgeError(
            f"禁止同源前缀模型自审：judge_model={judge_model!r} ({j_norm!r}) 与 "
            f"writer_model={writer_model!r} ({w_norm!r}) 存在同源前缀包含关系。"
        )


def _strip_json_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
    return text


def _parse_strict_json_object(raw: Any, *, context: str) -> Dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise JudgeResponseError(f"{context}: 裁判响应为空。")
    cleaned = _strip_json_code_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JudgeResponseError(
            f"{context}: 裁判响应不是合法 JSON ({exc}): {cleaned[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise JudgeResponseError(
            f"{context}: 期望 JSON 对象，实际得到 {type(parsed).__name__}。"
        )
    return parsed


def _require_non_empty_str(payload: Mapping[str, Any], key: str, *, context: str) -> str:
    if key not in payload:
        raise JudgeResponseError(f"{context}: 缺少必填字段 {key!r}。")
    val = payload[key]
    if not isinstance(val, str) or not val.strip():
        raise JudgeResponseError(
            f"{context}: 字段 {key!r} 必须为非空字符串，实际得到 {val!r}。"
        )
    return val.strip()


def _format_context(context: Mapping[str, Any] | str) -> str:
    if isinstance(context, str):
        return context.strip()
    if isinstance(context, MappingABC):
        return json.dumps(dict(context), ensure_ascii=False, indent=2)
    raise ValueError(
        f"context must be a str or Mapping, got {type(context).__name__}."
    )


def _map_ab_winner(raw_winner: str, *, a_is_candidate: bool) -> str:
    if raw_winner == "TIE":
        return "tie"
    if raw_winner == "A":
        return "candidate" if a_is_candidate else "reference"
    if raw_winner == "B":
        return "reference" if a_is_candidate else "candidate"
    raise JudgeResponseError(f"非法的 winner 值 {raw_winner!r}。")


def _validate_volume_health_payload(
    parsed: Mapping[str, Any],
    *,
    context: str,
) -> Tuple[str, List[Dict[str, Any]], str]:
    verdict = _require_non_empty_str(parsed, "verdict", context=context)
    if verdict not in VALID_ARC_VERDICTS:
        raise JudgeResponseError(
            f"{context}: verdict 必须为 {sorted(VALID_ARC_VERDICTS)} 之一，实际得到 {verdict!r}。"
        )
    summary = _require_non_empty_str(parsed, "summary", context=context)

    if "checks" not in parsed:
        raise JudgeResponseError(f"{context}: 缺少必填字段 'checks'。")
    raw_checks = parsed["checks"]
    if not isinstance(raw_checks, list):
        raise JudgeResponseError(
            f"{context}: 'checks' 必须为列表，实际得到 {type(raw_checks).__name__}。"
        )

    valid_id_set = set(VALID_VOLUME_HEALTH_CHECK_IDS)
    validated_by_id: Dict[str, Dict[str, Any]] = {}
    for idx, item in enumerate(raw_checks):
        item_ctx = f"{context} checks[{idx}]"
        if not isinstance(item, MappingABC):
            raise JudgeResponseError(
                f"{item_ctx}: 每条体检项必须为 JSON 对象，实际得到 {type(item).__name__}。"
            )
        check_id = _require_non_empty_str(item, "id", context=item_ctx)
        if check_id not in valid_id_set:
            raise JudgeResponseError(
                f"{item_ctx}: id 必须为 {list(VALID_VOLUME_HEALTH_CHECK_IDS)} 之一，实际得到 {check_id!r}。"
            )
        if check_id in validated_by_id:
            raise JudgeResponseError(f"{item_ctx}: 体检项 id {check_id!r} 重复出现。")

        status = _require_non_empty_str(item, "status", context=item_ctx)
        if status not in VALID_VOLUME_HEALTH_STATUSES:
            raise JudgeResponseError(
                f"{item_ctx}: status 必须为 {sorted(VALID_VOLUME_HEALTH_STATUSES)} 之一，实际得到 {status!r}。"
            )

        if "chapters" not in item:
            raise JudgeResponseError(f"{item_ctx}: 缺少必填字段 'chapters'。")
        raw_chs = item["chapters"]
        if not isinstance(raw_chs, list):
            raise JudgeResponseError(
                f"{item_ctx}: 'chapters' 必须为整数列表，实际得到 {type(raw_chs).__name__}。"
            )
        norm_chs: List[int] = []
        for c_idx, ch in enumerate(raw_chs):
            if isinstance(ch, bool) or not isinstance(ch, int) or ch < 1:
                raise JudgeResponseError(
                    f"{item_ctx}.chapters[{c_idx}] 必须为正整数，实际得到 {ch!r}。"
                )
            norm_chs.append(ch)
        if status == "FAIL" and len(norm_chs) == 0:
            raise JudgeResponseError(
                f"{item_ctx}: status='FAIL' 时 'chapters' 必须为非空正整数列表。"
            )

        evidence = _require_non_empty_str(item, "evidence", context=item_ctx)

        if status == "FAIL":
            repair_hint = _require_non_empty_str(item, "repair_hint", context=item_ctx)
        else:
            if "repair_hint" in item:
                raw_hint = item["repair_hint"]
                if not isinstance(raw_hint, str):
                    raise JudgeResponseError(
                        f"{item_ctx}: 字段 'repair_hint' 必须为字符串，实际得到 {raw_hint!r}。"
                    )
                repair_hint = raw_hint.strip()
            else:
                repair_hint = ""

        validated_by_id[check_id] = {
            "id": check_id,
            "status": status,
            "chapters": norm_chs,
            "evidence": evidence,
            "repair_hint": repair_hint,
        }

    missing_ids = [cid for cid in VALID_VOLUME_HEALTH_CHECK_IDS if cid not in validated_by_id]
    if missing_ids:
        raise JudgeResponseError(
            f"{context}: checks 缺少必填体检项 {missing_ids}（必须完整覆盖全部 6 项）。"
        )

    ordered_checks = [validated_by_id[cid] for cid in VALID_VOLUME_HEALTH_CHECK_IDS]
    failed_ids = [c["id"] for c in ordered_checks if c["status"] == "FAIL"]
    if failed_ids and verdict != "FAIL":
        raise JudgeResponseError(
            f"{context}: 存在 FAIL 项 {failed_ids}，但顶层 verdict={verdict!r}（必须为 'FAIL'）。"
        )
    if not failed_ids and verdict != "PASS":
        raise JudgeResponseError(
            f"{context}: 全部 6 项均为 PASS，但顶层 verdict={verdict!r}（必须为 'PASS'）。"
        )

    return verdict, ordered_checks, summary


class PairwiseJudge:
    """Cross-family blind pairwise judge and multi-chapter volume-arc reviewer."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        judge_model: str,
        writer_model: str,
        rng: Optional[random.Random] = None,
    ) -> None:
        if provider is None or not callable(getattr(provider, "generate", None)):
            raise LLMConfigurationError("provider must implement generate(LLMRequest) -> LLMResponse.")
        if not isinstance(judge_model, str) or not judge_model.strip():
            raise LLMConfigurationError("judge_model must be a non-empty string.")
        if not isinstance(writer_model, str) or not writer_model.strip():
            raise LLMConfigurationError("writer_model must be a non-empty string.")

        self.provider = provider
        self.judge_model = judge_model.strip()
        self.writer_model = writer_model.strip()
        self._rng = rng if rng is not None else random

        _check_same_or_same_family_models(self.judge_model, self.writer_model)

    def _invoke_judge(
        self,
        *,
        request_id: str,
        system_prompt: str,
        user_prompt: str,
        chapter: int = 0,
        attempt: int = 1,
        max_tokens: int = 2048,
        extra_meta: Optional[Mapping[str, Any]] = None,
    ) -> str:
        total_attempts = 1 + MAX_EMPTY_RESPONSE_RETRIES
        for retry_idx in range(total_attempts):
            meta: Dict[str, Any] = {"model": self.judge_model}
            if extra_meta:
                meta.update(extra_meta)
            if retry_idx > 0:
                meta["empty_response_retry"] = retry_idx
            req = LLMRequest(
                request_id=request_id,
                chapter=chapter,
                attempt=attempt + retry_idx,
                purpose="critic",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.2,
                max_tokens=max_tokens,
                metadata=meta,
            )
            resp = self.provider.generate(req)
            if not isinstance(resp, LLMResponse) or not isinstance(resp.content, str):
                raise JudgeResponseError(
                    f"{request_id}: provider.generate 必须返回含字符串 content 的 LLMResponse。"
                )
            if resp.content.strip():
                return resp.content
            if retry_idx < MAX_EMPTY_RESPONSE_RETRIES:
                logger.warning(
                    "%s: provider returned empty content on attempt %d/%d (model=%s); retrying once.",
                    request_id,
                    retry_idx + 1,
                    total_attempts,
                    self.judge_model,
                )
        raise JudgeResponseError(
            f"{request_id}: provider 返回空文本响应（已重试 {MAX_EMPTY_RESPONSE_RETRIES} 次仍为空）。"
        )

    def _run_pairwise_round(
        self,
        *,
        round_num: int,
        sample_a: str,
        sample_b: str,
        a_is_candidate: bool,
        context_str: str,
    ) -> Tuple[str, str, str, str]:
        system_prompt = (
            "你是资深网文主编，正在执行盲评两两对比（样本 A 与样本 B 顺序已随机打乱，请勿受位置影响）。\n"
            "请从开篇钩子、叙事节奏、人物区分度、画面感与读者追读动力出发，判断哪个样本更优。\n"
            "只允许输出合法 JSON 对象，严禁输出额外说明或 Markdown 以外的文字：\n"
            '{"winner": "A"|"B"|"TIE", "reason": "具体对比理由（非空）", '
            '"drop_risk_point": "最可能让读者弃书的位置或片段说明（非空，若无明显弃书点也须明确写出判断依据）"}'
        )
        ctx_block = f"【背景/大纲约束】\n{context_str}\n\n" if context_str else ""
        user_prompt = (
            f"{ctx_block}"
            f"=== 样本 A ===\n{sample_a.strip()}\n\n"
            f"=== 样本 B ===\n{sample_b.strip()}\n\n"
            "请严格输出 JSON。"
        )
        raw_content = self._invoke_judge(
            request_id=f"pairwise-r{round_num}",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            attempt=round_num,
            extra_meta={"judge_task": "pairwise_compare", "round": round_num},
        )
        ctx_label = f"compare round {round_num}"
        parsed = _parse_strict_json_object(raw_content, context=ctx_label)
        raw_winner = _require_non_empty_str(parsed, "winner", context=ctx_label)
        if raw_winner not in VALID_PAIRWISE_RAW_WINNERS:
            raise JudgeResponseError(
                f"{ctx_label}: winner 必须为 {sorted(VALID_PAIRWISE_RAW_WINNERS)} 之一，实际得到 {raw_winner!r}。"
            )
        reason = _require_non_empty_str(parsed, "reason", context=ctx_label)
        drop_risk_point = _require_non_empty_str(parsed, "drop_risk_point", context=ctx_label)
        mapped_winner = _map_ab_winner(raw_winner, a_is_candidate=a_is_candidate)
        return mapped_winner, reason, drop_risk_point, raw_content

    def compare(
        self,
        candidate: str,
        reference: str,
        *,
        context: Mapping[str, Any] | str = "",
    ) -> PairwiseResult:
        """Run a 2-round order-swapped blind comparison between ``candidate`` and ``reference``."""
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("candidate must be a non-empty string.")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("reference must be a non-empty string.")
        context_str = _format_context(context)

        r1_a_is_cand = bool(self._rng.choice((True, False)))
        if r1_a_is_cand:
            r1_a, r1_b = candidate, reference
            r2_a, r2_b = reference, candidate
        else:
            r1_a, r1_b = reference, candidate
            r2_a, r2_b = candidate, reference

        r1_winner, r1_reason, r1_drop, r1_raw = self._run_pairwise_round(
            round_num=1,
            sample_a=r1_a,
            sample_b=r1_b,
            a_is_candidate=r1_a_is_cand,
            context_str=context_str,
        )
        r2_winner, r2_reason, r2_drop, r2_raw = self._run_pairwise_round(
            round_num=2,
            sample_a=r2_a,
            sample_b=r2_b,
            a_is_candidate=not r1_a_is_cand,
            context_str=context_str,
        )

        if r1_winner == r2_winner:
            final_winner = r1_winner
            combined_reason = (
                r1_reason
                if r1_reason == r2_reason
                else f"[轮1一致:{r1_winner}] {r1_reason} | [轮2一致:{r2_winner}] {r2_reason}"
            )
        else:
            final_winner = "tie"
            combined_reason = (
                f"[交换顺序结果不一致判平] 轮1={r1_winner} ({r1_reason}) | "
                f"轮2={r2_winner} ({r2_reason})"
            )

        combined_drop = (
            r1_drop
            if r1_drop == r2_drop
            else f"[轮1] {r1_drop} | [轮2] {r2_drop}"
        )

        return PairwiseResult(
            winner=final_winner,
            reason=combined_reason,
            drop_risk_point=combined_drop,
            round_1_winner=r1_winner,
            round_2_winner=r2_winner,
            raw_responses=[r1_raw, r2_raw],
        )

    def arc_review(
        self,
        chapters: Sequence[str],
        volume_card: Mapping[str, Any],
    ) -> ArcVerdict:
        """Review a batch of consecutive chapters against ``volume_card`` for arc-level defects."""
        if (
            chapters is None
            or isinstance(chapters, (str, bytes, bytearray))
            or not isinstance(chapters, SequenceABC)
            or len(chapters) == 0
        ):
            raise ValueError("chapters must be a non-empty sequence of chapter texts.")
        cleaned_chapters: List[str] = []
        for idx, ch_text in enumerate(chapters):
            if not isinstance(ch_text, str) or not ch_text.strip():
                raise ValueError(f"chapters[{idx}] must be a non-empty string.")
            cleaned_chapters.append(ch_text.strip())

        if volume_card is None or not isinstance(volume_card, MappingABC) or len(volume_card) == 0:
            raise ValueError("volume_card must be a non-empty Mapping.")

        system_prompt = (
            "你是资深网文连载总编，负责卷级弧线会诊。\n"
            "请对照本卷卷卡（volume_card），审视连续多章是否存在以下 5 类结构性病灶：\n"
            '- "同质化"：连续多章桥段/冲突/套路高度重复\n'
            '- "原地踏步"：主线与人物关系缺乏实质推进\n'
            '- "爽感疲劳"：打脸或爽点模式单一、边际递减\n'
            '- "工具人"：配角沦为单一触发器或毫无自主性\n'
            '- "跑偏"：偏离本卷目标、核心卖点或设定边界\n'
            "硬性一致约束：\n"
            '- 若判定 "verdict": "PASS"，则 "problems" 必须严格为空数组 []（轻微改进建议只能写在 "summary" 中）；\n'
            '- 只要 "problems" 列出任意 1 条结构性病灶，"verdict" 必须为 "FAIL"；\n'
            '- 若判定 "verdict": "FAIL"，则 "problems" 必须至少列出 1 条具体病灶。\n'
            "只允许输出严格 JSON 对象（每条 evidence 控制在 120 字以内、summary 控制在 150 字以内，JSON 字符串值内部严禁使用英文双引号 \"，引用字词一律用中文单引号 ‘’，保证 JSON 完整闭合）：\n"
            '{"verdict": "PASS"|"FAIL", '
            '"problems": [{"type": "同质化"|"原地踏步"|"爽感疲劳"|"工具人"|"跑偏", '
            '"chapters": [int, ...], "evidence": "具体依据（非空）"}], '
            '"summary": "整段评估总结（非空）"}'
        )
        card_json = json.dumps(dict(volume_card), ensure_ascii=False, indent=2)
        chapters_block = "\n\n".join(
            f"--- 第 {idx} 章样本 ---\n{txt}" for idx, txt in enumerate(cleaned_chapters, start=1)
        )
        base_user_prompt = (
            f"【本卷卷卡】\n{card_json}\n\n"
            f"【待审连续章节（共 {len(cleaned_chapters)} 章）】\n{chapters_block}\n\n"
            "请严格输出 JSON（PASS 时 problems 必须为 []；FAIL 时 problems 必须非空）。"
        )

        ctx_label = "arc_review"
        max_contract_attempts = 2
        retry_hint = ""
        for contract_attempt in range(1, max_contract_attempts + 1):
            user_prompt = base_user_prompt if not retry_hint else f"{base_user_prompt}\n\n{retry_hint}"
            raw_content = self._invoke_judge(
                request_id="arc-review" if contract_attempt == 1 else f"arc-review-fix{contract_attempt - 1}",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                chapter=len(cleaned_chapters),
                attempt=contract_attempt,
                max_tokens=8192,
                extra_meta={
                    "judge_task": "arc_review",
                    "chapter_count": len(cleaned_chapters),
                    "contract_attempt": contract_attempt,
                },
            )

            try:
                parsed = _parse_strict_json_object(raw_content, context=ctx_label)
            except JudgeResponseError as parse_exc:
                if contract_attempt < max_contract_attempts:
                    logger.warning(
                        "%s malformed JSON on attempt %d/%d (%s); retrying once with explicit JSON syntax correction.",
                        ctx_label,
                        contract_attempt,
                        max_contract_attempts,
                        parse_exc,
                    )
                    retry_hint = (
                        f"【JSON语法纠偏重审】你上一次输出不是合法 JSON：{parse_exc}\n"
                        '请重新输出严格合法的 JSON 对象，字符串值内部严禁包含未转义英文双引号 "（引用字词请改用中文单引号 ‘’）。'
                    )
                    continue
                raise
            verdict = _require_non_empty_str(parsed, "verdict", context=ctx_label)
            if verdict not in VALID_ARC_VERDICTS:
                raise JudgeResponseError(
                    f"{ctx_label}: verdict 必须为 {sorted(VALID_ARC_VERDICTS)} 之一，实际得到 {verdict!r}。"
                )
            summary = _require_non_empty_str(parsed, "summary", context=ctx_label)

            if "problems" not in parsed:
                raise JudgeResponseError(f"{ctx_label}: 缺少必填字段 'problems'。")
            raw_problems = parsed["problems"]
            if not isinstance(raw_problems, list):
                raise JudgeResponseError(
                    f"{ctx_label}: 'problems' 必须为列表，实际得到 {type(raw_problems).__name__}。"
                )

            validated_problems: List[Dict[str, Any]] = []
            for idx, item in enumerate(raw_problems):
                item_ctx = f"{ctx_label} problems[{idx}]"
                if not isinstance(item, MappingABC):
                    raise JudgeResponseError(
                        f"{item_ctx}: 每条问题必须为 JSON 对象，实际得到 {type(item).__name__}。"
                    )
                prob_type = _require_non_empty_str(item, "type", context=item_ctx)
                if prob_type not in VALID_ARC_PROBLEM_TYPES:
                    raise JudgeResponseError(
                        f"{item_ctx}: type 必须为 {sorted(VALID_ARC_PROBLEM_TYPES)} 之一，实际得到 {prob_type!r}。"
                    )
                if "chapters" not in item:
                    raise JudgeResponseError(f"{item_ctx}: 缺少必填字段 'chapters'。")
                prob_chs = item["chapters"]
                if not isinstance(prob_chs, list) or len(prob_chs) == 0:
                    raise JudgeResponseError(
                        f"{item_ctx}: 'chapters' 必须为非空整数列表，实际得到 {prob_chs!r}。"
                    )
                norm_chs: List[int] = []
                for c_idx, ch in enumerate(prob_chs):
                    if isinstance(ch, bool) or not isinstance(ch, int) or ch < 1:
                        raise JudgeResponseError(
                            f"{item_ctx}.chapters[{c_idx}] 必须为正整数，实际得到 {ch!r}。"
                        )
                    norm_chs.append(ch)
                evidence = _require_non_empty_str(item, "evidence", context=item_ctx)
                validated_problems.append(
                    {
                        "type": prob_type,
                        "chapters": norm_chs,
                        "evidence": evidence,
                    }
                )

            mismatch_err: Optional[str] = None
            if verdict == "FAIL" and len(validated_problems) == 0:
                mismatch_err = f"{ctx_label}: verdict='FAIL' 时 'problems' 不能为空。"
            elif verdict == "PASS" and len(validated_problems) > 0:
                mismatch_err = (
                    f"{ctx_label}: verdict='PASS' 时 'problems' 必须为空列表，实际有 {len(validated_problems)} 项。"
                )

            if mismatch_err is None:
                return ArcVerdict(
                    verdict=verdict,
                    problems=validated_problems,
                    summary=summary,
                    raw_response=raw_content,
                )

            if contract_attempt < max_contract_attempts:
                logger.warning(
                    "%s contract mismatch on attempt %d/%d (%s); retrying once with explicit schema correction.",
                    ctx_label,
                    contract_attempt,
                    max_contract_attempts,
                    mismatch_err,
                )
                retry_hint = (
                    f"【格式纠偏重审】你上一次输出触发契约冲突：{mismatch_err}\n"
                    '请明确做出一致裁决：若整体达标通过，请输出 "verdict": "PASS" 且 "problems": []（微瑕写进 summary）；'
                    '若存在必须整改的结构性病灶，请输出 "verdict": "FAIL" 且在 "problems" 中列出。只输出严格 JSON。'
                )
                continue
            raise JudgeResponseError(mismatch_err)

        raise JudgeResponseError(f"{ctx_label}: exhausted contract retries.")

    def volume_health_review(
        self,
        chapters: Sequence[str],
        volume_card: Mapping[str, Any],
        *,
        prev_volume_card: Optional[Mapping[str, Any]] = None,
        cast_cards: Optional[Sequence[Mapping[str, Any]]] = None,
        deterministic_summary: Optional[Mapping[str, Any]] = None,
    ) -> VolumeHealthJudgeResult:
        """Run end-of-volume 6-point semantic health review against ``volume_card``."""
        if (
            chapters is None
            or isinstance(chapters, (str, bytes, bytearray))
            or not isinstance(chapters, SequenceABC)
            or len(chapters) == 0
        ):
            raise ValueError("chapters must be a non-empty sequence of chapter texts.")
        cleaned_chapters: List[str] = []
        for idx, ch_text in enumerate(chapters):
            if not isinstance(ch_text, str) or not ch_text.strip():
                raise ValueError(f"chapters[{idx}] must be a non-empty string.")
            cleaned_chapters.append(ch_text.strip())

        if volume_card is None or not isinstance(volume_card, MappingABC) or len(volume_card) == 0:
            raise ValueError("volume_card must be a non-empty Mapping.")
        if prev_volume_card is not None and not isinstance(prev_volume_card, MappingABC):
            raise ValueError(
                f"prev_volume_card must be a Mapping or None, got {type(prev_volume_card).__name__}."
            )
        cleaned_cast_cards: Optional[List[Dict[str, Any]]] = None
        if cast_cards is not None:
            if isinstance(cast_cards, (str, bytes, bytearray)) or not isinstance(
                cast_cards, SequenceABC
            ):
                raise ValueError("cast_cards must be a sequence of Mapping objects or None.")
            cleaned_cast_cards = []
            for idx, card in enumerate(cast_cards):
                if not isinstance(card, MappingABC):
                    raise ValueError(
                        f"cast_cards[{idx}] must be a Mapping, got {type(card).__name__}."
                    )
                cleaned_cast_cards.append(dict(card))
        if deterministic_summary is not None and not isinstance(deterministic_summary, MappingABC):
            raise ValueError(
                "deterministic_summary must be a Mapping or None, "
                f"got {type(deterministic_summary).__name__}."
            )

        system_prompt = (
            "你是资深网文连载总编，正在执行【卷末六项硬体检】终审。本卷体检不通过将直接锁卷、禁止开下一卷。\n"
            "请对照本卷卷卡（volume_card）、上一卷卷卡（如有）、本卷角色卡与确定性核验摘要，逐一审查以下全部 6 项（严禁遗漏、严禁重复）：\n"
            '- "VH_01_NO_ROUTINE_REPEAT"：无套路重复与同质化（严查与上一卷及本卷前文桥段/爽点/冲突结构复读、换皮重复或触犯上一卷禁用套路）\n'
            '- "VH_02_FORESHADOW_AND_REWARD"：伏笔闭环与阶段回报（检查本卷应埋/应回收伏笔是否自然落地，高潮与阶段回报是否真正兑现）\n'
            '- "VH_03_CHARACTER_AND_ARC"：人物弧光与退场边界（主角与核心配角是否具备独立目标/缺陷/成长弧光，避免工具人化，且前卷退场角色不越界）\n'
            '- "VH_04_CORE_FLAVOR_QUOTA"：核心卖点风味与配额（是否紧扣主线核心卖点与本卷风味/案件配额，无跑偏、无单一词垄断、无注水）\n'
            '- "VH_05_THREE_ACT_AND_HOOK"：三幕结构与卷末钩子（本卷是否起承转合完整、高潮发力，并在卷尾留足进入下一卷的强钩子与主线推进）\n'
            '- "VH_06_LEDGER_AND_DEBT_MATH"：数值账本与代价逻辑（资金/粉丝/债务/战力与金手指代价、限制是否逻辑自洽，无凭空暴涨或吃书）\n'
            "硬性一致约束：\n"
            '- "checks" 必须恰好包含上述全部 6 个 id，无遗漏、无重复；\n'
            '- 每项必须含 "id", "status" ("PASS"|"FAIL"), "chapters" (正整数列表：FAIL 时必填非空，PASS 时允许 []), '
            '"evidence" (非空，控制在 120 字以内), "repair_hint" (FAIL 时必填具体可执行返修指引，PASS 时可为空字符串 "")；\n'
            '- 只要任意 1 项 status=="FAIL"，顶层 "verdict" 必须为 "FAIL"；全部 6 项均为 "PASS" 时，顶层 "verdict" 必须为 "PASS"。\n'
            "只允许输出严格 JSON 对象（summary 控制在 180 字以内，JSON 字符串值内部严禁使用英文双引号 \"，引用字词一律用中文单引号 ‘’，保证 JSON 完整闭合）：\n"
            '{"verdict": "PASS"|"FAIL", '
            '"checks": [{"id": "VH_01_NO_ROUTINE_REPEAT", "status": "PASS"|"FAIL", '
            '"chapters": [int, ...], "evidence": "具体依据（非空）", "repair_hint": "返修指引（FAIL时非空）"}], '
            '"summary": "整卷终审总结（非空）"}'
        )

        card_json = json.dumps(dict(volume_card), ensure_ascii=False, indent=2)
        sections: List[str] = [f"【本卷卷卡】\n{card_json}"]
        if prev_volume_card is not None:
            prev_json = json.dumps(dict(prev_volume_card), ensure_ascii=False, indent=2)
            sections.append(f"【上一卷卷卡（用于对照去重与承接）】\n{prev_json}")
        if cleaned_cast_cards is not None:
            cast_json = json.dumps(cleaned_cast_cards, ensure_ascii=False, indent=2)
            sections.append(f"【本卷相关角色卡】\n{cast_json}")
        if deterministic_summary is not None:
            det_json = json.dumps(dict(deterministic_summary), ensure_ascii=False, indent=2)
            sections.append(f"【确定性硬核验摘要】\n{det_json}")

        raw_start = volume_card.get("chapter_start")
        start_ch = (
            int(raw_start)
            if isinstance(raw_start, int) and not isinstance(raw_start, bool) and raw_start >= 1
            else 1
        )
        chapters_block = "\n\n".join(
            f"--- 第 {start_ch + idx} 章（本卷第 {idx + 1} 章样本）---\n{txt}"
            for idx, txt in enumerate(cleaned_chapters)
        )
        sections.append(f"【待审本卷章节（共 {len(cleaned_chapters)} 章）】\n{chapters_block}")
        sections.append(
            "请严格输出包含全部 6 项体检结果的 JSON（任一项 FAIL 则顶层 verdict 必须为 FAIL，全 PASS 则必须为 PASS）。"
        )
        base_user_prompt = "\n\n".join(sections)

        raw_vol_index = volume_card.get("index")
        vol_meta: Dict[str, Any] = {}
        if isinstance(raw_vol_index, int) and not isinstance(raw_vol_index, bool) and raw_vol_index >= 1:
            vol_meta["volume_index"] = raw_vol_index

        ctx_label = "volume_health_review"
        max_contract_attempts = 2
        retry_hint = ""
        for contract_attempt in range(1, max_contract_attempts + 1):
            user_prompt = (
                base_user_prompt if not retry_hint else f"{base_user_prompt}\n\n{retry_hint}"
            )
            raw_content = self._invoke_judge(
                request_id=(
                    "volume-health-review"
                    if contract_attempt == 1
                    else f"volume-health-review-fix{contract_attempt - 1}"
                ),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                chapter=len(cleaned_chapters),
                attempt=contract_attempt,
                max_tokens=8192,
                extra_meta={
                    "judge_task": "volume_health_review",
                    "chapter_count": len(cleaned_chapters),
                    "contract_attempt": contract_attempt,
                    **vol_meta,
                },
            )

            try:
                parsed = _parse_strict_json_object(raw_content, context=ctx_label)
            except JudgeResponseError as parse_exc:
                if contract_attempt < max_contract_attempts:
                    logger.warning(
                        "%s malformed JSON on attempt %d/%d (%s); retrying once with explicit JSON syntax correction.",
                        ctx_label,
                        contract_attempt,
                        max_contract_attempts,
                        parse_exc,
                    )
                    retry_hint = (
                        f"【JSON语法纠偏重审】你上一次输出不是合法 JSON：{parse_exc}\n"
                        '请重新输出严格合法的 JSON 对象，字符串值内部严禁包含未转义英文双引号 "（引用字词请改用中文单引号 ‘’）。'
                    )
                    continue
                raise

            try:
                verdict, ordered_checks, summary = _validate_volume_health_payload(
                    parsed, context=ctx_label
                )
            except JudgeResponseError as contract_exc:
                if contract_attempt < max_contract_attempts:
                    logger.warning(
                        "%s contract violation on attempt %d/%d (%s); retrying once with explicit schema correction.",
                        ctx_label,
                        contract_attempt,
                        max_contract_attempts,
                        contract_exc,
                    )
                    retry_hint = (
                        f"【格式纠偏重审】你上一次输出触发契约违规：{contract_exc}\n"
                        f"必须完整返回全部 6 项体检 id（{list(VALID_VOLUME_HEALTH_CHECK_IDS)}）且无重复；"
                        '任意一项 status=="FAIL" 时必须填写非空正整数 chapters 与非空 repair_hint，且顶层 verdict 必须为 "FAIL"；'
                        '全部 6 项为 "PASS" 时顶层 verdict 必须为 "PASS"。只输出严格 JSON。'
                    )
                    continue
                raise

            return VolumeHealthJudgeResult(
                verdict=verdict,
                checks=ordered_checks,
                summary=summary,
                raw_response=raw_content,
            )

        raise JudgeResponseError(f"{ctx_label}: exhausted contract retries.")


__all__ = [
    "ArcVerdict",
    "JudgeResponseError",
    "PairwiseJudge",
    "PairwiseResult",
    "SameModelJudgeError",
    "VALID_ARC_PROBLEM_TYPES",
    "VALID_ARC_VERDICTS",
    "VALID_PAIRWISE_RAW_WINNERS",
    "VALID_VOLUME_HEALTH_CHECK_IDS",
    "VALID_VOLUME_HEALTH_STATUSES",
    "VolumeHealthJudgeResult",
]
