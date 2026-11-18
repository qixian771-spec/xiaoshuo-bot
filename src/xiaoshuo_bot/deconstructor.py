from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence
import uuid

from .genres import GenreRecipe, get_genre
from .llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse
from .llm.router import RoleModelRouter
from .models import EMOTION_POLARITIES
from .plagiarism_guard import (
    PlagiarismAndCollisionGuard,
    PlagiarismCheckResult,
    PlagiarismViolationError,
)


class DeconstructorError(RuntimeError):
    """Base exception for benchmark bestseller deconstruction and skin-swap errors."""


class BlueprintExtractionError(DeconstructorError):
    """Raised when LLM output cannot be parsed into a valid 5D structural blueprint."""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>[\s\S]*?)\s*```", re.IGNORECASE)


def _parse_json_object(raw_text: str, stage: str) -> Dict[str, Any]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise BlueprintExtractionError(f"{stage}: LLM returned empty content")
    text = raw_text.strip()
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group("body").strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlueprintExtractionError(
            f"{stage}: failed to parse JSON from LLM output ({exc})"
        ) from exc
    if not isinstance(parsed, dict):
        raise BlueprintExtractionError(f"{stage}: expected JSON object, got {type(parsed).__name__}")
    return parsed


def _require_nonempty(mapping: Mapping[str, Any], key: str, stage: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise BlueprintExtractionError(f"{stage}: missing required key '{key}'")
    val = mapping[key]
    if isinstance(val, (str, list, dict)) and not val:
        raise BlueprintExtractionError(f"{stage}: key '{key}' must not be empty")
    return val


@dataclass
class DeconstructedBlueprint:
    target_genre_id: str
    source_domain: str
    target_domain: str
    power_topology: Dict[str, Any]
    info_asymmetry: Dict[str, Any]
    emotion_curve: List[Dict[str, Any]]
    conflict_ladder: List[Dict[str, Any]]
    micro_hooks_500w: List[str]
    isomorphic_setting: Dict[str, Any] = field(default_factory=dict)
    opening_motif: str = ""
    golden_finger_type: str = ""
    sanitized: bool = False
    plagiarism_report: Optional[PlagiarismCheckResult] = None

    def __post_init__(self) -> None:
        get_genre(self.target_genre_id)
        for idx, item in enumerate(self.emotion_curve):
            if isinstance(item, dict) and "polarity" in item:
                pol = str(item["polarity"])
                if pol not in EMOTION_POLARITIES:
                    raise BlueprintExtractionError(
                        f"emotion_curve[{idx}] invalid polarity {pol!r}; expected one of {EMOTION_POLARITIES}"
                    )

    def to_summary_text(self) -> str:
        """Flatten all textual fields for plagiarism verification and downstream planning prompts."""
        parts: List[str] = [
            self.source_domain,
            self.target_domain,
            self.opening_motif,
            self.golden_finger_type,
            json.dumps(self.power_topology, ensure_ascii=False),
            json.dumps(self.info_asymmetry, ensure_ascii=False),
            json.dumps(self.emotion_curve, ensure_ascii=False),
            json.dumps(self.conflict_ladder, ensure_ascii=False),
            "\n".join(str(h) for h in self.micro_hooks_500w),
            json.dumps(self.isomorphic_setting, ensure_ascii=False),
        ]
        return "\n".join(p for p in parts if p)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_genre_id": self.target_genre_id,
            "source_domain": self.source_domain,
            "target_domain": self.target_domain,
            "power_topology": dict(self.power_topology),
            "info_asymmetry": dict(self.info_asymmetry),
            "emotion_curve": list(self.emotion_curve),
            "conflict_ladder": list(self.conflict_ladder),
            "micro_hooks_500w": list(self.micro_hooks_500w),
            "isomorphic_setting": dict(self.isomorphic_setting),
            "opening_motif": self.opening_motif,
            "golden_finger_type": self.golden_finger_type,
            "sanitized": self.sanitized,
            "plagiarism_report": (
                self.plagiarism_report.to_dict() if self.plagiarism_report is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeconstructedBlueprint":
        rep_raw = data.get("plagiarism_report")
        report = (
            PlagiarismCheckResult(
                passed=bool(rep_raw["passed"]),
                max_lcs_hanzi=int(rep_raw["max_lcs_hanzi"]),
                matched_lcs_text=str(rep_raw.get("matched_lcs_text", "")),
                max_4gram_jaccard=float(rep_raw["max_4gram_jaccard"]),
                checked_against_count=int(rep_raw["checked_against_count"]),
                details=list(rep_raw.get("details", [])),
            )
            if isinstance(rep_raw, Mapping)
            else None
        )
        return cls(
            target_genre_id=str(data["target_genre_id"]),
            source_domain=str(data.get("source_domain", "")),
            target_domain=str(data.get("target_domain", "")),
            power_topology=dict(data["power_topology"]),
            info_asymmetry=dict(data["info_asymmetry"]),
            emotion_curve=list(data["emotion_curve"]),
            conflict_ladder=list(data["conflict_ladder"]),
            micro_hooks_500w=[str(x) for x in data["micro_hooks_500w"]],
            isomorphic_setting=dict(data.get("isomorphic_setting", {})),
            opening_motif=str(data.get("opening_motif", "")),
            golden_finger_type=str(data.get("golden_finger_type", "")),
            sanitized=bool(data.get("sanitized", False)),
            plagiarism_report=report,
        )


def _invoke_planner(
    provider: LLMProvider | RoleModelRouter,
    request: LLMRequest,
) -> LLMResponse:
    if isinstance(provider, RoleModelRouter):
        return provider.generate("planner", request)
    if isinstance(provider, LLMProvider):
        return provider.generate(request)
    raise LLMConfigurationError(
        f"provider must be RoleModelRouter or LLMProvider, got {type(provider).__name__}"
    )


class BestsellerDeconstructor:
    """Extracts 5D structural skeletons from benchmark chapters 3-10 and runs isomorphic skin-swap."""

    def __init__(
        self,
        provider: LLMProvider | RoleModelRouter,
        *,
        guard: Optional[PlagiarismAndCollisionGuard] = None,
        max_attempts: int = 3,
    ) -> None:
        if provider is None:
            raise LLMConfigurationError("BestsellerDeconstructor requires a configured LLM provider/router")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self.provider = provider
        self.guard = guard or PlagiarismAndCollisionGuard()
        self.max_attempts = max_attempts

    def extract_5d_structure(
        self,
        benchmark_chapters: Sequence[str],
        *,
        source_domain: str = "",
    ) -> Dict[str, Any]:
        """Step 1: Extract abstract 5D structural metrics from benchmark chapters 3-10."""
        cleaned_chapters = [ch.strip() for ch in benchmark_chapters if isinstance(ch, str) and ch.strip()]
        if not cleaned_chapters:
            raise BlueprintExtractionError("benchmark_chapters must contain at least one non-empty chapter")

        joined = "\n\n".join(
            f"=== 对标样章 {idx} ===\n{txt}" for idx, txt in enumerate(cleaned_chapters, start=1)
        )
        system_prompt = (
            "你是资深网文结构拆解主编。请只抽取对标文本的抽象五维结构骨架，严禁照抄原文句子、人名、地名与具体物品。\n"
            "必须严格输出单个 JSON 对象，包含且仅包含以下核心字段：\n"
            "- source_domain: 原书赛道/题材描述\n"
            "- power_topology: 对象（含 protagonist_leverage, opponent_advantage, reversal_mechanism）\n"
            "- info_asymmetry: 对象（含 hidden_card, dramatic_irony_anchors, reveal_cadence）\n"
            "- emotion_curve: 数组（每项含 chapter_offset, polarity [suppress|burst|neutral|mixed], intensity [1-10], beat_purpose）\n"
            "- conflict_ladder: 数组（每项含 tier, antagonist_archetype, stake, resolution_mode）\n"
            "- micro_hooks_500w: 数组（抽象后的500字微钩子结构模板，不含原文词句）"
        )
        user_prompt = (
            f"原书领域提示：{source_domain or '自动识别'}\n"
            f"请对以下 {len(cleaned_chapters)} 章对标样章执行五维骨架提取（只输出 JSON）：\n\n{joined}"
        )
        req = LLMRequest(
            request_id=f"deconstruct-extract-{uuid.uuid4().hex[:8]}",
            chapter=0,
            attempt=1,
            purpose="plan",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=4000,
            metadata={"stage": "extract_5d", "chapter_count": len(cleaned_chapters)},
        )
        resp = _invoke_planner(self.provider, req)
        data = _parse_json_object(resp.content, "extract_5d_structure")
        for key in (
            "power_topology",
            "info_asymmetry",
            "emotion_curve",
            "conflict_ladder",
            "micro_hooks_500w",
        ):
            _require_nonempty(data, key, "extract_5d_structure")
        return data

    def isomorphic_skin_swap(
        self,
        raw_structure: Mapping[str, Any],
        *,
        target_genre_id: str,
        target_domain: str = "",
        hot_keywords: Sequence[str] = (),
        motif_hint: str = "",
        violation_feedback: str = "",
        attempt: int = 1,
    ) -> DeconstructedBlueprint:
        """Step 2: Execute cross-domain isomorphic skin-swap into target genre & domain."""
        recipe: GenreRecipe = get_genre(target_genre_id)
        resolved_target_domain = target_domain.strip() or recipe.display_name

        system_prompt = (
            "你是番茄爆款跨界同构策划师。请将抽象五维骨架彻底跨界置换到目标赛道与新世界观，\n"
            "严禁保留原书任何人物关系标签原文、专有名词或连续4字以上原句措辞。\n"
            "必须严格输出单个 JSON 对象，包含以下字段：\n"
            "- source_domain, target_domain\n"
            "- opening_motif (一句话开篇母题), golden_finger_type (金手指机制类型)\n"
            "- power_topology, info_asymmetry, emotion_curve, conflict_ladder, micro_hooks_500w\n"
            "- isomorphic_setting: 对象（含 world_backdrop, protagonist_identity, golden_finger_rule, core_conflict）"
        )
        feedback_block = (
            f"\n[上一轮查重失败，必须彻底重写措辞并消除以下重合]：\n{violation_feedback}\n"
            if violation_feedback
            else ""
        )
        user_prompt = (
            f"目标类目：{recipe.genre_id}（{recipe.display_name}，频道={recipe.channel}）\n"
            f"目标领域：{resolved_target_domain}\n"
            f"热榜关键词：{', '.join(hot_keywords) if hot_keywords else '无'}\n"
            f"母题偏好：{motif_hint or '结合目标赛道自动选择高留存母题'}\n"
            f"{feedback_block}\n"
            f"五维结构骨架 JSON：\n{json.dumps(raw_structure, ensure_ascii=False, indent=2)}"
        )
        req = LLMRequest(
            request_id=f"deconstruct-swap-{uuid.uuid4().hex[:8]}",
            chapter=0,
            attempt=attempt,
            purpose="plan",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.5,
            max_tokens=4500,
            metadata={
                "stage": "skin_swap",
                "target_genre_id": recipe.genre_id,
                "attempt": attempt,
            },
        )
        resp = _invoke_planner(self.provider, req)
        data = _parse_json_object(resp.content, "isomorphic_skin_swap")
        for key in (
            "power_topology",
            "info_asymmetry",
            "emotion_curve",
            "conflict_ladder",
            "micro_hooks_500w",
            "isomorphic_setting",
        ):
            _require_nonempty(data, key, "isomorphic_skin_swap")

        return DeconstructedBlueprint(
            target_genre_id=recipe.genre_id,
            source_domain=str(data.get("source_domain") or raw_structure.get("source_domain") or ""),
            target_domain=str(data.get("target_domain") or resolved_target_domain),
            power_topology=dict(data["power_topology"]),
            info_asymmetry=dict(data["info_asymmetry"]),
            emotion_curve=list(data["emotion_curve"]),
            conflict_ladder=list(data["conflict_ladder"]),
            micro_hooks_500w=[str(x) for x in data["micro_hooks_500w"]],
            isomorphic_setting=dict(data["isomorphic_setting"]),
            opening_motif=str(data.get("opening_motif") or motif_hint or ""),
            golden_finger_type=str(data.get("golden_finger_type") or ""),
            sanitized=False,
            plagiarism_report=None,
        )

    def deconstruct(
        self,
        benchmark_chapters: Sequence[str],
        *,
        target_genre_id: str,
        source_domain: str = "",
        target_domain: str = "",
        hot_keywords: Sequence[str] = (),
        motif_hint: str = "",
    ) -> DeconstructedBlueprint:
        """Full 5D extraction + cross-domain isomorphic skin-swap + hard anti-rewrite check."""
        raw_structure = self.extract_5d_structure(
            benchmark_chapters, source_domain=source_domain
        )

        last_violation: Optional[PlagiarismViolationError] = None
        feedback = ""
        for attempt in range(1, self.max_attempts + 1):
            blueprint = self.isomorphic_skin_swap(
                raw_structure,
                target_genre_id=target_genre_id,
                target_domain=target_domain,
                hot_keywords=hot_keywords,
                motif_hint=motif_hint,
                violation_feedback=feedback,
                attempt=attempt,
            )
            try:
                report = self.guard.check_against_benchmark(
                    blueprint.to_summary_text(),
                    benchmark_chapters,
                    raise_on_violation=True,
                )
            except PlagiarismViolationError as exc:
                last_violation = exc
                feedback = str(exc)
                continue

            blueprint.sanitized = True
            blueprint.plagiarism_report = report
            return blueprint

        assert last_violation is not None
        raise last_violation


__all__ = [
    "DeconstructorError",
    "BlueprintExtractionError",
    "DeconstructedBlueprint",
    "BestsellerDeconstructor",
]
