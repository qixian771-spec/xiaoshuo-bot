from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid
import yaml

from .blueprint import (
    BlueprintValidationError,
    BookBlueprint,
    ScaleRule,
    VolumeSpec,
    load_blueprint,
)
from .checker import HOOK_DEFLATING_ENDING_PATTERNS
from .llm.base import LLMConfigurationError, LLMProvider, LLMRequest
from .llm.router import RoleModelRouter
from .loader import load_project
from .models import (
    CHARACTER_STATUSES,
    EMOTION_POLARITIES,
    EXPECTATION_ACTIONS,
    AssetState,
    CharacterWorldState,
    NovelProject,
    ProjectValidationError,
    WorldStateSnapshot,
)
from .planner import (
    PlannerError,
    PlanningValidationError,
    STAGE_SCENES_MAX_TOKENS,
    _CHAPTER_PREFIX_RE,
    _collect_open_expectations,
    _dump_yaml,
    _format_balances_summary,
    _format_due_expectations_block,
    _invoke_planner,
    _is_strict_int,
    _is_strict_number,
    _nonempty_str,
    _parse_json_payload,
    _simulate_ledger_balances,
    _trailing_location_run,
    _trailing_suppress_count,
    find_ledger_overdrafts,
    find_stale_location_runs,
)


MIN_EVENTS_PER_CHAPTER = 3
MAX_CONSECUTIVE_SAME_LOCATION = 2
MAX_CONSECUTIVE_SUPPRESS = 2
MIN_CLIFFHANGER_CHARS = 4

_PLACEHOLDER_HOOKS = frozenset(
    {"无", "暂无", "待定", "略", "tbd", "n/a", "na", "none", "null", "...", "……", "-", "--"}
)

_EXTRA_DEFLATING_HOOK_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (
        r"回家休息|回房休息|安心休息|平淡收尾|相安无事|风平浪静",
        "章末严禁‘回家休息/相安无事’等泄气闭环表述，必须是具体未解冲突或突发事件的事件型钩子",
    ),
)

_COMPILED_DEFLATING_HOOK_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat), hint)
    for pat, hint in (*HOOK_DEFLATING_ENDING_PATTERNS, *_EXTRA_DEFLATING_HOOK_PATTERNS)
)

_FLASHBACK_RE = re.compile(
    r"(?:\[|【|\(|（)?\b(?:flashback|memory)\b(?:\]|】|\)|）)?|"
    r"(?:\[|【|\(|（)?(?:回忆|闪回)(?:片段)?(?:\]|】|\)|）)?",
    re.IGNORECASE,
)

_BLUEPRINT_SCENES_JSON_EXAMPLE: Dict[str, Any] = {
    "scenes": [
        {
            "chapter": 4,
            "scene_id": "ch0004_night_market_clue",
            "internal_title": "<内部章纲标题>",
            "public_title": "<对外章节标题，严禁带第X章/回/节前缀>",
            "beat_type": "<爽点节拍，如：压抑蓄力/反手打脸/危机升级>",
            "emotion_polarity": "mixed",
            "pov_character": "<项目characters.yaml中已有的视点角色id，必须在active_characters内>",
            "active_characters": ["<已有角色id1>", "<已有角色id2>"],
            "active_assets": ["<已有器物id，无则写[]>"],
            "style_profile_ids": ["<已有文风id>"],
            "location_time": "<本章具体时间与物理地点>",
            "location_id": "<场景英文坐标id，连续同location_id严禁超过2章>",
            "min_chars": 2100,
            "max_chars": 2500,
            "scene_goal": "<三问1：本章主角具体要达成什么目标>",
            "scene_obstacle": "<三问2：谁在阻挠或核心物理阻力是什么>",
            "scene_outcome": "<三问3：本章结束时的具体得失与局势变化>",
            "events": [
                "<具体物理事件1：开场冲突与目标切入>",
                "<具体物理事件2：阻力交锋与金手指/物证运作>",
                "<具体物理事件3：阶段结果落定与新变数浮现>",
            ],
            "stop_condition": "<本章写到哪一个具体动作或画面立即停笔>",
            "cliffhanger_hook": "<事件型悬念钩子：具体突发动作/证据/新威胁，严禁一夜无话/沉沉睡去/圆满解决/皆大欢喜/回家休息>",
            "segment_comment_cue": "<可供读者吐槽或站队的爽点/反转点>",
            "volume": 1,
            "calendar_days_elapsed": 1,
            "post_scene_asset_mutations": [],
            "ledger_mutations": [],
            "character_mutations": [
                {
                    "character_id": "<已有角色id；无变化时整个数组写[]>",
                    "role": "<可选：换卷或身份跃迁时更新后的角色身份，同步推进current_role>",
                    "note": "<变化原因一句话>",
                }
            ],
            "expectation_directives": [],
        }
    ]
}


@dataclass
class BlueprintExpansionResult:
    """Result returned after expanding blueprint chapters into a validated outline."""

    volume_index: int
    start_chapter: int
    end_chapter: int
    added_chapters: List[int]
    outline_path: Path
    attempts_used: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "volume_index": self.volume_index,
            "start_chapter": self.start_chapter,
            "end_chapter": self.end_chapter,
            "added_chapters": list(self.added_chapters),
            "outline_path": str(self.outline_path),
            "attempts_used": self.attempts_used,
        }


def _is_strict_finite_number(value: Any) -> bool:
    return _is_strict_number(value) and math.isfinite(float(value))


def _fmt_num(value: float | int) -> str:
    fval = float(value)
    if fval.is_integer():
        return str(int(fval))
    return f"{fval:.6f}".rstrip("0").rstrip(".")


def _fmt_signed(value: float | int) -> str:
    fval = float(value)
    body = _fmt_num(abs(fval))
    return f"+{body}" if fval >= 0 else f"-{body}"


def _scene_field(scene: Any, key: str, default: Any = None) -> Any:
    if isinstance(scene, Mapping):
        return scene.get(key, default)
    return getattr(scene, key, default)


def _scene_chapter(scene: Any) -> int:
    val = _scene_field(scene, "chapter", 0)
    return int(val) if _is_strict_int(val) else 0


def _scene_id_val(scene: Any) -> str:
    return str(_scene_field(scene, "scene_id", "") or "").strip()


def _scene_has_flashback_marker(scene: Any) -> bool:
    for field_name in ("beat_type", "location_time", "internal_title", "scene_id"):
        val = str(_scene_field(scene, field_name, "") or "")
        if val and _FLASHBACK_RE.search(val):
            return True
    return False


def resolve_target_volume(
    blueprint: BookBlueprint,
    start_chapter: int,
    end_chapter: int,
) -> VolumeSpec:
    """Resolve the single VolumeSpec covering [start_chapter..end_chapter]; reject cross-volume spans."""
    if not _is_strict_int(start_chapter) or start_chapter < 1:
        raise ValueError(f"start_chapter must be an integer >= 1, got {start_chapter!r}")
    if not _is_strict_int(end_chapter) or end_chapter < start_chapter:
        raise ValueError(
            f"end_chapter ({end_chapter!r}) must be an integer >= start_chapter ({start_chapter!r})"
        )
    if not blueprint.volumes:
        raise ValueError("BookBlueprint contains no volumes")

    start_vol: Optional[VolumeSpec] = None
    end_vol: Optional[VolumeSpec] = None
    for vol in blueprint.volumes:
        if vol.chapter_start <= start_chapter <= vol.chapter_end:
            start_vol = vol
        if vol.chapter_start <= end_chapter <= vol.chapter_end:
            end_vol = vol

    vol_ranges = ", ".join(
        f"Vol {v.index}: {v.chapter_start}..{v.chapter_end}" for v in blueprint.volumes
    )
    if start_vol is None or end_vol is None:
        raise ValueError(
            f"Chapter range {start_chapter}..{end_chapter} falls outside blueprint volume spans ({vol_ranges})"
        )
    if start_vol.index != end_vol.index:
        raise ValueError(
            f"Chapter range {start_chapter}..{end_chapter} crosses volume boundary "
            f"(Vol {start_vol.index} [{start_vol.chapter_start}..{start_vol.chapter_end}] -> "
            f"Vol {end_vol.index} [{end_vol.chapter_start}..{end_vol.chapter_end}]); "
            f"please expand one volume at a time"
        )
    return start_vol


