from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .checker import AITicChecker
from .llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse
from .llm.router import RoleModelRouter
from .models import (
    CharacterAnchor,
    ForeshadowItem,
    ProjectMeta,
    ScanReport,
    SceneOutline,
    WorldStateSnapshot,
)

MAX_AUTHOR_NOTE_CHARS = 180
MAX_SURGICAL_POLISH_PARAGRAPHS = 10

_LOGGER = logging.getLogger(__name__)
_QUOTE_PRESERVE_RE = re.compile(r"[“”「」『』\"]")

AUTHOR_NOTE_DIVERSION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"微信|微信号|v信|vx|wx", "禁止出现微信/VX/WX导流词"),
    (r"qq群|q群|qq号|qq|扣扣群|企鹅群", "禁止出现QQ/Q群导流词"),
    (r"公众号|订阅号|服务号", "禁止出现公众号导流词"),
    (r"加群|进群|粉丝群|读者群|交流群|书友群", "禁止出现加群/读者群导流词"),
    (r"微博|小红书|抖音|快手|b站|哔哩哔哩", "禁止出现站外社媒平台导流词"),
    (r"私信|扫码|二维码|https?://", "禁止出现私信/扫码/外链导流词"),
)

_SCORE_DIMENSIONS: Tuple[str, ...] = (
    "first_screen_hook",
    "cool_point_density",
    "comment_interactivity",
    "pacing_drag",
)


class CriticPolisherError(RuntimeError):
    """Base exception for TomatoCriticPolisher errors."""


class CriticResponseParseError(CriticPolisherError):
    """Raised when critic or polisher LLM output is missing, malformed JSON, or invalid schema."""


class SurgicalPolishGateError(CriticPolisherError):
    """Raised when post-polish AITicChecker.scan() detects a CRITICAL violation."""

    def __init__(
        self,
        message: str,
        *,
        report: Optional[ScanReport] = None,
        polished_text: str = "",
    ) -> None:
        super().__init__(message)
        self.report = report
        self.polished_text = polished_text


class AuthorNoteViolationError(CriticPolisherError, ValueError):
    """Raised when an author's note violates length (<= 180 chars) or anti-diversion rules."""


@dataclass
class CriticEvaluation:
    first_screen_hook: float
    cool_point_density: float
    comment_interactivity: float
    pacing_drag: float
    overall_score: float
    weak_paragraphs: List[int] = field(default_factory=list)
    llm_weak_paragraphs: List[int] = field(default_factory=list)
    checker_warning_paragraphs: List[int] = field(default_factory=list)
    paragraph_reasons: Dict[int, str] = field(default_factory=dict)
    critique: str = ""
    passed_threshold: bool = True
    model: str = ""
    raw_response: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SurgicalPolishResult:
    polished_text: str
    original_text: str
    replaced_indices: List[int]
    paragraphs_before: List[str]
    paragraphs_after: List[str]
    scan_report: ScanReport
    model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "polished_text": self.polished_text,
            "original_text": self.original_text,
            "replaced_indices": list(self.replaced_indices),
            "paragraphs_before": list(self.paragraphs_before),
            "paragraphs_after": list(self.paragraphs_after),
            "scan_report": self.scan_report.to_dict(),
            "model": self.model,
        }


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


