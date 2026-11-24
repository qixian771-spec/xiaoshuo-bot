"""Whole-book structural blueprint architect.

Transforms a raw creative idea + genre recipe + target total character count +
cross-book cast exclusion list into a strictly validated ``BookBlueprint`` via
the planner-role LLM, with structured validation feedback retries and zero
silent defaults.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import uuid
import yaml

from .blueprint import (
    BlueprintValidationError,
    BookBlueprint,
    MAX_TOTAL_CHARS_DEVIATION,
    MAX_VOLUMES,
    MIN_VOLUMES,
    SCALE_POLICIES,
    load_blueprint,
    validate_blueprint,
)
from .genres import GenreRecipe, UnknownGenreError, get_genre
from .llm.base import LLMProvider, LLMRequest
from .llm.router import RoleModelRouter
from .models import ProjectValidationError
from .namer import AI_NAME_BLACKLIST
from .planner import _invoke_planner, _parse_json_payload


logger = logging.getLogger(__name__)

MIN_TARGET_TOTAL_CHARS = 100_000
ARCHITECT_MAX_TOKENS = 16384


class StoryArchitectError(ProjectValidationError):
    """Raised when StoryArchitect input validation or blueprint drafting fails."""

    def __init__(self, message: str, errors: Optional[Sequence[str]] = None) -> None:
        self.errors: Tuple[str, ...] = tuple(str(e) for e in (errors or []) if str(e).strip())
        super().__init__(message)


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_existing_cast_names(existing_cast_names: Iterable[str]) -> List[str]:
    if existing_cast_names is None or isinstance(existing_cast_names, (str, bytes)):
        raise StoryArchitectError(
            "existing_cast_names must be an iterable of non-empty strings (not str/bytes/None)"
        )
    try:
        raw_items = list(existing_cast_names)
    except TypeError as exc:
        raise StoryArchitectError(
            f"existing_cast_names must be iterable, got {type(existing_cast_names).__name__}"
        ) from exc

    cleaned: List[str] = []
    seen = set()
    for idx, item in enumerate(raw_items):
        if not isinstance(item, str) or not item.strip():
            raise StoryArchitectError(
                f"existing_cast_names[{idx}] must be a non-empty string, got {item!r}"
            )
        name = item.strip()
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
    return cleaned


def _validate_output_path(output_path: Optional[str | Path]) -> Optional[Path]:
    if output_path is None:
        return None
    if isinstance(output_path, str):
        if not output_path.strip():
            raise StoryArchitectError("output_path must not be an empty string")
        path = Path(output_path)
    elif isinstance(output_path, Path):
        path = output_path
    else:
        raise StoryArchitectError(
            f"output_path must be str, Path, or None, got {type(output_path).__name__}"
        )
    if path.exists() and path.is_dir():
        raise StoryArchitectError(f"output_path points to an existing directory: {path}")
    return path


def _build_schema_example(genre_id: str, target_total_chars: int, min_ch_chars: int, max_ch_chars: int) -> Dict[str, Any]:
    avg_chars = (min_ch_chars + max_ch_chars) / 2.0
    approx_chapters = max(60, round(target_total_chars / avg_chars))
    v1_end = max(20, approx_chapters // 3)
    v2_end = max(v1_end + 20, (approx_chapters * 2) // 3)
    v3_end = max(v2_end + 20, approx_chapters)
    return {
        "premise": {
            "logline": "<一句话核心故事卖点，交代主角、外挂/核心矛盾与终极目标>",
            "selling_points": [
                "<核心卖点 1>",
                "<核心卖点 2>",
            ],
            "selling_point_keywords": ["<关键词1>", "<关键词2>", "<关键词3>", "<关键词4>"],
            "benchmark_refs": [
                {"title": "<对标热书名>", "why": "<拆解借鉴的节奏/结构长板与本书差异化>"}
            ],
            "target_readers": "<精准目标读者画像>",
        },
        "target_total_chars": target_total_chars,
        "chapter_chars_range": [min_ch_chars, max_ch_chars],
        "protagonist": {
            "character_id": "mc",
            "desire": "<主角最迫切的显性诉求与深层执念>",
            "flaw": "<主角真实性格缺陷，不能是无缺点爽文机器>",
            "arc": "<从开篇到结局的清晰成长弧线>",
            "voice_card": {
                "speech_patterns": "<说话节奏、用词习惯与反击风格>",
                "catchphrases": ["<至多 1 条口头禅，也可为空列表 []>"],
            },
        },
        "golden_finger": {
            "rules": "<外挂或核心能力的具体触发条件与呈现方式>",
            "cost": "<使用能力的明确物理/精神/资源代价>",
            "limits": "<不可越过的规则边界，严禁无脑全知全能>",
            "upgrade_path": [
                {"volume": 1, "unlock": "<第 1 卷初始技法与破局玩法>"},
                {"volume": 2, "unlock": "<第 2 卷技法轮换/进阶解锁，防审美疲劳>"},
                {"volume": 3, "unlock": "<第 3 卷终局形态与决战用法>"},
            ],
        },
        "scale_rules": [
            {
                "ledger_key": "cash",
                "policy": "bounded_realistic",
                "hard_max": 5000000,
                "unit": "元",
            }
        ],
        "volumes": [
            {
                "index": 1,
                "title": "<第 1 卷卷名>",
                "chapter_start": 1,
                "chapter_end": v1_end,
                "protagonist_goal": "<本卷核心目标>",
                "antagonist_id": "vol1_boss",
                "cool_point_modes": ["<本卷主打爽点技法 A>", "<本卷主打爽点技法 B>"],
                "climax": "<本卷最高潮冲突事件>",
                "payoff": "<本卷结算回报与阶段跃迁>",
                "enters": ["mc", "vol1_boss"],
                "exits": [{"id": "vol1_boss", "how": "<具体退场方式与代价>"}],
                "map": None,
                "key_scenes": ["<名场面 1>", "<名场面 2>"],
                "foreshadows_plant": ["fs_core_01"],
                "foreshadows_payoff": [],
            },
            {
                "index": 2,
                "title": "<第 2 卷卷名>",
                "chapter_start": v1_end + 1,
                "chapter_end": v2_end,
                "protagonist_goal": "<本卷核心目标>",
                "antagonist_id": "vol2_boss",
                "cool_point_modes": ["<区别于第 1 卷的爽点技法 C>"],
                "climax": "<本卷高潮>",
                "payoff": "<本卷回报>",
                "enters": ["vol2_boss"],
                "exits": [{"id": "vol2_boss", "how": "<退场方式>"}],
                "map": {"location_id": "loc_vol2", "name": "<第 2 卷新地图名称>"},
                "key_scenes": ["<名场面 1>", "<名场面 2>"],
                "foreshadows_plant": [],
                "foreshadows_payoff": [],
            },
            {
                "index": 3,
                "title": "<第 3 卷终局卷名>",
                "chapter_start": v2_end + 1,
                "chapter_end": v3_end,
                "protagonist_goal": "<终卷核心目标>",
                "antagonist_id": "vol3_boss",
                "cool_point_modes": ["<终局总清算技法 D>"],
                "climax": "<全书终极决战>",
                "payoff": "<主线彻底闭环与结局落定>",
                "enters": ["vol3_boss"],
                "exits": [{"id": "vol3_boss", "how": "<终局落幕方式>"}],
                "map": {"location_id": "loc_vol3", "name": "<终局主舞台>"},
                "key_scenes": ["<名场面 1>", "<名场面 2>"],
                "foreshadows_plant": [],
                "foreshadows_payoff": ["fs_core_01"],
            },
        ],
        "cast": [
            {
                "id": "mc",
                "name": "<2~4字通用简体常用汉字姓名，禁止AI黑名单、禁止与已有作品重名撞音>",
                "role": "<主角身份定位>",
                "first_volume": 1,
                "exit_volume": None,
                "appearance_card": "<具体辨识度外貌、衣着与随身物件>",
                "voice_card": {"speech_patterns": "<说话特点>", "catchphrases": []},
                "arc": "<全书弧线>",
            }
        ],
        "foreshadow_plan": [
            {
                "id": "fs_core_01",
                "title": "<核心跨卷伏笔标题>",
                "plant_volume": 1,
                "payoff_volume": 3,
            }
        ],
        "ending": "<清晰完整、有情感与主线双闭环的全书大结局，严禁开放式烂尾或无限续卷>",
        "style": {
            "genre_id": genre_id,
            "allow_pov_inner_monologue": True,
            "humor": False,
            "narration_rules": "<可执行的叙事规则、对话比例、禁忌与细节质感要求>",
            "sample_text": "<120~260字真实风格示范正文，展现人物张力与画面感>",
        },
        "adaptation": {
            "visual_hook": "<漫改/短剧一眼抓住观众的核心视觉钩子>",
            "protagonist_look": "<主角标志性视觉形象锚点>",
        },
    }


def _atomic_write_and_verify_blueprint(
    bp: BookBlueprint,
    out_path: Path,
    existing_cast_names: Sequence[str],
) -> BookBlueprint:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    yaml_text = yaml.safe_dump(bp.to_dict(), allow_unicode=True, sort_keys=False)
    try:
        tmp_path.write_text(yaml_text, encoding="utf-8", newline="\n")
        staged = load_blueprint(tmp_path)
        validate_blueprint(staged, existing_cast_names=existing_cast_names)
        tmp_path.replace(out_path)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise StoryArchitectError(f"Failed to atomically write and verify blueprint {out_path}: {exc}") from exc

    reloaded = load_blueprint(out_path)
    validate_blueprint(reloaded, existing_cast_names=existing_cast_names)
    return reloaded


class StoryArchitect:
    """Whole-book structural blueprint architect backed by a planner-role LLM."""

    @staticmethod
    def _build_system_prompt() -> str:
        return (
            "你是番茄/网文资深总编兼全书骨架设计师。你的任务是把立项创意、目标总字数、对标标尺与创作约束，"
            "一次性设计成结构严密、有明确大结局、分卷爽点与技法持续轮换、绝不注水不烂尾的完整全书蓝图（BookBlueprint）。\n"
            "【输出硬规矩】：\n"
            "1. 严格只输出单个合法 JSON 对象，禁止在 JSON 前后添加解释、注释或多余文字。\n"
            "2. 所有必填字段必须完整给出，严禁省略字段、严禁把 null 填进非空字符串字段、严禁把字符串填进布尔/整数字段。\n"
            "3. 卷号、章号、角色 ID、伏笔 ID 必须 100% 前后引用自洽；全书估算总字数与目标总字数偏差不得超过 15%。\n"
            "4. 所有角色姓名必须为 2~4 个通用简体常用汉字，严禁使用 AI 高频网文黑名单名，且书内与跨书严禁重名或无声调拼音撞音。"
        )

    @staticmethod
    def _build_user_prompt(
        *,
        idea: str,
        recipe: GenreRecipe,
        target_total_chars: int,
        existing_cast_names: Sequence[str],
        benchmark_notes: str,
        constraints: str,
    ) -> str:
        min_ch, max_ch = recipe.golden_word_count_range
        if not (_is_strict_int(min_ch) and _is_strict_int(max_ch) and 0 < min_ch < max_ch):
            min_ch, max_ch = 2100, 2500
        avg_ch = (min_ch + max_ch) / 2.0
        est_chapters = round(target_total_chars / avg_ch)

        schema_example = _build_schema_example(recipe.genre_id, target_total_chars, min_ch, max_ch)
        blacklist_sample = "、".join(sorted(AI_NAME_BLACKLIST)[:16])

        lines: List[str] = [
            "【立项任务单】",
            f"- 核心创意：{idea}",
            f"- 所属赛道：{recipe.display_name}（genre_id={recipe.genre_id}，频道={recipe.channel}，主分类={recipe.tomato_primary_category}）",
            f"- 目标总字数：{target_total_chars} 字（按单章约 {min_ch}~{max_ch} 字、均长约 {avg_ch:.0f} 字估算约 {est_chapters} 章）",
            f"- 卷数约束：{MIN_VOLUMES}..{MAX_VOLUMES} 卷，index 从 1 连续递增；chapter_start..chapter_end 从第 1 章连续覆盖至全书终章，无缺口无重叠；总章数 × ((chapter_chars_range[0] + chapter_chars_range[1]) / 2) 与 target_total_chars 偏差必须 <= {int(MAX_TOTAL_CHARS_DEVIATION * 100)}%。",
            "",
            "【赛道常识与底线】",
            recipe.common_sense_card.format_prompt_block(),
            "",
            "【跨书角色防撞名撞音清单（严禁出现完全同名或无声调拼音相同的人名）】",
            "、".join(existing_cast_names) if existing_cast_names else "（当前书库暂无已有角色名，但仍须保证本书角色之间互不重名撞音）",
            f"同时严禁命中 AI 高频黑名单名（如：{blacklist_sample} 等）以及生僻字/繁体字/非汉字字符。",
            "",
        ]
        if benchmark_notes.strip():
            lines.extend(
                [
                    "【对标标尺与拆书笔记】",
                    benchmark_notes.strip(),
                    "",
                ]
            )
        if constraints.strip():
            lines.extend(
                [
                    "【额外创作硬约束】",
                    constraints.strip(),
                    "",
                ]
            )
        lines.extend(
            [
                "【核心设计硬要求】",
                "1. premise.selling_points 至少 1 条；premise.selling_point_keywords 至少 3 个具体核心卖点词；premise.benchmark_refs 至少 1 条（含 title 和 why）。",
                "2. golden_finger 必须写清 rules、cost、limits 与分卷技法轮换 upgrade_path（每个 volume 必须在有效卷号内），相邻卷的 cool_point_modes 必须轮换升级，避免套路重复。",
                f"3. scale_rules 至少 1 条，policy 只能取 {SCALE_POLICIES}；policy='bounded_realistic' 时 hard_max 必须为 > 0 的有限数值，policy='unbounded_fantasy' 时 hard_max 可为 null 或 > 0。",
                "4. 第 2 卷起每卷 enters 至少包含 1 名新登场/回归角色 ID；每卷 key_scenes 至少 2 个具体名场面；所有 protagonist.character_id、antagonist_id、enters、exits[].id 必须存在于 cast[].id 且 cast ID 唯一；每个角色 first_volume/exit_volume 必须在有效卷号内且 exit_volume >= first_volume（常驻未退场填 null）。",
                "5. voice_card.catchphrases 为字符串列表，至多 1 条（可为 []）。",
                "6. foreshadow_plan 中每条满足 plant_volume <= payoff_volume 且在有效卷号内；各卷 foreshadows_plant / foreshadows_payoff 引用的 ID 必须全部定义在 foreshadow_plan 中。",
                f"7. style.genre_id 必须严格填写 {recipe.genre_id!r}；style.allow_pov_inner_monologue 与 style.humor 必须是严格 JSON 布尔值 true 或 false。",
                "8. ending 必须给出清晰完满的大结局，绝不允许无限续卷。",
                "",
                "【必须严格遵守的 JSON 字段结构示例】",
                json.dumps(schema_example, ensure_ascii=False, indent=2),
            ]
        )
        return "\n".join(lines)

    def draft_blueprint(
        self,
        *,
        idea: str,
        genre_id: str,
        target_total_chars: int,
        existing_cast_names: Iterable[str],
        provider: LLMProvider | RoleModelRouter,
        benchmark_notes: str = "",
        constraints: str = "",
        max_retries: int = 3,
        output_path: Optional[str | Path] = None,
    ) -> BookBlueprint:
        """Draft, strictly validate, retry with full error feedback, and optionally persist a BookBlueprint."""
        if not isinstance(idea, str) or not idea.strip():
            raise StoryArchitectError("idea must be a non-empty string")
        clean_idea = idea.strip()

        if not isinstance(genre_id, str) or not genre_id.strip():
            raise StoryArchitectError("genre_id must be a non-empty string")
        clean_genre_id = genre_id.strip()
        try:
            recipe = get_genre(clean_genre_id)
        except UnknownGenreError as exc:
            raise StoryArchitectError(f"Unknown genre_id {clean_genre_id!r}: {exc}") from exc

        if not _is_strict_int(target_total_chars) or target_total_chars < MIN_TARGET_TOTAL_CHARS:
            raise StoryArchitectError(
                f"target_total_chars must be an integer >= {MIN_TARGET_TOTAL_CHARS}, got {target_total_chars!r}"
            )

        clean_existing = _validate_existing_cast_names(existing_cast_names)

        if not isinstance(provider, (LLMProvider, RoleModelRouter)):
            raise StoryArchitectError(
                f"provider must be LLMProvider or RoleModelRouter, got {type(provider).__name__}"
            )
        if not isinstance(benchmark_notes, str):
            raise StoryArchitectError(
                f"benchmark_notes must be str, got {type(benchmark_notes).__name__}"
            )
        if not isinstance(constraints, str):
            raise StoryArchitectError(
                f"constraints must be str, got {type(constraints).__name__}"
            )
        if not _is_strict_int(max_retries) or max_retries < 1:
            raise StoryArchitectError(f"max_retries must be an integer >= 1, got {max_retries!r}")

        out_path = _validate_output_path(output_path)

        system_prompt = self._build_system_prompt()
        base_user_prompt = self._build_user_prompt(
            idea=clean_idea,
            recipe=recipe,
            target_total_chars=target_total_chars,
            existing_cast_names=clean_existing,
            benchmark_notes=benchmark_notes,
            constraints=constraints,
        )

        last_errors: List[str] = []
        attempt_history: List[str] = []

        for attempt in range(1, max_retries + 1):
            feedback = ""
            if last_errors:
                bullet_list = "\n".join(f"- {err}" for err in last_errors)
                feedback = (
                    f"\n\n【第 {attempt - 1} 次输出未通过蓝图硬校验，必须逐条彻底修复以下全部错误后重新输出完整合法 JSON（严禁省略其他字段）】：\n"
                    f"{bullet_list}\n"
                )
            req = LLMRequest(
                request_id=f"story-architect-{attempt}-{uuid.uuid4().hex[:8]}",
                chapter=0,
                attempt=attempt,
                purpose="plan",
                system_prompt=system_prompt,
                user_prompt=base_user_prompt + feedback,
                temperature=0.7,
                max_tokens=ARCHITECT_MAX_TOKENS,
                metadata={
                    "stage": "story_architect_blueprint",
                    "genre_id": clean_genre_id,
                    "attempt": attempt,
                },
            )
            resp = _invoke_planner(provider, req)
            try:
                payload = _parse_json_payload(resp.content, "story_architect_blueprint")
                bp = BookBlueprint.from_dict(payload)
                validate_blueprint(bp, existing_cast_names=clean_existing)

                consistency_errors: List[str] = []
                if bp.style.genre_id.strip() != clean_genre_id:
                    consistency_errors.append(
                        f"style.genre_id: must match requested genre_id {clean_genre_id!r}, "
                        f"got {bp.style.genre_id!r}"
                    )
                req_deviation = abs(bp.target_total_chars - target_total_chars) / target_total_chars
                if req_deviation > MAX_TOTAL_CHARS_DEVIATION:
                    consistency_errors.append(
                        f"target_total_chars: blueprint target_total_chars={bp.target_total_chars} "
                        f"deviates by {req_deviation:.1%} from requested target_total_chars={target_total_chars} "
                        f"(exceeds {MAX_TOTAL_CHARS_DEVIATION:.1%} tolerance)"
                    )
                if consistency_errors:
                    raise BlueprintValidationError(consistency_errors)

                if out_path is not None:
                    return _atomic_write_and_verify_blueprint(bp, out_path, clean_existing)
                return bp
            except BlueprintValidationError as exc:
                last_errors = list(exc.errors)
                attempt_history.append(f"attempt {attempt}: " + "; ".join(last_errors))
                logger.warning(
                    "StoryArchitect attempt %d/%d failed validation (%d errors): %s",
                    attempt,
                    max_retries,
                    len(last_errors),
                    "; ".join(last_errors),
                )
            except ProjectValidationError as exc:
                last_errors = [str(exc)]
                attempt_history.append(f"attempt {attempt}: {exc}")
                logger.warning(
                    "StoryArchitect attempt %d/%d failed payload parsing: %s",
                    attempt,
                    max_retries,
                    exc,
                )

        summary = " | ".join(attempt_history)
        raise StoryArchitectError(
            f"StoryArchitect failed to produce a valid BookBlueprint after {max_retries} attempt(s): {summary}",
            errors=last_errors,
        )


__all__ = [
    "ARCHITECT_MAX_TOKENS",
    "MIN_TARGET_TOTAL_CHARS",
    "StoryArchitect",
    "StoryArchitectError",
]