def _collect_prior_exited_characters(
    blueprint: BookBlueprint,
    volume: VolumeSpec,
) -> Dict[str, str]:
    exited: Dict[str, str] = {}
    for prev_vol in blueprint.volumes:
        if prev_vol.index < volume.index:
            for ex in prev_vol.exits:
                cid = ex.id.strip()
                if cid:
                    exited[cid] = f"第{prev_vol.index}卷已退场（{ex.how}）"
    for member in blueprint.cast:
        cid = member.id.strip()
        if cid and member.exit_volume is not None and member.exit_volume < volume.index:
            exited.setdefault(cid, f"第{member.exit_volume}卷已退场")
    return exited


def _simulate_character_states(
    project: NovelProject,
    scenes: Sequence[Any],
    *,
    snapshot: Optional[WorldStateSnapshot] = None,
) -> Dict[str, CharacterWorldState]:
    """Compute latest CharacterWorldState (including status and current_role) after scenes."""
    states: Dict[str, CharacterWorldState] = {}
    if snapshot is not None and snapshot.character_states:
        for cid, cs in snapshot.character_states.items():
            states[str(cid)] = copy.deepcopy(cs)
            if states[str(cid)].current_role is None and cid in project.characters:
                states[str(cid)].current_role = project.characters[cid].role
        snap_ch = int(snapshot.current_chapter)
    else:
        snap_ch = 0

    for cid, char in project.characters.items():
        if cid not in states:
            states[cid] = CharacterWorldState(
                character_id=char.id,
                name=char.name,
                status=char.initial_status,
                faction=char.faction,
                relations=dict(char.relations),
                forbidden_titles=list(char.forbidden_titles),
                injuries={},
                deceased_chapter=0 if char.initial_status == "deceased" else None,
                current_role=char.role,
            )

    ordered = sorted(scenes, key=_scene_chapter)
    for sc in ordered:
        ch = _scene_chapter(sc)
        if ch <= snap_ch:
            continue
        raw_cms = _scene_field(sc, "character_mutations", []) or []
        for cm in raw_cms:
            if isinstance(cm, Mapping):
                cid = _nonempty_str(cm.get("character_id"))
                status = cm.get("status")
                faction = cm.get("faction")
                rel_target = cm.get("relation_target_id")
                rel_label = cm.get("relation_label")
                role = cm.get("role")
            else:
                cid = _nonempty_str(getattr(cm, "character_id", ""))
                status = getattr(cm, "status", None)
                faction = getattr(cm, "faction", None)
                rel_target = getattr(cm, "relation_target_id", None)
                rel_label = getattr(cm, "relation_label", None)
                role = getattr(cm, "role", None)
            if not cid or cid not in states:
                continue
            cs = states[cid]
            if isinstance(status, str) and status in CHARACTER_STATUSES:
                cs.status = status
                if status == "deceased" and cs.deceased_chapter is None:
                    cs.deceased_chapter = ch
            if isinstance(faction, str):
                cs.faction = faction
            if isinstance(rel_target, str) and rel_target and rel_label is not None:
                cs.relations[rel_target] = str(rel_label)
            if isinstance(role, str) and role.strip():
                cs.current_role = role.strip()

    return states


def _simulate_asset_states(
    project: NovelProject,
    scenes: Sequence[Any],
    *,
    snapshot: Optional[WorldStateSnapshot] = None,
) -> Dict[str, AssetState]:
    if snapshot is not None and snapshot.assets:
        assets = {str(aid): copy.deepcopy(a) for aid, a in snapshot.assets.items()}
        snap_ch = int(snapshot.current_chapter)
    else:
        assets = copy.deepcopy(project.assets)
        snap_ch = 0

    for aid, asset in project.assets.items():
        if aid not in assets:
            assets[aid] = copy.deepcopy(asset)

    ordered = sorted(scenes, key=_scene_chapter)
    for sc in ordered:
        ch = _scene_chapter(sc)
        if ch <= snap_ch:
            continue
        raw_muts = _scene_field(sc, "post_scene_asset_mutations", []) or []
        for mut in raw_muts:
            if isinstance(mut, Mapping):
                aid = _nonempty_str(mut.get("asset_id"))
                loc = mut.get("location")
                holder = mut.get("holder")
                cond = mut.get("condition")
            else:
                aid = _nonempty_str(getattr(mut, "asset_id", ""))
                loc = getattr(mut, "location", None)
                holder = getattr(mut, "holder", None)
                cond = getattr(mut, "condition", None)
            if not aid or aid not in assets:
                continue
            if loc is not None:
                assets[aid].location = str(loc)
            if holder is not None:
                assets[aid].holder = str(holder)
            if cond is not None:
                assets[aid].condition = str(cond)
            assets[aid].last_updated_chapter = ch

    return assets


def _validate_cliffhanger_hook(raw_hook: Any, ch_label: str, errors: List[str]) -> None:
    hook = _nonempty_str(raw_hook)
    if not hook:
        errors.append(f"{ch_label}: missing non-empty event-driven 'cliffhanger_hook'")
        return
    if hook.lower() in _PLACEHOLDER_HOOKS or len(hook) < MIN_CLIFFHANGER_CHARS:
        errors.append(
            f"{ch_label}: cliffhanger_hook {hook!r} is too short or a placeholder; "
            f"must be a concrete event-driven cliffhanger (>= {MIN_CLIFFHANGER_CHARS} chars)"
        )
        return
    for pattern, hint in _COMPILED_DEFLATING_HOOK_PATTERNS:
        match = pattern.search(hook)
        if match:
            errors.append(
                f"{ch_label}: cliffhanger_hook {hook!r} hits deflating ending pattern "
                f"{match.group(0)!r} ({hint})"
            )