def _parse_json_dict(raw: str, *, context: str) -> Dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise CriticResponseParseError(f"{context}: empty LLM response")
    cleaned = _strip_json_code_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CriticResponseParseError(
            f"{context}: malformed JSON response ({exc}): {cleaned[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise CriticResponseParseError(
            f"{context}: expected JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _validate_score(payload: Dict[str, Any], key: str) -> float:
    if key not in payload:
        raise CriticResponseParseError(f"Critic response missing required score field '{key}'")
    val = payload[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise CriticResponseParseError(
            f"Critic score '{key}' must be numeric (0~100), got {val!r}"
        )
    fval = float(val)
    if not (0.0 <= fval <= 100.0):
        raise CriticResponseParseError(
            f"Critic score '{key}' out of range [0, 100]: {fval}"
        )
    return round(fval, 2)


def _extract_paragraph_line_positions(text: str) -> Tuple[List[str], List[Tuple[int, int, str]]]:
    """Return (raw_lines_with_endings, [(0-based para_idx, 0-based raw_line_idx, stripped_text), ...])."""
    raw_lines = text.splitlines(keepends=True)
    positions: List[Tuple[int, int, str]] = []
    para_idx = 0
    for line_idx, raw_line in enumerate(raw_lines):
        stripped = raw_line.strip()
        if stripped:
            positions.append((para_idx, line_idx, stripped))
            para_idx += 1
    return raw_lines, positions


def _split_line_ending(raw_line: str) -> Tuple[str, str]:
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], "\r\n"
    if raw_line.endswith("\n"):
        return raw_line[:-1], "\n"
    if raw_line.endswith("\r"):
        return raw_line[:-1], "\r"
    return raw_line, ""


def validate_author_note(note: str, max_chars: int = MAX_AUTHOR_NOTE_CHARS) -> str:
    """Validate that an author's note is non-empty, <= max_chars, and free of traffic-diversion terms."""
    if not isinstance(note, str):
        raise AuthorNoteViolationError("Author note must be a string.")
    cleaned = note.strip()
    if not cleaned:
        raise AuthorNoteViolationError("Author note cannot be empty.")
    if max_chars <= 0 or max_chars > MAX_AUTHOR_NOTE_CHARS:
        max_chars = min(max(1, max_chars), MAX_AUTHOR_NOTE_CHARS)
    if len(cleaned) > max_chars:
        raise AuthorNoteViolationError(
            f"Author note length {len(cleaned)} exceeds strict limit of {max_chars} chars."
        )
    for pattern, reason in AUTHOR_NOTE_DIVERSION_PATTERNS:
        m = re.search(pattern, cleaned, re.IGNORECASE)
        if m:
            raise AuthorNoteViolationError(
                f"Author note rejected by anti-diversion guard ({reason}): matched {m.group(0)!r}"
            )
    return cleaned


def select_polish_targets(
    evaluation: CriticEvaluation,
    num_paragraphs: int,
    *,
    max_targets: int = MAX_SURGICAL_POLISH_PARAGRAPHS,
) -> List[int]:
    """Select the highest-impact weak paragraphs for surgical polishing.

    Prioritizes toxic-editor LLM-flagged paragraphs (especially opening/ending and explicitly
    reasoned drags) before checker-only rhythm warnings, preventing 25+ paragraph mass rewrites
    that drift past chapter length gates.
    """
    valid: List[int] = []
    for idx in evaluation.weak_paragraphs:
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < num_paragraphs:
            if idx not in valid:
                valid.append(idx)
    valid.sort()
    if max_targets <= 0 or len(valid) <= max_targets:
        return valid

    llm_set = {
        idx
        for idx in evaluation.llm_weak_paragraphs
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < num_paragraphs
    }
    reason_set = {
        idx
        for idx, reason in evaluation.paragraph_reasons.items()
        if isinstance(idx, int) and str(reason or "").strip() and 0 <= idx < num_paragraphs
    }
    priority_llm = llm_set | reason_set

    def _rank_key(idx: int) -> Tuple[int, int, int, int]:
        is_llm = 0 if idx in priority_llm else 1
        has_reason = 0 if idx in reason_set else 1
        is_edge = 0 if (idx <= 2 or idx >= num_paragraphs - 2) else 1
        return (is_llm, has_reason, is_edge, idx)

    ranked = sorted(valid, key=_rank_key)
    return sorted(ranked[:max_targets])


class TomatoCriticPolisher:
    """Toxic-editor 4D critic, surgical paragraph-only polisher, and compliant author-note generator."""

    def __init__(
        self,
        provider_or_router: Optional[RoleModelRouter | LLMProvider] = None,
        *,
        router: Optional[RoleModelRouter] = None,
        critic_provider: Optional[LLMProvider] = None,
        polisher_provider: Optional[LLMProvider] = None,
        checker: Optional[AITicChecker] = None,
        pass_score_threshold: float = 75.0,
        max_polish_paragraphs: int = MAX_SURGICAL_POLISH_PARAGRAPHS,
    ) -> None:
        self._router: Optional[RoleModelRouter] = router
        self._fallback_provider: Optional[LLMProvider] = None

        if provider_or_router is not None:
            if isinstance(provider_or_router, RoleModelRouter) or (
                hasattr(provider_or_router, "has_role") and hasattr(provider_or_router, "as_provider")
            ):
                self._router = provider_or_router  # type: ignore[assignment]
            elif hasattr(provider_or_router, "generate"):
                self._fallback_provider = provider_or_router  # type: ignore[assignment]
            else:
                raise LLMConfigurationError(
                    "provider_or_router must be a RoleModelRouter or LLMProvider."
                )

        self._critic_provider = critic_provider
        self._polisher_provider = polisher_provider
        self.checker = checker or AITicChecker()
        self.pass_score_threshold = float(pass_score_threshold)
        self.max_polish_paragraphs = int(max_polish_paragraphs)

    def _call_role(self, role: str, request: LLMRequest) -> LLMResponse:
        if role == "critic" and self._critic_provider is not None:
            return self._critic_provider.generate(request)
        if role == "polisher" and self._polisher_provider is not None:
            return self._polisher_provider.generate(request)
        if self._router is not None:
            return self._router.generate(role, request)
        if self._fallback_provider is not None:
            return self._fallback_provider.generate(request)
        raise LLMConfigurationError(
            f"No LLMProvider or RoleModelRouter configured for role {role!r}."
        )

    def _run_checker(
        self,
        text: str,
        *,
        scene: Optional[SceneOutline] = None,
        project_meta: Optional[ProjectMeta] = None,
        characters: Optional[Dict[str, CharacterAnchor]] = None,
        due_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
        plant_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
        world_state: Optional[WorldStateSnapshot] = None,
        chapter: Optional[int] = None,
    ) -> ScanReport:
        return self.checker.scan(
            text=text,
            scene=scene,
            characters=characters,
            due_foreshadows=due_foreshadows,
            plant_foreshadows=plant_foreshadows,
            project_meta=project_meta,
            world_state=world_state,
            chapter=chapter,
        )

    def evaluate_chapter(
        self,
        text: str,
        scene: Optional[SceneOutline] = None,
        *,
        project_meta: Optional[ProjectMeta] = None,
        scan_report: Optional[ScanReport] = None,
        characters: Optional[Dict[str, CharacterAnchor]] = None,
        world_state: Optional[WorldStateSnapshot] = None,
        chapter: Optional[int] = None,
        combine_diagnostics: bool = True,
    ) -> CriticEvaluation:
        """Score a draft across 4 Tomato dimensions (0~100) and pinpoint 0-based weak paragraph indices."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Cannot evaluate an empty chapter draft.")

        _, positions = _extract_paragraph_line_positions(text)
        paragraphs = [p for _, _, p in positions]
        num_paras = len(paragraphs)

        eff_report = scan_report or self._run_checker(
            text,
            scene=scene,
            project_meta=project_meta,
            characters=characters,
            world_state=world_state,
            chapter=chapter,
        )
        diagnostics: Dict[str, Any] = dict(getattr(eff_report, "diagnostics", {}) or {})

        diag_warning_set: set[int] = set()
        for diag_key in (
            "burstiness_paragraph_indices",
            "bystander_shock_paragraphs",
            "warning_paragraph_indices",
        ):
            for raw_idx in diagnostics.get(diag_key, []) or []:
                if isinstance(raw_idx, int) and 0 <= raw_idx < num_paras:
                    diag_warning_set.add(raw_idx)
        checker_warning_paragraphs = sorted(diag_warning_set)

        numbered_paras = "\n".join(f"[{idx}] {p}" for idx, p in enumerate(paragraphs))
        eff_ch = scene.chapter if scene is not None else (chapter or 1)
        beat_info = scene.beat_type if scene and scene.beat_type else "常规推进"
        hook_info = scene.cliffhanger_hook if scene and scene.cliffhanger_hook else "章末保留追读悬念"
        genre_info = project_meta.genre_id if project_meta and project_meta.genre_id else "未指定"

        diag_note = (
            f"- 静态诊断软警告（Burstiness σ={diagnostics.get('burstiness_sigma', 'N/A')}）："
            f"句长均匀段落={diagnostics.get('burstiness_paragraph_indices', [])}，"
            f"路人震惊套话段落={diagnostics.get('bystander_shock_paragraphs', [])}\n"
            if diagnostics
            else ""
        )

        sys_prompt = (
            "# Role: 番茄小说毒舌主编（严苛追读率审稿专家）\n"
            "你负责对已经通过基础合规检查的章节草稿进行商业追读率苛刻打分，并精准定位拖沓/注水/套话/无爽感的段落索引（0-based）。\n"
            "所有四个维度均按 0~100 分制评分（分数越高代表该维度表现越好、越抓人/越紧凑）。\n"
            "严格只输出合法 JSON，禁止输出 Markdown 以外的多余解释。"
        )
        user_prompt = (
            f"### 章节元数据\n"
            f"- 章节号：第 {eff_ch} 章 ｜ 题材：{genre_info} ｜ 节拍：{beat_info}\n"
            f"- 预期章末钩子：{hook_info}\n"
            f"{diag_note}\n"
            f"### 按 0-based 索引编号的正文段落（共 {num_paras} 段）\n"
            f"{numbered_paras}\n\n"
            "### 输出 JSON 格式要求（字段名必须完全一致）\n"
            "{\n"
            '  "first_screen_hook": 0到100数值（首屏200字抓力）,\n'
            '  "cool_point_density": 0到100数值（压爆爽感密度）,\n'
            '  "comment_interactivity": 0到100数值（段评互动感）,\n'
            '  "pacing_drag": 0到100数值（节奏紧凑与无拖沓表现，越高越好）,\n'
            '  "overall_score": 0到100综合分,\n'
            '  "weak_paragraphs": [0-based整数段落索引列表，若无则[]],\n'
            '  "paragraph_reasons": {"3": "具体扣分原因"},\n'
            '  "critique": "毒舌主编一句话点评"\n'
            "}\n"
        )

        req = LLMRequest(
            request_id=f"ch{eff_ch:04d}_critic_eval",
            chapter=eff_ch,
            attempt=1,
            purpose="critic",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=1600,
        )
        resp = self._call_role("critic", req)
        payload = _parse_json_dict(resp.content, context="evaluate_chapter")

        scores = {dim: _validate_score(payload, dim) for dim in _SCORE_DIMENSIONS}
        if "overall_score" in payload and payload["overall_score"] is not None:
            overall_score = _validate_score(payload, "overall_score")
        else:
            overall_score = round(sum(scores.values()) / len(_SCORE_DIMENSIONS), 2)

        if "weak_paragraphs" not in payload or not isinstance(payload["weak_paragraphs"], list):
            raise CriticResponseParseError(
                "evaluate_chapter: 'weak_paragraphs' must be a list of 0-based paragraph indices"
            )
        llm_weak: List[int] = []
        dropped_out_of_range: List[int] = []
        for raw_idx in payload["weak_paragraphs"]:
            if isinstance(raw_idx, bool) or not isinstance(raw_idx, int):
                raise CriticResponseParseError(
                    f"evaluate_chapter: invalid weak_paragraphs element {raw_idx!r}; expected int"
                )
            if not (0 <= raw_idx < num_paras):
                # LLM hallucinated a paragraph index: ignore it rather than discarding a gated chapter.
                dropped_out_of_range.append(raw_idx)
                continue
            if raw_idx not in llm_weak:
                llm_weak.append(raw_idx)
        llm_weak.sort()
        if dropped_out_of_range:
            _LOGGER.warning(
                "evaluate_chapter: ignoring out-of-range weak paragraph indices %s (valid range [0, %d])",
                dropped_out_of_range,
                num_paras - 1,
            )

        raw_reasons = payload.get("paragraph_reasons", {})
        if raw_reasons is None:
            raw_reasons = {}
        if not isinstance(raw_reasons, dict):
            raise CriticResponseParseError(
                "evaluate_chapter: 'paragraph_reasons' must be an object mapping index -> reason"
            )
        paragraph_reasons: Dict[int, str] = {}
        for k, v in raw_reasons.items():
            try:
                p_idx = int(k)
            except (TypeError, ValueError) as exc:
                raise CriticResponseParseError(
                    f"evaluate_chapter: invalid paragraph_reasons key {k!r}"
                ) from exc
            paragraph_reasons[p_idx] = str(v).strip()

        critique_raw = payload.get("critique", "")
        if critique_raw is None:
            critique_raw = ""
        if not isinstance(critique_raw, str):
            raise CriticResponseParseError("evaluate_chapter: 'critique' must be a string")

        merged_weak = (
            sorted(set(llm_weak) | set(checker_warning_paragraphs))
            if combine_diagnostics
            else list(llm_weak)
        )
        passed_threshold = overall_score >= self.pass_score_threshold and not merged_weak

        return CriticEvaluation(
            first_screen_hook=scores["first_screen_hook"],
            cool_point_density=scores["cool_point_density"],
            comment_interactivity=scores["comment_interactivity"],
            pacing_drag=scores["pacing_drag"],
            overall_score=overall_score,
            weak_paragraphs=merged_weak,
            llm_weak_paragraphs=llm_weak,
            checker_warning_paragraphs=checker_warning_paragraphs,
            paragraph_reasons=paragraph_reasons,
            critique=critique_raw.strip(),
            passed_threshold=passed_threshold,
            model=resp.model,
            raw_response=resp.content,
        )

    def surgical_polish(
        self,
        text: str,
        evaluation_or_indices: CriticEvaluation | Sequence[int],
        *,
        scene: Optional[SceneOutline] = None,
        project_meta: Optional[ProjectMeta] = None,
        characters: Optional[Dict[str, CharacterAnchor]] = None,
        due_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
        plant_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
        world_state: Optional[WorldStateSnapshot] = None,
        chapter: Optional[int] = None,
    ) -> SurgicalPolishResult:
        """Surgically rewrite ONLY flagged paragraphs (+1/-1 context sent to polisher) and re-scan via AITicChecker."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Cannot polish an empty chapter draft.")

        raw_lines, positions = _extract_paragraph_line_positions(text)
        paragraphs_before = [p for _, _, p in positions]
        num_paras = len(paragraphs_before)

        paragraph_reasons: Dict[int, str] = {}
        critique = ""
        if isinstance(evaluation_or_indices, CriticEvaluation):
            raw_targets = select_polish_targets(
                evaluation_or_indices,
                num_paras,
                max_targets=self.max_polish_paragraphs,
            )
            paragraph_reasons = dict(evaluation_or_indices.paragraph_reasons)
            critique = evaluation_or_indices.critique
        else:
            raw_targets = list(evaluation_or_indices)

        target_indices: List[int] = []
        for idx in raw_targets:
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise ValueError(f"Target paragraph index must be int, got {idx!r}")
            if not (0 <= idx < num_paras):
                raise ValueError(
                    f"Target paragraph index {idx} out of range [0, {num_paras - 1}]"
                )
            if idx not in target_indices:
                target_indices.append(idx)
        target_indices.sort()

        eff_ch = scene.chapter if scene is not None else (chapter or 1)
        plant_fs_list = list(plant_foreshadows or [])
        due_fs_list = list(due_foreshadows or [])

        if not target_indices:
            post_report = self._run_checker(
                text,
                scene=scene,
                project_meta=project_meta,
                characters=characters,
                due_foreshadows=due_fs_list,
                plant_foreshadows=plant_fs_list,
                world_state=world_state,
                chapter=chapter,
            )
            if not post_report.passed:
                raise SurgicalPolishGateError(
                    f"Post-polish AITicChecker failed with {len(post_report.critical_violations)} CRITICAL violation(s)",
                    report=post_report,
                    polished_text=text,
                )
            return SurgicalPolishResult(
                polished_text=text,
                original_text=text,
                replaced_indices=[],
                paragraphs_before=paragraphs_before,
                paragraphs_after=list(paragraphs_before),
                scan_report=post_report,
                model="",
            )

        # Collect mandatory verbatim foreshadow keywords so the polisher never paraphrases or drops them.
        required_fs_groups: List[Tuple[str, List[str]]] = []
        all_required_keywords: List[str] = []
        for fs in plant_fs_list:
            kws = [str(k).strip() for k in (fs.plant_keywords or []) if str(k).strip()]
            if kws:
                required_fs_groups.append((f"[埋设 {fs.id} {fs.title}]", kws))
                for kw in kws:
                    if kw not in all_required_keywords:
                        all_required_keywords.append(kw)
        for fs in due_fs_list:
            kws = [str(k).strip() for k in (fs.payoff_keywords or []) if str(k).strip()]
            if kws:
                required_fs_groups.append((f"[回收 {fs.id} {fs.title}]", kws))
                for kw in kws:
                    if kw not in all_required_keywords:
                        all_required_keywords.append(kw)
        if scene is not None and scene.expectation_directives:
            for ed in scene.expectation_directives:
                if ed.action == "fulfill":
                    kws = [str(k).strip() for k in (ed.evidence_keywords or []) if str(k).strip()]
                    if kws:
                        required_fs_groups.append((f"[核销期待 {ed.id} {ed.title}]", kws))
                        for kw in kws:
                            if kw not in all_required_keywords:
                                all_required_keywords.append(kw)

        # Build minimal local context window: ONLY flagged paragraphs plus 1 preceding and 1 following context paragraph
        target_set = set(target_indices)
        context_indices_set: set[int] = set(target_set)
        for idx in target_indices:
            if idx - 1 >= 0:
                context_indices_set.add(idx - 1)
            if idx + 1 < num_paras:
                context_indices_set.add(idx + 1)
        window_indices = sorted(context_indices_set)

        cur_chars = len(re.sub(r"\s+", "", text))
        near_min_floor = scene is not None and (cur_chars - scene.min_chars) <= 60
        near_max_ceil = scene is not None and (scene.max_chars - cur_chars) <= 60

        window_blocks: List[str] = []
        prev_idx: Optional[int] = None
        for idx in window_indices:
            if prev_idx is not None and idx > prev_idx + 1:
                window_blocks.append("--- （非相邻局部窗口分隔，中间段落保持原样） ---")
            role_tag = "【待精修目标段落】" if idx in target_set else "【只读上下文参考（严禁改写）】"
            reason = paragraph_reasons.get(idx, "")
            reason_sfx = f" 扣分原因：{reason}" if (idx in target_set and reason) else ""
            budget_sfx = ""
            quote_sfx = ""
            if idx in target_set:
                orig_len = len(re.sub(r"\s+", "", paragraphs_before[idx]))
                lo_len = orig_len if near_min_floor else max(12, orig_len - 12)
                hi_len = orig_len if near_max_ceil else min(115, orig_len + 12)
                if lo_len > hi_len:
                    lo_len, hi_len = min(orig_len, 115), min(orig_len, 115)
                budget_sfx = f" 【字数预算：原长{orig_len}字，精修后请控制在{lo_len}~{hi_len}字】"
                if _QUOTE_PRESERVE_RE.search(paragraphs_before[idx]):
                    quote_sfx = " 【原段含引号对白/引文，精修后必须保留双引号台词】"
            locked_kws = [kw for kw in all_required_keywords if kw in paragraphs_before[idx]]
            kw_sfx = (
                f" 【必保伏笔原词：{'、'.join(locked_kws)} — 必须原字保留，严禁同义改写或删除】"
                if (idx in target_set and locked_kws)
                else ""
            )
            window_blocks.append(
                f"[{idx}] {role_tag}{reason_sfx}{budget_sfx}{quote_sfx}{kw_sfx}\n{paragraphs_before[idx]}"
            )
            prev_idx = idx

        pov_mode = project_meta.pov_mode if project_meta else self.checker.pov_mode
        panel_policy = project_meta.panel_policy if project_meta else self.checker.panel_policy
        contract_track = project_meta.contract_track if project_meta else self.checker.contract_track

        pov_rule = (
            "保持第一人称“我”限知视角。"
            if pov_mode == "first_person"
            else "保持第三人称限知视角，严禁叙述段出现“我”或切换他人内心。"
        )
        panel_rule = (
            "若含【...】系统面板，单条严格 <= 65 字且禁止 '|' 与斜杠进度条。"
            if panel_policy == "allow_system_tag"
            else "严禁输出任何【...】面板或提示框。"
        )

        gate_guard_lines: List[str] = []
        if required_fs_groups:
            fs_desc = "；".join(
                f"{label} 必保原词（至少保留其一）：{'、'.join(f'「{k}」' for k in kws)}"
                for label, kws in required_fs_groups
            )
            gate_guard_lines.append(
                f"- **伏笔原词零破坏红线**：{fs_desc}。凡是包含上述原词的目标段，精修时必须一字不差保留原词，严禁同义替换（例如不得把“秒针不动”改成“秒针仍不动”或把“游丝仍紧”改成“游丝绷着”）。"
            )
        if scene is not None:
            gate_guard_lines.append(
                f"- **篇幅区间红线**：当前全文约 {cur_chars} 字，章纲硬性允许区间为 {scene.min_chars}～{scene.max_chars} 字；"
                "必须严格遵守每段标注的【字数预算】，做到删水不减量（用高张力博弈/爽感细节置换废话），严禁导致全文跌破下限或暴涨超限。"
            )
        if contract_track != "legacy":
            gate_guard_lines.append(
                "- **竖屏节奏红线**：单段严禁超过 120 字；原段带双引号的对白精修后必须保留双引号以免制造超过 320 字纯旁白墙；"
                "也不得制造超过 4 轮无动作神态的连续裸对话。"
            )
        gate_guard_block = (
            ("\n### 门禁零破坏硬性红线（精修后将立刻复检，破坏即作废）\n" + "\n".join(gate_guard_lines) + "\n")
            if gate_guard_lines
            else ""
        )

        sys_prompt = (
            "# Role: 番茄爆款手术刀精修编辑\n"
            "你只对标有【待精修目标段落】的段落执行局部精修重写，重点治好三大掉分病灶：\n"
            "1. 砍掉无剧情增量的道具反复摆弄（给道具做体检）与同义台词复读，在同一字数预算内换成更锋利的反问、新破绽、利益交锋或打脸反应；\n"
            "2. 把连续 6～12 字的碎短句分镜体合并重排为「25～40 字推进复句 + 4～12 字爆点短句」的强呼吸错落节奏；\n"
            "3. 强化首屏冲击力、爽感兑现力度与段评可吐槽性，让读者忍不住追读。\n"
            f"硬性约束：{pov_rule}{panel_rule}"
            "严禁引入 AI 套话（如“不禁一愣”“眸光微闪”“嘴角勾起一抹”“三分薄凉”“空气仿佛凝固”“不是…而是…”“全场鸦雀无声”），"
            "严禁删除或同义改写任何标记为【必保伏笔原词】的关键字，"
            "严禁引入真实地名或涉黄血腥敏感词，严禁改动任何未标记的上下文段落，严禁输出多段落合并或章节标题。\n"
            "只输出合法 JSON 对象。"
        )
        user_prompt = (
            f"### 局部手术精修任务（第 {eff_ch} 章）\n"
            f"- 需精修的 0-based 段落索引：{target_indices}\n"
            f"- 主编意见：{critique or '压缩拖沓，增强画面动作感与长短句交错节奏'}\n"
            f"{gate_guard_block}\n"
            f"### 局部窗口段落（仅含目标段及其前后各1段上下文）\n"
            + "\n\n".join(window_blocks)
            + "\n\n"
            "### 输出 JSON 格式（必须包含全部待精修索引，每个索引对应一条单段精修正文字符串）\n"
            "{\n"
            '  "replacements": {\n'
            + ",\n".join(f'    "{idx}": "精修后的第{idx}段正文"' for idx in target_indices)
            + "\n  }\n"
            "}\n"
        )

        req = LLMRequest(
            request_id=f"ch{eff_ch:04d}_surgical_polish",
            chapter=eff_ch,
            attempt=1,
            purpose="polish",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            temperature=0.35,
            max_tokens=6000,
            metadata={"target_indices": target_indices},
        )
        resp = self._call_role("polisher", req)
        payload = _parse_json_dict(resp.content, context="surgical_polish")

        replacements_raw = payload.get("replacements")
        if not isinstance(replacements_raw, dict):
            raise CriticResponseParseError(
                "surgical_polish: expected JSON object with 'replacements' dict"
            )

        normalized_replacements: Dict[int, str] = {}
        for k, v in replacements_raw.items():
            try:
                p_idx = int(k)
            except (TypeError, ValueError) as exc:
                raise CriticResponseParseError(
                    f"surgical_polish: invalid replacement index key {k!r}"
                ) from exc
            if p_idx not in target_set:
                continue
            if not isinstance(v, str) or not v.strip():
                raise CriticResponseParseError(
                    f"surgical_polish: replacement for paragraph {p_idx} must be a non-empty string"
                )
            # Collapse accidental internal newlines inside a single paragraph replacement
            single_para = " ".join(part.strip() for part in v.strip().splitlines() if part.strip())
            if not single_para:
                raise CriticResponseParseError(
                    f"surgical_polish: replacement for paragraph {p_idx} is blank"
                )
            normalized_replacements[p_idx] = single_para

        missing = [idx for idx in target_indices if idx not in normalized_replacements]
        if missing:
            raise CriticResponseParseError(
                f"surgical_polish: polisher omitted replacement(s) for flagged paragraph(s): {missing}"
            )

        # Stitch full text while preserving every untouched line & blank separator byte-for-byte
        stitched_lines = list(raw_lines)
        paragraphs_after = list(paragraphs_before)
        for p_idx, line_idx, _ in positions:
            if p_idx in normalized_replacements:
                _, ending = _split_line_ending(stitched_lines[line_idx])
                new_para = normalized_replacements[p_idx]
                stitched_lines[line_idx] = f"{new_para}{ending}"
                paragraphs_after[p_idx] = new_para

        polished_text = "".join(stitched_lines)

        # MANDATORY second AITicChecker.scan() pass on the full stitched text
        post_report = self._run_checker(
            polished_text,
            scene=scene,
            project_meta=project_meta,
            characters=characters,
            due_foreshadows=due_fs_list,
            plant_foreshadows=plant_fs_list,
            world_state=world_state,
            chapter=chapter,
        )
        if not post_report.passed:
            crit_summary = "; ".join(
                f"[{v.rule_id}] {v.matched_text}" for v in post_report.critical_violations[:5]
            )
            raise SurgicalPolishGateError(
                f"Surgical polish introduced {len(post_report.critical_violations)} CRITICAL violation(s) "
                f"and failed post-polish AITicChecker gate: {crit_summary}",
                report=post_report,
                polished_text=polished_text,
            )

        return SurgicalPolishResult(
            polished_text=polished_text,
            original_text=text,
            replaced_indices=target_indices,
            paragraphs_before=paragraphs_before,
            paragraphs_after=paragraphs_after,
            scan_report=post_report,
            model=resp.model,
        )

    def generate_author_note(
        self,
        chapter_text: str,
        scene: Optional[SceneOutline] = None,
        *,
        project_meta: Optional[ProjectMeta] = None,
        cliffhanger_hook: str = "",
        chapter: Optional[int] = None,
        max_chars: int = MAX_AUTHOR_NOTE_CHARS,
    ) -> str:
        """Independently generate <= 180 char interactive author's note with zero traffic-diversion words."""
        if not isinstance(chapter_text, str) or not chapter_text.strip():
            raise ValueError("Cannot generate author_note for empty chapter_text.")

        eff_limit = min(max(1, int(max_chars)), MAX_AUTHOR_NOTE_CHARS)
        eff_ch = scene.chapter if scene is not None else (chapter or 1)
        eff_hook = (
            cliffhanger_hook.strip()
            or (scene.cliffhanger_hook.strip() if scene and scene.cliffhanger_hook else "")
            or chapter_text.strip()[-180:]
        )
        title_hint = (
            project_meta.title
            if project_meta and project_meta.title
            else "本书"
        )

        sys_prompt = (
            "# Role: 番茄爆款互动运营作者\n"
            f"请根据本章结尾悬念独立撰写一段贴合剧情的“作者有话说”求催更互动语。\n"
            f"硬性约束：\n"
            f"1. 严格不超过 {eff_limit} 个字符；\n"
            f"2. 仅围绕本章悬念向读者提问、求催更、求段评投票；\n"
            f"3. 绝对禁止任何站外导流词汇（严禁出现微信、VX、V信、QQ、Q群、公众号、加群、微博、小红书、抖音、快手、B站、扫码、链接等）。\n"
            "仅输出纯文本作者有话说本身，不要加“作者有话说：”前缀或引号。"
        )
        user_prompt = (
            f"- 作品：{title_hint} ｜ 第 {eff_ch} 章\n"
            f"- 本章悬念钩子：{eff_hook}\n"
            f"- 本章结尾片段：{chapter_text.strip()[-240:]}\n"
        )

        req = LLMRequest(
            request_id=f"ch{eff_ch:04d}_author_note",
            chapter=eff_ch,
            attempt=1,
            purpose="polish",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            temperature=0.6,
            max_tokens=300,
            metadata={"sub_purpose": "author_note"},
        )
        resp = self._call_role("polisher", req)
        return validate_author_note(resp.content, max_chars=eff_limit)
