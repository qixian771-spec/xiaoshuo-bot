from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid
import yaml

from .blueprint import ScaleRule, load_blueprint
from .deconstructor import BestsellerDeconstructor, DeconstructedBlueprint
from .evolution import (
    MotifWeightEntry,
    MotifWeightStore,
    OptimizationReport,
    OutlineAdjustmentDirective,
    assert_chapter_mutable,
)
from .genres import GenreRecipe, get_genre, get_genre_ids
from .llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse
from .llm.router import RoleModelRouter
from .loader import load_project
from .models import (
    AUTHOR_LEVELS,
    CHARACTER_STATUSES,
    EMOTION_POLARITIES,
    EXPECTATION_ACTIONS,
    PANEL_POLICIES,
    POV_MODES,
    SHANGHAI_TZ,
    NovelProject,
    ProjectValidationError,
    VolumeCapLock,
    WorldStateSnapshot,
)
from .namer import CharacterNamer, NamerError
from .plagiarism_guard import (
    CrossBookCollisionError,
    PlagiarismAndCollisionGuard,
    PlagiarismViolationError,
)
from .sanitizers import InvalidTweetAliasError, validate_tweet_alias


class PlannerError(RuntimeError):
    """Base error for AutoNovelPlanner."""


class PlanningValidationError(PlannerError, ProjectValidationError):
    """Raised when generated project/volume YAML fails load_project() validation after max retries."""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>[\s\S]*?)\s*```", re.IGNORECASE)
_CHAPTER_PREFIX_RE = re.compile(r"^\s*第\s*[0-9零一二三四五六七八九十百千万]+\s*[章回节]\s*[:：\-—\s]*")


def _parse_json_payload(raw_text: str, stage: str) -> Dict[str, Any]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ProjectValidationError(f"{stage}: LLM returned empty content")
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
        raise ProjectValidationError(f"{stage}: invalid JSON from planner LLM ({exc})") from exc
    if not isinstance(parsed, dict):
        raise ProjectValidationError(f"{stage}: expected JSON object, got {type(parsed).__name__}")
    return parsed


SCENE_BATCH_SIZE = 10
MASTER_OUTLINE_TOTAL = 100
MASTER_OUTLINE_BATCH_SIZE = 25
STAGE_BIBLE_MAX_TOKENS = 32768
STAGE_SCENES_MAX_TOKENS = 32768
STAGE_MASTER_OUTLINE_MAX_TOKENS = 32768

PlanProgressCallback = Callable[[Dict[str, Any]], None]

_BIBLE_JSON_EXAMPLE: Dict[str, Any] = {
    "project": {
        "title": "<书名，1~15个字符，严禁包含书名号《》>",
        "alternate_titles": [
            "<备选书名1，1~15字无《》>",
            "<备选书名2，1~15字无《》>",
            "<备选书名3，1~15字无《》>",
            "<备选书名4，1~15字无《》>",
            "<备选书名5，1~15字无《》>",
        ],
        "synopsis": "<作品主简介，说明核心冲突、金手指与主线目标>",
        "alternate_blurbs": [
            "<备选简介1，三段式>",
            "<备选简介2，三段式>",
            "<备选简介3，三段式>",
        ],
        "tweet_aliases": [
            "<推文别名1，4~8字纯简体常用汉字>",
            "<推文别名2，4~8字纯简体常用汉字>",
            "<推文别名3，4~8字纯简体常用汉字>",
        ],
        "default_pov_character": "mc",
        "pov_mode": "third_limited",
        "panel_policy": "forbid",
        "opening_motif": "<开篇母题关键词>",
        "golden_finger_type": "<金手指类型>",
        "golden_finger_description": "<金手指触发机制、上限与使用代价>",
        "target_word_count": 176000,
        "initial_balances": {"cash": 500.0},
    },
    "characters": [
        {
            "id": "mc",
            "name": "<必须从推荐安全角色名池选取的正式姓名>",
            "role": "<主角身份定位与核心困境>",
            "personality": "<核心性格锚点>",
            "speech_style": "<说话方式与句长特征>",
            "behavioral_anchors": {
                "<克制或紧张时的触发情境>": "<可见物理微动作，严禁抽象套话>"
            },
            "forbidden_ooc": ["<严禁出现的违背人设行为>"],
            "initial_status": "alive",
            "faction": "<所属阵营>",
            "relations": {"rival": "<与该角色的关系标签>"},
            "forbidden_titles": [],
        },
        {
            "id": "rival",
            "name": "<从推荐安全角色名池选取的另一不撞音姓名>",
            "role": "<主要冲突方身份定位>",
            "personality": "<核心性格>",
            "speech_style": "<说话方式>",
            "behavioral_anchors": {"<施压场景>": "<可见肢体动作>"},
            "forbidden_ooc": ["<禁止降智行为>"],
            "initial_status": "alive",
            "faction": "<敌对阵营>",
            "relations": {"mc": "<关系标签>"},
            "forbidden_titles": [],
        },
    ],
    "assets": [
        {
            "id": "core_item",
            "name": "<核心器物/道具名称>",
            "location": "<初始物理位置>",
            "holder": "<当前持有人姓名>",
            "condition": "<当前外观与完好状态>",
            "physical_details": "<材质、尺寸、磨损或编号等物理细节>",
        }
    ],
    "foreshadows": [
        {
            "id": "FS-01",
            "title": "<伏笔标题>",
            "plant_chapter": 1,
            "due_chapter": 10,
            "plant_instruction": "<埋设章节必须出现的具体物理细节指令>",
            "payoff_instruction": "<回收章节必须完成的物理兑现指令>",
            "plant_keywords": ["<埋设物理证据词1>", "<埋设物理证据词2>"],
            "payoff_keywords": ["<回收物理证据词1>", "<回收物理证据词2>"],
            "cross_volume": False,
        }
    ],
    "style_profiles": [
        {
            "id": "<文风id，可直接沿用赛道默认模板ID或新增>",
            "name": "<文风名称>",
            "granularity_notes": "<叙述视角、句式节奏、感官与动作密度要求>",
            "sample_text": "<60~120字正文示范切片，严禁总结腔>",
        }
    ],
    "volume_cap_locks": [
        {
            "ledger_key": "cash",
            "volume": 1,
            "max_value": 1000000.0,
            "unit": "元",
        }
    ],
}

_SCENES_BATCH_JSON_EXAMPLE: Dict[str, Any] = {
    "scenes": [
        {
            "chapter": 1,
            "scene_id": "ch0001_scene",
            "internal_title": "<内部章纲节点名>",
            "public_title": "<对外章节标题，严禁带第X章前缀>",
            "pov_character": "<设定包中已有角色id，且必须在active_characters内>",
            "active_characters": ["<设定包角色id1>", "<设定包角色id2>"],
            "active_assets": ["<设定包器物id>"],
            "style_profile_ids": ["<设定包文风id或赛道默认模板ID>"],
            "location_time": "<本章具体时间与物理地点>",
            "location_id": "<场景英文坐标id>",
            "events": [
                "<本章发生的第1个具体物理事件>",
                "<本章发生的第2个具体物理事件与冲突推进>",
            ],
            "stop_condition": "<本章写到哪一个具体动作/画面立即停笔>",
            "min_chars": 2050,
            "max_chars": 2300,
            "beat_type": "<爽点节拍，如：压抑蓄力/反转打脸/危机加码>",
            "emotion_polarity": "mixed",
            "cliffhanger_hook": "<章末最高张力悬念钩子>",
            "segment_comment_cue": "<可供读者段评吐槽或站队的引爆点>",
            "volume": 1,
            "calendar_days_elapsed": 1,
            "post_scene_asset_mutations": [
                {
                    "asset_id": "<设定包已有器物id；本章器物无变化时整个数组写 []>",
                    "location": "<器物本章结束时所在位置，可省略>",
                    "holder": "<本章结束时持有者的角色id，可省略>",
                    "condition": "<器物状态变化，如：表盖撬开，可省略>",
                    "note": "<变化原因一句话>",
                }
            ],
            "ledger_mutations": [
                {
                    "ledger_key": "<设定包 ledgers 里已有的账本键名，如 cash；本章数值无变化时整个数组写 []>",
                    "delta": -1,
                    "reason": "<数值变化原因一句话>",
                }
            ],
            "character_mutations": [
                {
                    "character_id": "<设定包已有角色id；本章人物状态无变化时整个数组写 []>",
                    "status": "alive",
                    "note": "<状态或关系变化说明>",
                }
            ],
            "expectation_directives": [
                {
                    "id": "<期待感条目英文id，如 exp_pocketwatch_truth>",
                    "title": "<读者期待的悬念一句话>",
                    "due_chapter": 10,
                    "action": "open",
                    "description": "<何时、如何兑现>",
                }
            ],
        }
    ]
}

_MASTER_OUTLINE_BATCH_JSON_EXAMPLE: Dict[str, Any] = {
    "master_outline": [
        {
            "chapter": 1,
            "title": "<章节标题，不带第X章前缀>",
            "summary": "<本章核心冲突、爽点兑现或地图推进一句话梗概>",
        }
    ]
}

_EXTEND_VOLUME_JSON_EXAMPLE: Dict[str, Any] = {
    "map_name": "<本卷主地图名称>",
    "location_id": "<全新英文地图坐标id，换地图时严禁与旧地图重名>",
    "soft_identity_reboot": "<换地图时主角身份软重启/新身份切入说明>",
    "volume_cap_locks": [
        {
            "ledger_key": "<账本键名，如 cash>",
            "volume": 2,
            "max_value": 10000000.0,
            "unit": "<单位，如 元>",
        }
    ],
    "scenes": _SCENES_BATCH_JSON_EXAMPLE["scenes"],
}

_FINALE_VOLUME_JSON_EXAMPLE: Dict[str, Any] = {
    "foreshadow_resolutions": {"<未收伏笔id>": 76},
    "expectation_fulfillments": {"<未兑现期待感id>": 77},
    "scenes": _SCENES_BATCH_JSON_EXAMPLE["scenes"],
}


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _check_tomato_title_str(raw: Any, label: str, errors: List[str]) -> None:
    if not isinstance(raw, str) or not raw.strip():
        errors.append(f"{label} must be a non-empty string (1..15 chars without 《》)")
        return
    title = raw.strip()
    if "《" in title or "》" in title:
        errors.append(f"{label} must not contain '《》': {title!r}")
    if len(title) > 15:
        errors.append(f"{label} length must be 1..15 chars (got {len(title)}): {title!r}")


def _collect_bible_errors(
    payload: Mapping[str, Any],
    *,
    author_level: str,
    target_chapters: int,
    recipe: GenreRecipe,
) -> List[str]:
    """Collect ALL validation errors for Stage 1 (bible) without stopping at the first error."""
    errors: List[str] = []
    if not isinstance(payload, Mapping):
        return [f"bible payload must be a JSON object, got {type(payload).__name__}"]

    # 1. characters (parse first so we can verify default_pov_character)
    valid_char_ids: set[str] = set()
    raw_chars = payload.get("characters")
    if not isinstance(raw_chars, list) or not raw_chars:
        errors.append("characters must be a non-empty list of objects")
    else:
        cast_namer = CharacterNamer()
        for idx, item in enumerate(raw_chars):
            prefix = f"characters[{idx}]"
            if not isinstance(item, Mapping):
                errors.append(f"{prefix} must be an object, got {type(item).__name__}")
                continue
            cid = _nonempty_str(item.get("id"))
            if not cid:
                errors.append(f"{prefix}: missing non-empty string 'id'")
            elif cid in valid_char_ids:
                errors.append(f"{prefix}: duplicate character id {cid!r}")
            else:
                valid_char_ids.add(cid)

            cname = _nonempty_str(item.get("name"))
            if not cname:
                errors.append(f"{prefix}: missing non-empty string 'name'")
            else:
                try:
                    cast_namer.register_name(cname)
                except NamerError as exc:
                    errors.append(f"{prefix}.name ({cname!r}): {exc}")

            for req_field in ("role", "personality", "speech_style"):
                if not _nonempty_str(item.get(req_field)):
                    errors.append(f"{prefix}: missing non-empty string '{req_field}'")

            if "initial_status" in item and item["initial_status"] is not None:
                ist = item["initial_status"]
                if not isinstance(ist, str) or ist not in CHARACTER_STATUSES:
                    errors.append(
                        f"{prefix}.initial_status {ist!r} must be one of {CHARACTER_STATUSES}"
                    )
            if "behavioral_anchors" in item and item["behavioral_anchors"] is not None:
                if not isinstance(item["behavioral_anchors"], Mapping):
                    errors.append(f"{prefix}.behavioral_anchors must be an object")
            if "relations" in item and item["relations"] is not None:
                if not isinstance(item["relations"], Mapping):
                    errors.append(f"{prefix}.relations must be an object")
            for list_field in ("forbidden_ooc", "forbidden_titles"):
                if list_field in item and item[list_field] is not None:
                    if not isinstance(item[list_field], list):
                        errors.append(f"{prefix}.{list_field} must be a list")

    # 2. project
    raw_meta = payload.get("project")
    if not isinstance(raw_meta, Mapping):
        errors.append("project must be a non-empty object")
    else:
        _check_tomato_title_str(raw_meta.get("title"), "project.title", errors)

        raw_alt_titles = raw_meta.get("alternate_titles")
        if not isinstance(raw_alt_titles, list):
            errors.append("project.alternate_titles must be a list of exactly 5 titles")
        else:
            cleaned_alts = [t for t in raw_alt_titles if isinstance(t, str) and t.strip()]
            if len(cleaned_alts) != 5 or len(raw_alt_titles) != 5:
                errors.append(
                    f"project.alternate_titles must contain exactly 5 non-empty titles, got {len(cleaned_alts)}"
                )
            for idx, alt_t in enumerate(raw_alt_titles):
                _check_tomato_title_str(alt_t, f"project.alternate_titles[{idx}]", errors)

        raw_blurbs = raw_meta.get("alternate_blurbs")
        if not isinstance(raw_blurbs, list):
            errors.append("project.alternate_blurbs must be a list of exactly 3 blurbs")
        else:
            cleaned_blurbs = [b for b in raw_blurbs if isinstance(b, str) and b.strip()]
            if len(cleaned_blurbs) != 3 or len(raw_blurbs) != 3:
                errors.append(
                    f"project.alternate_blurbs must contain exactly 3 non-empty blurbs, got {len(cleaned_blurbs)}"
                )

        raw_aliases = raw_meta.get("tweet_aliases")
        if not isinstance(raw_aliases, list) or not [a for a in raw_aliases if isinstance(a, str) and a.strip()]:
            errors.append("project.tweet_aliases must be a non-empty list of validated aliases")
        else:
            for idx, alias in enumerate(raw_aliases):
                alias_s = _nonempty_str(alias)
                if not alias_s:
                    errors.append(f"project.tweet_aliases[{idx}] must be a non-empty string")
                    continue
                try:
                    validate_tweet_alias(alias_s)
                except InvalidTweetAliasError as exc:
                    errors.append(f"project.tweet_aliases[{idx}] ({alias_s!r}): {exc}")

        if not _nonempty_str(raw_meta.get("synopsis")):
            errors.append("project.synopsis must be a non-empty string")

        default_pov = _nonempty_str(raw_meta.get("default_pov_character"))
        if not default_pov:
            errors.append("project.default_pov_character must be a non-empty string")
        elif valid_char_ids and default_pov not in valid_char_ids:
            errors.append(
                f"project.default_pov_character {default_pov!r} not found in characters ids {sorted(valid_char_ids)}"
            )

        if "pov_mode" in raw_meta and raw_meta["pov_mode"] is not None:
            if not isinstance(raw_meta["pov_mode"], str) or raw_meta["pov_mode"] not in POV_MODES:
                errors.append(f"project.pov_mode {raw_meta['pov_mode']!r} must be one of {POV_MODES}")
        if "panel_policy" in raw_meta and raw_meta["panel_policy"] is not None:
            if not isinstance(raw_meta["panel_policy"], str) or raw_meta["panel_policy"] not in PANEL_POLICIES:
                errors.append(
                    f"project.panel_policy {raw_meta['panel_policy']!r} must be one of {PANEL_POLICIES}"
                )

        raw_twc = raw_meta.get("target_word_count")
        if raw_twc is not None:
            if not _is_strict_int(raw_twc):
                errors.append(f"project.target_word_count must be an integer, got {raw_twc!r}")
            elif author_level == "lv0_1":
                eff_twc = target_chapters * 2200 if raw_twc == 0 else raw_twc
                if not (150_000 <= eff_twc <= 200_000):
                    errors.append(
                        f"project.target_word_count for lv0_1 must be in 150000..200000, got {eff_twc}"
                    )

        if "initial_balances" in raw_meta and raw_meta["initial_balances"] is not None:
            if not isinstance(raw_meta["initial_balances"], Mapping):
                errors.append("project.initial_balances must be an object mapping ledger keys to numbers")
            else:
                for bk, bv in raw_meta["initial_balances"].items():
                    if not _is_strict_number(bv):
                        errors.append(f"project.initial_balances[{bk!r}] must be numeric, got {bv!r}")

    # 3. assets
    valid_asset_ids: set[str] = set()
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        errors.append("assets must be a non-empty list of objects")
    else:
        for idx, item in enumerate(raw_assets):
            prefix = f"assets[{idx}]"
            if not isinstance(item, Mapping):
                errors.append(f"{prefix} must be an object, got {type(item).__name__}")
                continue
            aid = _nonempty_str(item.get("id"))
            if not aid:
                errors.append(f"{prefix}: missing non-empty string 'id'")
            elif aid in valid_asset_ids:
                errors.append(f"{prefix}: duplicate asset id {aid!r}")
            else:
                valid_asset_ids.add(aid)
            for req_field in ("name", "location", "holder", "condition", "physical_details"):
                if not _nonempty_str(item.get(req_field)):
                    errors.append(f"{prefix}: missing non-empty string '{req_field}'")

    # 4. foreshadows
    seen_fs_ids: set[str] = set()
    raw_foreshadows = payload.get("foreshadows")
    if not isinstance(raw_foreshadows, list) or not raw_foreshadows:
        errors.append("foreshadows must be a non-empty list of objects")
    else:
        for idx, item in enumerate(raw_foreshadows):
            prefix = f"foreshadows[{idx}]"
            if not isinstance(item, Mapping):
                errors.append(f"{prefix} must be an object, got {type(item).__name__}")
                continue
            fid = _nonempty_str(item.get("id"))
            if not fid:
                errors.append(f"{prefix}: missing non-empty string 'id'")
            elif fid in seen_fs_ids:
                errors.append(f"{prefix}: duplicate foreshadow id {fid!r}")
            else:
                seen_fs_ids.add(fid)

            for req_field in ("title", "plant_instruction", "payoff_instruction"):
                if not _nonempty_str(item.get(req_field)):
                    errors.append(f"{prefix}: missing non-empty string '{req_field}'")

            cross_vol = item.get("cross_volume", False)
            if not isinstance(cross_vol, bool):
                errors.append(f"{prefix}.cross_volume must be a boolean, got {cross_vol!r}")
                cross_vol = False

            status = str(item.get("status", "pending"))
            p_ch = item.get("plant_chapter")
            d_ch = item.get("due_chapter")
            p_ok = _is_strict_int(p_ch)
            d_ok = _is_strict_int(d_ch)
            if not p_ok:
                errors.append(f"{prefix}.plant_chapter must be an integer, got {p_ch!r}")
            if not d_ok:
                errors.append(f"{prefix}.due_chapter must be an integer, got {d_ch!r}")
            if p_ok and d_ok:
                if d_ch < p_ch:
                    errors.append(f"{prefix}: due_chapter ({d_ch}) < plant_chapter ({p_ch})")
                if not (1 <= p_ch <= target_chapters):
                    if not (cross_vol and status in ("planted", "resolved")):
                        errors.append(
                            f"{prefix}: plant_chapter {p_ch} must be in 1..{target_chapters}"
                        )
                if status != "resolved" and not (1 <= d_ch <= target_chapters):
                    if not cross_vol:
                        errors.append(
                            f"{prefix}: due_chapter {d_ch} must be in 1..{target_chapters} (or set cross_volume=true for cross-volume foreshadow)"
                        )

            raw_pk = item.get("plant_keywords")
            pk_list = (
                [k.strip() for k in ([raw_pk] if isinstance(raw_pk, str) else raw_pk) if isinstance(k, str) and k.strip()]
                if isinstance(raw_pk, (list, str))
                else []
            )
            if status == "pending" and not pk_list:
                errors.append(f"{prefix}.plant_keywords must contain at least one non-empty keyword")

            raw_rk = item.get("payoff_keywords")
            rk_list = (
                [k.strip() for k in ([raw_rk] if isinstance(raw_rk, str) else raw_rk) if isinstance(k, str) and k.strip()]
                if isinstance(raw_rk, (list, str))
                else []
            )
            if status != "resolved" and not rk_list:
                errors.append(f"{prefix}.payoff_keywords must contain at least one non-empty keyword")

    # 5. style_profiles
    raw_styles = payload.get("style_profiles")
    if raw_styles is None or not isinstance(raw_styles, list):
        errors.append("style_profiles must be a list of objects")
    else:
        for idx, item in enumerate(raw_styles):
            prefix = f"style_profiles[{idx}]"
            if not isinstance(item, Mapping):
                errors.append(f"{prefix} must be an object, got {type(item).__name__}")
                continue
            for req_field in ("id", "name", "granularity_notes", "sample_text"):
                if not _nonempty_str(item.get(req_field)):
                    errors.append(f"{prefix}: missing non-empty string '{req_field}'")

    # 6. volume_cap_locks
    raw_locks = payload.get("volume_cap_locks")
    if not isinstance(raw_locks, list) or not raw_locks:
        errors.append("volume_cap_locks must be a non-empty list of objects")
    else:
        has_vol1 = False
        for idx, item in enumerate(raw_locks):
            prefix = f"volume_cap_locks[{idx}]"
            if not isinstance(item, Mapping):
                errors.append(f"{prefix} must be an object, got {type(item).__name__}")
                continue
            if not _nonempty_str(item.get("ledger_key")):
                errors.append(f"{prefix}: missing non-empty string 'ledger_key'")
            vol = item.get("volume")
            if not _is_strict_int(vol) or vol < 1:
                errors.append(f"{prefix}.volume must be a positive integer, got {vol!r}")
            elif vol == 1:
                has_vol1 = True
            if not _is_strict_number(item.get("max_value")):
                errors.append(f"{prefix}.max_value must be numeric, got {item.get('max_value')!r}")
        if not has_vol1:
            errors.append("volume_cap_locks must define at least one lock with volume=1")

    return errors


def _collect_scene_batch_errors(
    batch: Any,
    bible: Mapping[str, Any],
    chapter_range: Tuple[int, int],
    *,
    recipe: Optional[GenreRecipe] = None,
    prior_trailing_suppress: int = 0,
    prior_trailing_location: Tuple[str, int] = ("", 0),
    max_consecutive_same_location: Optional[int] = None,
    prior_open_expectations: Optional[Mapping[str, Mapping[str, Any]]] = None,
    prior_balances: Optional[Mapping[str, float]] = None,
) -> List[str]:
    """Collect ALL validation errors for a Stage 2 scene batch without stopping at the first error."""
    start_ch, end_ch = chapter_range
    expected_chapters = list(range(start_ch, end_ch + 1))
    if not isinstance(batch, list) or not batch:
        return [f"scenes must be a non-empty list covering chapters {start_ch}..{end_ch}"]

    valid_char_ids = {
        str(c["id"]).strip()
        for c in (bible.get("characters") or [])
        if isinstance(c, Mapping) and _nonempty_str(c.get("id"))
    }
    valid_asset_ids = {
        str(a["id"]).strip()
        for a in (bible.get("assets") or [])
        if isinstance(a, Mapping) and _nonempty_str(a.get("id"))
    }
    valid_style_ids = {
        str(sp["id"]).strip()
        for sp in (bible.get("style_profiles") or [])
        if isinstance(sp, Mapping) and _nonempty_str(sp.get("id"))
    }
    if recipe is not None:
        valid_style_ids.add(recipe.style_profile.id)
    default_pov = str((bible.get("project") or {}).get("default_pov_character") or "").strip()

    errors: List[str] = []
    seen_chapters: List[int] = []
    seen_chapter_set: set[int] = set()
    valid_scenes_by_ch: Dict[int, Mapping[str, Any]] = {}

    for idx, item in enumerate(batch):
        prefix = f"scenes[{idx}]"
        if not isinstance(item, Mapping):
            errors.append(f"{prefix} must be an object, got {type(item).__name__}")
            continue

        raw_ch = item.get("chapter")
        if not _is_strict_int(raw_ch):
            errors.append(f"{prefix}: missing integer 'chapter' (got {raw_ch!r})")
            ch_label = prefix
        else:
            ch_label = f"Chapter {raw_ch}"
            if not (start_ch <= raw_ch <= end_ch):
                errors.append(
                    f"{ch_label}: chapter {raw_ch} is outside expected batch range {start_ch}..{end_ch}"
                )
            elif raw_ch in seen_chapter_set:
                errors.append(f"{ch_label}: duplicate chapter number {raw_ch} in batch")
            else:
                seen_chapter_set.add(raw_ch)
                seen_chapters.append(raw_ch)
                valid_scenes_by_ch[raw_ch] = item

        for req_str in ("scene_id", "internal_title", "location_time", "stop_condition"):
            if not _nonempty_str(item.get(req_str)):
                errors.append(f"{ch_label}: missing non-empty string '{req_str}'")

        raw_pub = item.get("public_title")
        pub_title = _nonempty_str(raw_pub)
        if not pub_title:
            errors.append(f"{ch_label}: missing non-empty string 'public_title'")
        elif _CHAPTER_PREFIX_RE.match(pub_title):
            errors.append(
                f"{ch_label}: public_title {pub_title!r} must not start with '第X章/回/节' prefix"
            )

        raw_events = item.get("events")
        if not isinstance(raw_events, list) or not [e for e in raw_events if isinstance(e, str) and e.strip()]:
            errors.append(f"{ch_label}: 'events' must be a non-empty list of non-empty strings")

        pov_char = str(item.get("pov_character") or default_pov).strip()
        if not pov_char or pov_char not in valid_char_ids:
            errors.append(
                f"{ch_label}: pov_character {pov_char!r} not in bible characters {sorted(valid_char_ids)}"
            )

        raw_active_chars = item.get("active_characters")
        if not isinstance(raw_active_chars, list) or not raw_active_chars:
            errors.append(f"{ch_label}: 'active_characters' must be a non-empty list of character ids")
        else:
            for cid in raw_active_chars:
                if not isinstance(cid, str) or cid not in valid_char_ids:
                    errors.append(
                        f"{ch_label}: unknown character id {cid!r} in active_characters (allowed: {sorted(valid_char_ids)})"
                    )
            if pov_char in valid_char_ids and pov_char not in raw_active_chars:
                errors.append(
                    f"{ch_label}: pov_character {pov_char!r} must be listed in active_characters"
                )

        raw_active_assets = item.get("active_assets", [])
        if not isinstance(raw_active_assets, list):
            errors.append(f"{ch_label}: 'active_assets' must be a list of asset ids")
        else:
            for aid in raw_active_assets:
                if not isinstance(aid, str) or aid not in valid_asset_ids:
                    errors.append(
                        f"{ch_label}: unknown asset id {aid!r} in active_assets (allowed: {sorted(valid_asset_ids)})"
                    )

        raw_styles = item.get("style_profile_ids", [])
        if not isinstance(raw_styles, list):
            errors.append(f"{ch_label}: 'style_profile_ids' must be a list of style profile ids")
        else:
            for sp_id in raw_styles:
                if not isinstance(sp_id, str) or sp_id not in valid_style_ids:
                    errors.append(
                        f"{ch_label}: unknown style_profile id {sp_id!r} (allowed: {sorted(valid_style_ids)})"
                    )

        if "emotion_polarity" in item and item["emotion_polarity"] is not None:
            ep = item["emotion_polarity"]
            if not isinstance(ep, str) or ep not in EMOTION_POLARITIES:
                errors.append(
                    f"{ch_label}: unknown emotion_polarity {ep!r}; expected one of {EMOTION_POLARITIES}"
                )

        if "min_chars" in item and item["min_chars"] is not None and not _is_strict_int(item["min_chars"]):
            errors.append(f"{ch_label}: min_chars must be an integer, got {item['min_chars']!r}")
        if "max_chars" in item and item["max_chars"] is not None and not _is_strict_int(item["max_chars"]):
            errors.append(f"{ch_label}: max_chars must be an integer, got {item['max_chars']!r}")
        if (
            _is_strict_int(item.get("min_chars"))
            and _is_strict_int(item.get("max_chars"))
            and item["min_chars"] > item["max_chars"]
        ):
            errors.append(
                f"{ch_label}: min_chars ({item['min_chars']}) > max_chars ({item['max_chars']})"
            )

        # Nested optional mutation lists
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
                if not _is_strict_number(lm.get("delta")):
                    errors.append(f"{ch_label}: ledger_mutations[{m_idx}] delta must be numeric")

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

    # Contiguous chapter coverage check
    missing = [ch for ch in expected_chapters if ch not in seen_chapter_set]
    if missing:
        errors.append(
            f"scenes batch {start_ch}..{end_ch}: missing chapters {missing}"
        )

    # Consecutive suppress polarity check (carrying over trailing suppress from previous batch)
    consecutive_suppress = max(0, int(prior_trailing_suppress))
    suppress_run: List[int] = []
    for ch in sorted(valid_scenes_by_ch):
        ep = valid_scenes_by_ch[ch].get("emotion_polarity", "neutral")
        if ep == "suppress":
            consecutive_suppress += 1
            suppress_run.append(ch)
            if consecutive_suppress > 2:
                errors.append(
                    f"Chapter {ch}: consecutive emotion_polarity='suppress' exceeds max 2 "
                    f"(prior_trailing={prior_trailing_suppress}, batch_run={suppress_run})"
                )
        else:
            consecutive_suppress = 0
            suppress_run.clear()

    # Consecutive identical location_id check (aligns with STATE_STALE_LOCATION gate <= 2)
    if max_consecutive_same_location is not None and int(max_consecutive_same_location) > 0:
        limit = int(max_consecutive_same_location)
        cur_loc = str(prior_trailing_location[0] or "").strip() if prior_trailing_location else ""
        consec_loc = max(0, int(prior_trailing_location[1])) if cur_loc else 0
        loc_run: List[int] = []
        for ch in sorted(valid_scenes_by_ch):
            loc = str(valid_scenes_by_ch[ch].get("location_id") or "").strip()
            if not loc:
                cur_loc = ""
                consec_loc = 0
                loc_run.clear()
                continue
            if loc == cur_loc:
                consec_loc += 1
                loc_run.append(ch)
                if consec_loc > limit:
                    errors.append(
                        f"Chapter {ch}: consecutive location_id={loc!r} exceeds max {limit} "
                        f"(would trigger STATE_STALE_LOCATION gate; prior_trailing={prior_trailing_location}, "
                        f"batch_run={loc_run}; switch to a distinct sub-space or new location_id)"
                    )
            else:
                cur_loc = loc
                consec_loc = 1
                loc_run = [ch]

    # Expectation open <-> fulfill deadline consistency check (prevents STATE_EXPECTATION_OVERDUE deadlocks)
    open_exps: Dict[str, Dict[str, Any]] = {
        str(eid): dict(info)
        for eid, info in (prior_open_expectations or {}).items()
        if str(eid).strip()
    }
    reported_overdue: set[str] = set()
    for ch in sorted(valid_scenes_by_ch):
        raw_eds = valid_scenes_by_ch[ch].get("expectation_directives") or []
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
            if due_ch > 0 and due_ch <= ch and eid not in reported_overdue:
                reported_overdue.add(eid)
                errors.append(
                    f"Chapter {ch}: expectation {eid!r} ({info.get('title')!r}, opened Ch.{info.get('opened_chapter', '?')}, "
                    f"due Ch.{due_ch}) has no matching action='fulfill' directive on or before Ch.{due_ch} "
                    f"(would trigger STATE_EXPECTATION_OVERDUE deadlock at Ch.{due_ch + 1}); "
                    f"add action='fulfill' with evidence_keywords on Ch.{due_ch} (or earlier)"
                )

    # Ledger balance overdraft pre-check (prevents STATE_OVERDRAFT deadlocks across batches & volumes)
    if prior_balances is not None:
        running_balances: Dict[str, float] = {
            str(k): float(v)
            for k, v in prior_balances.items()
            if _is_strict_number(v)
        }
        for ch in sorted(valid_scenes_by_ch):
            raw_lms = valid_scenes_by_ch[ch].get("ledger_mutations") or []
            if not isinstance(raw_lms, list):
                continue
            for lm in raw_lms:
                if not isinstance(lm, Mapping):
                    continue
                lkey = _nonempty_str(lm.get("ledger_key"))
                raw_delta = lm.get("delta")
                if not lkey or not _is_strict_number(raw_delta):
                    continue
                delta = float(raw_delta)
                before = float(running_balances.get(lkey, 0.0))
                after = before + delta
                if after < 0:
                    errors.append(
                        f"Chapter {ch}: ledger_mutation {lkey!r} delta={delta:g} drives balance from "
                        f"{before:g} to {after:g} < 0 (would trigger STATE_OVERDRAFT deadlock); "
                        f"reduce this expenditure or add prior income for {lkey!r}"
                    )
                running_balances[lkey] = after

    # Golden-three-chapter beat alignment check (chapters 1..3 must match genre recipe beats)
    if recipe is not None and recipe.golden_three_beats:
        golden_beats_by_ch = {b.chapter: b for b in recipe.golden_three_beats}
        for ch in (1, 2, 3):
            if ch not in valid_scenes_by_ch or ch not in golden_beats_by_ch:
                continue
            sc = valid_scenes_by_ch[ch]
            beat = golden_beats_by_ch[ch]
            beat_type_val = _nonempty_str(sc.get("beat_type"))
            if not beat_type_val:
                errors.append(
                    f"Chapter {ch}: missing 'beat_type' matching golden-three beat role {beat.role!r} "
                    f"(genre={recipe.genre_id})"
                )
            elif beat.role not in beat_type_val:
                errors.append(
                    f"Chapter {ch}: beat_type {beat_type_val!r} does not match golden-three beat role "
                    f"{beat.role!r} for genre {recipe.genre_id!r} "
                    f"(expected beat_type to include {beat.role!r})"
                )
            if not _nonempty_str(sc.get("cliffhanger_hook")):
                errors.append(
                    f"Chapter {ch}: missing non-empty 'cliffhanger_hook' required by golden-three beat "
                    f"{beat.role!r} (expected hook direction: {beat.chapter_end_hook})"
                )

    return errors


def _collect_master_outline_batch_errors(
    batch: Any,
    chapter_range: Tuple[int, int],
) -> List[str]:
    """Collect ALL validation errors for a Stage 3 master_outline batch."""
    start_ch, end_ch = chapter_range
    expected_chapters = list(range(start_ch, end_ch + 1))
    if not isinstance(batch, list) or not batch:
        return [f"master_outline must be a non-empty list covering chapters {start_ch}..{end_ch}"]

    errors: List[str] = []
    seen_chapters: set[int] = set()
    for idx, item in enumerate(batch):
        prefix = f"master_outline[{idx}]"
        if not isinstance(item, Mapping):
            errors.append(f"{prefix} must be an object, got {type(item).__name__}")
            continue
        raw_ch = item.get("chapter")
        if not _is_strict_int(raw_ch):
            errors.append(f"{prefix}: missing integer 'chapter' (got {raw_ch!r})")
            label = prefix
        else:
            label = f"master_outline chapter {raw_ch}"
            if not (start_ch <= raw_ch <= end_ch):
                errors.append(
                    f"{label}: chapter {raw_ch} is outside expected batch range {start_ch}..{end_ch}"
                )
            elif raw_ch in seen_chapters:
                errors.append(f"{label}: duplicate chapter {raw_ch}")
            else:
                seen_chapters.add(raw_ch)
        if not _nonempty_str(item.get("title")):
            errors.append(f"{label}: missing non-empty string 'title'")
        if not _nonempty_str(item.get("summary")):
            errors.append(f"{label}: missing non-empty string 'summary'")

    missing = [ch for ch in expected_chapters if ch not in seen_chapters]
    if missing:
        errors.append(f"master_outline batch {start_ch}..{end_ch}: missing chapters {missing}")
    return errors


def _format_recipe_title_and_blurb_block(recipe: GenreRecipe) -> str:
    """Format genre title_templates, title_examples, and blurb_templates for bible planning."""
    lines: List[str] = ["### 题材书名与简介模板参考（参考结构，严禁照抄范例人物与情节）"]
    lines.append(
        "- 高点击商业书名硬性法则：书名（7～15字为佳，硬上限15字，严禁《》）必须一眼露出"
        "「主角身份/核心金手指或奇异能力/强烈反差冲突或爽点利益」至少两项；"
        "严禁起《三点十七分》《白雾渡口》《雨夜孤灯》这类纯时间、纯地名、纯景物或抽象文艺短名（除非老板在立项要求中明确指定）。"
    )
    if recipe.title_templates:
        lines.append("- 爆款书名句式模板：" + " ｜ ".join(recipe.title_templates))
    if recipe.title_examples:
        lines.append("- 同赛道书名范例（仅作结构参考，严禁照抄）：" + " / ".join(recipe.title_examples))
    if recipe.blurb_templates:
        lines.append("- 三段式简介范本（主简介与 3 版备选简介均须参照其冲突-金手指-推进结构，严禁照抄）：")
        for idx, tmpl in enumerate(recipe.blurb_templates, start=1):
            indented = "\n    ".join(tmpl.strip().splitlines())
            lines.append(f"  范本{idx}:\n    {indented}")
    return "\n".join(lines)


def _format_recipe_golden_three_block(
    recipe: GenreRecipe,
    *,
    chapter_range: Optional[Tuple[int, int]] = None,
) -> str:
    """Format genre opening_300w_directive and golden_three_beats for bible & scenes planning."""
    lines: List[str] = [f"### 题材黄金三章节拍 blueprints（{recipe.display_name} / {recipe.genre_id}）"]
    include_ch1 = chapter_range is None or (chapter_range[0] <= 1 <= chapter_range[1])
    if include_ch1 and recipe.opening_300w_directive:
        lines.append(f"- 第 1 章首屏黄金 300 字开局指令：{recipe.opening_300w_directive}")
    beats = [
        b
        for b in recipe.golden_three_beats
        if chapter_range is None or (chapter_range[0] <= b.chapter <= chapter_range[1])
    ]
    for b in beats:
        lines.append(
            f"- 第 {b.chapter} 章｜必填节拍定位（beat_type 必须包含此词）：「{b.role}」\n"
            f"  - 开篇钩子：{b.opening_hook}\n"
            f"  - 核心冲突：{b.core_conflict}\n"
            f"  - 爽点/反转：{b.payoff_or_twist}\n"
            f"  - 章末断章（cliffhanger_hook 方向）：{b.chapter_end_hook}"
        )
    return "\n".join(lines)


def _three_act_position(start_ch: int, end_ch: int, total_chapters: int, author_level: str) -> str:
    if author_level == "lv2_plus":
        if end_ch <= 10:
            return "首秀前10章（黄金三章起爆 + 前两万字直签期首轮小高潮）"
        return "首秀第11~15章（承上启下，兑现首轮战果并抛向百章长线与换地图引子）"
    ratio = ((start_ch + end_ch) / 2.0) / max(1, total_chapters)
    if ratio <= 0.28:
        return f"第一幕·开篇建势（全书共 {total_chapters} 章，黄金三章确立核心矛盾与金手指立威）"
    if ratio <= 0.78:
        return f"第二幕·中段升级（全书共 {total_chapters} 章，对手升级、伏笔交织与阶段爽点兑现）"
    return f"第三幕·终局收官（全书共 {total_chapters} 章，全面清算反派、回收所有伏笔并完成大结局）"


def _trailing_suppress_count(scenes: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for sc in reversed(scenes):
        if sc.get("emotion_polarity") == "suppress":
            count += 1
        else:
            break
    return count


def _trailing_location_run(scenes: Sequence[Mapping[str, Any]]) -> Tuple[str, int]:
    """Return (location_id, consecutive_count) for the trailing non-empty location run."""
    if not scenes:
        return ("", 0)
    last_loc = str(scenes[-1].get("location_id") or "").strip()
    if not last_loc:
        return ("", 0)
    count = 0
    for sc in reversed(scenes):
        loc = str(sc.get("location_id") or "").strip()
        if loc == last_loc:
            count += 1
        else:
            break
    return (last_loc, count)


def find_stale_location_runs(
    scenes: Sequence[Any],
    *,
    max_consecutive: int = 2,
) -> List[Dict[str, Any]]:
    """Audit ordered scenes and return any chapters violating >max_consecutive identical location_id."""
    limit = max(1, int(max_consecutive))
    ordered = sorted(
        scenes,
        key=lambda sc: int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0)),
    )
    violations: List[Dict[str, Any]] = []
    cur_loc = ""
    run_chapters: List[int] = []
    for sc in ordered:
        ch = int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0))
        loc = str((sc.get("location_id") if isinstance(sc, Mapping) else getattr(sc, "location_id", "")) or "").strip()
        if not loc:
            cur_loc = ""
            run_chapters = []
            continue
        if loc == cur_loc:
            run_chapters.append(ch)
            if len(run_chapters) > limit:
                violations.append(
                    {
                        "chapter": ch,
                        "location_id": loc,
                        "consecutive_count": len(run_chapters),
                        "run_chapters": list(run_chapters),
                    }
                )
        else:
            cur_loc = loc
            run_chapters = [ch]
    return violations


def _collect_open_expectations(
    scenes: Sequence[Any],
    *,
    snapshot: Optional[WorldStateSnapshot] = None,
) -> Dict[str, Dict[str, Any]]:
    """Compute currently unfulfilled open expectations from optional snapshot + ordered scenes."""
    open_exps: Dict[str, Dict[str, Any]] = {}
    if snapshot is not None and getattr(snapshot, "active_expectations", None):
        for eid, exp in snapshot.active_expectations.items():
            if getattr(exp, "status", "") != "fulfilled":
                open_exps[str(eid)] = {
                    "id": str(eid),
                    "title": str(getattr(exp, "title", "")),
                    "opened_chapter": int(getattr(exp, "planted_chapter", 0) or 0),
                    "due_chapter": int(getattr(exp, "due_chapter", 0) or 0),
                }
    ordered = sorted(
        scenes,
        key=lambda sc: int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0)),
    )
    for sc in ordered:
        ch = int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0))
        eds = sc.get("expectation_directives") if isinstance(sc, Mapping) else getattr(sc, "expectation_directives", [])
        for ed in (eds or []):
            if isinstance(ed, Mapping):
                eid = str(ed.get("id") or "").strip()
                act = str(ed.get("action") or "open").strip()
                title = str(ed.get("title") or "").strip()
                due = int(ed.get("due_chapter") or 0) if _is_strict_int(ed.get("due_chapter")) else 0
            else:
                eid = str(getattr(ed, "id", "") or "").strip()
                act = str(getattr(ed, "action", "open") or "open").strip()
                title = str(getattr(ed, "title", "") or "").strip()
                due = int(getattr(ed, "due_chapter", 0) or 0)
            if not eid:
                continue
            if act == "open":
                open_exps[eid] = {
                    "id": eid,
                    "title": title,
                    "opened_chapter": ch,
                    "due_chapter": due,
                }
            elif act == "fulfill":
                open_exps.pop(eid, None)
    return open_exps


def _format_due_expectations_block(
    open_exps: Mapping[str, Mapping[str, Any]],
    start_ch: int,
    end_ch: int,
) -> str:
    due_items = [
        info
        for info in open_exps.values()
        if int(info.get("due_chapter") or 0) <= end_ch
    ]
    due_items.sort(key=lambda x: (int(x.get("due_chapter") or 0), str(x.get("id") or "")))
    if not due_items:
        return "（本批无前序到期需核销的期待感承诺；若本批内新开 action='open' 且 due_chapter 落在本批内，必须在本批对应章配齐 action='fulfill'）"
    lines = [
        f"- [最晚第{int(info['due_chapter'])}章必须核销] id={info['id']}｜标题={info.get('title', '')}｜"
        f"开启于第{info.get('opened_chapter', '?')}章｜必须在 <= 第{int(info['due_chapter'])}章写 action='fulfill' 及 evidence_keywords"
        for info in due_items
    ]
    return "\n".join(lines)


def find_unfulfilled_expectations(
    scenes: Sequence[Any],
    *,
    through_chapter: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Audit ordered scenes and return all open expectations due <= through_chapter lacking fulfill."""
    ordered = sorted(
        scenes,
        key=lambda sc: int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0)),
    )
    if not ordered:
        return []
    max_ch = int(
        through_chapter
        if through_chapter is not None
        else (
            ordered[-1].get("chapter", 0)
            if isinstance(ordered[-1], Mapping)
            else getattr(ordered[-1], "chapter", 0)
        )
    )
    open_exps: Dict[str, Dict[str, Any]] = {}
    violations: List[Dict[str, Any]] = []
    reported: set[str] = set()
    for sc in ordered:
        ch = int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0))
        if ch > max_ch:
            break
        eds = sc.get("expectation_directives") if isinstance(sc, Mapping) else getattr(sc, "expectation_directives", [])
        for ed in (eds or []):
            if isinstance(ed, Mapping):
                eid = str(ed.get("id") or "").strip()
                act = str(ed.get("action") or "open").strip()
                title = str(ed.get("title") or "").strip()
                due = int(ed.get("due_chapter") or 0) if _is_strict_int(ed.get("due_chapter")) else 0
            else:
                eid = str(getattr(ed, "id", "") or "").strip()
                act = str(getattr(ed, "action", "open") or "open").strip()
                title = str(getattr(ed, "title", "") or "").strip()
                due = int(getattr(ed, "due_chapter", 0) or 0)
            if not eid:
                continue
            if act == "open":
                open_exps[eid] = {
                    "id": eid,
                    "title": title,
                    "opened_chapter": ch,
                    "due_chapter": due,
                }
            elif act == "fulfill":
                open_exps.pop(eid, None)
                reported.discard(eid)
        for eid, info in list(open_exps.items()):
            due = int(info.get("due_chapter") or 0)
            if 0 < due <= ch and eid not in reported:
                reported.add(eid)
                violations.append(dict(info))
    return violations


def _simulate_ledger_balances(
    scenes: Sequence[Any],
    initial_balances: Optional[Mapping[str, Any]] = None,
    *,
    snapshot: Optional[WorldStateSnapshot] = None,
) -> Dict[str, float]:
    """Compute running numeric ledger balances after all provided scenes (respecting snapshot baseline)."""
    if snapshot is not None:
        balances: Dict[str, float] = {
            str(k): float(v)
            for k, v in (snapshot.numeric_balances or {}).items()
            if _is_strict_number(v)
        }
        snap_ch = int(snapshot.current_chapter)
    else:
        balances = {
            str(k): float(v)
            for k, v in (initial_balances or {}).items()
            if _is_strict_number(v)
        }
        snap_ch = 0

    ordered = sorted(
        scenes,
        key=lambda sc: int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0)),
    )
    for sc in ordered:
        ch = int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0))
        if ch <= snap_ch:
            continue
        lms = sc.get("ledger_mutations") if isinstance(sc, Mapping) else getattr(sc, "ledger_mutations", [])
        for lm in (lms or []):
            if isinstance(lm, Mapping):
                lkey = _nonempty_str(lm.get("ledger_key"))
                raw_delta = lm.get("delta")
            else:
                lkey = _nonempty_str(getattr(lm, "ledger_key", ""))
                raw_delta = getattr(lm, "delta", None)
            if not lkey or not _is_strict_number(raw_delta):
                continue
            balances[lkey] = float(balances.get(lkey, 0.0)) + float(raw_delta)
    return balances


def _format_balances_summary(balances: Mapping[str, float]) -> str:
    if not balances:
        return "无初始账本（任何支出前需先有正向入账）"
    parts = [f"{k}={v:g}" for k, v in sorted(balances.items())]
    return ", ".join(parts)


def _load_master_outline_slice(project_root: Path, start_ch: int, end_ch: int) -> str:
    """Load chapters start_ch..end_ch from master_outline.yaml when present for rolling continuity."""
    mo_path = project_root / "master_outline.yaml"
    if not mo_path.is_file():
        return "无（按前序剧情与本卷主线自然延展）"
    try:
        raw = yaml.safe_load(mo_path.read_text(encoding="utf-8"))
    except Exception:
        return "无（按前序剧情与本卷主线自然延展）"
    items = raw.get("master_outline", raw) if isinstance(raw, Mapping) else raw
    if not isinstance(items, list):
        return "无（按前序剧情与本卷主线自然延展）"
    lines: List[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        ch = item.get("chapter")
        if _is_strict_int(ch) and start_ch <= int(ch) <= end_ch:
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or "").strip()
            lines.append(f"- 第 {int(ch)} 章《{title}》：{summary}")
    return "\n".join(lines) if lines else "无（已超出百章总纲范围，按长篇大地图自由滚动延展）"


def find_ledger_overdrafts(
    scenes: Sequence[Any],
    initial_balances: Optional[Mapping[str, Any]] = None,
    *,
    snapshot: Optional[WorldStateSnapshot] = None,
) -> List[Dict[str, Any]]:
    """Audit a sequence of scenes for any ledger_mutation driving a balance below 0 (STATE_OVERDRAFT)."""
    if snapshot is not None:
        balances: Dict[str, float] = {
            str(k): float(v)
            for k, v in (snapshot.numeric_balances or {}).items()
            if _is_strict_number(v)
        }
        snap_ch = int(snapshot.current_chapter)
    else:
        balances = {
            str(k): float(v)
            for k, v in (initial_balances or {}).items()
            if _is_strict_number(v)
        }
        snap_ch = 0

    ordered = sorted(
        scenes,
        key=lambda sc: int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0)),
    )
    overdrafts: List[Dict[str, Any]] = []
    for sc in ordered:
        ch = int(sc.get("chapter", 0) if isinstance(sc, Mapping) else getattr(sc, "chapter", 0))
        if ch <= snap_ch:
            continue
        lms = sc.get("ledger_mutations") if isinstance(sc, Mapping) else getattr(sc, "ledger_mutations", [])
        for lm in (lms or []):
            if isinstance(lm, Mapping):
                lkey = _nonempty_str(lm.get("ledger_key"))
                raw_delta = lm.get("delta")
                reason = str(lm.get("reason") or "").strip()
            else:
                lkey = _nonempty_str(getattr(lm, "ledger_key", ""))
                raw_delta = getattr(lm, "delta", None)
                reason = str(getattr(lm, "reason", "") or "").strip()
            if not lkey or not _is_strict_number(raw_delta):
                continue
            delta = float(raw_delta)
            before = float(balances.get(lkey, 0.0))
            after = before + delta
            if after < 0:
                overdrafts.append(
                    {
                        "chapter": ch,
                        "ledger_key": lkey,
                        "before": before,
                        "delta": delta,
                        "after": after,
                        "reason": reason,
                    }
                )
            balances[lkey] = after
    return overdrafts


def _make_chapter_batches(
    start_chapter: int, end_chapter: int, batch_size: int = SCENE_BATCH_SIZE
) -> List[Tuple[int, int]]:
    batches: List[Tuple[int, int]] = []
    cur = start_chapter
    while cur <= end_chapter:
        b_end = min(end_chapter, cur + batch_size - 1)
        batches.append((cur, b_end))
        cur = b_end + 1
    return batches


def _project_to_bible_view(project: NovelProject) -> Dict[str, Any]:
    return {
        "project": {
            "title": project.meta.title,
            "default_pov_character": project.meta.default_pov_character,
        },
        "characters": [
            {"id": c.id, "name": c.name, "role": c.role}
            for c in project.characters.values()
        ],
        "assets": [
            {"id": a.id, "name": a.name}
            for a in project.assets.values()
        ],
        "style_profiles": [
            {"id": sp.id, "name": sp.name}
            for sp in project.style_profiles.values()
        ],
    }


def _project_valid_ids_block(project: NovelProject) -> str:
    char_items = [f"{c.id}({c.name})" for c in project.characters.values()]
    asset_items = [f"{a.id}({a.name})" for a in project.assets.values()]
    style_items = [sp.id for sp in project.style_profiles.values()]
    return (
        f"可用人物 id（严禁编造新 id）: {char_items}\n"
        f"可用器物 id（严禁编造新 id）: {asset_items}\n"
        f"可用文风 id（严禁编造新 id）: {style_items}\n"
        f"默认 POV 人物 id: {project.meta.default_pov_character}"
    )


def _format_prior_scene_summaries(
    existing_scenes: Sequence[Any],
    generated_scenes: Sequence[Mapping[str, Any]],
    *,
    max_items: int = 12,
) -> str:
    combined: List[str] = []
    for sc in existing_scenes:
        ev = "；".join(str(e) for e in (getattr(sc, "events", None) or [])[:2])
        title = getattr(sc, "public_title", "") or getattr(sc, "internal_title", "")
        combined.append(f"- 第 {sc.chapter} 章《{title}》({sc.emotion_polarity}): {ev}")
    for sd in generated_scenes:
        ev = "；".join(str(e) for e in (sd.get("events") or [])[:2])
        title = str(sd.get("public_title") or sd.get("internal_title") or "")
        pol = str(sd.get("emotion_polarity") or "mixed")
        combined.append(f"- 第 {sd.get('chapter')} 章《{title}》({pol}): {ev}")
    tail = combined[-max_items:]
    return "\n".join(tail) if tail else "无"


def _normalize_scale_rules_map(
    scale_rules: Optional[Sequence[Mapping[str, Any] | ScaleRule]],
) -> Tuple[Dict[str, Tuple[str, Optional[float]]], List[str]]:
    rules_by_key: Dict[str, Tuple[str, Optional[float]]] = {}
    errors: List[str] = []
    if scale_rules is None:
        return rules_by_key, errors
    for idx, rule in enumerate(scale_rules):
        if isinstance(rule, Mapping):
            raw_key = rule.get("ledger_key")
            raw_policy = rule.get("policy")
            raw_hard_max = rule.get("hard_max")
        elif isinstance(rule, ScaleRule):
            raw_key = rule.ledger_key
            raw_policy = rule.policy
            raw_hard_max = rule.hard_max
        else:
            errors.append(
                f"scale_rules[{idx}] must be a mapping or ScaleRule, got {type(rule).__name__}"
            )
            continue
        key = _nonempty_str(raw_key)
        policy = _nonempty_str(raw_policy)
        if not key or not policy:
            errors.append(f"scale_rules[{idx}] missing non-empty 'ledger_key' or 'policy'")
            continue
        hard_max: Optional[float] = None
        if raw_hard_max is not None:
            if not _is_strict_number(raw_hard_max) or not math.isfinite(float(raw_hard_max)):
                errors.append(
                    f"scale_rules[{idx}] ({key!r}) hard_max must be a finite number or None, got {raw_hard_max!r}"
                )
                continue
            hard_max = float(raw_hard_max)
        if policy == "bounded_realistic" and (hard_max is None or hard_max <= 0):
            errors.append(
                f"scale_rules[{idx}] ({key!r}) policy='bounded_realistic' requires hard_max > 0, got {raw_hard_max!r}"
            )
            continue
        rules_by_key[key] = (policy, hard_max)
    return rules_by_key, errors


def _collect_extend_volume_meta_errors(
    payload: Mapping[str, Any],
    *,
    target_vol: int,
    trigger_map_change: bool,
    old_locations: set[str],
    prev_locks: Mapping[str, VolumeCapLock],
    scale_rules: Optional[Sequence[Mapping[str, Any] | ScaleRule]] = None,
) -> Tuple[List[str], str, str, str, List[VolumeCapLock]]:
    """Collect ALL volume-level metadata errors for extend_volume Batch 1 without stopping early."""
    errors: List[str] = []
    rules_by_key, rule_errors = _normalize_scale_rules_map(scale_rules)
    errors.extend(rule_errors)

    new_map_name = str(payload.get("map_name") or "").strip()
    new_loc_id = str(payload.get("location_id") or "").strip()
    soft_reboot = str(payload.get("soft_identity_reboot") or "").strip()

    new_locks_raw = payload.get("volume_cap_locks")
    new_locks: List[VolumeCapLock] = []
    if new_locks_raw is not None:
        if not isinstance(new_locks_raw, list):
            errors.append("volume_cap_locks must be a list of VolumeCapLock objects")
        else:
            for idx, lk_raw in enumerate(new_locks_raw):
                if not isinstance(lk_raw, Mapping):
                    errors.append(f"volume_cap_locks[{idx}] must be an object")
                    continue
                try:
                    new_locks.append(VolumeCapLock.from_dict(lk_raw))
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append(f"volume_cap_locks[{idx}] invalid: {exc}")

    for lk in new_locks:
        rule_info = rules_by_key.get(lk.ledger_key)
        if rule_info is not None and rule_info[0] == "bounded_realistic":
            hard_max = rule_info[1]
            if lk.max_value <= 0:
                errors.append(
                    f"New VolumeCapLock '{lk.ledger_key}' max_value ({lk.max_value}) must be > 0 "
                    f"under bounded_realistic scale rule"
                )
            elif hard_max is not None and lk.max_value > hard_max:
                errors.append(
                    f"New VolumeCapLock '{lk.ledger_key}' max_value ({lk.max_value}) exceeds "
                    f"bounded_realistic hard_max ({hard_max})"
                )

    if trigger_map_change:
        if not new_map_name:
            errors.append("Map-change volume extension requires non-empty map_name")
        if not new_loc_id:
            errors.append("Map-change volume extension requires non-empty location_id")
        elif new_loc_id in old_locations:
            errors.append(
                f"New map location_id {new_loc_id!r} collides with prior map locations {sorted(old_locations)}"
            )
        if not soft_reboot:
            errors.append("Map-change volume extension requires non-empty soft_identity_reboot")
        if not new_locks:
            errors.append("Map-change volume extension requires higher-tier volume_cap_locks")
        for lk in new_locks:
            if lk.volume != target_vol:
                errors.append(
                    f"New VolumeCapLock for '{lk.ledger_key}' must have volume={target_vol}, got {lk.volume}"
                )
            rule_info = rules_by_key.get(lk.ledger_key)
            is_bounded_realistic = rule_info is not None and rule_info[0] == "bounded_realistic"
            if (
                not is_bounded_realistic
                and lk.ledger_key in prev_locks
                and lk.max_value <= prev_locks[lk.ledger_key].max_value
            ):
                errors.append(
                    f"New VolumeCapLock '{lk.ledger_key}' max_value ({lk.max_value}) must exceed "
                    f"previous volume cap ({prev_locks[lk.ledger_key].max_value})"
                )
        for prev_key in prev_locks:
            if not any(lk.ledger_key == prev_key for lk in new_locks):
                errors.append(f"Missing upgraded VolumeCapLock for existing ledger_key '{prev_key}'")

    return errors, new_map_name, new_loc_id, soft_reboot, new_locks


def _collect_finale_resolution_errors(
    payload: Mapping[str, Any],
    *,
    start_chapter: int,
    end_chapter: int,
    unresolved_fs: Mapping[str, Any],
    active_exps: Mapping[str, Any],
    raw_scenes: Any,
    is_single_batch: bool,
) -> Tuple[List[str], Dict[str, int], Dict[str, int]]:
    """Collect ALL foreshadow/expectation resolution errors for plan_finale_volume Batch 1."""
    errors: List[str] = []
    finale_chapters = set(range(start_chapter, end_chapter + 1))

    raw_fs_res = payload.get("foreshadow_resolutions")
    fs_resolutions: Dict[str, int] = {}
    if raw_fs_res is None:
        raw_fs_res = {}
    if not isinstance(raw_fs_res, Mapping):
        errors.append("foreshadow_resolutions must be an object mapping foreshadow_id -> payoff_chapter")
    else:
        for fid in unresolved_fs:
            if fid not in raw_fs_res:
                errors.append(f"Finale missing foreshadow_resolutions mapping for '{fid}'")
                continue
            raw_val = raw_fs_res[fid]
            try:
                ch_val = int(raw_val)
            except (ValueError, TypeError):
                errors.append(f"Finale foreshadow '{fid}' due_chapter must be integer, got {raw_val!r}")
                continue
            if ch_val not in finale_chapters:
                errors.append(
                    f"Finale foreshadow '{fid}' due_chapter {ch_val} outside finale range "
                    f"{start_chapter}..{end_chapter}"
                )
            else:
                fs_resolutions[fid] = ch_val

    fulfilled_in_batch1: set[str] = set()
    if is_single_batch and isinstance(raw_scenes, list):
        for sc in raw_scenes:
            if isinstance(sc, Mapping):
                for ed in sc.get("expectation_directives") or []:
                    if isinstance(ed, Mapping) and ed.get("action") == "fulfill" and ed.get("id"):
                        fulfilled_in_batch1.add(str(ed["id"]))

    raw_exp_res = payload.get("expectation_fulfillments")
    exp_fulfillments: Dict[str, int] = {}
    if raw_exp_res is None:
        raw_exp_res = {}
    if not isinstance(raw_exp_res, Mapping):
        errors.append("expectation_fulfillments must be an object mapping expectation_id -> fulfill_chapter")
    else:
        for eid in active_exps:
            if eid not in raw_exp_res and eid not in fulfilled_in_batch1:
                errors.append(f"Finale missing fulfillment for active expectation '{eid}'")
                continue
            if eid in raw_exp_res:
                raw_val = raw_exp_res[eid]
                try:
                    f_ch = int(raw_val)
                except (ValueError, TypeError):
                    errors.append(
                        f"Expectation '{eid}' fulfill_chapter must be integer, got {raw_val!r}"
                    )
                    continue
                if f_ch not in finale_chapters:
                    errors.append(
                        f"Expectation '{eid}' fulfill_chapter {f_ch} outside finale range "
                        f"{start_chapter}..{end_chapter}"
                    )
                else:
                    exp_fulfillments[eid] = f_ch

    return errors, fs_resolutions, exp_fulfillments


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


def _dump_yaml(data: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        dict(data),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _archive_existing_file(path: Path, project_root: Path, stamp: str) -> None:
    if not path.exists():
        return
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        rel = Path(path.name)
    archive_target = project_root / "_archive" / stamp / rel
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(archive_target))


def _write_text_archived(path: Path, content: str, project_root: Path, stamp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _archive_existing_file(path, project_root, stamp)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8", newline="\n")
    tmp_path.replace(path)


def _strip_chapter_prefix(title: str) -> str:
    cleaned = _CHAPTER_PREFIX_RE.sub("", str(title or "").strip()).strip()
    return cleaned or str(title or "").strip()


@dataclass
class PlannedProjectResult:
    project_dir: Path
    project: NovelProject
    contract_track: str
    author_level: str
    opening_motif: str
    golden_finger_type: str
    attempts_used: int
    signing_application_path: Optional[Path] = None
    master_outline_path: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_dir": str(self.project_dir),
            "project_id": self.project.meta.project_id,
            "title": self.project.meta.title,
            "contract_track": self.contract_track,
            "author_level": self.author_level,
            "opening_motif": self.opening_motif,
            "golden_finger_type": self.golden_finger_type,
            "attempts_used": self.attempts_used,
            "scene_count": len(self.project.scenes),
            "signing_application_path": (
                str(self.signing_application_path) if self.signing_application_path else None
            ),
            "master_outline_path": (
                str(self.master_outline_path) if self.master_outline_path else None
            ),
        }


@dataclass
class VolumeExtensionResult:
    project_dir: Path
    project: NovelProject
    volume_index: int
    start_chapter: int
    end_chapter: int
    triggered_map_change: bool
    new_map_name: str
    new_map_location_id: str
    soft_identity_reboot: str
    new_volume_cap_locks: List[VolumeCapLock]
    volume_file_path: Path
    attempts_used: int


@dataclass
class FinaleVolumeResult:
    project_dir: Path
    project: NovelProject
    volume_index: int
    start_chapter: int
    end_chapter: int
    resolved_foreshadow_ids: List[str]
    fulfilled_expectation_ids: List[str]
    volume_file_path: Path
    attempts_used: int


class AutoNovelPlanner:
    """3-source auto-initiation, dual-track signing, rolling map-change continuation, and finale wrap-up."""

    def __init__(
        self,
        provider: LLMProvider | RoleModelRouter,
        *,
        guard: Optional[PlagiarismAndCollisionGuard] = None,
        deconstructor: Optional[BestsellerDeconstructor] = None,
        max_validation_retries: int = 3,
        max_consecutive_same_location: Optional[int] = None,
    ) -> None:
        if provider is None:
            raise LLMConfigurationError("AutoNovelPlanner requires a configured LLM provider/router")
        if max_validation_retries < 1:
            raise ValueError(f"max_validation_retries must be >= 1, got {max_validation_retries}")
        self.provider = provider
        self.guard = guard or PlagiarismAndCollisionGuard()
        self.deconstructor = deconstructor or BestsellerDeconstructor(provider, guard=self.guard)
        self.max_validation_retries = max_validation_retries
        self.max_consecutive_same_location = (
            int(max_consecutive_same_location) if max_consecutive_same_location is not None else None
        )

    @staticmethod
    def _build_signing_application_md(
        meta: Dict[str, Any],
        characters: Sequence[Mapping[str, Any]],
        scenes: Sequence[Mapping[str, Any]],
        master_outline: Sequence[Mapping[str, Any]],
        recipe: GenreRecipe,
        golden_finger_desc: str,
    ) -> str:
        pov_char = next(
            (c for c in characters if c.get("id") == meta.get("default_pov_character")),
            characters[0] if characters else {},
        )
        antagonist_lines = [
            f"- {c.get('name')}：{c.get('role')}（{c.get('personality')}）"
            for c in characters
            if c.get("id") != pov_char.get("id")
        ]
        golden_three = []
        for s in scenes[:3]:
            events_txt = "；".join(str(e) for e in s.get("events", []))
            golden_three.append(
                f"- 第{s.get('chapter')}章《{s.get('public_title') or s.get('internal_title')}》"
                f"（{s.get('beat_type', '推进')}）：{events_txt}｜章末钩子：{s.get('cliffhanger_hook', '')}"
            )
        debut_summary = []
        for s in scenes[3:10]:
            debut_summary.append(
                f"- 第{s.get('chapter')}章 {s.get('public_title') or s.get('internal_title')}：{s.get('stop_condition', '')}"
            )
        milestone_lines = []
        for item in master_outline:
            ch = item.get("chapter")
            if ch in (1, 15, 30, 60, 80, 100) or len(master_outline) <= 10:
                milestone_lines.append(f"- 第{ch}章：{item.get('title', '')} — {item.get('summary', '')}")

        alt_titles = " / ".join(str(t) for t in meta.get("alternate_titles", []))
        est_debut_words = sum(int((int(s.get("min_chars", 2050)) + int(s.get("max_chars", 2300))) // 2) for s in scenes[:10])
        return "\n".join(
            [
                f"# 番茄小说责编直签申请表 — 《{meta.get('title')}》",
                "",
                "## 一、作品基础信息与备选包装",
                f"- 书名：{meta.get('title')}",
                f"- 备选书名池：{alt_titles}",
                f"- 作者笔名：{meta.get('pen_name') or meta.get('author_name')}",
                f"- 频道与主分类：{recipe.channel} / {meta.get('primary_category') or recipe.tomato_primary_category}",
                f"- 标签：{', '.join(meta.get('tags', []))}",
                f"- 签约轨道：{meta.get('contract_track')}（作者等级：{meta.get('author_level')}）",
                "",
                "## 二、核心卖点与金手指机制",
                f"- 作品简介：\n{meta.get('synopsis', '').strip()}",
                f"- 核心金手指与升级机制：{golden_finger_desc}",
                "",
                "## 三、主角人设与核心矛盾",
                f"- 主角：{pov_char.get('name', '')} — {pov_char.get('role', '')}",
                f"- 性格锚点：{pov_char.get('personality', '')}",
                "- 核心关系网与主要冲突方：",
                *(antagonist_lines or ["- 见 characters.yaml"]),
                "",
                "## 四、黄金三章与前两万字剧情节奏大纲",
                "### 黄金前三章核心节拍",
                *golden_three,
                "### 第4～10章（前2万字直签期）节奏推进",
                *(debut_summary or ["- 按首秀15章节奏推进，确保第10章完成首轮小高潮兑现。"]),
                "",
                "## 五、百章总纲与长篇换地图升级路线",
                *milestone_lines,
                "",
                "## 六、首秀包字数与稳定更新承诺",
                f"- 首卷已规划章数：{len(scenes)} 章（前10章约 {est_debut_words} 字，首秀15章约 {len(scenes) * 2200} 字）",
                f"- 规划目标总字数：{meta.get('target_word_count')} 字",
                "- 更新承诺：每日稳定双更（4400+字/日），开书即自带10章以上安全垫存稿，严守不断更承诺。",
                "",
            ]
        )

    def _validate_and_write_staging(
        self,
        staging_dir: Path,
        *,
        payload: Mapping[str, Any],
        project_id: str,
        recipe: GenreRecipe,
        author_level: str,
        contract_track: str,
        pen_name: str,
        target_chapters: int,
        benchmark_chapters: Optional[Sequence[str]],
        projects_root: Optional[Path],
        namer: CharacterNamer,
    ) -> Tuple[NovelProject, Dict[str, Any]]:
        """Validate LLM JSON payload strictly, write YAML into staging_dir, and run load_project()."""
        raw_meta = payload.get("project")
        if not isinstance(raw_meta, Mapping):
            raise ProjectValidationError("Missing 'project' object in planner payload")

        title = str(raw_meta.get("title") or "").strip()
        alt_titles = [str(t).strip() for t in (raw_meta.get("alternate_titles") or []) if str(t).strip()]
        if len(alt_titles) != 5:
            raise ProjectValidationError(
                f"project.alternate_titles must contain exactly 5 titles, got {len(alt_titles)}"
            )
        alt_blurbs = [str(b).strip() for b in (raw_meta.get("alternate_blurbs") or []) if str(b).strip()]
        if len(alt_blurbs) != 3:
            raise ProjectValidationError(
                f"project.alternate_blurbs must contain exactly 3 blurbs, got {len(alt_blurbs)}"
            )

        tweet_aliases = [str(a).strip() for a in (raw_meta.get("tweet_aliases") or []) if str(a).strip()]
        if not tweet_aliases:
            raise ProjectValidationError("project.tweet_aliases must contain at least 1 validated alias")
        for alias in tweet_aliases:
            try:
                validate_tweet_alias(alias)
            except InvalidTweetAliasError as exc:
                raise ProjectValidationError(f"Invalid tweet_alias {alias!r}: {exc}") from exc

        # Validate characters via T03 CharacterNamer (zero AI names, zero homophones, common hanzi)
        raw_chars = payload.get("characters")
        if not isinstance(raw_chars, list) or not raw_chars:
            raise ProjectValidationError("Missing non-empty 'characters' list in planner payload")
        cast_namer = CharacterNamer()
        validated_chars: List[Dict[str, Any]] = []
        for ch_item in raw_chars:
            if not isinstance(ch_item, Mapping):
                raise ProjectValidationError("Each character entry must be a mapping")
            cname = str(ch_item.get("name") or "").strip()
            try:
                cast_namer.register_name(cname)
            except NamerError as exc:
                raise ProjectValidationError(f"Character name rejected by CharacterNamer: {exc}") from exc
            validated_chars.append(dict(ch_item))

        raw_assets = payload.get("assets")
        if not isinstance(raw_assets, list) or not raw_assets:
            raise ProjectValidationError("Missing non-empty 'assets' list in planner payload")

        raw_foreshadows = payload.get("foreshadows")
        if not isinstance(raw_foreshadows, list) or not raw_foreshadows:
            raise ProjectValidationError("Missing non-empty 'foreshadows' list in planner payload")

        raw_styles = list(payload.get("style_profiles") or [])
        style_ids = {str(sp.get("id")) for sp in raw_styles if isinstance(sp, Mapping) and sp.get("id")}
        if recipe.style_profile.id not in style_ids:
            raw_styles.insert(0, recipe.style_profile.to_dict())

        raw_locks = payload.get("volume_cap_locks")
        if not isinstance(raw_locks, list) or not raw_locks:
            raise ProjectValidationError("Missing non-empty 'volume_cap_locks' list in planner payload")
        parsed_locks = [VolumeCapLock.from_dict(lk) for lk in raw_locks]
        if not any(lk.volume == 1 for lk in parsed_locks):
            raise ProjectValidationError("volume_cap_locks must define at least one lock for volume=1")

        raw_scenes = payload.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise ProjectValidationError("Missing non-empty 'scenes' list in planner payload")

        if author_level == "lv0_1":
            if not (70 <= len(raw_scenes) <= 95):
                raise ProjectValidationError(
                    f"lv0_1 complete_150k track requires 70..95 chapters in vol_01, got {len(raw_scenes)}"
                )
        elif author_level == "lv2_plus":
            if len(raw_scenes) != 15:
                raise ProjectValidationError(
                    f"lv2_plus serial_20k debut package requires exactly 15 chapters in vol_01, got {len(raw_scenes)}"
                )
            master_outline = payload.get("master_outline")
            if not isinstance(master_outline, list) or len(master_outline) != 100:
                got_len = len(master_outline) if isinstance(master_outline, list) else 0
                raise ProjectValidationError(
                    f"lv2_plus track requires 100-chapter master_outline, got {got_len}"
                )

        normalized_scenes: List[Dict[str, Any]] = []
        for s_item in raw_scenes:
            if not isinstance(s_item, Mapping):
                raise ProjectValidationError("Each scene entry must be a mapping")
            s_dict = dict(s_item)
            if s_dict.get("public_title"):
                s_dict["public_title"] = _strip_chapter_prefix(str(s_dict["public_title"]))
            s_dict.setdefault("volume", 1)
            s_dict.setdefault("min_chars", recipe.min_chapter_words)
            s_dict.setdefault("max_chars", recipe.max_chapter_words)
            normalized_scenes.append(s_dict)

        opening_motif = str(raw_meta.get("opening_motif") or "").strip()
        golden_finger_type = str(raw_meta.get("golden_finger_type") or "").strip()
        golden_finger_desc = str(
            raw_meta.get("golden_finger_description") or golden_finger_type
        ).strip()

        target_word_count = int(raw_meta.get("target_word_count") or 0)
        if author_level == "lv0_1":
            if target_word_count == 0:
                target_word_count = len(normalized_scenes) * 2200
            if not (150_000 <= target_word_count <= 200_000):
                raise ProjectValidationError(
                    f"lv0_1 target_word_count must be in 150000..200000, got {target_word_count}"
                )
        else:
            if target_word_count <= 0:
                target_word_count = 1_000_000

        initial_balances = {
            str(k): float(v)
            for k, v in dict(raw_meta.get("initial_balances") or {}).items()
        }

        project_yaml_data: Dict[str, Any] = {
            "project_id": project_id,
            "title": title,
            "genre": recipe.display_name,
            "genre_id": recipe.genre_id,
            "synopsis": str(raw_meta.get("synopsis") or "").strip(),
            "default_pov_character": str(raw_meta.get("default_pov_character") or "").strip(),
            "max_rewrite_retries": int(raw_meta.get("max_rewrite_retries", 3)),
            "author_level": author_level,
            "contract_track": contract_track,
            "pov_mode": str(raw_meta.get("pov_mode") or recipe.pov_mode),
            "panel_policy": str(raw_meta.get("panel_policy") or recipe.panel_policy),
            "pen_name": pen_name,
            "author_name": pen_name,
            "primary_category": str(
                raw_meta.get("primary_category") or recipe.tomato_primary_category
            ),
            "tags": list(raw_meta.get("tags") or recipe.tomato_tags),
            "alternate_titles": alt_titles,
            "alternate_blurbs": alt_blurbs,
            "tweet_aliases": tweet_aliases,
            "target_word_count": target_word_count,
            "signed": False,
            "max_published_chapter": 0,
            "volume_index": 1,
            "is_final_volume": False,
            "opening_motif": opening_motif,
            "golden_finger_type": golden_finger_type,
            "golden_finger_description": golden_finger_desc,
            "initial_balances": initial_balances,
            "volume_cap_locks": [lk.to_dict() for lk in parsed_locks],
        }

        # Plagiarism & Cross-book collision checks BEFORE persisting
        all_outline_text_parts = [
            title,
            *alt_titles,
            project_yaml_data["synopsis"],
            *alt_blurbs,
            golden_finger_desc,
        ]
        for s_dict in normalized_scenes:
            all_outline_text_parts.append(str(s_dict.get("public_title", "")))
            all_outline_text_parts.append(str(s_dict.get("internal_title", "")))
            all_outline_text_parts.extend(str(e) for e in s_dict.get("events", []))
            all_outline_text_parts.append(str(s_dict.get("stop_condition", "")))
        all_outline_text = "\n".join(p for p in all_outline_text_parts if p)

        if benchmark_chapters:
            self.guard.check_against_benchmark(
                all_outline_text, benchmark_chapters, raise_on_violation=True
            )

        if projects_root is not None:
            lead_char = next(
                (c for c in validated_chars if c.get("id") == project_yaml_data["default_pov_character"]),
                validated_chars[0],
            )
            opening_ch_texts = [
                " ".join(
                    [
                        str(s.get("public_title", "")),
                        str(s.get("internal_title", "")),
                        " ".join(str(e) for e in s.get("events", [])),
                    ]
                )
                for s in normalized_scenes[:3]
            ]
            self.guard.check_cross_book_collision(
                title=title,
                alternate_titles=alt_titles,
                projects_dir=projects_root,
                genre_id=recipe.genre_id,
                golden_finger=golden_finger_desc or golden_finger_type,
                opening_summary=project_yaml_data["synopsis"],
                opening_chapters=opening_ch_texts,
                protagonist_persona=f"{lead_char.get('role', '')} {lead_char.get('personality', '')}",
                exclude_project_id=project_id,
                raise_on_collision=True,
            )

        # Write YAML files into staging directory and validate strictly via T01 load_project()
        (staging_dir / "outline").mkdir(parents=True, exist_ok=True)
        (staging_dir / "project.yaml").write_text(
            _dump_yaml(project_yaml_data), encoding="utf-8", newline="\n"
        )
        (staging_dir / "characters.yaml").write_text(
            _dump_yaml({"characters": validated_chars}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "assets.yaml").write_text(
            _dump_yaml({"assets": list(raw_assets)}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "foreshadows.yaml").write_text(
            _dump_yaml({"foreshadows": list(raw_foreshadows)}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "style_profiles.yaml").write_text(
            _dump_yaml({"style_profiles": raw_styles}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "outline" / "vol_01.yaml").write_text(
            _dump_yaml(
                {
                    "volume": 1,
                    "is_final_volume": False,
                    "volume_cap_locks": [lk.to_dict() for lk in parsed_locks],
                    "scenes": normalized_scenes,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )

        loaded = load_project(staging_dir)
        prepared = {
            "project_yaml": project_yaml_data,
            "characters_yaml": {"characters": validated_chars},
            "assets_yaml": {"assets": list(raw_assets)},
            "foreshadows_yaml": {"foreshadows": list(raw_foreshadows)},
            "style_profiles_yaml": {"style_profiles": raw_styles},
            "vol_01_yaml": {
                "volume": 1,
                "is_final_volume": False,
                "volume_cap_locks": [lk.to_dict() for lk in parsed_locks],
                "scenes": normalized_scenes,
            },
            "master_outline": list(payload.get("master_outline") or []),
            "golden_finger_description": golden_finger_desc,
        }
        return loaded, prepared

    def _plan_bible_stage(
        self,
        *,
        project_id: str,
        recipe: GenreRecipe,
        author_level: str,
        contract_track: str,
        resolved_chapters: int,
        suggested_cast: Sequence[str],
        motif_lines: Sequence[str],
        hot_keywords: Sequence[str],
        blueprint_block: str,
        brief_section: str,
        on_progress: Optional[PlanProgressCallback] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """Stage 1: generate project bible (project/characters/assets/foreshadows/style_profiles/volume_cap_locks)."""
        track_instruction = (
            f"账号轨=lv0_1（complete_150k）：全书共 {resolved_chapters} 章紧凑完本（15万~20万字，target_word_count 必须在 150000..200000 之间）。所有伏笔的 plant_chapter 与 due_chapter 必须落在 1..{resolved_chapters} 范围内。"
            if author_level == "lv0_1"
            else "账号轨=lv2_plus（serial_20k）：第1卷首秀共 15 章（后续按百章总纲续卷）。跨卷伏笔须显式设 cross_volume=true，且第1卷内伏笔 plant_chapter 必须在 1..15 范围内。"
        )
        system_prompt = (
            "你是番茄爆款总策划师。当前处于【阶段1：设定包 bible】。\n"
            "请严格输出单个 JSON 对象（不要输出 scenes 或 master_outline，也不要输出 JSON 以外的解释文字）。\n"
            "硬性约束：\n"
            "1. project.title 与 5 个 project.alternate_titles 均须为 1~15 个字符，严禁包含书名号《》，并按高点击商业书名法则写出强烈钩子/外挂/身份反差（7~15字为佳，严禁起《三点十七分》《白雾渡口》这类纯时间/纯地名/无钩子文艺短名，除非老板明确指定；严禁照抄范例）。\n"
            "2. project.synopsis 与 3 版 project.alternate_blurbs（三段式简介）须参考下方赛道三段式简介范本结构（严禁照抄范本人物与情节）；project.tweet_aliases 至少 1 个（建议 3 个）4~8 字纯简体常用汉字别名。\n"
            "3. 所有角色姓名必须严格取自推荐安全角色名池，严禁黑名单人名、生僻字或同音名；default_pov_character 必须是 characters 中的某个 id。\n"
            "4. foreshadows 每条必须包含非空 plant_keywords 与 payoff_keywords（具体物理证据词），且 due_chapter >= plant_chapter。\n"
            "5. volume_cap_locks 必须至少包含一条 volume=1 的数值上限锁。\n"
            "6. 所有角色身份、器物设定、初始余额、伏笔与世界观必须严格遵守下方【题材硬常识防穿帮卡】，严禁触发严禁穿帮项；人物与开局设定须预先支撑下方【题材黄金三章节拍】。\n"
            "7. 必须严格遵循以下完整 JSON 格式样例（字段名、数据类型、必填项须完全一致）：\n"
            f"{json.dumps(_BIBLE_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
        )

        title_blurb_block = _format_recipe_title_and_blurb_block(recipe)
        common_sense_block = recipe.common_sense_card.format_prompt_block()
        golden_three_block = _format_recipe_golden_three_block(recipe)

        last_errors: List[str] = []
        for attempt in range(1, self.max_validation_retries + 1):
            if on_progress is not None:
                on_progress({"stage": "bible", "batch": 1, "batches": 1, "attempt": attempt})
            feedback_section = (
                "\n[上一次输出存在以下全部问题，必须逐条修正后整份重新输出]：\n"
                + "\n".join(f"- {err}" for err in last_errors)
                + "\n"
                if last_errors
                else ""
            )
            user_prompt = (
                f"阶段: bible（设定包）\n"
                f"project_id: {project_id}\n"
                f"类目: {recipe.genre_id}（{recipe.display_name}，主分类={recipe.tomato_primary_category}，频道={recipe.channel}）\n"
                f"默认视角: {recipe.pov_mode} | 面板策略: {recipe.panel_policy}\n"
                f"黄金单章字数: {recipe.min_chapter_words}~{recipe.max_chapter_words}\n"
                f"样式模板ID（可直接引用）: {recipe.style_profile.id}\n"
                f"推荐安全角色名池（严禁同音或黑名单名）: {', '.join(suggested_cast)}\n"
                f"贝叶斯高权重母题候选:\n{chr(10).join(motif_lines) if motif_lines else '无历史样本，按类目最佳实践'}\n"
                f"热榜风向关键词: {', '.join(hot_keywords) if hot_keywords else '无'}\n"
                f"对标拆书换皮蓝图:\n{blueprint_block}\n"
                f"{title_blurb_block}\n\n"
                f"{common_sense_block}\n\n"
                f"{golden_three_block}\n\n"
                f"{track_instruction}\n"
                f"{brief_section}"
                f"{feedback_section}"
            )
            req = LLMRequest(
                request_id=f"plan-bible-{project_id}-{attempt}-{uuid.uuid4().hex[:6]}",
                chapter=0,
                attempt=attempt,
                purpose="plan",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.5,
                max_tokens=STAGE_BIBLE_MAX_TOKENS,
                metadata={
                    "stage": "bible",
                    "batch": 1,
                    "batches": 1,
                    "project_id": project_id,
                    "genre_id": recipe.genre_id,
                    "author_level": author_level,
                    "contract_track": contract_track,
                    "target_chapters": resolved_chapters,
                    "suggested_cast": list(suggested_cast),
                    "attempt": attempt,
                },
            )
            resp = _invoke_planner(self.provider, req)
            try:
                payload = _parse_json_payload(resp.content, "bible")
            except ProjectValidationError as exc:
                last_errors = [str(exc)]
                continue

            errors = _collect_bible_errors(
                payload,
                author_level=author_level,
                target_chapters=resolved_chapters,
                recipe=recipe,
            )
            if not errors:
                return payload, attempt
            last_errors = errors

        raise PlanningValidationError(
            f"Staged planning failed at stage 'bible' (batch 1/1) after {self.max_validation_retries} attempts: "
            + " | ".join(last_errors)
        )

    def _plan_scenes_stage(
        self,
        *,
        project_id: str,
        bible: Mapping[str, Any],
        recipe: GenreRecipe,
        author_level: str,
        resolved_chapters: int,
        brief_section: str,
        on_progress: Optional[PlanProgressCallback] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Stage 2: generate vol_01 scenes in sequential batches of SCENE_BATCH_SIZE chapters."""
        ranges: List[Tuple[int, int]] = []
        ch = 1
        while ch <= resolved_chapters:
            end_ch = min(ch + SCENE_BATCH_SIZE - 1, resolved_chapters)
            ranges.append((ch, end_ch))
            ch = end_ch + 1
        total_batches = len(ranges)

        char_ids = [
            f"{c['id']}（{c['name']}：{c['role']}）"
            for c in bible.get("characters", [])
            if isinstance(c, Mapping)
        ]
        asset_ids = [
            f"{a['id']}（{a['name']}：{a['condition']}）"
            for a in bible.get("assets", [])
            if isinstance(a, Mapping)
        ]
        style_ids = sorted(
            {
                *(
                    str(sp["id"])
                    for sp in bible.get("style_profiles", [])
                    if isinstance(sp, Mapping) and sp.get("id")
                ),
                recipe.style_profile.id,
            }
        )
        default_pov = str((bible.get("project") or {}).get("default_pov_character") or "")

        system_prompt = (
            "你是番茄爆款总策划师。当前处于【阶段2：分批章纲 scenes】。\n"
            "请严格输出单个 JSON 对象：{\"scenes\": [ ... ]}，仅包含本批要求的章号范围，不要输出 JSON 以外的解释文字。\n"
            "硬性约束：\n"
            "1. chapter 必须连续完整覆盖本批指定章号，不得缺章、重章或越界。\n"
            "2. public_title 严禁带有「第X章」「第X回」「第X节」前缀。\n"
            "3. pov_character、active_characters、active_assets、style_profile_ids 只能使用设定包给出的已有 id，且 pov_character 必须在 active_characters 中。\n"
            "4. emotion_polarity 只能取 suppress / burst / neutral / mixed，且全书（含跨批衔接）连续 suppress 绝对不超过 2 章；同一 location_id 绝对不可连续出现超过 2 章（否则触发 STATE_STALE_LOCATION 场景僵化门禁），若剧情围绕同一大区域请细分子空间（如 *_front / *_backroom / *_alley）或切换新场景。\n"
            "5. 本批要求埋设或回收的伏笔，必须在对应章节的 events 中显式体现该伏笔 id 及物理关键词。\n"
            "6. 若本批包含第 1~3 章，必须严格对齐下方【题材黄金三章节拍】：第 1、2、3 章的 beat_type 必须包含对应章节的必填节拍定位名称，cliffhanger_hook 必须非空并贴合章末断章方向，events 必须落实开篇钩子、核心冲突与爽点/反转；所有章节情节须遵守下方【题材硬常识防穿帮卡】。\n"
            "7. 四个变化数组（post_scene_asset_mutations / ledger_mutations / character_mutations / expectation_directives）本章无变化就写 []；有变化时每一项都必须照样例的字段写：ledger_mutations 的 delta 必须是 JSON 数字（如 3、-1、0.5），不能加引号、不能写「+」号或中文；due_chapter 必须是整数；status 只能取 alive / deceased / missing；expectation_directives 的 action 只能取 open / fulfill。特别红线：凡在本批到期（due_chapter <= 本批末章）的已开启期待感，必须在对应 due_chapter 或更早章节配对写出完全相同 id 的 action='fulfill' 及 evidence_keywords（严禁漏配或改名，否则下章触发 STATE_EXPECTATION_OVERDUE 拦截）。\n"
            "8. 必须严格遵循以下完整 JSON 格式样例：\n"
            f"{json.dumps(_SCENES_BATCH_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
        )

        common_sense_block = recipe.common_sense_card.format_prompt_block()

        all_scenes: List[Dict[str, Any]] = []
        max_attempts_used = 1
        bible_foreshadows = [
            fs for fs in (bible.get("foreshadows") or []) if isinstance(fs, Mapping)
        ]

        for batch_idx, (start_ch, end_ch) in enumerate(ranges, start=1):
            prior_trailing_suppress = _trailing_suppress_count(all_scenes)
            prior_trailing_loc = _trailing_location_run(all_scenes)
            prior_open_exps = _collect_open_expectations(all_scenes)
            prior_balances = _simulate_ledger_balances(
                all_scenes,
                (bible.get("project") or {}).get("initial_balances"),
            )
            batch_exp_block = _format_due_expectations_block(prior_open_exps, start_ch, end_ch)
            prior_summary_lines = [
                f"- 第{s['chapter']}章《{s.get('public_title') or s.get('internal_title')}》："
                f"{'；'.join(str(e) for e in (s.get('events') or [])[:2])}"
                for s in all_scenes
            ]
            prior_summary_block = (
                "\n".join(prior_summary_lines) if prior_summary_lines else "（本批为开篇第1批，尚无前序章节）"
            )

            batch_fs_lines: List[str] = []
            for fs in bible_foreshadows:
                p_ch = fs.get("plant_chapter")
                d_ch = fs.get("due_chapter")
                if isinstance(p_ch, int) and start_ch <= p_ch <= end_ch:
                    batch_fs_lines.append(
                        f"- [第{p_ch}章必须埋设] {fs['id']}《{fs['title']}》｜关键词={fs.get('plant_keywords', [])}｜指令={fs.get('plant_instruction', '')}"
                    )
                if isinstance(d_ch, int) and start_ch <= d_ch <= end_ch:
                    batch_fs_lines.append(
                        f"- [第{d_ch}章必须回收] {fs['id']}《{fs['title']}》｜关键词={fs.get('payoff_keywords', [])}｜指令={fs.get('payoff_instruction', '')}"
                    )
            batch_fs_block = (
                "\n".join(batch_fs_lines) if batch_fs_lines else "（本批无强制埋设/回收伏笔，专注主线推进）"
            )
            act_pos = _three_act_position(start_ch, end_ch, resolved_chapters, author_level)
            golden_three_section = (
                f"{_format_recipe_golden_three_block(recipe, chapter_range=(start_ch, end_ch))}\n\n"
                if start_ch <= 3 and recipe.golden_three_beats
                else ""
            )

            last_errors: List[str] = []
            batch_result: Optional[List[Dict[str, Any]]] = None
            for attempt in range(1, self.max_validation_retries + 1):
                if on_progress is not None:
                    on_progress(
                        {
                            "stage": "scenes",
                            "batch": batch_idx,
                            "batches": total_batches,
                            "chapter_start": start_ch,
                            "chapter_end": end_ch,
                            "attempt": attempt,
                        }
                    )
                feedback_section = (
                    "\n[上一次输出存在以下全部问题，必须逐条修正后整批重新输出]：\n"
                    + "\n".join(f"- {err}" for err in last_errors)
                    + "\n"
                    if last_errors
                    else ""
                )
                user_prompt = (
                    f"阶段: scenes（章纲第 {batch_idx}/{total_batches} 批）\n"
                    f"本批章号范围: 第 {start_ch} 章 ~ 第 {end_ch} 章（共 {end_ch - start_ch + 1} 章，全书第1卷共 {resolved_chapters} 章）\n"
                    f"全书三幕位置: {act_pos}\n"
                    f"书名: {(bible.get('project') or {}).get('title', '')}\n"
                    f"简介: {(bible.get('project') or {}).get('synopsis', '')}\n"
                    f"默认视点角色 id: {default_pov}\n"
                    f"可用角色 id 列表（严禁编造其他 id）: {'; '.join(char_ids)}\n"
                    f"可用器物 id 列表（严禁编造其他 id）: {'; '.join(asset_ids)}\n"
                    f"可用文风 id 列表（严禁编造其他 id）: {', '.join(style_ids)}\n"
                    f"单章默认字数区间: {recipe.min_chapter_words}~{recipe.max_chapter_words}\n"
                    f"前批末尾连续 suppress 章数: {prior_trailing_suppress}（与本批衔接后连续 suppress 绝对不超过 2 章）\n"
                    f"前批末尾场景 location_id: {prior_trailing_loc[0] or '无'}（已连续 {prior_trailing_loc[1]} 章；衔接后同一 location_id 连续绝对不超过 2 章）\n"
                    f"进入本批前的账本余额（任何支出后余额绝对不得小于 0，否则触发 STATE_OVERDRAFT 拦截）: {_format_balances_summary(prior_balances)}\n"
                    f"{golden_three_section}"
                    f"{common_sense_block}\n\n"
                    f"本批必须埋设/回收的伏笔:\n{batch_fs_block}\n"
                    f"本批到期必须核销的期待感承诺（严禁漏写 action='fulfill'）:\n{batch_exp_block}\n"
                    f"此前各章梗概:\n{prior_summary_block}\n"
                    f"{brief_section}"
                    f"{feedback_section}"
                )
                req = LLMRequest(
                    request_id=f"plan-scenes-{project_id}-b{batch_idx}-{attempt}-{uuid.uuid4().hex[:6]}",
                    chapter=start_ch,
                    attempt=attempt,
                    purpose="plan",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.5,
                    max_tokens=STAGE_SCENES_MAX_TOKENS,
                    metadata={
                        "stage": "scenes",
                        "batch": batch_idx,
                        "batches": total_batches,
                        "chapter_start": start_ch,
                        "chapter_end": end_ch,
                        "project_id": project_id,
                        "genre_id": recipe.genre_id,
                        "author_level": author_level,
                        "attempt": attempt,
                    },
                )
                resp = _invoke_planner(self.provider, req)
                try:
                    payload = _parse_json_payload(resp.content, f"scenes_batch_{batch_idx}")
                except ProjectValidationError as exc:
                    last_errors = [str(exc)]
                    continue

                raw_batch = payload.get("scenes")
                errors = _collect_scene_batch_errors(
                    raw_batch,
                    bible,
                    (start_ch, end_ch),
                    recipe=recipe,
                    prior_trailing_suppress=prior_trailing_suppress,
                    prior_trailing_location=prior_trailing_loc,
                    max_consecutive_same_location=self.max_consecutive_same_location,
                    prior_open_expectations=prior_open_exps,
                    prior_balances=prior_balances,
                )
                if not errors:
                    assert isinstance(raw_batch, list)
                    batch_result = sorted(
                        (dict(item) for item in raw_batch),
                        key=lambda x: int(x["chapter"]),
                    )
                    max_attempts_used = max(max_attempts_used, attempt)
                    break
                last_errors = errors

            if batch_result is None:
                raise PlanningValidationError(
                    f"Staged planning failed at stage 'scenes' (batch {batch_idx}/{total_batches}, "
                    f"chapters {start_ch}..{end_ch}) after {self.max_validation_retries} attempts: "
                    + " | ".join(last_errors)
                )
            all_scenes.extend(batch_result)

        return all_scenes, max_attempts_used

    def _plan_master_outline_stage(
        self,
        *,
        project_id: str,
        bible: Mapping[str, Any],
        scenes: Sequence[Mapping[str, Any]],
        recipe: GenreRecipe,
        brief_section: str,
        on_progress: Optional[PlanProgressCallback] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Stage 3 (lv2_plus only): generate 100-chapter master_outline in 4 batches of 25."""
        ranges: List[Tuple[int, int]] = []
        ch = 1
        while ch <= MASTER_OUTLINE_TOTAL:
            end_ch = min(ch + MASTER_OUTLINE_BATCH_SIZE - 1, MASTER_OUTLINE_TOTAL)
            ranges.append((ch, end_ch))
            ch = end_ch + 1
        total_batches = len(ranges)

        system_prompt = (
            "你是番茄爆款总策划师。当前处于【阶段3：百章总纲 master_outline】。\n"
            "请严格输出单个 JSON 对象：{\"master_outline\": [ ... ]}，仅包含本批要求的章号范围，不要输出 JSON 以外的解释文字。\n"
            "硬性约束：\n"
            "1. chapter 必须连续完整覆盖本批指定章号（共 25 条），不得缺章、重章或越界。\n"
            "2. 每条必须包含 chapter（整数）、title（非空字符串）、summary（非空字符串）。\n"
            "3. 第 1~15 章须与第1卷首秀细纲一致，第 60~80 章安排换地图与实力跃迁。\n"
            "4. 必须严格遵循以下完整 JSON 格式样例：\n"
            f"{json.dumps(_MASTER_OUTLINE_BATCH_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
        )

        vol1_lines = [
            f"- 第{s['chapter']}章《{s.get('public_title') or s.get('internal_title')}》"
            for s in scenes[:15]
        ]
        all_items: List[Dict[str, Any]] = []
        max_attempts_used = 1

        for batch_idx, (start_ch, end_ch) in enumerate(ranges, start=1):
            prior_milestones = [
                f"- 第{m['chapter']}章《{m['title']}》：{m['summary']}"
                for m in all_items[-10:]
            ]
            last_errors: List[str] = []
            batch_result: Optional[List[Dict[str, Any]]] = None

            for attempt in range(1, self.max_validation_retries + 1):
                if on_progress is not None:
                    on_progress(
                        {
                            "stage": "master_outline",
                            "batch": batch_idx,
                            "batches": total_batches,
                            "chapter_start": start_ch,
                            "chapter_end": end_ch,
                            "attempt": attempt,
                        }
                    )
                feedback_section = (
                    "\n[上一次输出存在以下全部问题，必须逐条修正后整批重新输出]：\n"
                    + "\n".join(f"- {err}" for err in last_errors)
                    + "\n"
                    if last_errors
                    else ""
                )
                user_prompt = (
                    f"阶段: master_outline（总纲第 {batch_idx}/{total_batches} 批）\n"
                    f"本批章号范围: 第 {start_ch} 章 ~ 第 {end_ch} 章（共 {end_ch - start_ch + 1} 条）\n"
                    f"书名: {(bible.get('project') or {}).get('title', '')}\n"
                    f"简介: {(bible.get('project') or {}).get('synopsis', '')}\n"
                    f"第1卷首秀15章标题参考:\n{chr(10).join(vol1_lines)}\n"
                    f"前序总纲最近节点:\n{chr(10).join(prior_milestones) if prior_milestones else '（本批为第1批）'}\n"
                    f"{brief_section}"
                    f"{feedback_section}"
                )
                req = LLMRequest(
                    request_id=f"plan-mo-{project_id}-b{batch_idx}-{attempt}-{uuid.uuid4().hex[:6]}",
                    chapter=start_ch,
                    attempt=attempt,
                    purpose="plan",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.5,
                    max_tokens=STAGE_MASTER_OUTLINE_MAX_TOKENS,
                    metadata={
                        "stage": "master_outline",
                        "batch": batch_idx,
                        "batches": total_batches,
                        "chapter_start": start_ch,
                        "chapter_end": end_ch,
                        "project_id": project_id,
                        "genre_id": recipe.genre_id,
                        "author_level": "lv2_plus",
                        "attempt": attempt,
                    },
                )
                resp = _invoke_planner(self.provider, req)
                try:
                    payload = _parse_json_payload(resp.content, f"master_outline_batch_{batch_idx}")
                except ProjectValidationError as exc:
                    last_errors = [str(exc)]
                    continue

                raw_batch = payload.get("master_outline")
                errors = _collect_master_outline_batch_errors(raw_batch, (start_ch, end_ch))
                if not errors:
                    assert isinstance(raw_batch, list)
                    batch_result = sorted(
                        (dict(item) for item in raw_batch),
                        key=lambda x: int(x["chapter"]),
                    )
                    max_attempts_used = max(max_attempts_used, attempt)
                    break
                last_errors = errors

            if batch_result is None:
                raise PlanningValidationError(
                    f"Staged planning failed at stage 'master_outline' (batch {batch_idx}/{total_batches}, "
                    f"chapters {start_ch}..{end_ch}) after {self.max_validation_retries} attempts: "
                    + " | ".join(last_errors)
                )
            all_items.extend(batch_result)

        return all_items, max_attempts_used

    def plan_new_project(
        self,
        *,
        project_id: str,
        genre_id: str,
        output_dir: str | Path,
        author_level: str = "lv0_1",
        projects_root: Optional[str | Path] = None,
        pen_name: str = "砚山行",
        hot_keywords: Sequence[str] = (),
        motif_store: Optional[MotifWeightStore] = None,
        motif_candidates: Optional[Sequence[Tuple[str, str]]] = None,
        blueprint: Optional[DeconstructedBlueprint] = None,
        benchmark_chapters: Optional[Sequence[str]] = None,
        target_chapters: Optional[int] = None,
        seed: Optional[int] = None,
        creative_brief: str = "",
        on_progress: Optional[PlanProgressCallback] = None,
    ) -> PlannedProjectResult:
        """3-source auto-initiation via staged planning (bible -> scene batches -> optional master_outline)."""
        if not isinstance(project_id, str) or not project_id.strip():
            raise ProjectValidationError("project_id must be a non-empty string")
        if author_level not in AUTHOR_LEVELS:
            raise ProjectValidationError(
                f"Unknown author_level {author_level!r}; expected one of {AUTHOR_LEVELS}"
            )
        recipe = get_genre(genre_id)
        contract_track = "complete_150k" if author_level == "lv0_1" else "serial_20k"

        resolved_chapters: int
        if author_level == "lv0_1":
            resolved_chapters = 80 if target_chapters is None else int(target_chapters)
            if not (70 <= resolved_chapters <= 95):
                raise ProjectValidationError(
                    f"lv0_1 target_chapters must be in 70..95, got {resolved_chapters}"
                )
        else:
            resolved_chapters = 15

        # Source 1: Bayesian motif weights from T09
        sampled_motifs: List[MotifWeightEntry] = []
        if motif_store is not None:
            sampled_motifs = motif_store.sample_top_motifs(
                recipe.genre_id,
                top_k=3,
                candidates=motif_candidates,
                mode="deterministic",
            )

        # Source 3: Bestseller deconstruction if benchmark chapters supplied without blueprint
        effective_blueprint = blueprint
        if effective_blueprint is None and benchmark_chapters:
            motif_hint = sampled_motifs[0].opening_motif if sampled_motifs else ""
            effective_blueprint = self.deconstructor.deconstruct(
                benchmark_chapters,
                target_genre_id=recipe.genre_id,
                hot_keywords=hot_keywords,
                motif_hint=motif_hint,
            )
        elif effective_blueprint is not None and not effective_blueprint.sanitized:
            if benchmark_chapters:
                bp_report = self.guard.check_against_benchmark(
                    effective_blueprint.to_summary_text(),
                    benchmark_chapters,
                    raise_on_violation=True,
                )
                effective_blueprint = replace(
                    effective_blueprint,
                    sanitized=True,
                    plagiarism_report=bp_report,
                )
            else:
                raise PlanningValidationError(
                    "External blueprint must have sanitized=True unless benchmark_chapters are supplied for plagiarism verification"
                )

        # Pre-generate clean cast using T03 CharacterNamer
        namer = CharacterNamer()
        suggested_cast = namer.generate_cast(
            count=5,
            era=recipe.genre_id,
            channel=recipe.channel,
            seed=seed,
        )

        motif_lines = [
            f"- {m.opening_motif} | 金手指={m.golden_finger_type} (后验胜率={m.posterior_mean:.3f})"
            for m in sampled_motifs
        ]
        blueprint_block = (
            json.dumps(effective_blueprint.to_dict(), ensure_ascii=False, indent=2)
            if effective_blueprint is not None
            else "无对标拆书蓝图（按本赛道配方与热榜关键词自主立项）"
        )

        brief_text = (creative_brief or "").strip()
        brief_section = (
            "[老板在立项向导中逐项选定的要求，必须全部严格落实；向导中出现的人物姓名只是示意，正式姓名必须从推荐安全角色名池中选取，其余主角设定（身份、性格、金手指、代价、成长线）、方向、笔风与书名必须保留]：\n"
            f"{brief_text}\n"
            if brief_text
            else ""
        )

        target_dir = Path(output_dir).resolve()
        projects_root_path = Path(projects_root).resolve() if projects_root is not None else None

        # Stage 1: Bible
        bible, bible_attempts = self._plan_bible_stage(
            project_id=project_id,
            recipe=recipe,
            author_level=author_level,
            contract_track=contract_track,
            resolved_chapters=resolved_chapters,
            suggested_cast=suggested_cast,
            motif_lines=motif_lines,
            hot_keywords=hot_keywords,
            blueprint_block=blueprint_block,
            brief_section=brief_section,
            on_progress=on_progress,
        )

        # Stage 2: Scene batches (10 chapters per batch)
        scenes, scene_attempts = self._plan_scenes_stage(
            project_id=project_id,
            bible=bible,
            recipe=recipe,
            author_level=author_level,
            resolved_chapters=resolved_chapters,
            brief_section=brief_section,
            on_progress=on_progress,
        )

        # Stage 3: Master outline batches (lv2_plus only, 4 x 25 = 100 items)
        master_outline: List[Dict[str, Any]] = []
        mo_attempts = 1
        if author_level == "lv2_plus":
            master_outline, mo_attempts = self._plan_master_outline_stage(
                project_id=project_id,
                bible=bible,
                scenes=scenes,
                recipe=recipe,
                brief_section=brief_section,
                on_progress=on_progress,
            )

        payload: Dict[str, Any] = {
            "project": bible["project"],
            "characters": bible["characters"],
            "assets": bible["assets"],
            "foreshadows": bible["foreshadows"],
            "style_profiles": bible["style_profiles"],
            "volume_cap_locks": bible["volume_cap_locks"],
            "scenes": scenes,
        }
        if author_level == "lv2_plus":
            payload["master_outline"] = master_outline

        # Final authoritative validation via untouched _validate_and_write_staging() + load_project()
        try:
            with tempfile.TemporaryDirectory(prefix="xsb_plan_") as tmpdir:
                loaded, prepared = self._validate_and_write_staging(
                    Path(tmpdir),
                    payload=payload,
                    project_id=project_id,
                    recipe=recipe,
                    author_level=author_level,
                    contract_track=contract_track,
                    pen_name=pen_name,
                    target_chapters=resolved_chapters,
                    benchmark_chapters=benchmark_chapters,
                    projects_root=projects_root_path,
                    namer=namer,
                )
        except (PlagiarismViolationError, CrossBookCollisionError):
            raise
        except (
            ProjectValidationError,
            NamerError,
            InvalidTweetAliasError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise PlanningValidationError(
                f"plan_new_project failed final staging/load_project() validation: {exc}"
            ) from exc

        # Persist validated project into output_dir (archive any existing files into _archive/)
        stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")
        target_dir.mkdir(parents=True, exist_ok=True)
        _write_text_archived(
            target_dir / "project.yaml",
            _dump_yaml(prepared["project_yaml"]),
            target_dir,
            stamp,
        )
        _write_text_archived(
            target_dir / "characters.yaml",
            _dump_yaml(prepared["characters_yaml"]),
            target_dir,
            stamp,
        )
        _write_text_archived(
            target_dir / "assets.yaml",
            _dump_yaml(prepared["assets_yaml"]),
            target_dir,
            stamp,
        )
        _write_text_archived(
            target_dir / "foreshadows.yaml",
            _dump_yaml(prepared["foreshadows_yaml"]),
            target_dir,
            stamp,
        )
        _write_text_archived(
            target_dir / "style_profiles.yaml",
            _dump_yaml(prepared["style_profiles_yaml"]),
            target_dir,
            stamp,
        )
        _write_text_archived(
            target_dir / "outline" / "vol_01.yaml",
            _dump_yaml(prepared["vol_01_yaml"]),
            target_dir,
            stamp,
        )

        master_outline_path: Optional[Path] = None
        signing_app_path: Optional[Path] = None
        if author_level == "lv2_plus":
            master_outline_path = target_dir / "master_outline.yaml"
            _write_text_archived(
                master_outline_path,
                _dump_yaml({"master_outline": prepared["master_outline"]}),
                target_dir,
                stamp,
            )
            signing_app_path = target_dir / "signing_application.md"
            signing_md = self._build_signing_application_md(
                prepared["project_yaml"],
                prepared["characters_yaml"]["characters"],
                prepared["vol_01_yaml"]["scenes"],
                prepared["master_outline"],
                recipe,
                prepared["golden_finger_description"],
            )
            _write_text_archived(signing_app_path, signing_md, target_dir, stamp)

        final_loaded = load_project(target_dir)
        attempts_used = max(bible_attempts, scene_attempts, mo_attempts)
        return PlannedProjectResult(
            project_dir=target_dir,
            project=final_loaded,
            contract_track=contract_track,
            author_level=author_level,
            opening_motif=prepared["project_yaml"]["opening_motif"],
            golden_finger_type=prepared["project_yaml"]["golden_finger_type"],
            attempts_used=attempts_used,
            signing_application_path=signing_app_path,
            master_outline_path=master_outline_path,
        )

    def extend_volume(
        self,
        project_dir: str | Path,
        *,
        snapshot: Optional[WorldStateSnapshot] = None,
        snapshot_path: Optional[str | Path] = None,
        optimization_report: Optional[OptimizationReport] = None,
        directives: Optional[Sequence[OutlineAdjustmentDirective]] = None,
        batch_size: int = 15,
        map_change_interval: int = 60,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> VolumeExtensionResult:
        """Staged rolling continuation (<=10 chapters/batch) with full error collection & retry."""
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if map_change_interval < 10:
            raise ValueError(f"map_change_interval must be >= 10, got {map_change_interval}")

        root = Path(project_dir).resolve()
        project = load_project(root)

        effective_snapshot = snapshot
        if effective_snapshot is None and snapshot_path is not None:
            sp = Path(snapshot_path)
            if not sp.is_file():
                raise PlannerError(f"Snapshot file does not exist: {sp}")
            raw_snap = json.loads(sp.read_text(encoding="utf-8"))
            effective_snapshot = WorldStateSnapshot.from_dict(raw_snap)

        all_directives: List[OutlineAdjustmentDirective] = []
        if optimization_report is not None:
            all_directives.extend(optimization_report.directives)
        if directives is not None:
            all_directives.extend(directives)
        for d in all_directives:
            assert_chapter_mutable(d.target_chapter, project.meta.max_published_chapter)

        max_scene_ch = max(s.chapter for s in project.scenes)
        snap_ch = effective_snapshot.current_chapter if effective_snapshot is not None else 0
        start_chapter = max(max_scene_ch, project.meta.max_published_chapter, snap_ch) + 1
        end_chapter = start_chapter + batch_size - 1

        current_vol = max(
            project.meta.volume_index,
            max(s.volume for s in project.scenes),
            effective_snapshot.current_volume if effective_snapshot is not None else 1,
        )
        current_vol_scenes = [s for s in project.scenes if s.volume == current_vol]
        current_vol_start = min((s.chapter for s in current_vol_scenes), default=1)
        chapters_in_vol = start_chapter - current_vol_start
        crossed_boundary = (
            (start_chapter - 1) // map_change_interval
            > (current_vol_start - 1) // map_change_interval
        )
        trigger_map_change = chapters_in_vol >= map_change_interval or crossed_boundary
        target_vol = current_vol + 1 if trigger_map_change else current_vol

        # Collect existing locks for previous volume to verify volume caps on map change
        prev_locks: Dict[str, VolumeCapLock] = {}
        for lk in project.volume_cap_locks.values():
            if lk.volume == current_vol:
                prev_locks[lk.ledger_key] = lk
        if effective_snapshot is not None:
            for lk in effective_snapshot.volume_cap_locks.values():
                if lk.volume == current_vol and lk.ledger_key not in prev_locks:
                    prev_locks[lk.ledger_key] = lk

        blueprint_scale_rules: Optional[List[ScaleRule]] = None
        bp_path = root / "blueprint.yaml"
        if bp_path.is_file():
            blueprint_scale_rules = list(load_blueprint(bp_path).scale_rules)

        old_locations = {
            s.location_id.strip() for s in project.scenes if s.location_id and s.location_id.strip()
        }
        if effective_snapshot is not None:
            old_locations.update(
                loc.strip() for loc in effective_snapshot.recent_locations if loc and loc.strip()
            )

        deceased_ids = (
            [
                cid
                for cid, cs in effective_snapshot.character_states.items()
                if cs.status == "deceased"
            ]
            if effective_snapshot is not None
            else []
        )
        directives_text = "\n".join(
            f"- [目标章>={d.target_chapter} | {d.signal_type}] {d.instruction}"
            for d in all_directives
        )

        bible_view = _project_to_bible_view(project)
        valid_ids_block = _project_valid_ids_block(project)
        recipe: Optional[GenreRecipe] = (
            get_genre(project.meta.genre_id)
            if project.meta.genre_id and project.meta.genre_id in get_genre_ids()
            else None
        )
        chapter_batches = _make_chapter_batches(start_chapter, end_chapter, SCENE_BATCH_SIZE)
        total_batches = len(chapter_batches)

        _mutation_rules_block = (
            "四个变化数组（post_scene_asset_mutations / ledger_mutations / character_mutations / expectation_directives）本章无变化就写 []；"
            "有变化时每一项都照样例字段写：ledger_mutations 的 delta 必须是 JSON 数字（如 3、-1、0.5），不能加引号、不能写「+」号或中文，且任何支出后余额严禁小于 0；"
            "due_chapter 必须是整数；status 只能取 alive / deceased / missing；expectation_directives 的 action 只能取 open / fulfill。\n"
            "【期待感配对红线（必守）】：① 下方列出的前序到期期待感，必须在对应 due_chapter 或更早章节写出完全相同 id 的 action='fulfill' 及非空 evidence_keywords；"
            "② 本批内新开（action='open'）的期待感，如果 due_chapter <= 本批末章，也必须在本批对应 due_chapter 或更早章节配对写出相同 id 的 action='fulfill' 及 evidence_keywords（若想留到下一批再回收，请把 due_chapter 设为 > 本批末章）！\n"
            "【场景防僵化红线】：同一 location_id（含与前批衔接）绝对不可连续超过 2 章，否则触发 STATE_STALE_LOCATION 门禁，请合理切换子空间或新场景。\n"
        )
        first_batch_sys = (
            "你是番茄长篇滚动续纲策划师。请严格输出单个 JSON 对象（严禁 Markdown 以外多余文字）：\n"
            "- map_name: 本批次主地图名称\n"
            "- location_id: 本批次地图标识（换地图时必须不同于旧地图）\n"
            "- soft_identity_reboot: 身份软重启/新马甲切入说明（换地图时必须非空）\n"
            "- volume_cap_locks: 数组（包含本卷更高阶数值上限锁：{ledger_key, volume, max_value, unit}）\n"
            "- scenes: 数组（仅输出本批章号范围的连续章节大纲，符合 SceneOutline 结构，"
            "所有角色/器物/文风 id 必须来自项目既有 id，public_title 严禁带 '第X章' 前缀，连续纯压抑<=2章）\n\n"
            f"{_mutation_rules_block}"
            f"JSON 模板示例（所有尖括号为占位说明）：\n"
            f"{json.dumps(_EXTEND_VOLUME_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
        )
        later_batch_sys = (
            "你是番茄长篇滚动续纲策划师。本阶段仅生成后续批次的连续章纲，请严格输出单个 JSON 对象：\n"
            "- scenes: 数组（仅输出本批章号范围的连续章节大纲，承接已锁定的本卷地图与前序章节，"
            "所有角色/器物/文风 id 必须来自项目既有 id，public_title 严禁带 '第X章' 前缀，连续纯压抑<=2章）\n\n"
            f"{_mutation_rules_block}"
            f"JSON 模板示例（所有尖括号为占位说明）：\n"
            f"{json.dumps(_SCENES_BATCH_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
        )

        new_map_name = ""
        new_loc_id = ""
        soft_reboot = ""
        new_locks: List[VolumeCapLock] = []
        normalized_new_scenes: List[Dict[str, Any]] = []
        max_attempts_used = 1

        existing_ordered = sorted(project.scenes, key=lambda sc: sc.chapter)
        trailing_suppress = _trailing_suppress_count(
            [{"emotion_polarity": sc.emotion_polarity} for sc in existing_ordered]
        )
        trailing_loc = _trailing_location_run(
            [{"location_id": sc.location_id} for sc in existing_ordered]
        )

        for batch_idx, (b_start, b_end) in enumerate(chapter_batches, start=1):
            b_count = b_end - b_start + 1
            is_first_batch = batch_idx == 1
            if on_progress is not None:
                on_progress(
                    {
                        "stage": "extend_volume",
                        "batch": batch_idx,
                        "total_batches": total_batches,
                        "start_chapter": b_start,
                        "end_chapter": b_end,
                    }
                )

            prior_summary = _format_prior_scene_summaries(existing_ordered, normalized_new_scenes)
            prior_open_exps = _collect_open_expectations(
                list(existing_ordered) + list(normalized_new_scenes),
                snapshot=effective_snapshot,
            )
            prior_balances = _simulate_ledger_balances(
                list(existing_ordered) + list(normalized_new_scenes),
                project.initial_balances,
                snapshot=effective_snapshot,
            )
            due_exp_block = _format_due_expectations_block(prior_open_exps, b_start, b_end)
            fs_source = (
                effective_snapshot.foreshadows
                if effective_snapshot is not None and effective_snapshot.foreshadows
                else project.foreshadows
            )
            due_fs_lines = [
                f"- [第{fs.due_chapter}章必须回收伏笔] {fs.id}《{fs.title}》｜必含关键词={fs.payoff_keywords}｜回收指令={fs.payoff_instruction}"
                for fs in fs_source.values()
                if fs.status != "resolved" and b_start <= fs.due_chapter <= b_end
            ]
            due_fs_block = "\n".join(due_fs_lines) if due_fs_lines else "（本批无到期必收伏笔）"
            master_outline_slice = _load_master_outline_slice(root, b_start, b_end)
            if is_first_batch:
                if trigger_map_change:
                    bounded_desc = ""
                    if blueprint_scale_rules:
                        bounded_items = [
                            f"{r.ledger_key}(hard_max={r.hard_max}{r.unit})"
                            for r in blueprint_scale_rules
                            if r.policy == "bounded_realistic"
                        ]
                        if bounded_items:
                            bounded_desc = (
                                f"；注意 bounded_realistic 账本键（{', '.join(bounded_items)}）允许持平或微调但严禁超过 hard_max，"
                                f"其余 unbounded_fantasy 账本键必须严格高于前卷上限"
                            )
                    map_instruction = (
                        f"【强制触发 60~80 章换地图与身份软重启】进入第 {target_vol} 卷！\n"
                        f"必须切换全新地图（location_id 禁止复用旧地图：{sorted(old_locations)}），"
                        f"必须设计主角身份软重启（soft_identity_reboot），并配置新卷 volume_cap_locks（"
                        f"前卷上限：{ {k: v.max_value for k, v in prev_locks.items()} }"
                        f"{bounded_desc or '，新卷必须严格更高'}）。"
                    )
                else:
                    map_instruction = f"继续第 {target_vol} 卷连载节奏。"
            else:
                map_instruction = (
                    f"沿用第 1 批已锁定本卷设定：map_name={new_map_name!r}, "
                    f"location_id={new_loc_id!r}, soft_identity_reboot={soft_reboot!r}。"
                )

            feedback = ""
            batch_passed = False
            last_error: Optional[Exception] = None
            for attempt in range(1, self.max_validation_retries + 1):
                max_attempts_used = max(max_attempts_used, attempt)
                feedback_section = (
                    f"\n[上一轮校验失败（请逐条全部修复）]：\n{feedback}\n" if feedback else ""
                )
                user_prompt = (
                    f"阶段: extend_volume（批次 {batch_idx}/{total_batches}）\n"
                    f"项目: {project.meta.project_id}《{project.meta.title}》（genre_id={project.meta.genre_id}）\n"
                    f"续写范围: 第 {b_start} 章 ～ 第 {b_end} 章（本批共 {b_count} 章，总范围 第 {start_chapter}～{end_chapter} 章，target_volume={target_vol}）\n"
                    f"{valid_ids_block}\n"
                    f"最新世界状态: current_chapter={snap_ch}, balances={effective_snapshot.numeric_balances if effective_snapshot else {}}, "
                    f"deceased_characters={deceased_ids}, forbidden_titles={effective_snapshot.get_all_forbidden_titles() if effective_snapshot else []}\n"
                    f"前序紧邻章节末尾连续纯压抑章数: {trailing_suppress}（衔接本批后连续纯压抑严禁超过 2 章）\n"
                    f"前序紧邻章节末尾场景 location_id: {trailing_loc[0] or '无'}（已连续 {trailing_loc[1]} 章；衔接后同一 location_id 严禁连续超过 2 章，否则触发 STATE_STALE_LOCATION 门禁）\n"
                    f"进入本批前的账本余额（任何支出后余额绝对不得小于 0，否则触发 STATE_OVERDRAFT 拦截）: {_format_balances_summary(prior_balances)}\n"
                    f"本批到期必须核销的期待感承诺:\n{due_exp_block}\n"
                    f"本批到期必须回收的伏笔（必须在对应章 events 落实并体现必含关键词）:\n{due_fs_block}\n"
                    f"百章总纲中本批章号主线指引（人物 id 仍须严格使用上方已有 id）:\n{master_outline_slice}\n"
                    f"前序章节梗概:\n{prior_summary}\n"
                    f"章评舆情调优指令（必须严格注入第 {start_chapter} 章及之后，严禁触碰 <={project.meta.max_published_chapter} 的已发章节）:\n"
                    f"{directives_text or '无'}\n"
                    f"{map_instruction}\n"
                    f"{feedback_section}"
                )
                req = LLMRequest(
                    request_id=f"extend-{project.meta.project_id}-v{target_vol}-b{batch_idx}-{attempt}-{uuid.uuid4().hex[:6]}",
                    chapter=b_start,
                    attempt=attempt,
                    purpose="plan",
                    system_prompt=first_batch_sys if is_first_batch else later_batch_sys,
                    user_prompt=user_prompt,
                    temperature=0.5,
                    max_tokens=STAGE_SCENES_MAX_TOKENS,
                    metadata={
                        "stage": "extend_volume",
                        "project_id": project.meta.project_id,
                        "target_volume": target_vol,
                        "start_chapter": b_start,
                        "end_chapter": b_end,
                        "total_start_chapter": start_chapter,
                        "total_end_chapter": end_chapter,
                        "batch_index": batch_idx,
                        "total_batches": total_batches,
                        "trigger_map_change": trigger_map_change,
                        "attempt": attempt,
                    },
                )
                resp = _invoke_planner(self.provider, req)
                try:
                    payload = _parse_json_payload(resp.content, f"extend_volume[{batch_idx}/{total_batches}]")
                except (ProjectValidationError, ValueError, TypeError) as exc:
                    last_error = exc
                    feedback = f"1. {exc}"
                    continue

                batch_errors: List[str] = []
                cand_map_name = new_map_name
                cand_loc_id = new_loc_id
                cand_soft_reboot = soft_reboot
                cand_locks = new_locks
                if is_first_batch:
                    meta_errs, cand_map_name, cand_loc_id, cand_soft_reboot, cand_locks = (
                        _collect_extend_volume_meta_errors(
                            payload,
                            target_vol=target_vol,
                            trigger_map_change=trigger_map_change,
                            old_locations=old_locations,
                            prev_locks=prev_locks,
                            scale_rules=blueprint_scale_rules,
                        )
                    )
                    batch_errors.extend(meta_errs)

                raw_scenes = payload.get("scenes")
                scene_errs = _collect_scene_batch_errors(
                    raw_scenes,
                    bible_view,
                    (b_start, b_end),
                    recipe=recipe,
                    prior_trailing_suppress=trailing_suppress,
                    prior_trailing_location=trailing_loc,
                    max_consecutive_same_location=self.max_consecutive_same_location,
                    prior_open_expectations=prior_open_exps,
                    prior_balances=prior_balances,
                )
                batch_errors.extend(scene_errs)

                if isinstance(raw_scenes, list):
                    for sc in raw_scenes:
                        if isinstance(sc, Mapping) and _is_strict_int(sc.get("chapter")):
                            ch_num = int(sc["chapter"])
                            if ch_num <= project.meta.max_published_chapter:
                                batch_errors.append(
                                    f"Cannot overwrite published chapter {ch_num} <= {project.meta.max_published_chapter}"
                                )

                if batch_errors:
                    feedback = "\n".join(f"{i}. {err}" for i, err in enumerate(batch_errors, start=1))
                    last_error = ProjectValidationError(feedback)
                    continue

                # Batch passed full validation; normalize scenes sorted by chapter
                if is_first_batch:
                    new_map_name = cand_map_name
                    new_loc_id = cand_loc_id
                    soft_reboot = cand_soft_reboot
                    new_locks = cand_locks

                sorted_batch = sorted(raw_scenes, key=lambda sc: int(sc["chapter"]))
                batch_normalized: List[Dict[str, Any]] = []
                for item in sorted_batch:
                    s_dict = dict(item)
                    s_dict["volume"] = target_vol
                    if trigger_map_change and not s_dict.get("location_id") and new_loc_id:
                        s_dict["location_id"] = new_loc_id
                    batch_normalized.append(s_dict)

                normalized_new_scenes.extend(batch_normalized)
                trailing_suppress = _trailing_suppress_count(
                    [{"emotion_polarity": sc.emotion_polarity} for sc in existing_ordered]
                    + normalized_new_scenes
                )
                trailing_loc = _trailing_location_run(
                    [{"location_id": sc.location_id} for sc in existing_ordered]
                    + normalized_new_scenes
                )
                batch_passed = True
                break

            if not batch_passed:
                raise PlanningValidationError(
                    f"extend_volume batch {batch_idx}/{total_batches} (chapters {b_start}..{b_end}) "
                    f"failed validation after {self.max_validation_retries} attempts:\n{feedback or last_error}"
                ) from last_error

        # Final authoritative staging validation via untouched load_project()
        try:
            with tempfile.TemporaryDirectory(prefix="xsb_ext_") as tmpdir:
                staging = Path(tmpdir)
                shutil.copytree(
                    root, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns("_archive", "*.tmp")
                )
                self._prepare_multi_volume_dir(staging)

                meta_raw = yaml.safe_load((staging / "project.yaml").read_text(encoding="utf-8"))
                meta_raw["volume_index"] = target_vol
                (staging / "project.yaml").write_text(
                    _dump_yaml(meta_raw), encoding="utf-8", newline="\n"
                )

                vol_path = staging / "outline" / f"vol_{target_vol:02d}.yaml"
                if target_vol == current_vol and vol_path.exists():
                    vol_raw = yaml.safe_load(vol_path.read_text(encoding="utf-8")) or {}
                    existing_scenes = list(vol_raw.get("scenes", []))
                    existing_scenes.extend(normalized_new_scenes)
                    vol_raw["scenes"] = existing_scenes
                    if new_locks:
                        vol_raw["volume_cap_locks"] = [lk.to_dict() for lk in new_locks]
                else:
                    vol_raw = {
                        "volume": target_vol,
                        "is_final_volume": False,
                        "map_name": new_map_name,
                        "location_id": new_loc_id,
                        "soft_identity_reboot": soft_reboot,
                        "volume_cap_locks": [lk.to_dict() for lk in new_locks],
                        "scenes": normalized_new_scenes,
                    }
                vol_path.write_text(_dump_yaml(vol_raw), encoding="utf-8", newline="\n")
                load_project(staging)
        except (ProjectValidationError, ValueError, KeyError, TypeError) as exc:
            raise PlanningValidationError(
                f"extend_volume final staging validation failed: {exc}"
            ) from exc

        # Persist validated extension into project_dir (archiving overwritten files into _archive/)
        stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")
        self._migrate_single_outline_if_needed(root, stamp)

        meta_raw = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
        meta_raw["volume_index"] = target_vol
        _write_text_archived(root / "project.yaml", _dump_yaml(meta_raw), root, stamp)

        target_vol_path = root / "outline" / f"vol_{target_vol:02d}.yaml"
        if target_vol == current_vol and target_vol_path.exists():
            vol_raw = yaml.safe_load(target_vol_path.read_text(encoding="utf-8")) or {}
            existing_scenes = list(vol_raw.get("scenes", []))
            existing_scenes.extend(normalized_new_scenes)
            vol_raw["scenes"] = existing_scenes
            if new_locks:
                vol_raw["volume_cap_locks"] = [lk.to_dict() for lk in new_locks]
        else:
            vol_raw = {
                "volume": target_vol,
                "is_final_volume": False,
                "map_name": new_map_name,
                "location_id": new_loc_id,
                "soft_identity_reboot": soft_reboot,
                "volume_cap_locks": [lk.to_dict() for lk in new_locks],
                "scenes": normalized_new_scenes,
            }
        _write_text_archived(target_vol_path, _dump_yaml(vol_raw), root, stamp)

        final_project = load_project(root)
        return VolumeExtensionResult(
            project_dir=root,
            project=final_project,
            volume_index=target_vol,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            triggered_map_change=trigger_map_change,
            new_map_name=new_map_name,
            new_map_location_id=new_loc_id,
            soft_identity_reboot=soft_reboot,
            new_volume_cap_locks=new_locks,
            volume_file_path=target_vol_path,
            attempts_used=max_attempts_used,
        )

    @staticmethod
    def _prepare_multi_volume_dir(root: Path) -> None:
        outline_file = root / "outline.yaml"
        outline_dir = root / "outline"
        if outline_file.exists() and not outline_dir.exists():
            outline_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(outline_file), str(outline_dir / "vol_01.yaml"))

    @staticmethod
    def _migrate_single_outline_if_needed(root: Path, stamp: str) -> None:
        outline_file = root / "outline.yaml"
        outline_dir = root / "outline"
        if outline_file.exists():
            outline_dir.mkdir(parents=True, exist_ok=True)
            vol1 = outline_dir / "vol_01.yaml"
            if not vol1.exists():
                content = outline_file.read_text(encoding="utf-8")
                _archive_existing_file(outline_file, root, stamp)
                vol1.write_text(content, encoding="utf-8", newline="\n")

    @staticmethod
    def _build_finale_foreshadows_list(
        fs_raw: Mapping[str, Any],
        *,
        fs_resolutions: Mapping[str, Any],
        unresolved_fs: Mapping[str, Any],
        effective_snapshot: Optional[WorldStateSnapshot],
    ) -> List[Dict[str, Any]]:
        updated_fs_list: List[Dict[str, Any]] = []
        seen_fs_ids: set[str] = set()
        snap_foreshadows = (
            effective_snapshot.foreshadows if effective_snapshot is not None else {}
        )
        for fs_item in fs_raw.get("foreshadows", []):
            item_copy = dict(fs_item)
            fid = str(item_copy["id"])
            seen_fs_ids.add(fid)
            if fid in fs_resolutions:
                item_copy["due_chapter"] = int(fs_resolutions[fid])
            if fid in snap_foreshadows:
                snap_fs = snap_foreshadows[fid]
                if snap_fs.status == "resolved":
                    item_copy["status"] = "resolved"
                    if snap_fs.planted_evidence:
                        item_copy["planted_evidence"] = snap_fs.planted_evidence
                    if snap_fs.payoff_evidence:
                        item_copy["payoff_evidence"] = snap_fs.payoff_evidence
            updated_fs_list.append(item_copy)
        for fid, fs_obj in unresolved_fs.items():
            if fid not in seen_fs_ids and fid in fs_resolutions:
                fs_copy = fs_obj.to_dict()
                fs_copy["due_chapter"] = int(fs_resolutions[fid])
                updated_fs_list.append(fs_copy)
        return updated_fs_list

    def plan_finale_volume(
        self,
        project_dir: str | Path,
        *,
        snapshot: Optional[WorldStateSnapshot] = None,
        snapshot_path: Optional[str | Path] = None,
        num_chapters: int = 8,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> FinaleVolumeResult:
        """Staged finale planning (<=10 chapters/batch) with full error collection & retry."""
        if num_chapters < 1:
            raise ValueError(f"num_chapters must be >= 1, got {num_chapters}")

        root = Path(project_dir).resolve()
        project = load_project(root)

        effective_snapshot = snapshot
        if effective_snapshot is None and snapshot_path is not None:
            sp = Path(snapshot_path)
            if not sp.is_file():
                raise PlannerError(f"Snapshot file does not exist: {sp}")
            raw_snap = json.loads(sp.read_text(encoding="utf-8"))
            effective_snapshot = WorldStateSnapshot.from_dict(raw_snap)

        # Inventory unresolved foreshadows and active expectations
        unresolved_fs: Dict[str, Any] = {}
        for fid, fs in project.foreshadows.items():
            if fs.status != "resolved":
                unresolved_fs[fid] = fs
        if effective_snapshot is not None:
            for fid, fs in effective_snapshot.foreshadows.items():
                if fs.status == "resolved":
                    unresolved_fs.pop(fid, None)
                else:
                    unresolved_fs[fid] = fs

        active_exps: Dict[str, Any] = {}
        if effective_snapshot is not None:
            for eid, exp in effective_snapshot.active_expectations.items():
                if exp.status != "fulfilled":
                    active_exps[eid] = exp

        start_chapter = max(
            max(s.chapter for s in project.scenes),
            project.meta.max_published_chapter,
            effective_snapshot.current_chapter if effective_snapshot is not None else 0,
        ) + 1
        end_chapter = start_chapter + num_chapters - 1
        finale_vol = max(
            project.meta.volume_index,
            max(s.volume for s in project.scenes),
            effective_snapshot.current_volume if effective_snapshot is not None else 1,
        ) + 1

        fs_summary = [
            {
                "id": fs.id,
                "title": fs.title,
                "plant_chapter": fs.plant_chapter,
                "current_due_chapter": fs.due_chapter,
                "payoff_instruction": fs.payoff_instruction,
                "payoff_keywords": list(fs.payoff_keywords),
            }
            for fs in unresolved_fs.values()
        ]
        exp_summary = [
            {
                "id": exp.id,
                "title": exp.title,
                "planted_chapter": exp.planted_chapter,
                "due_chapter": exp.due_chapter,
                "description": exp.description,
            }
            for exp in active_exps.values()
        ]

        bible_view = _project_to_bible_view(project)
        valid_ids_block = _project_valid_ids_block(project)
        recipe: Optional[GenreRecipe] = (
            get_genre(project.meta.genre_id)
            if project.meta.genre_id and project.meta.genre_id in get_genre_ids()
            else None
        )
        chapter_batches = _make_chapter_batches(start_chapter, end_chapter, SCENE_BATCH_SIZE)
        total_batches = len(chapter_batches)
        is_single_batch = total_batches == 1

        first_batch_sys = (
            "你是番茄长篇完结总收官策划师。请将全部未收伏笔与未兑现期待感承诺全部清账收官。\n"
            "必须严格输出单个 JSON 对象（严禁 Markdown 以外多余文字）：\n"
            "- foreshadow_resolutions: 对象 {foreshadow_id: payoff_chapter}（全部未收伏笔必须落入本终卷总章号范围内）\n"
            "- expectation_fulfillments: 对象 {expectation_id: fulfill_chapter}（全部未兑现期待感必须落入本终卷总章号范围内）\n"
            "- scenes: 数组（仅输出本批章号范围的连续终卷大纲，所有角色/器物/文风 id 必须来自项目既有 id，"
            "public_title 严禁带 '第X章' 前缀，连续纯压抑<=2章，对应章包含兑现伏笔关键词事件及 expectation_directives action='fulfill'）\n\n"
            f"JSON 模板示例（所有尖括号为占位说明）：\n"
            f"{json.dumps(_FINALE_VOLUME_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
        )
        later_batch_sys = (
            "你是番茄长篇完结总收官策划师。本阶段仅生成终卷后续批次的连续章纲，请严格输出单个 JSON 对象：\n"
            "- scenes: 数组（仅输出本批章号范围的连续终卷大纲，按已锁定的伏笔与期待感清账排期落地对应章事件，"
            "所有角色/器物/文风 id 必须来自项目既有 id，public_title 严禁带 '第X章' 前缀，连续纯压抑<=2章）\n\n"
            "四个变化数组本章无变化就写 []；有变化时每一项都照样例字段写：ledger_mutations 的 delta 必须是 JSON 数字（如 3、-1、0.5），不能加引号、不能写「+」号或中文；due_chapter 必须是整数；status 只能取 alive / deceased / missing；action 只能取 open / fulfill。\n"
            f"JSON 模板示例（所有尖括号为占位说明）：\n"
            f"{json.dumps(_SCENES_BATCH_JSON_EXAMPLE, ensure_ascii=False, indent=2)}"
        )

        fs_resolutions: Dict[str, int] = {}
        exp_fulfillments: Dict[str, int] = {}
        normalized_scenes: List[Dict[str, Any]] = []
        fulfilled_exp_in_scenes: set[str] = set()
        max_attempts_used = 1

        existing_ordered = sorted(project.scenes, key=lambda sc: sc.chapter)
        trailing_suppress = _trailing_suppress_count(
            [{"emotion_polarity": sc.emotion_polarity} for sc in existing_ordered]
        )
        trailing_loc = _trailing_location_run(
            [{"location_id": sc.location_id} for sc in existing_ordered]
        )

        for batch_idx, (b_start, b_end) in enumerate(chapter_batches, start=1):
            b_count = b_end - b_start + 1
            is_first_batch = batch_idx == 1
            if on_progress is not None:
                on_progress(
                    {
                        "stage": "plan_finale_volume",
                        "batch": batch_idx,
                        "total_batches": total_batches,
                        "start_chapter": b_start,
                        "end_chapter": b_end,
                    }
                )

            prior_summary = _format_prior_scene_summaries(existing_ordered, normalized_scenes)
            prior_open_exps = _collect_open_expectations(normalized_scenes)
            prior_balances = _simulate_ledger_balances(
                list(existing_ordered) + list(normalized_scenes),
                project.initial_balances,
                snapshot=effective_snapshot,
            )
            due_exp_block = _format_due_expectations_block(prior_open_exps, b_start, b_end)
            fs_source = (
                effective_snapshot.foreshadows
                if effective_snapshot is not None and effective_snapshot.foreshadows
                else project.foreshadows
            )
            due_fs_lines = [
                f"- [第{fs.due_chapter}章必须回收伏笔] {fs.id}《{fs.title}》｜必含关键词={fs.payoff_keywords}｜回收指令={fs.payoff_instruction}"
                for fs in fs_source.values()
                if fs.status != "resolved" and b_start <= fs.due_chapter <= b_end
            ]
            due_fs_block = "\n".join(due_fs_lines) if due_fs_lines else "（本批无到期必收伏笔）"
            master_outline_slice = _load_master_outline_slice(root, b_start, b_end)
            if is_first_batch:
                schedule_block = (
                    f"待清账未收伏笔清单:\n{json.dumps(fs_summary, ensure_ascii=False, indent=2)}\n"
                    f"待兑现期待感承诺清单:\n{json.dumps(exp_summary, ensure_ascii=False, indent=2)}"
                )
            else:
                batch_fs = [
                    {"id": fid, "payoff_chapter": ch, "title": unresolved_fs[fid].title}
                    for fid, ch in fs_resolutions.items()
                    if b_start <= ch <= b_end and fid in unresolved_fs
                ]
                batch_exp = [
                    {"id": eid, "fulfill_chapter": ch, "title": active_exps[eid].title}
                    for eid, ch in exp_fulfillments.items()
                    if b_start <= ch <= b_end and eid in active_exps
                ]
                schedule_block = (
                    f"已锁定本批回收伏笔: {json.dumps(batch_fs, ensure_ascii=False)}\n"
                    f"已锁定本批兑现期待感: {json.dumps(batch_exp, ensure_ascii=False)}"
                )

            feedback = ""
            batch_passed = False
            last_error: Optional[Exception] = None
            for attempt in range(1, self.max_validation_retries + 1):
                max_attempts_used = max(max_attempts_used, attempt)
                feedback_section = (
                    f"\n[上一轮校验失败（请逐条全部修复）]：\n{feedback}\n" if feedback else ""
                )
                user_prompt = (
                    f"阶段: plan_finale_volume（批次 {batch_idx}/{total_batches}）\n"
                    f"项目: {project.meta.project_id}《{project.meta.title}》\n"
                    f"终卷编号: 第 {finale_vol} 卷 (is_final_volume=True)\n"
                    f"终卷章号范围: 第 {start_chapter} 章 ～ 第 {end_chapter} 章（共 {num_chapters} 章）\n"
                    f"本批章号范围: 第 {b_start} 章 ～ 第 {b_end} 章（本批共 {b_count} 章）\n"
                    f"{valid_ids_block}\n"
                    f"前序紧邻章节末尾连续纯压抑章数: {trailing_suppress}（衔接本批后连续纯压抑严禁超过 2 章）\n"
                    f"前序紧邻章节末尾场景 location_id: {trailing_loc[0] or '无'}（已连续 {trailing_loc[1]} 章；衔接后同一 location_id 严禁连续超过 2 章，否则触发 STATE_STALE_LOCATION 门禁）\n"
                    f"进入本批前的账本余额（任何支出后余额绝对不得小于 0，否则触发 STATE_OVERDRAFT 拦截）: {_format_balances_summary(prior_balances)}\n"
                    f"本批到期必须核销的期待感承诺:\n{due_exp_block}\n"
                    f"本批到期必须回收的伏笔（必须在对应章 events 落实并体现必含关键词）:\n{due_fs_block}\n"
                    f"百章总纲中本批章号主线指引（人物 id 仍须严格使用上方已有 id）:\n{master_outline_slice}\n"
                    f"前序章节梗概:\n{prior_summary}\n"
                    f"{schedule_block}\n"
                    f"{feedback_section}"
                )
                req = LLMRequest(
                    request_id=f"finale-{project.meta.project_id}-v{finale_vol}-b{batch_idx}-{attempt}-{uuid.uuid4().hex[:6]}",
                    chapter=b_start,
                    attempt=attempt,
                    purpose="plan",
                    system_prompt=first_batch_sys if is_first_batch else later_batch_sys,
                    user_prompt=user_prompt,
                    temperature=0.4,
                    max_tokens=STAGE_SCENES_MAX_TOKENS,
                    metadata={
                        "stage": "plan_finale_volume",
                        "project_id": project.meta.project_id,
                        "finale_volume": finale_vol,
                        "start_chapter": b_start,
                        "end_chapter": b_end,
                        "total_start_chapter": start_chapter,
                        "total_end_chapter": end_chapter,
                        "batch_index": batch_idx,
                        "total_batches": total_batches,
                        "unresolved_foreshadow_ids": list(unresolved_fs.keys()),
                        "active_expectation_ids": list(active_exps.keys()),
                        "attempt": attempt,
                    },
                )
                resp = _invoke_planner(self.provider, req)
                try:
                    payload = _parse_json_payload(
                        resp.content, f"plan_finale_volume[{batch_idx}/{total_batches}]"
                    )
                except (ProjectValidationError, ValueError, TypeError) as exc:
                    last_error = exc
                    feedback = f"1. {exc}"
                    continue

                batch_errors: List[str] = []
                raw_scenes = payload.get("scenes")
                cand_fs_res = fs_resolutions
                cand_exp_res = exp_fulfillments
                if is_first_batch:
                    res_errs, cand_fs_res, cand_exp_res = _collect_finale_resolution_errors(
                        payload,
                        start_chapter=start_chapter,
                        end_chapter=end_chapter,
                        unresolved_fs=unresolved_fs,
                        active_exps=active_exps,
                        raw_scenes=raw_scenes,
                        is_single_batch=is_single_batch,
                    )
                    batch_errors.extend(res_errs)

                scene_errs = _collect_scene_batch_errors(
                    raw_scenes,
                    bible_view,
                    (b_start, b_end),
                    recipe=recipe,
                    prior_trailing_suppress=trailing_suppress,
                    prior_trailing_location=trailing_loc,
                    max_consecutive_same_location=self.max_consecutive_same_location,
                    prior_open_expectations=prior_open_exps,
                    prior_balances=prior_balances,
                )
                batch_errors.extend(scene_errs)

                if batch_errors:
                    feedback = "\n".join(f"{i}. {err}" for i, err in enumerate(batch_errors, start=1))
                    last_error = ProjectValidationError(feedback)
                    continue

                if is_first_batch:
                    fs_resolutions = cand_fs_res
                    exp_fulfillments = cand_exp_res

                sorted_batch = sorted(raw_scenes, key=lambda sc: int(sc["chapter"]))
                batch_normalized: List[Dict[str, Any]] = []
                for item in sorted_batch:
                    s_dict = dict(item)
                    s_dict["volume"] = finale_vol
                    for ed in s_dict.get("expectation_directives") or []:
                        if isinstance(ed, Mapping) and ed.get("action") == "fulfill" and ed.get("id"):
                            fulfilled_exp_in_scenes.add(str(ed["id"]))
                    batch_normalized.append(s_dict)

                normalized_scenes.extend(batch_normalized)
                trailing_suppress = _trailing_suppress_count(
                    [{"emotion_polarity": sc.emotion_polarity} for sc in existing_ordered]
                    + normalized_scenes
                )
                trailing_loc = _trailing_location_run(
                    [{"location_id": sc.location_id} for sc in existing_ordered]
                    + normalized_scenes
                )
                batch_passed = True
                break

            if not batch_passed:
                raise PlanningValidationError(
                    f"plan_finale_volume batch {batch_idx}/{total_batches} (chapters {b_start}..{b_end}) "
                    f"failed validation after {self.max_validation_retries} attempts:\n{feedback or last_error}"
                ) from last_error

        # Attach fulfill directives for any mapped active expectations not yet inlined in scenes
        for eid, f_ch in exp_fulfillments.items():
            if eid in active_exps and eid not in fulfilled_exp_in_scenes:
                target_scene = normalized_scenes[f_ch - start_chapter]
                directives_list = list(target_scene.get("expectation_directives") or [])
                directives_list.append(
                    {
                        "id": eid,
                        "title": active_exps[eid].title,
                        "due_chapter": f_ch,
                        "description": active_exps[eid].description,
                        "action": "fulfill",
                        "evidence_keywords": [active_exps[eid].title[:4] or "兑现"],
                    }
                )
                target_scene["expectation_directives"] = directives_list
                fulfilled_exp_in_scenes.add(eid)

        # Validate full project in staging with is_final_volume=True
        try:
            with tempfile.TemporaryDirectory(prefix="xsb_fin_") as tmpdir:
                staging = Path(tmpdir)
                shutil.copytree(
                    root, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns("_archive", "*.tmp")
                )
                self._prepare_multi_volume_dir(staging)

                meta_raw = yaml.safe_load((staging / "project.yaml").read_text(encoding="utf-8"))
                meta_raw["volume_index"] = finale_vol
                meta_raw["is_final_volume"] = True
                (staging / "project.yaml").write_text(
                    _dump_yaml(meta_raw), encoding="utf-8", newline="\n"
                )

                fs_raw = yaml.safe_load((staging / "foreshadows.yaml").read_text(encoding="utf-8")) or {}
                updated_fs_list = self._build_finale_foreshadows_list(
                    fs_raw,
                    fs_resolutions=fs_resolutions,
                    unresolved_fs=unresolved_fs,
                    effective_snapshot=effective_snapshot,
                )
                (staging / "foreshadows.yaml").write_text(
                    _dump_yaml({"foreshadows": updated_fs_list}),
                    encoding="utf-8",
                    newline="\n",
                )

                finale_vol_data = {
                    "volume": finale_vol,
                    "is_final_volume": True,
                    "scenes": normalized_scenes,
                }
                (staging / "outline" / f"vol_{finale_vol:02d}.yaml").write_text(
                    _dump_yaml(finale_vol_data),
                    encoding="utf-8",
                    newline="\n",
                )

                load_project(staging, is_final_volume=True)
        except (ProjectValidationError, ValueError, KeyError, TypeError) as exc:
            raise PlanningValidationError(
                f"plan_finale_volume final staging validation failed: {exc}"
            ) from exc

        # Persist validated finale volume into project_dir
        stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")
        self._migrate_single_outline_if_needed(root, stamp)

        meta_raw = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
        meta_raw["volume_index"] = finale_vol
        meta_raw["is_final_volume"] = True
        _write_text_archived(root / "project.yaml", _dump_yaml(meta_raw), root, stamp)

        fs_raw = yaml.safe_load((root / "foreshadows.yaml").read_text(encoding="utf-8")) or {}
        updated_fs_list = self._build_finale_foreshadows_list(
            fs_raw,
            fs_resolutions=fs_resolutions,
            unresolved_fs=unresolved_fs,
            effective_snapshot=effective_snapshot,
        )
        _write_text_archived(
            root / "foreshadows.yaml",
            _dump_yaml({"foreshadows": updated_fs_list}),
            root,
            stamp,
        )

        finale_vol_path = root / "outline" / f"vol_{finale_vol:02d}.yaml"
        finale_vol_data = {
            "volume": finale_vol,
            "is_final_volume": True,
            "scenes": normalized_scenes,
        }
        _write_text_archived(finale_vol_path, _dump_yaml(finale_vol_data), root, stamp)

        final_project = load_project(root, is_final_volume=True)
        return FinaleVolumeResult(
            project_dir=root,
            project=final_project,
            volume_index=finale_vol,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            resolved_foreshadow_ids=sorted(unresolved_fs.keys()),
            fulfilled_expectation_ids=sorted(active_exps.keys()),
            volume_file_path=finale_vol_path,
            attempts_used=max_attempts_used,
        )

    def expand_blueprint_chapters(
        self,
        project_dir: str | Path,
        *,
        blueprint_path: Optional[str | Path] = None,
        start_chapter: int,
        end_chapter: int,
        max_retries: Optional[int] = None,
        snapshot_path: Optional[str | Path] = None,
    ) -> Any:
        """Delegate blueprint-driven chapter expansion using this planner's provider and retry budget."""
        from .blueprint_planner import expand_blueprint_chapters as _expand_blueprint_chapters

        return _expand_blueprint_chapters(
            project_dir=project_dir,
            blueprint_path=blueprint_path,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            provider=self.provider,
            max_retries=self.max_validation_retries if max_retries is None else max_retries,
            snapshot_path=snapshot_path,
        )


def __getattr__(name: str) -> Any:
    if name in (
        "BlueprintExpansionResult",
        "collect_blueprint_scene_errors",
        "expand_blueprint_chapters",
    ):
        from . import blueprint_planner

        return getattr(blueprint_planner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PlannerError",
    "PlanningValidationError",
    "PlannedProjectResult",
    "VolumeExtensionResult",
    "FinaleVolumeResult",
    "AutoNovelPlanner",
    "BlueprintExpansionResult",
    "collect_blueprint_scene_errors",
    "expand_blueprint_chapters",
    "find_stale_location_runs",
    "find_unfulfilled_expectations",
]