def collect_blueprint_scene_errors(
    scenes: Any,
    *,
    blueprint: BookBlueprint,
    project: NovelProject,
    volume: VolumeSpec,
    start_chapter: int,
    end_chapter: int,
    existing_scenes: Optional[Sequence[Any]] = None,
    snapshot: Optional[WorldStateSnapshot] = None,
    max_consecutive_same_location: int = MAX_CONSECUTIVE_SAME_LOCATION,
) -> List[str]:
    """Collect ALL deterministic validation errors for blueprint-expanded scenes without stopping early."""
    errors: List[str] = []

    if not _is_strict_int(start_chapter) or start_chapter < 1:
        errors.append(f"start_chapter must be an integer >= 1, got {start_chapter!r}")
    if not _is_strict_int(end_chapter) or (
        _is_strict_int(start_chapter) and end_chapter < start_chapter
    ):
        errors.append(
            f"end_chapter ({end_chapter!r}) must be an integer >= start_chapter ({start_chapter!r})"
        )
    if (
        _is_strict_int(start_chapter)
        and _is_strict_int(end_chapter)
        and (start_chapter < volume.chapter_start or end_chapter > volume.chapter_end)
    ):
        errors.append(
            f"Target chapter range {start_chapter}..{end_chapter} exceeds Volume {volume.index} "
            f"bounds [{volume.chapter_start}..{volume.chapter_end}]"
        )

    bp_min: Optional[int] = None
    bp_max: Optional[int] = None
    if (
        not isinstance(blueprint.chapter_chars_range, (list, tuple))
        or len(blueprint.chapter_chars_range) != 2
        or not _is_strict_int(blueprint.chapter_chars_range[0])
        or not _is_strict_int(blueprint.chapter_chars_range[1])
        or blueprint.chapter_chars_range[0] <= 0
        or blueprint.chapter_chars_range[0] >= blueprint.chapter_chars_range[1]
    ):
        errors.append(
            f"blueprint.chapter_chars_range must be [min, max] positive ints with min < max, "
            f"got {blueprint.chapter_chars_range!r}"
        )
    else:
        bp_min = int(blueprint.chapter_chars_range[0])
        bp_max = int(blueprint.chapter_chars_range[1])

    if not isinstance(scenes, list) or not scenes:
        errors.append(
            f"scenes must be a non-empty list covering chapters {start_chapter}..{end_chapter}"
        )
        return errors

    valid_char_ids = set(project.characters.keys())
    valid_asset_ids = set(project.assets.keys())
    valid_style_ids = set(project.style_profiles.keys())

    if existing_scenes is not None:
        existing_all = sorted(existing_scenes, key=_scene_chapter)
    else:
        existing_all = sorted(
            (
                s
                for s in project.scenes
                if not (
                    _is_strict_int(start_chapter)
                    and _is_strict_int(end_chapter)
                    and start_chapter <= s.chapter <= end_chapter
                )
            ),
            key=_scene_chapter,
        )

    existing_before = [s for s in existing_all if _scene_chapter(s) < start_chapter]
    existing_after = [s for s in existing_all if _scene_chapter(s) > end_chapter]
    existing_scene_ids = {_scene_id_val(s) for s in existing_all if _scene_id_val(s)}

    prior_exited = _collect_prior_exited_characters(blueprint, volume)
    bp_foreshadow_ids = {f.id for f in blueprint.foreshadow_plan}
    known_foreshadow_ids = bp_foreshadow_ids | set(project.foreshadows.keys())

    seen_chapter_set: set[int] = set()
    seen_scene_ids: set[str] = set()
    valid_scenes_by_ch: Dict[int, Mapping[str, Any]] = {}

    for idx, item in enumerate(scenes):
        prefix = f"scenes[{idx}]"
        if not isinstance(item, Mapping):
            errors.append(f"{prefix} must be an object, got {type(item).__name__}")
            continue

        raw_ch = item.get("chapter")
        if not _is_strict_int(raw_ch):
            errors.append(f"{prefix}: missing integer 'chapter' (got {raw_ch!r})")
            ch_label = prefix
        else:
            ch = int(raw_ch)
            ch_label = f"Chapter {ch}"
            if not (volume.chapter_start <= ch <= volume.chapter_end):
                errors.append(
                    f"{ch_label}: chapter {ch} is outside Volume {volume.index} bounds "
                    f"[{volume.chapter_start}..{volume.chapter_end}]"
                )
            elif not (start_chapter <= ch <= end_chapter):
                errors.append(
                    f"{ch_label}: chapter {ch} is outside requested expansion range "
                    f"{start_chapter}..{end_chapter}"
                )
            elif ch in seen_chapter_set:
                errors.append(f"{ch_label}: duplicate chapter number {ch} in batch")
            else:
                seen_chapter_set.add(ch)
                valid_scenes_by_ch[ch] = item

        if "volume" in item and item["volume"] is not None:
            if not _is_strict_int(item["volume"]) or int(item["volume"]) != volume.index:
                errors.append(
                    f"{ch_label}: volume must be {volume.index}, got {item['volume']!r}"
                )

        sid = _nonempty_str(item.get("scene_id"))
        if not sid:
            errors.append(f"{ch_label}: missing non-empty string 'scene_id'")
        elif sid in seen_scene_ids:
            errors.append(f"{ch_label}: duplicate scene_id {sid!r} in batch")
        elif sid in existing_scene_ids:
            errors.append(f"{ch_label}: scene_id {sid!r} collides with existing outline scene_id")
        else:
            seen_scene_ids.add(sid)

        for req_str in ("internal_title", "location_time", "location_id", "stop_condition"):
            if not _nonempty_str(item.get(req_str)):
                errors.append(f"{ch_label}: missing non-empty string '{req_str}'")

        pub_title = _nonempty_str(item.get("public_title"))
        if not pub_title:
            errors.append(f"{ch_label}: missing non-empty string 'public_title'")
        elif _CHAPTER_PREFIX_RE.match(pub_title):
            errors.append(
                f"{ch_label}: public_title {pub_title!r} must not start with '第X章/回/节' prefix"
            )

        # Requirement 2: Three questions (scene_goal, scene_obstacle, scene_outcome)
        for q_key, q_desc in (
            ("scene_goal", "主角要什么"),
            ("scene_obstacle", "谁挡着/阻力是什么"),
            ("scene_outcome", "本章结果得失"),
        ):
            if not _nonempty_str(item.get(q_key)):
                errors.append(
                    f"{ch_label}: missing non-empty string '{q_key}' (三问必填：{q_desc})"
                )

        # Requirement 3: Non-empty event-driven cliffhanger_hook (no deflating ending patterns)
        _validate_cliffhanger_hook(item.get("cliffhanger_hook"), ch_label, errors)

        # Requirement 4a: min_chars / max_chars aligned with blueprint.chapter_chars_range
        raw_min = item.get("min_chars")
        raw_max = item.get("max_chars")
        min_ok = _is_strict_int(raw_min) and int(raw_min) > 0
        max_ok = _is_strict_int(raw_max) and int(raw_max) > 0
        if not min_ok:
            errors.append(f"{ch_label}: min_chars must be a positive integer, got {raw_min!r}")
        if not max_ok:
            errors.append(f"{ch_label}: max_chars must be a positive integer, got {raw_max!r}")
        if min_ok and max_ok:
            min_v, max_v = int(raw_min), int(raw_max)
            if min_v > max_v:
                errors.append(f"{ch_label}: min_chars ({min_v}) > max_chars ({max_v})")
            if bp_min is not None and bp_max is not None:
                if min_v < bp_min or max_v > bp_max:
                    errors.append(
                        f"{ch_label}: char range [{min_v}, {max_v}] is outside "
                        f"blueprint.chapter_chars_range [{bp_min}, {bp_max}]"
                    )

        # Requirement 4b: Entity references in project
        pov_char = _nonempty_str(item.get("pov_character"))
        if not pov_char:
            errors.append(f"{ch_label}: missing non-empty string 'pov_character'")
        elif pov_char not in valid_char_ids:
            errors.append(
                f"{ch_label}: pov_character {pov_char!r} not in project.characters "
                f"{sorted(valid_char_ids)}"
            )

        raw_active_chars = item.get("active_characters")
        if not isinstance(raw_active_chars, list) or not raw_active_chars:
            errors.append(f"{ch_label}: 'active_characters' must be a non-empty list of character ids")
        else:
            for cid in raw_active_chars:
                if not isinstance(cid, str) or cid not in valid_char_ids:
                    errors.append(
                        f"{ch_label}: unknown character id {cid!r} in active_characters "
                        f"(allowed: {sorted(valid_char_ids)})"
                    )
            if pov_char and pov_char in valid_char_ids and pov_char not in raw_active_chars:
                errors.append(
                    f"{ch_label}: pov_character {pov_char!r} must be listed in active_characters"
                )

        if "active_assets" not in item or not isinstance(item.get("active_assets"), list):
            errors.append(f"{ch_label}: 'active_assets' must be a list of asset ids")
        else:
            for aid in item["active_assets"]:
                if not isinstance(aid, str) or aid not in valid_asset_ids:
                    errors.append(
                        f"{ch_label}: unknown asset id {aid!r} in active_assets "
                        f"(allowed: {sorted(valid_asset_ids)})"
                    )

        raw_styles = item.get("style_profile_ids")
        if not isinstance(raw_styles, list):
            errors.append(f"{ch_label}: 'style_profile_ids' must be a list of style profile ids")
        else:
            if valid_style_ids and not raw_styles:
                errors.append(
                    f"{ch_label}: 'style_profile_ids' must not be empty "
                    f"(allowed: {sorted(valid_style_ids)})"
                )
            for sp_id in raw_styles:
                if not isinstance(sp_id, str) or sp_id not in valid_style_ids:
                    errors.append(
                        f"{ch_label}: unknown style_profile id {sp_id!r} "
                        f"(allowed: {sorted(valid_style_ids)})"
                    )

        # Requirement 8 (part 2): >= 3 concrete physical events
        raw_events = item.get("events")
        if not isinstance(raw_events, list):
            errors.append(f"{ch_label}: 'events' must be a list of >= {MIN_EVENTS_PER_CHAPTER} physical events")
        else:
            valid_events: List[str] = []
            for e_idx, ev in enumerate(raw_events):
                ev_str = _nonempty_str(ev)
                if not ev_str:
                    errors.append(f"{ch_label}: events[{e_idx}] must be a non-empty string")
                else:
                    valid_events.append(ev_str)
            if len(valid_events) < MIN_EVENTS_PER_CHAPTER:
                errors.append(
                    f"{ch_label}: 'events' must contain at least {MIN_EVENTS_PER_CHAPTER} "
                    f"concrete physical events, got {len(valid_events)}"
                )

        if "emotion_polarity" in item and item["emotion_polarity"] is not None:
            ep = item["emotion_polarity"]
            if not isinstance(ep, str) or ep not in EMOTION_POLARITIES:
                errors.append(
                    f"{ch_label}: unknown emotion_polarity {ep!r}; expected one of {EMOTION_POLARITIES}"
                )

        if "calendar_days_elapsed" in item and item["calendar_days_elapsed"] is not None:
            cde = item["calendar_days_elapsed"]
            if not _is_strict_int(cde) or int(cde) < 0:
                errors.append(
                    f"{ch_label}: calendar_days_elapsed must be an integer >= 0, got {cde!r}"
                )

        # Nested mutation structure & referential integrity
        raw_asset_muts = item.get("post_scene_asset_mutations", [])
        if not isinstance(raw_asset_muts, list):
            errors.append(f"{ch_label}: post_scene_asset_mutations must be a list")
        else:
            for m_idx, mut in enumerate(raw_asset_muts):
                if not isinstance(mut, Mapping):
                    errors.append(f"{ch_label}: post_scene_asset_mutations[{m_idx}] must be an object")
                    continue
                aid = mut.get("asset_id")
                if not isinstance(aid, str) or aid not in valid_asset_ids:
                    errors.append(
                        f"{ch_label}: post_scene_asset_mutations[{m_idx}] unknown asset_id {aid!r}"
                    )

        raw_char_muts = item.get("character_mutations", [])
        if not isinstance(raw_char_muts, list):
            errors.append(f"{ch_label}: character_mutations must be a list")
        else:
            for m_idx, cm in enumerate(raw_char_muts):
                if not isinstance(cm, Mapping):
                    errors.append(f"{ch_label}: character_mutations[{m_idx}] must be an object")
                    continue
                cid = cm.get("character_id")
                if not isinstance(cid, str) or cid not in valid_char_ids:
                    errors.append(
                        f"{ch_label}: character_mutations[{m_idx}] unknown character_id {cid!r}"
                    )
                if cm.get("status") is not None and cm.get("status") not in CHARACTER_STATUSES:
                    errors.append(
                        f"{ch_label}: character_mutations[{m_idx}] invalid status {cm.get('status')!r}"
                    )
                rel_target = cm.get("relation_target_id")
                if rel_target and (not isinstance(rel_target, str) or rel_target not in valid_char_ids):
                    errors.append(
                        f"{ch_label}: character_mutations[{m_idx}] unknown relation_target_id {rel_target!r}"
                    )
                if "role" in cm and cm["role"] is not None:
                    if not _nonempty_str(cm["role"]):
                        errors.append(
                            f"{ch_label}: character_mutations[{m_idx}] role must be a non-empty string"
                        )
                if "injury_recovery_chapters" in cm and cm["injury_recovery_chapters"] is not None:
                    irc = cm["injury_recovery_chapters"]
                    if not _is_strict_int(irc) or int(irc) < 0:
                        errors.append(
                            f"{ch_label}: character_mutations[{m_idx}] injury_recovery_chapters "
                            f"must be an integer >= 0, got {irc!r}"
                        )

        raw_ledger_muts = item.get("ledger_mutations", [])
        if not isinstance(raw_ledger_muts, list):
            errors.append(f"{ch_label}: ledger_mutations must be a list")
        else:
            for m_idx, lm in enumerate(raw_ledger_muts):
                if not isinstance(lm, Mapping):
                    errors.append(f"{ch_label}: ledger_mutations[{m_idx}] must be an object")
                    continue
                if not _nonempty_str(lm.get("ledger_key")):
                    errors.append(f"{ch_label}: ledger_mutations[{m_idx}] missing ledger_key")
                if not _is_strict_finite_number(lm.get("delta")):
                    errors.append(
                        f"{ch_label}: ledger_mutations[{m_idx}] delta must be a finite number"
                    )
                if "breakthrough" in lm and not isinstance(lm["breakthrough"], bool):
                    errors.append(
                        f"{ch_label}: ledger_mutations[{m_idx}] breakthrough must be a boolean"
                    )

        raw_exp_dirs = item.get("expectation_directives", [])
        if not isinstance(raw_exp_dirs, list):
            errors.append(f"{ch_label}: expectation_directives must be a list")
        else:
            for m_idx, ed in enumerate(raw_exp_dirs):
                if not isinstance(ed, Mapping):
                    errors.append(f"{ch_label}: expectation_directives[{m_idx}] must be an object")
                    continue
                if not _nonempty_str(ed.get("id")) or not _nonempty_str(ed.get("title")):
                    errors.append(f"{ch_label}: expectation_directives[{m_idx}] missing id or title")
                if not _is_strict_int(ed.get("due_chapter")):
                    errors.append(f"{ch_label}: expectation_directives[{m_idx}] due_chapter must be int")
                act = ed.get("action", "open")
                if act not in EXPECTATION_ACTIONS:
                    errors.append(
                        f"{ch_label}: expectation_directives[{m_idx}] invalid action {act!r}"
                    )

    # Requirement 1: Contiguous coverage check
    if _is_strict_int(start_chapter) and _is_strict_int(end_chapter) and end_chapter >= start_chapter:
        expected_chapters = list(range(start_chapter, end_chapter + 1))
        missing = [ch for ch in expected_chapters if ch not in seen_chapter_set]
        if missing:
            errors.append(
                f"scenes batch {start_chapter}..{end_chapter}: missing chapters {missing}"
            )

    ordered_batch = [valid_scenes_by_ch[ch] for ch in sorted(valid_scenes_by_ch)]
    full_timeline: List[Any] = list(existing_before) + list(ordered_batch) + list(existing_after)

    # Consecutive suppress polarity check aligned with loader.py validate_project()
    if project.meta.contract_track != "legacy":
        suppress_run: List[int] = []
        for sc in full_timeline:
            ch = _scene_chapter(sc)
            ep = str(_scene_field(sc, "emotion_polarity", "neutral") or "neutral")
            if ep == "suppress":
                suppress_run.append(ch)
                if len(suppress_run) > MAX_CONSECUTIVE_SUPPRESS and any(
                    start_chapter <= rch <= end_chapter for rch in suppress_run
                ):
                    errors.append(
                        f"Chapter {ch}: consecutive emotion_polarity='suppress' exceeds max "
                        f"{MAX_CONSECUTIVE_SUPPRESS} (run={suppress_run})"
                    )
            else:
                suppress_run = []

    # Requirement 6: Stale location guard across prior-chapter boundary via find_stale_location_runs
    loc_timeline: List[Any] = list(existing_before)
    prior_loc_id, prior_loc_count = _trailing_location_run(
        [{"location_id": _scene_field(s, "location_id", "")} for s in existing_before]
    )
    if (
        not prior_loc_id
        and snapshot is not None
        and snapshot.recent_locations
    ):
        snap_tail = [loc.strip() for loc in snapshot.recent_locations if str(loc).strip()]
        for offset, loc in enumerate(snap_tail):
            loc_timeline.append({"chapter": -(len(snap_tail) - offset), "location_id": loc})
    loc_timeline.extend(ordered_batch)
    loc_timeline.extend(existing_after)

    stale_runs = find_stale_location_runs(
        loc_timeline,
        max_consecutive=max_consecutive_same_location,
    )
    for run in stale_runs:
        run_chapters = [int(c) for c in run["run_chapters"]]
        if any(start_chapter <= rch <= end_chapter for rch in run_chapters):
            display_chapters = [c for c in run_chapters if c > 0]
            errors.append(
                f"Chapter {run['chapter']}: consecutive location_id={run['location_id']!r} "
                f"exceeds max {max_consecutive_same_location} (STATE_STALE_LOCATION; "
                f"run_chapters={display_chapters}, count={run['consecutive_count']})"
            )

    # Requirement 5a: Overdraft guard via find_ledger_overdrafts
    overdrafts = find_ledger_overdrafts(
        full_timeline,
        project.initial_balances,
        snapshot=snapshot,
    )
    for od in overdrafts:
        if int(od["chapter"]) >= start_chapter:
            errors.append(
                f"Chapter {od['chapter']}: ledger_mutation {od['ledger_key']!r} "
                f"delta={_fmt_signed(od['delta'])} drives balance from {_fmt_num(od['before'])} to "
                f"{_fmt_num(od['after'])} < 0 (STATE_OVERDRAFT)"
            )

    # Requirement 5b: bounded_realistic hard_max cumulative ceiling guard
    bounded_rules: Dict[str, ScaleRule] = {
        rule.ledger_key: rule
        for rule in blueprint.scale_rules
        if rule.policy == "bounded_realistic"
    }
    if snapshot is not None:
        running_balances: Dict[str, float] = {
            str(k): float(v)
            for k, v in (snapshot.numeric_balances or {}).items()
            if _is_strict_finite_number(v)
        }
        snap_ch = int(snapshot.current_chapter)
    else:
        running_balances = {
            str(k): float(v)
            for k, v in (project.initial_balances or {}).items()
            if _is_strict_finite_number(v)
        }
        snap_ch = 0

    for sc in full_timeline:
        ch = _scene_chapter(sc)
        if ch <= snap_ch:
            continue
        raw_lms = _scene_field(sc, "ledger_mutations", []) or []
        if not isinstance(raw_lms, list):
            continue
        for lm in raw_lms:
            if isinstance(lm, Mapping):
                lkey = _nonempty_str(lm.get("ledger_key"))
                raw_delta = lm.get("delta")
            else:
                lkey = _nonempty_str(getattr(lm, "ledger_key", ""))
                raw_delta = getattr(lm, "delta", None)
            if not lkey or not _is_strict_finite_number(raw_delta):
                continue
            delta = float(raw_delta)
            before = float(running_balances.get(lkey, 0.0))
            after = before + delta
            running_balances[lkey] = after
            rule = bounded_rules.get(lkey)
            if rule is not None and ch >= start_chapter:
                if rule.hard_max is None or not _is_strict_finite_number(rule.hard_max) or float(rule.hard_max) <= 0:
                    errors.append(
                        f"Chapter {ch}: bounded_realistic scale rule for {lkey!r} is missing valid hard_max > 0"
                    )
                elif after > float(rule.hard_max):
                    errors.append(
                        f"Chapter {ch}: bounded_realistic ledger {lkey!r} cumulative balance "
                        f"{_fmt_num(after)} (before={_fmt_num(before)}, delta={_fmt_signed(delta)}) exceeds blueprint "
                        f"hard_max {_fmt_num(rule.hard_max)}{rule.unit}"
                    )

    # Requirement 8 (part 1): Forbid exited or deceased characters in active_characters/pov_character
    char_states = _simulate_character_states(project, existing_before, snapshot=snapshot)
    for sc in ordered_batch:
        ch = _scene_chapter(sc)
        is_flashback = _scene_has_flashback_marker(sc)
        on_stage: List[str] = []
        pov_id = _nonempty_str(sc.get("pov_character"))
        if pov_id:
            on_stage.append(pov_id)
        for cid in sc.get("active_characters") or []:
            if isinstance(cid, str) and cid not in on_stage:
                on_stage.append(cid)

        if not is_flashback:
            for cid in on_stage:
                if cid in prior_exited:
                    errors.append(
                        f"Chapter {ch}: character {cid!r} has already exited ({prior_exited[cid]}) "
                        f"and cannot appear in active_characters/pov_character for real-time action"
                    )
                cs = char_states.get(cid)
                if cs is not None and cs.status == "deceased":
                    errors.append(
                        f"Chapter {ch}: character {cid!r} ({cs.name}) is deceased "
                        f"(Ch.{cs.deceased_chapter}) and cannot appear in active_characters/pov_character "
                        f"for real-time action"
                    )

        # Advance simulated character states (status & role) for subsequent chapters in batch
        raw_cms = sc.get("character_mutations") or []
        if isinstance(raw_cms, list):
            for m_idx, cm in enumerate(raw_cms):
                if not isinstance(cm, Mapping):
                    continue
                cid = _nonempty_str(cm.get("character_id"))
                if not cid or cid not in char_states:
                    continue
                cs = char_states[cid]
                new_status = cm.get("status")
                if cs.status == "deceased" and new_status is not None and new_status != "deceased":
                    errors.append(
                        f"Chapter {ch}: character_mutations[{m_idx}] cannot revive deceased "
                        f"character {cid!r} ({cs.name}) to status {new_status!r}"
                    )
                elif isinstance(new_status, str) and new_status in CHARACTER_STATUSES:
                    cs.status = new_status
                    if new_status == "deceased" and cs.deceased_chapter is None:
                        cs.deceased_chapter = ch
                if isinstance(cm.get("faction"), str):
                    cs.faction = str(cm["faction"])
                new_role = _nonempty_str(cm.get("role"))
                if new_role:
                    cs.current_role = new_role

    # Expectation open <-> fulfill deadline consistency check
    open_exps = _collect_open_expectations(existing_before, snapshot=snapshot)
    reported_overdue: set[str] = set()
    for sc in ordered_batch:
        ch = _scene_chapter(sc)
        raw_eds = sc.get("expectation_directives") or []
        if isinstance(raw_eds, list):
            for ed in raw_eds:
                if not isinstance(ed, Mapping):
                    continue
                eid = _nonempty_str(ed.get("id"))
                act = str(ed.get("action") or "open").strip()
                due_raw = ed.get("due_chapter")
                if not eid or not _is_strict_int(due_raw):
                    continue
                due_ch = int(due_raw)
                if act == "open":
                    if due_ch < ch:
                        errors.append(
                            f"Chapter {ch}: expectation {eid!r} action='open' has due_chapter={due_ch} < {ch}"
                        )
                    else:
                        open_exps[eid] = {
                            "id": eid,
                            "title": _nonempty_str(ed.get("title")),
                            "opened_chapter": ch,
                            "due_chapter": due_ch,
                        }
                elif act == "fulfill":
                    open_exps.pop(eid, None)
                    reported_overdue.discard(eid)
        for eid, info in list(open_exps.items()):
            due_ch = int(info.get("due_chapter") or 0)
            if 0 < due_ch <= ch and eid not in reported_overdue:
                reported_overdue.add(eid)
                errors.append(
                    f"Chapter {ch}: expectation {eid!r} ({info.get('title')!r}, due Ch.{due_ch}) "
                    f"lacks action='fulfill' on or before Ch.{due_ch} (STATE_EXPECTATION_OVERDUE)"
                )

    # Requirement 7: Volume transition synchronization when opening a volume > 1
    if start_chapter == volume.chapter_start and volume.index > 1:
        for cid in volume.enters:
            if cid not in valid_char_ids:
                errors.append(
                    f"Volume {volume.index} transition: enters character {cid!r} not found in project.characters"
                )
        for ex in volume.exits:
            if ex.id not in valid_char_ids:
                errors.append(
                    f"Volume {volume.index} transition: exits character {ex.id!r} not found in project.characters"
                )
        for fid in volume.foreshadows_plant:
            if fid not in known_foreshadow_ids:
                errors.append(
                    f"Volume {volume.index} transition: foreshadows_plant {fid!r} not found in blueprint/project"
                )
        for fid in volume.foreshadows_payoff:
            if fid not in known_foreshadow_ids:
                errors.append(
                    f"Volume {volume.index} transition: foreshadows_payoff {fid!r} not found in blueprint/project"
                )
        if volume.map is not None and volume.map.location_id.strip():
            expected_map_loc = volume.map.location_id.strip()
            batch_locs = {
                str(sc.get("location_id") or "").strip()
                for sc in ordered_batch
            }
            if expected_map_loc not in batch_locs:
                errors.append(
                    f"Volume {volume.index} transition batch ({start_chapter}..{end_chapter}): "
                    f"new volume map location_id {expected_map_loc!r} ({volume.map.name}) "
                    f"must appear in at least one scene's location_id"
                )

    return errors


