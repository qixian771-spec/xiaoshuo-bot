"""Golden-three-chapters (黄金三章) opening rubric and cross-model reviewer.

Benchmark thresholds and qualitative criteria in ``OPENING_RUBRIC`` are derived from
empirical measurement of benchmark work #1 documented in:
``G:\\xiaoshuo-bot-data\\benchmarks\\yeshi_suangua\\analysis\\book1_golden3.md``

Key empirical anchors from ``book1_golden3.md``:
- Chapter 1: conflict opens on paragraph 1 (``max_paragraph=1``); protagonist stake/crisis
  surfaces within paragraph 4 (``max_paragraph=4``); golden-finger panel / stall rules
  are clearly explained within the first 25% (measured ~24.3%-24.8%, ``max_char_ratio=0.25``);
  the first client / conflict target enters within the first 40% (measured ~37.2%-38.0%,
  ``max_char_ratio=0.40``); at least one 3-step divination verdict completes in 3-5 paragraphs;
  chapter ends mid-action rather than in a wrap-up/sleep.
- Chapter 2: continues 3-step verdicts and mid-action cliffhangers, forces a distinct case
  type compared to Chapter 1 (requiring ``previous_case_summary``), and verifies protagonist
  restraint against monetary temptation.
- Chapter 3: demands external/official payoff of prior cases, a new end-of-chapter hook, and
  bounded realistic protagonist scale (exaggerated numbers confined to bystanders/comments).

Strict no-fallback and cross-family judging policy:
- Reuses ``judge._check_same_or_same_family_models`` so same-model / same-slot / same-family
  self-judging raises ``SameModelJudgeError``.
- Positional items never trust the judge's boolean alone: ``locate_evidence`` performs exact
  whitespace-stripped substring matching (no fuzzy matching) and code enforces thresholds.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .judge import (
    JudgeResponseError,
    SameModelJudgeError,
    _check_same_or_same_family_models,
    _parse_strict_json_object,
    _require_non_empty_str,
)
from .llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse

VALID_RUBRIC_KINDS = frozenset({"positional", "judgment"})
_VALID_CHAPTERS = frozenset({1, 2, 3})
_WHITESPACE_RE = re.compile(r"\s+")


class EvidenceNotFoundError(ValueError):
    """Raised when quoted evidence cannot be found verbatim (ignoring whitespace) in text."""


@dataclass(frozen=True)
class OpeningRubricItem:
    """Single golden-three-chapters rubric rule for writer directives and judge review."""

    id: str
    chapter: int
    kind: str
    question: str
    writer_directive: str
    max_paragraph: Optional[int] = None
    max_char_ratio: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError(f"OpeningRubricItem.id must be a non-empty string, got {self.id!r}.")
        _validate_chapter(self.chapter, label="OpeningRubricItem.chapter")
        if not isinstance(self.kind, str) or self.kind not in VALID_RUBRIC_KINDS:
            raise ValueError(
                f"OpeningRubricItem.kind must be one of {sorted(VALID_RUBRIC_KINDS)}, got {self.kind!r}."
            )
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError(
                f"OpeningRubricItem.question must be a non-empty string, got {self.question!r}."
            )
        if not isinstance(self.writer_directive, str) or not self.writer_directive.strip():
            raise ValueError(
                "OpeningRubricItem.writer_directive must be a non-empty string, "
                f"got {self.writer_directive!r}."
            )

        if self.max_paragraph is not None:
            if (
                isinstance(self.max_paragraph, bool)
                or not isinstance(self.max_paragraph, int)
                or self.max_paragraph < 1
            ):
                raise ValueError(
                    "OpeningRubricItem.max_paragraph must be an int >= 1 or None, "
                    f"got {self.max_paragraph!r}."
                )

        if self.max_char_ratio is not None:
            if (
                isinstance(self.max_char_ratio, bool)
                or not isinstance(self.max_char_ratio, (int, float))
                or not math.isfinite(float(self.max_char_ratio))
                or not (0.0 < float(self.max_char_ratio) <= 1.0)
            ):
                raise ValueError(
                    "OpeningRubricItem.max_char_ratio must be a finite number in (0, 1] or None, "
                    f"got {self.max_char_ratio!r}."
                )

        if self.kind == "positional":
            if self.max_paragraph is None and self.max_char_ratio is None:
                raise ValueError(
                    f"Positional rubric item {self.id!r} must set max_paragraph and/or max_char_ratio."
                )
        else:
            if self.max_paragraph is not None or self.max_char_ratio is not None:
                raise ValueError(
                    f"Judgment rubric item {self.id!r} must not set max_paragraph or max_char_ratio."
                )


def _validate_chapter(chapter: Any, *, label: str = "chapter") -> int:
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter not in _VALID_CHAPTERS:
        raise ValueError(f"{label} must be an int in (1, 2, 3), got {chapter!r}.")
    return chapter


OPENING_RUBRIC: Dict[int, Tuple[OpeningRubricItem, ...]] = {
    1: (
        OpeningRubricItem(
            id="OPEN_FIRST_LINE_CONFLICT",
            chapter=1,
            kind="positional",
            max_paragraph=1,
            max_char_ratio=None,
            question="找出正文里第一次出现人与人冲突、危机或异常事件的原句。",
            writer_directive="第一段就是冲突，禁止天气景物或身世介绍开头。",
        ),
        OpeningRubricItem(
            id="OPEN_STAKE_BY_P4",
            chapter=1,
            kind="positional",
            max_paragraph=4,
            max_char_ratio=None,
            question="找出主角核心赌注（生死 / 债务 / 代价）第一次出现的原句。",
            writer_directive="第 4 段之内亮出主角的赌注。",
        ),
        OpeningRubricItem(
            id="OPEN_RULE_BY_25",
            chapter=1,
            kind="positional",
            max_paragraph=None,
            max_char_ratio=0.25,
            question="找出金手指或摊规第一次被讲清楚的原句。",
            writer_directive="全文前 25% 内交代清楚金手指或摊规。",
        ),
        OpeningRubricItem(
            id="OPEN_FIRST_CLIENT_BY_40",
            chapter=1,
            kind="positional",
            max_paragraph=None,
            max_char_ratio=0.40,
            question="找出第一位求卦者 / 冲突对象出场的原句。",
            writer_directive="第一位客人在前 40% 内出场。",
        ),
        OpeningRubricItem(
            id="OPEN_VERDICT_3STEP",
            chapter=1,
            kind="judgment",
            question="是否至少一次断语完整走完「术语 → 白话后果 → 当事人内心或事实印证」？引用印证那一句。",
            writer_directive="至少一次断语完整走完「术语 → 白话后果 → 当事人内心或事实印证」，并且 3～5 段内完成。",
        ),
        OpeningRubricItem(
            id="OPEN_END_MID_ACTION",
            chapter=1,
            kind="judgment",
            question="章末是否停在揭穿或冲突动作的中途，而不是总结、睡觉或圆满收尾？引用最后一段。",
            writer_directive="章末停在揭穿或冲突动作的中途，而不是总结、睡觉或圆满收尾。",
        ),
    ),
    2: (
        OpeningRubricItem(
            id="OPEN_VERDICT_3STEP",
            chapter=2,
            kind="judgment",
            question="是否至少一次断语完整走完「术语 → 白话后果 → 当事人内心或事实印证」？引用印证那一句。",
            writer_directive="至少一次断语完整走完「术语 → 白话后果 → 当事人内心或事实印证」，并且 3～5 段内完成。",
        ),
        OpeningRubricItem(
            id="OPEN_END_MID_ACTION",
            chapter=2,
            kind="judgment",
            question="章末是否停在揭穿或冲突动作的中途，而不是总结、睡觉或圆满收尾？引用最后一段。",
            writer_directive="章末停在揭穿或冲突动作的中途，而不是总结、睡觉或圆满收尾。",
        ),
        OpeningRubricItem(
            id="CASE_DISTINCT",
            chapter=2,
            kind="judgment",
            question="本章的案子和上一章（见前情摘要）雷点类型是否不同？",
            writer_directive="本章案子的核心雷点类型必须与上一章明显不同，不重复套路。",
        ),
        OpeningRubricItem(
            id="PROTAGONIST_RESTRAINT",
            chapter=2,
            kind="judgment",
            question="主角面对金钱或诱惑时是否守住摊规？",
            writer_directive="主角面对金钱或诱惑时必须保持克制并守住摊规。",
        ),
    ),
    3: (
        OpeningRubricItem(
            id="EXTERNAL_PAYOFF",
            chapter=3,
            kind="judgment",
            question="前面的案子有没有获得外部事实或官方印证？",
            writer_directive="前面的案子必须在外部舆论、当事人后续或官方层面获得实锤印证。",
        ),
        OpeningRubricItem(
            id="NEW_HOOK_END",
            chapter=3,
            kind="judgment",
            question="章末有没有挂出新任务或新冲突？",
            writer_directive="章末必须挂出新任务或新的升级冲突。",
        ),
        OpeningRubricItem(
            id="SCALE_REALISTIC",
            chapter=3,
            kind="judgment",
            question="主角自身的金额、粉丝数是否在现实量级？夸张数字是否只出现在旁人嘴里？",
            writer_directive="主角自身的金额与人气数值必须保持现实有界量级，夸张修辞仅限旁观网友/他人言语。",
        ),
    ),
}


def writer_directives(chapter: int) -> List[str]:
    """Return ordered writer directives for chapter 1, 2, or 3."""
    _validate_chapter(chapter)
    return [item.writer_directive for item in OPENING_RUBRIC[chapter]]


def split_paragraphs(text: str) -> List[str]:
    """Split text on blank lines or single newlines, strip each paragraph, and drop empties."""
    if not isinstance(text, str):
        raise ValueError(f"text must be a str, got {type(text).__name__}.")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _locate_evidence_if_present(text: str, evidence: str) -> Optional[Tuple[int, float]]:
    """Return ``(1-based paragraph_index, char_ratio)`` if verbatim match exists, else ``None``."""
    paragraphs = split_paragraphs(text)
    if not paragraphs or not isinstance(evidence, str):
        return None
    norm_evidence = _WHITESPACE_RE.sub("", evidence)
    if not norm_evidence:
        return None

    norm_paras = [_WHITESPACE_RE.sub("", para) for para in paragraphs]
    joined = "".join(norm_paras)
    total_chars = len(joined)
    if total_chars == 0:
        return None

    match_idx = joined.find(norm_evidence)
    if match_idx < 0:
        return None

    cumulative = 0
    for idx, norm_para in enumerate(norm_paras, start=1):
        next_cumulative = cumulative + len(norm_para)
        if match_idx < next_cumulative:
            return idx, match_idx / total_chars
        cumulative = next_cumulative
    return None


def locate_evidence(text: str, evidence: str) -> Tuple[int, float]:
    """Locate ``evidence`` in ``text`` by exact whitespace-stripped substring matching.

    Returns ``(1_based_paragraph_index, start_char_index_no_ws / total_chars_no_ws)``.
    Raises ``EvidenceNotFoundError`` when text has no paragraphs, evidence is empty, or no
    exact whitespace-stripped match exists (no fuzzy matching).
    """
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        raise EvidenceNotFoundError("正文没有任何有效段落，无法定位引文。")
    if not isinstance(evidence, str) or not _WHITESPACE_RE.sub("", evidence):
        raise EvidenceNotFoundError("引文 evidence 为空，无法定位。")

    found = _locate_evidence_if_present(text, evidence)
    if found is None:
        raise EvidenceNotFoundError(
            f"正文中找不到精确匹配的原句引文（已去除全部空白再比对）: {evidence[:80]!r}"
        )
    return found


def _locate_verified_evidence(text: str, evidence: str, *, context: str) -> Tuple[int, float]:
    """Locate verbatim judge evidence or translate missing quote into ``JudgeResponseError``."""
    try:
        return locate_evidence(text, evidence)
    except EvidenceNotFoundError as exc:
        raise JudgeResponseError(
            f"{context}: 裁判引用了原文中不存在的证据: {exc}"
        ) from exc


@dataclass
class OpeningItemResult:
    """Result for one rubric item returned by ``OpeningReviewer.review``."""

    id: str
    passed: bool
    evidence: str
    paragraph_index: Optional[int]
    char_ratio: Optional[float]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OpeningVerdict:
    """Chapter-level golden-three-chapters review verdict."""

    chapter: int
    items: List[OpeningItemResult]

    @property
    def passed_all(self) -> bool:
        return bool(self.items) and all(item.passed for item in self.items)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chapter": self.chapter,
            "passed_all": self.passed_all,
            "items": [item.to_dict() for item in self.items],
        }


def _build_review_prompts(
    chapter: int,
    text: str,
    rubric: Tuple[OpeningRubricItem, ...],
    previous_case_summary: Optional[str],
) -> Tuple[str, str]:
    """Construct strict JSON-output system and user prompts for ``OpeningReviewer``."""
    rubric_lines = "\n".join(
        f"- {item.id} ({item.kind}): {item.question}" for item in rubric
    )
    system_prompt = (
        "你是资深网文黄金三章审稿编辑。请严格按逐条审查清单检验给定章节正文。\n"
        "重要约束：\n"
        "1. 只允许输出一个合法 JSON 对象，严禁输出 JSON 以外的解释、前后缀或额外文本。\n"
        "2. 返回格式必须严格为：\n"
        '{"items": [{"id": "<清单条目id>", "found_or_pass": true|false, '
        '"evidence": "原文逐字引用", "reason": "具体判断依据（非空）"}]}\n'
        "3. items 必须精确包含下方全部 id，不多不少不重复。\n"
        "4. positional 条目：只要正文中出现对应内容就将 found_or_pass 设为 true，并逐字原样复制该句到 evidence；"
        "若完全未出现则设 false。\n"
        "5. judgment 条目：满足要求时 found_or_pass 设为 true 且 evidence 必须为正文逐字引用（禁止编造或改写）；"
        "不满足时设为 false（可逐字引用问题句，若无可引片段则填 \"\"）。"
    )
    summary_block = ""
    if isinstance(previous_case_summary, str) and previous_case_summary.strip():
        summary_block = f"【前情摘要（上一章/前案概要）】\n{previous_case_summary.strip()}\n\n"
    user_prompt = (
        f"【审查章节】第 {chapter} 章\n\n"
        f"【审查清单】\n{rubric_lines}\n\n"
        f"{summary_block}"
        f"【待审正文】\n{text.strip()}\n\n"
        "请严格输出 JSON。"
    )
    return system_prompt, user_prompt


def _parse_review_entries(
    raw_content: str,
    rubric: Tuple[OpeningRubricItem, ...],
    *,
    context: str,
) -> Dict[str, Tuple[bool, str, str]]:
    """Parse and strictly validate judge JSON against the chapter rubric ID set."""
    parsed = _parse_strict_json_object(raw_content, context=context)
    if "items" not in parsed:
        raise JudgeResponseError(f"{context}: 缺少必填字段 'items'。")
    raw_items = parsed["items"]
    if not isinstance(raw_items, list):
        raise JudgeResponseError(
            f"{context}: 'items' 必须为列表，实际得到 {type(raw_items).__name__}。"
        )

    rubric_by_id = {item.id: item for item in rubric}
    parsed_entries: Dict[str, Tuple[bool, str, str]] = {}
    for idx, entry in enumerate(raw_items):
        entry_ctx = f"{context} items[{idx}]"
        if not isinstance(entry, MappingABC):
            raise JudgeResponseError(
                f"{entry_ctx}: 每条评审结果必须为 JSON 对象，实际得到 {type(entry).__name__}。"
            )
        item_id = _require_non_empty_str(entry, "id", context=entry_ctx)
        if item_id not in rubric_by_id:
            raise JudgeResponseError(
                f"{entry_ctx}: 出现未定义的 rubric id {item_id!r}，"
                f"期望集合为 {sorted(rubric_by_id)}。"
            )
        if item_id in parsed_entries:
            raise JudgeResponseError(f"{entry_ctx}: 存在重复的 rubric id {item_id!r}。")

        if "found_or_pass" not in entry or not isinstance(entry["found_or_pass"], bool):
            raise JudgeResponseError(
                f"{entry_ctx} ({item_id}): 'found_or_pass' 必须为严格布尔值。"
            )
        found_or_pass = entry["found_or_pass"]
        reason = _require_non_empty_str(entry, "reason", context=f"{entry_ctx} ({item_id})")

        if found_or_pass:
            evidence = _require_non_empty_str(
                entry, "evidence", context=f"{entry_ctx} ({item_id})"
            )
        else:
            raw_ev = entry.get("evidence")
            if raw_ev is None:
                evidence = ""
            elif isinstance(raw_ev, str):
                evidence = raw_ev.strip()
            else:
                raise JudgeResponseError(
                    f"{entry_ctx} ({item_id}): 'evidence' 必须为字符串，实际得到 {raw_ev!r}。"
                )

        parsed_entries[item_id] = (found_or_pass, evidence, reason)

    missing_ids = [item.id for item in rubric if item.id not in parsed_entries]
    if missing_ids:
        raise JudgeResponseError(f"{context}: 缺少必须返回的 rubric id: {missing_ids}。")
    return parsed_entries


def _score_rubric_item(
    rubric_item: OpeningRubricItem,
    text: str,
    found_or_pass: bool,
    evidence: str,
    reason: str,
    *,
    context: str,
) -> OpeningItemResult:
    """Compute final ``OpeningItemResult`` with code-enforced thresholds and quote checks."""
    if not found_or_pass:
        located = (
            _locate_evidence_if_present(text, evidence)
            if rubric_item.kind == "judgment" and evidence
            else None
        )
        return OpeningItemResult(
            id=rubric_item.id,
            passed=False,
            evidence=evidence,
            paragraph_index=located[0] if located is not None else None,
            char_ratio=located[1] if located is not None else None,
            reason=reason,
        )

    para_idx, char_ratio = _locate_verified_evidence(
        text, evidence, context=f"{context} ({rubric_item.id})"
    )
    passed = True
    if rubric_item.kind == "positional":
        if rubric_item.max_paragraph is not None and para_idx > rubric_item.max_paragraph:
            passed = False
        if rubric_item.max_char_ratio is not None and char_ratio > rubric_item.max_char_ratio:
            passed = False

    return OpeningItemResult(
        id=rubric_item.id,
        passed=passed,
        evidence=evidence,
        paragraph_index=para_idx,
        char_ratio=char_ratio,
        reason=reason,
    )


class OpeningReviewer:
    """Cross-family LLM reviewer with deterministic code-enforced positional thresholds."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        judge_model: str,
        writer_model: str,
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
        _check_same_or_same_family_models(self.judge_model, self.writer_model)

    def _invoke_judge(
        self,
        *,
        request_id: str,
        chapter: int,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        req = LLMRequest(
            request_id=request_id,
            chapter=chapter,
            attempt=1,
            purpose="critic",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=2048,
            metadata={"model": self.judge_model},
        )
        resp = self.provider.generate(req)
        if not isinstance(resp, LLMResponse) or not isinstance(resp.content, str):
            raise JudgeResponseError(
                f"{request_id}: provider.generate 必须返回含字符串 content 的 LLMResponse。"
            )
        if not resp.content.strip():
            raise JudgeResponseError(f"{request_id}: provider 返回空文本响应。")
        return resp.content

    def review(
        self,
        text: str,
        chapter: int,
        *,
        previous_case_summary: Optional[str] = None,
    ) -> OpeningVerdict:
        """Review chapter 1, 2, or 3 text against ``OPENING_RUBRIC[chapter]`` in one LLM call."""
        _validate_chapter(chapter)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string.")
        if not split_paragraphs(text):
            raise ValueError("text must contain at least one non-empty paragraph.")

        if chapter == 2:
            if not isinstance(previous_case_summary, str) or not previous_case_summary.strip():
                raise ValueError(
                    "chapter 2 review requires a non-empty previous_case_summary."
                )
        elif previous_case_summary is not None and not isinstance(previous_case_summary, str):
            raise ValueError(
                f"previous_case_summary must be a str or None, got {type(previous_case_summary).__name__}."
            )

        rubric = OPENING_RUBRIC[chapter]
        system_prompt, user_prompt = _build_review_prompts(
            chapter, text, rubric, previous_case_summary
        )
        ctx_label = f"opening_review chapter={chapter}"
        raw_content = self._invoke_judge(
            request_id=f"opening-review-ch{chapter}",
            chapter=chapter,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        parsed_entries = _parse_review_entries(raw_content, rubric, context=ctx_label)
        results = [
            _score_rubric_item(
                rubric_item,
                text,
                *parsed_entries[rubric_item.id],
                context=ctx_label,
            )
            for rubric_item in rubric
        ]
        return OpeningVerdict(chapter=chapter, items=results)


__all__ = [
    "EvidenceNotFoundError",
    "JudgeResponseError",
    "OPENING_RUBRIC",
    "OpeningItemResult",
    "OpeningReviewer",
    "OpeningRubricItem",
    "OpeningVerdict",
    "SameModelJudgeError",
    "VALID_RUBRIC_KINDS",
    "locate_evidence",
    "split_paragraphs",
    "writer_directives",
]