def _normalize_scene_for_outline(
    raw_scene: Mapping[str, Any],
    *,
    volume_index: int,
) -> Dict[str, Any]:
    """Convert a validated blueprint scene dict into a clean standard SceneOutline YAML mapping."""
    out: Dict[str, Any] = {
        "chapter": int(raw_scene["chapter"]),
        "scene_id": str(raw_scene["scene_id"]).strip(),
        "internal_title": str(raw_scene["internal_title"]).strip(),
        "public_title": str(raw_scene["public_title"]).strip(),
        "beat_type": str(raw_scene.get("beat_type") or "").strip(),
        "emotion_polarity": str(raw_scene.get("emotion_polarity") or "neutral").strip(),
        "pov_character": str(raw_scene["pov_character"]).strip(),
        "active_characters": [str(c).strip() for c in raw_scene["active_characters"]],
        "active_assets": [str(a).strip() for a in raw_scene["active_assets"]],
        "style_profile_ids": [str(sp).strip() for sp in raw_scene["style_profile_ids"]],
        "location_time": str(raw_scene["location_time"]).strip(),
        "location_id": str(raw_scene["location_id"]).strip(),
        "min_chars": int(raw_scene["min_chars"]),
        "max_chars": int(raw_scene["max_chars"]),
        "events": [str(e).strip() for e in raw_scene["events"] if str(e).strip()],
        "stop_condition": str(raw_scene["stop_condition"]).strip(),
        "cliffhanger_hook": str(raw_scene["cliffhanger_hook"]).strip(),
        "segment_comment_cue": str(raw_scene.get("segment_comment_cue") or "").strip(),
        "volume": int(volume_index),
        "calendar_days_elapsed": (
            int(raw_scene["calendar_days_elapsed"])
            if raw_scene.get("calendar_days_elapsed") is not None
            else 1
        ),
    }

    if raw_scene.get("locked") is True:
        out["locked"] = True

    asset_muts: List[Dict[str, Any]] = []
    for mut in raw_scene.get("post_scene_asset_mutations") or []:
        entry: Dict[str, Any] = {"asset_id": str(mut["asset_id"]).strip()}
        for opt_k in ("location", "holder", "condition"):
            if mut.get(opt_k) is not None:
                entry[opt_k] = str(mut[opt_k])
        if mut.get("note"):
            entry["note"] = str(mut["note"])
        asset_muts.append(entry)
    if asset_muts:
        out["post_scene_asset_mutations"] = asset_muts

    ledger_muts: List[Dict[str, Any]] = []
    for lm in raw_scene.get("ledger_mutations") or []:
        entry = {
            "ledger_key": str(lm["ledger_key"]).strip(),
            "delta": float(lm["delta"]) if not _is_strict_int(lm["delta"]) else int(lm["delta"]),
        }
        if lm.get("reason"):
            entry["reason"] = str(lm["reason"])
        if lm.get("breakthrough") is True:
            entry["breakthrough"] = True
        ledger_muts.append(entry)
    if ledger_muts:
        out["ledger_mutations"] = ledger_muts

    char_muts: List[Dict[str, Any]] = []
    for cm in raw_scene.get("character_mutations") or []:
        entry = {"character_id": str(cm["character_id"]).strip()}
        if cm.get("status") is not None:
            entry["status"] = str(cm["status"]).strip()
        if cm.get("faction") is not None:
            entry["faction"] = str(cm["faction"])
        if cm.get("relation_target_id") is not None:
            entry["relation_target_id"] = str(cm["relation_target_id"]).strip()
        if cm.get("relation_label") is not None:
            entry["relation_label"] = str(cm["relation_label"])
        if cm.get("add_forbidden_titles"):
            entry["add_forbidden_titles"] = [str(t) for t in cm["add_forbidden_titles"]]
        if cm.get("injury") is not None:
            entry["injury"] = str(cm["injury"])
        if cm.get("injury_recovery_chapters") is not None and int(cm["injury_recovery_chapters"]) > 0:
            entry["injury_recovery_chapters"] = int(cm["injury_recovery_chapters"])
        if cm.get("note"):
            entry["note"] = str(cm["note"])
        if cm.get("role") is not None:
            entry["role"] = str(cm["role"]).strip()
        char_muts.append(entry)
    if char_muts:
        out["character_mutations"] = char_muts

    exp_dirs: List[Dict[str, Any]] = []
    for ed in raw_scene.get("expectation_directives") or []:
        entry = {
            "id": str(ed["id"]).strip(),
            "title": str(ed["title"]).strip(),
            "due_chapter": int(ed["due_chapter"]),
            "action": str(ed.get("action", "open")).strip(),
        }
        if ed.get("description"):
            entry["description"] = str(ed["description"])
        if ed.get("evidence_keywords"):
            entry["evidence_keywords"] = [str(k).strip() for k in ed["evidence_keywords"] if str(k).strip()]
        exp_dirs.append(entry)
    if exp_dirs:
        out["expectation_directives"] = exp_dirs

    return out


def _build_blueprint_expansion_prompts(
    *,
    project: NovelProject,
    blueprint: BookBlueprint,
    volume: VolumeSpec,
    start_chapter: int,
    end_chapter: int,
    existing_before: Sequence[Any],
    snapshot: Optional[WorldStateSnapshot],
    feedback_errors: Sequence[str],
    editorial_notes: str = "",
) -> Tuple[str, str]:
    bp_min, bp_max = int(blueprint.chapter_chars_range[0]), int(blueprint.chapter_chars_range[1])
    is_volume_transition = start_chapter == volume.chapter_start and volume.index > 1

    # 1. Golden finger current-volume unlocks & limits
    unlocked_upgrades = [u for u in blueprint.golden_finger.upgrade_path if u.volume <= volume.index]
    locked_upgrades = [u for u in blueprint.golden_finger.upgrade_path if u.volume > volume.index]
    gf_lines = [
        f"- 核心规则：{blueprint.golden_finger.rules}",
        f"- 代价：{blueprint.golden_finger.cost}",
        f"- 边界限制：{blueprint.golden_finger.limits}",
    ]
    if unlocked_upgrades:
        gf_lines.append("- 本卷可用金手指技法：")
        for u in unlocked_upgrades:
            tag = "【本卷新解锁】" if u.volume == volume.index else f"【第{u.volume}卷已解锁】"
            gf_lines.append(f"  - {tag} {u.unlock}")
    if locked_upgrades:
        gf_lines.append("- 尚未解锁的高卷技法（本卷严禁提前使用）：")
        for u in locked_upgrades:
            gf_lines.append(f"  - 【第{u.volume}卷方解锁】{u.unlock}")

    # 2. Scale rules & current balances
    current_balances = _simulate_ledger_balances(
        existing_before,
        project.initial_balances,
        snapshot=snapshot,
    )
    scale_lines: List[str] = []
    for r in blueprint.scale_rules:
        cur = current_balances.get(r.ledger_key, 0.0)
        if r.policy == "bounded_realistic" and r.hard_max is not None:
            headroom = float(r.hard_max) - cur
            scale_lines.append(
                f"- {r.ledger_key}（{r.unit}）：policy=bounded_realistic，当前={_fmt_num(cur)}，"
                f"硬上限 hard_max={_fmt_num(r.hard_max)}（剩余可用额度={_fmt_num(headroom)}，累计值严禁超 hard_max 且严禁负余额）"
            )
        else:
            scale_lines.append(
                f"- {r.ledger_key}（{r.unit}）：policy={r.policy}，当前={_fmt_num(cur)}（不设 hard_max，但严禁负余额透支）"
            )

    # 3. Character table with latest current_role & status + exited markers
    char_states = _simulate_character_states(project, existing_before, snapshot=snapshot)
    prior_exited = _collect_prior_exited_characters(blueprint, volume)
    cast_by_id = {m.id: m for m in blueprint.cast}
    char_lines: List[str] = []
    for cid, char in project.characters.items():
        cs = char_states.get(cid)
        cur_role = (cs.current_role if cs and cs.current_role else char.role)
        status = cs.status if cs else char.initial_status
        member = cast_by_id.get(cid)
        lifecycle = (
            f"入场卷={member.first_volume}, 退场卷={member.exit_volume or '无'}"
            if member is not None
            else ""
        )
        ban_tag = ""
        if cid in prior_exited:
            ban_tag = f" 【已退场禁实时出场：{prior_exited[cid]}】"
        elif status == "deceased":
            ban_tag = f" 【已死亡禁实时出场（Ch.{cs.deceased_chapter if cs else '?'}）】"
        char_lines.append(
            f"- {cid}（{char.name}）｜current_role={cur_role}｜status={status}"
            + (f"｜{lifecycle}" if lifecycle else "")
            + ban_tag
        )

    # 4. Assets table (with latest simulated locations/holders/conditions)
    asset_states = _simulate_asset_states(project, existing_before, snapshot=snapshot)
    asset_lines = [
        f"- {aid}（{a.name}）｜位置={a.location}｜持有人={a.holder}｜状态={a.condition}"
        for aid, a in asset_states.items()
    ]

    # 5. Unresolved foreshadows (project + blueprint volume plan)
    fs_source = (
        snapshot.foreshadows
        if snapshot is not None and snapshot.foreshadows
        else project.foreshadows
    )
    unresolved_fs_lines = [
        f"- {fs.id}《{fs.title}》｜status={fs.status}｜plant_ch={fs.plant_chapter}｜due_ch={fs.due_chapter}"
        f"｜plant_kw={fs.plant_keywords}｜payoff_kw={fs.payoff_keywords}"
        for fs in fs_source.values()
        if fs.status != "resolved"
    ]
    plan_by_id = {f.id: f for f in blueprint.foreshadow_plan}
    vol_plant_desc = [
        f"{fid}（{plan_by_id[fid].title}）" if fid in plan_by_id else fid
        for fid in volume.foreshadows_plant
    ]
    vol_payoff_desc = [
        f"{fid}（{plan_by_id[fid].title}）" if fid in plan_by_id else fid
        for fid in volume.foreshadows_payoff
    ]

    # 6. Prior chapter summaries, trailing location_id, trailing suppress, open expectations
    prior_tail = list(existing_before)[-10:]
    prior_summary_lines = [
        f"- 第{_scene_chapter(s)}章《{_scene_field(s, 'public_title', '') or _scene_field(s, 'internal_title', '')}》"
        f"({_scene_field(s, 'emotion_polarity', 'neutral')} | loc={_scene_field(s, 'location_id', '') or '未标注'})："
        f"{'；'.join(str(e) for e in (_scene_field(s, 'events', []) or [])[:2])}"
        for s in prior_tail
    ]
    trailing_loc = _trailing_location_run(
        [{"location_id": _scene_field(s, "location_id", "")} for s in existing_before]
    )
    trailing_suppress = _trailing_suppress_count(
        [{"emotion_polarity": _scene_field(s, "emotion_polarity", "neutral")} for s in existing_before]
    )
    open_exps = _collect_open_expectations(existing_before, snapshot=snapshot)
    due_exp_block = _format_due_expectations_block(open_exps, start_chapter, end_chapter)

    # 7. Volume spec details
    antagonist = project.characters.get(volume.antagonist_id)
    antagonist_label = (
        f"{volume.antagonist_id}（{antagonist.name}：{antagonist.role}）"
        if antagonist is not None
        else volume.antagonist_id
    )
    exits_desc = (
        "; ".join(f"{ex.id}（{ex.how}）" for ex in volume.exits) if volume.exits else "无"
    )
    map_desc = (
        f"{volume.map.location_id}（{volume.map.name}）"
        if volume.map is not None
        else "沿用当前卷主地图（请细分子空间 location_id，严禁连续超过 2 章相同）"
    )
    transition_block = ""
    if is_volume_transition:
        transition_block = (
            f"\n### 【换卷首批同步要求（第 {volume.index} 卷开卷第 {start_chapter} 章起）】\n"
            f"- 必须切入新卷主地图：{map_desc}（本批至少 1 章 location_id 必须使用新卷 map.location_id，且同一 location_id 连续不超过 2 章）。\n"
            f"- 新卷进场角色：{volume.enters or '无'}；本卷退场规划：{exits_desc}；前卷已退场角色严禁再列入 active_characters。\n"
            f"- 角色身份跃迁：若主角或核心配角身份升级，请在对应章 character_mutations 中写明非空 role 字段以同步推进 current_role。\n"
            f"- 本卷伏笔任务：埋设={vol_plant_desc or '无'}；回收={vol_payoff_desc or '无'}。\n"
        )

    style_ids = sorted(project.style_profiles.keys())

    system_prompt = (
        "你是番茄爆款蓝图驱动章纲展开师。严格输出单个 JSON 对象：{\"scenes\": [ ... ]}，"
        "不得输出 JSON 以外的解释文字。\n"
        "硬性约束（违反任意一条将全部打回重修）：\n"
        f"1. chapter 必须连续完整覆盖第 {start_chapter}..{end_chapter} 章（全部落在第 {volume.index} 卷 "
        f"[{volume.chapter_start}..{volume.chapter_end}] 内），无缺章、无重章、volume={volume.index}。\n"
        "2. 每章必须显式回答【三问】：scene_goal（主角要什么）、scene_obstacle（谁挡着/阻力是什么）、"
        "scene_outcome（本章结果得失），三项均为非空具体字符串，并具象落进 events。\n"
        f"3. 每章 events 必须不少于 {MIN_EVENTS_PER_CHAPTER} 条具体物理事件（含动作、器物、对话交锋或证据推进），严禁空泛总结。\n"
        "4. 每章 cliffhanger_hook 必须是非空【事件型悬念钩子】，严禁命中‘一夜无话/沉沉睡去/圆满解决/皆大欢喜/回家休息’等泄气结尾。\n"
        f"5. min_chars 与 max_chars 必须严格落在 blueprint.chapter_chars_range [{bp_min}, {bp_max}] 内且 min_chars <= max_chars。\n"
        "6. pov_character、active_characters、active_assets、style_profile_ids 必须全部取自下方项目已有 id，"
        "且 pov_character 必须在 active_characters 中；严禁将已死亡或前卷已退场角色放入 active_characters 实时施动。\n"
        "7. 同一 location_id（含与前序章衔接）绝对不可连续超过 2 章；连续 emotion_polarity='suppress' 绝对不可超过 2 章。\n"
        "8. 数值守卫：bounded_realistic 账本键累计余额绝对不得超过 hard_max；任何账本键变动后余额绝对不得小于 0。\n"
        "9. public_title 严禁带「第X章/回/节」前缀；character_mutations 可使用非空 role 同步推进 current_role。\n"
        "10. 严格遵循以下 JSON 格式样例：\n"
        f"{json.dumps(_BLUEPRINT_SCENES_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
    )

    feedback_section = (
        "\n### 【上一轮校验失败，以下全部问题必须逐条彻底修复后整批重新输出】\n"
        + "\n".join(f"{idx}. {err}" for idx, err in enumerate(feedback_errors, start=1))
        + "\n"
        if feedback_errors
        else ""
    )

    editorial_section = (
        f"\n### 【主编弧线会诊与本批强制纠偏指令】\n{editorial_notes.strip()}\n"
        if isinstance(editorial_notes, str) and editorial_notes.strip()
        else ""
    )

    user_prompt = (
        f"阶段: blueprint_expand（第 {volume.index} 卷《{volume.title}》章纲展开）\n"
        f"项目: {project.meta.project_id}《{project.meta.title}》｜genre_id={blueprint.style.genre_id}\n"
        f"本次展开区间: 第 {start_chapter} 章 ～ 第 {end_chapter} 章（本卷总范围：第 {volume.chapter_start}～{volume.chapter_end} 章）\n"
        f"单章字数红线: [{bp_min}, {bp_max}]（默认 pov_character={project.meta.default_pov_character}；可用 style_profile_ids={style_ids}）\n\n"
        f"### 当前卷蓝图（Volume {volume.index}：《{volume.title}》）\n"
        f"- 主角本卷目标：{volume.protagonist_goal}\n"
        f"- 本卷主要反派：{antagonist_label}\n"
        f"- 爽点玩法：{'；'.join(volume.cool_point_modes)}\n"
        f"- 本卷高潮：{volume.climax}\n"
        f"- 本卷收获：{volume.payoff}\n"
        f"- 必出名场面：{' ｜ '.join(volume.key_scenes)}\n"
        f"- 本卷进场：{volume.enters or '无'} ｜ 本卷退场：{exits_desc}\n"
        f"- 本卷地图：{map_desc}\n"
        f"- 本卷埋设伏笔：{vol_plant_desc or '无'} ｜ 本卷回收伏笔：{vol_payoff_desc or '无'}\n"
        f"{transition_block}\n"
        f"### 金手指规则、代价与本卷技法\n"
        + "\n".join(gf_lines)
        + "\n\n"
        f"### 数值上限规则（scale_rules）与当前余额\n"
        f"- 当前余额汇总：{_format_balances_summary(current_balances)}\n"
        + "\n".join(scale_lines)
        + "\n\n"
        f"### 当前角色表（含最新 current_role / status，严禁编造新 id）\n"
        + "\n".join(char_lines)
        + "\n\n"
        f"### 当前资产表（严禁编造新 id）\n"
        + ("\n".join(asset_lines) if asset_lines else "（无登记资产，active_assets 写 []）")
        + "\n\n"
        f"### 未完结伏笔与到期期待感\n"
        + ("\n".join(unresolved_fs_lines) if unresolved_fs_lines else "（当前无未完结项目伏笔）")
        + f"\n到期期待感提醒：\n{due_exp_block}\n\n"
        f"### 前序章节摘要与边界状态\n"
        f"- 前序末尾 location_id：{trailing_loc[0] or '无'}（已连续 {trailing_loc[1]} 章；衔接本批绝对不超过 2 章）\n"
        f"- 前序末尾连续 suppress 章数：{trailing_suppress}（衔接本批绝对不超过 2 章）\n"
        + ("\n".join(prior_summary_lines) if prior_summary_lines else "（尚无前序章节）")
        + f"{editorial_section}"
        + f"\n{feedback_section}"
    )

    return system_prompt, user_prompt


def _write_outline_atomic_with_verify(
    *,
    project_root: Path,
    outline_path: Path,
    updated_outline_data: Mapping[str, Any],
    existed_before: bool,
    original_bytes: Optional[bytes],
) -> NovelProject:
    """Atomically write outline YAML and verify with load_project(); rollback cleanly on any failure."""
    serialized = _dump_yaml(updated_outline_data)
    outline_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = outline_path.with_name(f".{outline_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    replaced = False
    try:
        tmp_path.write_text(serialized, encoding="utf-8", newline="\n")
        tmp_path.replace(outline_path)
        replaced = True
        return load_project(project_root)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        if replaced:
            if existed_before and original_bytes is not None:
                restore_tmp = outline_path.with_name(
                    f".{outline_path.name}.restore.{uuid.uuid4().hex[:8]}.tmp"
                )
                try:
                    restore_tmp.write_bytes(original_bytes)
                    restore_tmp.replace(outline_path)
                finally:
                    if restore_tmp.exists():
                        restore_tmp.unlink()
            elif not existed_before and outline_path.exists():
                outline_path.unlink()
        if isinstance(exc, ProjectValidationError):
            raise PlanningValidationError(
                f"Blueprint expansion failed post-write load_project() verification (rolled back): {exc}"
            ) from exc
        raise


def expand_blueprint_chapters(
    *,
    project_dir: str | Path,
    blueprint_path: Optional[str | Path] = None,
    start_chapter: int,
    end_chapter: int,
    provider: LLMProvider | RoleModelRouter,
    max_retries: int = 3,
    snapshot_path: Optional[str | Path] = None,
    editorial_notes: str = "",
) -> BlueprintExpansionResult:
    """Expand a single-volume chapter span from BookBlueprint into project outline with strict verification."""
    if not isinstance(provider, (LLMProvider, RoleModelRouter)):
        raise LLMConfigurationError(
            f"provider must be RoleModelRouter or LLMProvider, got {type(provider).__name__}"
        )
    if not _is_strict_int(max_retries) or max_retries < 1:
        raise ValueError(f"max_retries must be an integer >= 1, got {max_retries!r}")

    root = Path(project_dir).resolve()
    project = load_project(root)

    bp_file = (
        Path(blueprint_path).resolve()
        if blueprint_path is not None
        else (root / "blueprint.yaml")
    )
    blueprint = load_blueprint(bp_file)

    volume = resolve_target_volume(blueprint, start_chapter, end_chapter)

    effective_snapshot: Optional[WorldStateSnapshot] = None
    if snapshot_path is not None:
        sp = Path(snapshot_path)
        if not sp.is_file():
            raise PlannerError(f"Snapshot file does not exist: {sp}")
        raw_snap = json.loads(sp.read_text(encoding="utf-8"))
        effective_snapshot = WorldStateSnapshot.from_dict(raw_snap)

    existing_chapters = {s.chapter for s in project.scenes}
    requested_chapters = set(range(start_chapter, end_chapter + 1))
    overlap = sorted(existing_chapters & requested_chapters)
    if overlap:
        raise ValueError(
            f"Cannot overwrite existing chapters {overlap} in outline; "
            f"existing and locked chapters must remain untouched"
        )
    if start_chapter <= project.meta.max_published_chapter:
        raise ValueError(
            f"start_chapter ({start_chapter}) <= max_published_chapter ({project.meta.max_published_chapter})"
        )

    # Determine target outline file (preserve single-file outline.yaml when present)
    single_outline = root / "outline.yaml"
    outline_dir = root / "outline"
    vol_files = sorted(outline_dir.glob("vol_*.yaml")) if outline_dir.is_dir() else []
    if single_outline.is_file():
        outline_path = single_outline
        existed_before = True
        original_bytes: Optional[bytes] = outline_path.read_bytes()
        raw_outline = yaml.safe_load(original_bytes.decode("utf-8"))
        if not isinstance(raw_outline, Mapping) or not isinstance(raw_outline.get("scenes"), list):
            raise ProjectValidationError(
                f"Invalid outline.yaml structure in {outline_path}: expected mapping with 'scenes' list"
            )
    elif vol_files:
        outline_path = outline_dir / f"vol_{volume.index:02d}.yaml"
        existed_before = outline_path.is_file()
        if existed_before:
            original_bytes = outline_path.read_bytes()
            raw_outline = yaml.safe_load(original_bytes.decode("utf-8"))
            if not isinstance(raw_outline, Mapping) or not isinstance(raw_outline.get("scenes"), list):
                raise ProjectValidationError(
                    f"Invalid volume outline structure in {outline_path}: expected mapping with 'scenes' list"
                )
        else:
            original_bytes = None
            raw_outline = {
                "volume": volume.index,
                "is_final_volume": False,
                "scenes": [],
            }
    else:
        raise ProjectValidationError(
            f"Missing required outline.yaml or outline/vol_*.yaml in {root}"
        )

    existing_before = [s for s in sorted(project.scenes, key=lambda sc: sc.chapter) if s.chapter < start_chapter]

    last_errors: List[str] = []
    validated_scenes: Optional[List[Mapping[str, Any]]] = None
    attempts_used = 0

    for attempt in range(1, max_retries + 1):
        attempts_used = attempt
        system_prompt, user_prompt = _build_blueprint_expansion_prompts(
            project=project,
            blueprint=blueprint,
            volume=volume,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            existing_before=existing_before,
            snapshot=effective_snapshot,
            feedback_errors=last_errors,
            editorial_notes=editorial_notes,
        )
        req = LLMRequest(
            request_id=f"bp-expand-{project.meta.project_id}-v{volume.index}-{start_chapter}-{end_chapter}-{attempt}-{uuid.uuid4().hex[:6]}",
            chapter=start_chapter,
            attempt=attempt,
            purpose="plan",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.5,
            max_tokens=STAGE_SCENES_MAX_TOKENS,
            metadata={
                "stage": "blueprint_expand",
                "project_id": project.meta.project_id,
                "volume_index": volume.index,
                "start_chapter": start_chapter,
                "end_chapter": end_chapter,
                "attempt": attempt,
            },
        )
        resp = _invoke_planner(provider, req)
        try:
            payload = _parse_json_payload(
                resp.content,
                f"blueprint_expand_v{volume.index}_{start_chapter}_{end_chapter}",
            )
        except ProjectValidationError as exc:
            last_errors = [str(exc)]
            continue

        raw_scenes = payload.get("scenes")
        errors = collect_blueprint_scene_errors(
            raw_scenes,
            blueprint=blueprint,
            project=project,
            volume=volume,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            existing_scenes=project.scenes,
            snapshot=effective_snapshot,
        )
        if not errors:
            assert isinstance(raw_scenes, list)
            validated_scenes = sorted(raw_scenes, key=lambda sc: int(sc["chapter"]))
            break
        last_errors = errors

    if validated_scenes is None:
        raise PlanningValidationError(
            f"Blueprint chapter expansion failed for Volume {volume.index} "
            f"(chapters {start_chapter}..{end_chapter}) after {max_retries} attempts:\n"
            + "\n".join(f"{idx}. {err}" for idx, err in enumerate(last_errors, start=1))
        )

    normalized_new = [
        _normalize_scene_for_outline(sc, volume_index=volume.index)
        for sc in validated_scenes
    ]

    # Preserve all existing scene dicts completely untouched; merge and order by chapter
    preserved_existing = [copy.deepcopy(sc) for sc in raw_outline.get("scenes", [])]
    merged_scenes = sorted(
        preserved_existing + normalized_new,
        key=lambda sc: int(sc["chapter"]),
    )

    updated_outline = copy.deepcopy(dict(raw_outline))
    updated_outline["scenes"] = merged_scenes

    _write_outline_atomic_with_verify(
        project_root=root,
        outline_path=outline_path,
        updated_outline_data=updated_outline,
        existed_before=existed_before,
        original_bytes=original_bytes,
    )

    added_chapters = [int(sc["chapter"]) for sc in normalized_new]
    return BlueprintExpansionResult(
        volume_index=volume.index,
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        added_chapters=added_chapters,
        outline_path=outline_path,
        attempts_used=attempts_used,
    )


__all__ = [
    "BlueprintExpansionResult",
    "MAX_CONSECUTIVE_SAME_LOCATION",
    "MAX_CONSECUTIVE_SUPPRESS",
    "MIN_EVENTS_PER_CHAPTER",
    "collect_blueprint_scene_errors",
    "expand_blueprint_chapters",
    "resolve_target_volume",
]
