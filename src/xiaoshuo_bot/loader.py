from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
import yaml

from .models import (
    AUTHOR_LEVELS,
    CONTRACT_TRACKS,
    EMOTION_POLARITIES,
    PANEL_POLICIES,
    POV_MODES,
    AssetMutation,
    AssetState,
    CharacterAnchor,
    CharacterMutation,
    ExpectationDirective,
    ForeshadowItem,
    LedgerMutation,
    NovelProject,
    ProjectMeta,
    ProjectValidationError,
    SceneOutline,
    StyleProfile,
    VolumeCapLock,
)


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ProjectValidationError(f"Missing required project file: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProjectValidationError(f"YAML root must be a mapping in {path}")
    return data


def _require_key(raw: Dict[str, Any], key: str, source: str) -> Any:
    if key not in raw or raw[key] is None:
        raise ProjectValidationError(f"{source}: missing required field '{key}'")
    return raw[key]


def _require_enum(
    raw: Dict[str, Any],
    key: str,
    allowed: Tuple[str, ...],
    default: str,
    source: str,
) -> str:
    if key in raw:
        val = raw[key]
        if not isinstance(val, str) or val not in allowed:
            raise ProjectValidationError(
                f"{source}: unknown {key} {val!r}; expected one of {allowed}"
            )
        return val
    return default


def _volume_sort_key(path: Path) -> Tuple[int, str]:
    m = re.search(r"vol_(\d+)", path.stem)
    return (int(m.group(1)) if m else 0, path.name)


def _parse_scene(item: Dict[str, Any], default_pov: str, default_volume: int = 1) -> SceneOutline:
    chapter = int(_require_key(item, "chapter", "outline scene"))
    scene_id = str(_require_key(item, "scene_id", f"Chapter {chapter}"))

    emotion_polarity = _require_enum(
        item,
        "emotion_polarity",
        EMOTION_POLARITIES,
        "neutral",
        f"Chapter {chapter}",
    )

    mutations = [
        AssetMutation(
            asset_id=str(m["asset_id"]),
            location=m.get("location"),
            holder=m.get("holder"),
            condition=m.get("condition"),
            note=str(m.get("note", "")),
        )
        for m in item.get("post_scene_asset_mutations", [])
    ]

    ledger_mutations = [
        LedgerMutation.from_dict(lm)
        for lm in item.get("ledger_mutations", [])
    ]
    character_mutations = [
        CharacterMutation.from_dict(cm)
        for cm in item.get("character_mutations", [])
    ]
    expectation_directives = [
        ExpectationDirective.from_dict(ed)
        for ed in item.get("expectation_directives", [])
    ]

    return SceneOutline(
        chapter=chapter,
        scene_id=scene_id,
        internal_title=str(_require_key(item, "internal_title", f"Chapter {chapter}")),
        pov_character=str(item.get("pov_character", default_pov)),
        active_characters=list(item.get("active_characters", [])),
        active_assets=list(item.get("active_assets", [])),
        style_profile_ids=list(item.get("style_profile_ids", [])),
        location_time=str(_require_key(item, "location_time", f"Chapter {chapter}")),
        events=list(item.get("events", [])),
        stop_condition=str(_require_key(item, "stop_condition", f"Chapter {chapter}")),
        min_chars=int(item.get("min_chars", 1600)),
        max_chars=int(item.get("max_chars", 4500)),
        post_scene_asset_mutations=mutations,
        public_title=str(item.get("public_title", "")),
        beat_type=str(item.get("beat_type", "")),
        emotion_polarity=emotion_polarity,
        cliffhanger_hook=str(item.get("cliffhanger_hook", "")),
        segment_comment_cue=str(item.get("segment_comment_cue", "")),
        locked=bool(item.get("locked", False)),
        volume=int(item.get("volume", default_volume)),
        location_id=str(item.get("location_id", "")),
        calendar_days_elapsed=int(item.get("calendar_days_elapsed", 1)),
        ledger_mutations=ledger_mutations,
        character_mutations=character_mutations,
        expectation_directives=expectation_directives,
    )


def load_project(
    project_dir: str | Path,
    *,
    is_final_volume: Optional[bool] = None,
) -> NovelProject:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise ProjectValidationError(f"Project directory does not exist: {root}")

    meta_raw = _load_yaml(root / "project.yaml")
    chars_raw = _load_yaml(root / "characters.yaml")
    assets_raw = _load_yaml(root / "assets.yaml")
    foreshadows_raw = _load_yaml(root / "foreshadows.yaml")
    styles_raw = _load_yaml(root / "style_profiles.yaml")

    contract_track = _require_enum(
        meta_raw, "contract_track", CONTRACT_TRACKS, "legacy", "project.yaml"
    )
    author_level = _require_enum(
        meta_raw, "author_level", AUTHOR_LEVELS, "lv0_1", "project.yaml"
    )
    pov_mode = _require_enum(
        meta_raw, "pov_mode", POV_MODES, "third_limited", "project.yaml"
    )
    panel_policy = _require_enum(
        meta_raw, "panel_policy", PANEL_POLICIES, "forbid", "project.yaml"
    )

    meta_is_final = bool(meta_raw.get("is_final_volume", False))

    # Load single-file outline.yaml or multi-volume outline/vol_*.yaml
    outline_file = root / "outline.yaml"
    outline_dir = root / "outline"
    vol_files = (
        sorted(outline_dir.glob("vol_*.yaml"), key=_volume_sort_key)
        if outline_dir.is_dir()
        else []
    )

    if outline_file.exists() and vol_files:
        raise ProjectValidationError(
            f"Ambiguous outline layout in {root}: both 'outline.yaml' and 'outline/vol_*.yaml' exist"
        )

    default_pov = str(_require_key(meta_raw, "default_pov_character", "project.yaml"))
    scenes: List[SceneOutline] = []
    outline_final_flag = False
    volume_cap_locks: Dict[str, VolumeCapLock] = {}

    if outline_file.exists():
        outline_raw = _load_yaml(outline_file)
        if "is_final_volume" in outline_raw:
            outline_final_flag = bool(outline_raw["is_final_volume"])
        default_vol = int(outline_raw.get("volume", meta_raw.get("volume_index", 1)))
        for item in outline_raw.get("scenes", []):
            scenes.append(_parse_scene(item, default_pov=default_pov, default_volume=default_vol))
        for lock_item in outline_raw.get("volume_cap_locks", []):
            vlock = VolumeCapLock.from_dict(lock_item)
            volume_cap_locks[f"{vlock.volume}:{vlock.ledger_key}"] = vlock
    elif vol_files:
        for idx, vf in enumerate(vol_files, start=1):
            vol_raw = _load_yaml(vf)
            vol_match = re.search(r"vol_(\d+)", vf.stem)
            inferred_vol = int(vol_match.group(1)) if vol_match else idx
            vol_num = int(vol_raw.get("volume", inferred_vol))
            if idx == len(vol_files) and "is_final_volume" in vol_raw:
                outline_final_flag = bool(vol_raw["is_final_volume"])
            for item in vol_raw.get("scenes", []):
                scenes.append(_parse_scene(item, default_pov=default_pov, default_volume=vol_num))
            for lock_item in vol_raw.get("volume_cap_locks", []):
                vlock = VolumeCapLock.from_dict(lock_item)
                volume_cap_locks[f"{vlock.volume}:{vlock.ledger_key}"] = vlock
    else:
        raise ProjectValidationError(
            f"Missing required outline.yaml or outline/vol_*.yaml in {root}"
        )

    effective_final_volume = (
        bool(is_final_volume)
        if is_final_volume is not None
        else (meta_is_final or outline_final_flag)
    )

    meta = ProjectMeta(
        project_id=str(_require_key(meta_raw, "project_id", "project.yaml")),
        title=str(_require_key(meta_raw, "title", "project.yaml")),
        genre=str(meta_raw.get("genre", "现实悬疑")),
        synopsis=str(meta_raw.get("synopsis", "")),
        default_pov_character=default_pov,
        max_rewrite_retries=int(meta_raw.get("max_rewrite_retries", 3)),
        author_level=author_level,
        contract_track=contract_track,
        pov_mode=pov_mode,
        panel_policy=panel_policy,
        genre_id=str(meta_raw.get("genre_id", "")),
        pen_name=str(meta_raw.get("pen_name", "")),
        author_name=str(meta_raw.get("author_name", "")),
        book_id=str(meta_raw.get("book_id", "")),
        primary_category=str(meta_raw.get("primary_category", "")),
        tags=list(meta_raw.get("tags", [])),
        alternate_titles=list(meta_raw.get("alternate_titles", [])),
        alternate_blurbs=list(meta_raw.get("alternate_blurbs", [])),
        tweet_aliases=list(meta_raw.get("tweet_aliases", [])),
        target_word_count=int(meta_raw.get("target_word_count", 0)),
        signed=bool(meta_raw.get("signed", False)),
        max_published_chapter=int(meta_raw.get("max_published_chapter", 0)),
        volume_index=int(meta_raw.get("volume_index", 1)),
        is_final_volume=effective_final_volume,
    )

    characters: Dict[str, CharacterAnchor] = {}
    for item in chars_raw.get("characters", []):
        c = CharacterAnchor.from_dict(item)
        characters[c.id] = c

    assets: Dict[str, AssetState] = {}
    for item in assets_raw.get("assets", []):
        a = AssetState(
            id=str(item["id"]),
            name=str(item["name"]),
            location=str(item["location"]),
            holder=str(item["holder"]),
            condition=str(item["condition"]),
            physical_details=str(item["physical_details"]),
            last_updated_chapter=int(item.get("last_updated_chapter", 0)),
            history=list(item.get("history", ["初始建档"])),
        )
        assets[a.id] = a

    initial_balances: Dict[str, float] = {
        str(k): float(v)
        for k, v in dict(
            meta_raw.get("initial_balances", assets_raw.get("initial_balances", {}))
        ).items()
    }
    for lock_item in meta_raw.get("volume_cap_locks", assets_raw.get("volume_cap_locks", [])):
        vlock = VolumeCapLock.from_dict(lock_item)
        volume_cap_locks[f"{vlock.volume}:{vlock.ledger_key}"] = vlock

    foreshadows: Dict[str, ForeshadowItem] = {}
    for item in foreshadows_raw.get("foreshadows", []):
        if "cross_volume" in item and not isinstance(item["cross_volume"], bool):
            raise ProjectValidationError(
                f"Foreshadow '{item.get('id', '?')}': cross_volume must be a boolean"
            )
        fs = ForeshadowItem.from_dict(item)
        foreshadows[fs.id] = fs

    style_profiles: Dict[str, StyleProfile] = {}
    for item in styles_raw.get("style_profiles", []):
        sp = StyleProfile(
            id=str(item["id"]),
            name=str(item["name"]),
            granularity_notes=str(item["granularity_notes"]),
            sample_text=str(item["sample_text"]).strip(),
        )
        style_profiles[sp.id] = sp

    scenes.sort(key=lambda s: s.chapter)
    project = NovelProject(
        meta=meta,
        characters=characters,
        assets=assets,
        foreshadows=foreshadows,
        style_profiles=style_profiles,
        scenes=scenes,
        initial_balances=initial_balances,
        volume_cap_locks=volume_cap_locks,
    )
    validate_project(project)
    return project


def _validate_tomato_title(title: str, label: str = "title") -> None:
    if "《" in title or "》" in title:
        raise ProjectValidationError(
            f"Tomato {label} must not contain book title marks '《》': {title!r}"
        )
    stripped = title.strip()
    if not stripped or len(title) > 15:
        raise ProjectValidationError(
            f"Tomato {label} length must be 1..15 characters (got {len(title)}): {title!r}"
        )


def validate_project(project: NovelProject) -> None:
    # Re-validate enums in case caller mutated meta after construction
    if project.meta.author_level not in AUTHOR_LEVELS:
        raise ProjectValidationError(
            f"Unknown author_level {project.meta.author_level!r}; expected one of {AUTHOR_LEVELS}"
        )
    if project.meta.contract_track not in CONTRACT_TRACKS:
        raise ProjectValidationError(
            f"Unknown contract_track {project.meta.contract_track!r}; expected one of {CONTRACT_TRACKS}"
        )
    if project.meta.pov_mode not in POV_MODES:
        raise ProjectValidationError(
            f"Unknown pov_mode {project.meta.pov_mode!r}; expected one of {POV_MODES}"
        )
    if project.meta.panel_policy not in PANEL_POLICIES:
        raise ProjectValidationError(
            f"Unknown panel_policy {project.meta.panel_policy!r}; expected one of {PANEL_POLICIES}"
        )

    if not project.scenes:
        raise ProjectValidationError("Project must contain at least one scene in outline")

    if project.meta.default_pov_character not in project.characters:
        raise ProjectValidationError(
            f"default_pov_character '{project.meta.default_pov_character}' not found in characters.yaml"
        )

    chapter_numbers = {s.chapter for s in project.scenes}
    if len(chapter_numbers) != len(project.scenes):
        raise ProjectValidationError("Duplicate chapter numbers found in outline")

    sorted_scenes = sorted(project.scenes, key=lambda s: s.chapter)

    if project.meta.contract_track != "legacy":
        _validate_tomato_title(project.meta.title, "title")
        for idx, alt_title in enumerate(project.meta.alternate_titles):
            _validate_tomato_title(alt_title, f"alternate_titles[{idx}]")

        consecutive_suppress = 0
        suppress_run: List[int] = []
        for s in sorted_scenes:
            if s.emotion_polarity not in EMOTION_POLARITIES:
                raise ProjectValidationError(
                    f"Chapter {s.chapter}: unknown emotion_polarity {s.emotion_polarity!r}; "
                    f"expected one of {EMOTION_POLARITIES}"
                )
            if s.emotion_polarity == "suppress":
                consecutive_suppress += 1
                suppress_run.append(s.chapter)
                if consecutive_suppress > 2:
                    raise ProjectValidationError(
                        f"Consecutive pure-suppression chapters exceed max 2: chapters {suppress_run}"
                    )
            else:
                consecutive_suppress = 0
                suppress_run.clear()

    for s in sorted_scenes:
        if s.pov_character not in project.characters:
            raise ProjectValidationError(
                f"Chapter {s.chapter}: pov_character '{s.pov_character}' missing from characters.yaml"
            )
        if s.pov_character not in s.active_characters:
            raise ProjectValidationError(
                f"Chapter {s.chapter}: pov_character '{s.pov_character}' must be listed in active_characters"
            )
        for cid in s.active_characters:
            if cid not in project.characters:
                raise ProjectValidationError(
                    f"Chapter {s.chapter}: unknown character '{cid}' in active_characters"
                )
        for aid in s.active_assets:
            if aid not in project.assets:
                raise ProjectValidationError(
                    f"Chapter {s.chapter}: unknown asset '{aid}' in active_assets"
                )
        for sp_id in s.style_profile_ids:
            if sp_id not in project.style_profiles:
                raise ProjectValidationError(
                    f"Chapter {s.chapter}: unknown style_profile '{sp_id}'"
                )
        for m in s.post_scene_asset_mutations:
            if m.asset_id not in project.assets:
                raise ProjectValidationError(
                    f"Chapter {s.chapter}: mutation references unknown asset '{m.asset_id}'"
                )
        for cm in s.character_mutations:
            if cm.character_id not in project.characters:
                raise ProjectValidationError(
                    f"Chapter {s.chapter}: character_mutation references unknown character '{cm.character_id}'"
                )
            if cm.relation_target_id and cm.relation_target_id not in project.characters:
                raise ProjectValidationError(
                    f"Chapter {s.chapter}: character_mutation references unknown relation_target_id "
                    f"'{cm.relation_target_id}'"
                )

    for fs in project.foreshadows.values():
        if fs.status == "pending" and not [k for k in fs.plant_keywords if str(k).strip()]:
            raise ProjectValidationError(
                f"Foreshadow '{fs.id}': plant_keywords must contain at least one non-empty keyword "
                f"(required as physical planting evidence)"
            )
        if fs.status != "resolved" and not [k for k in fs.payoff_keywords if str(k).strip()]:
            raise ProjectValidationError(
                f"Foreshadow '{fs.id}': payoff_keywords must contain at least one non-empty keyword "
                f"(required as physical payoff evidence)"
            )
        if fs.due_chapter < fs.plant_chapter:
            raise ProjectValidationError(
                f"Foreshadow '{fs.id}': due_chapter ({fs.due_chapter}) < plant_chapter ({fs.plant_chapter})"
            )
        if fs.plant_chapter not in chapter_numbers:
            if not (fs.cross_volume and fs.status in ("planted", "resolved")):
                raise ProjectValidationError(
                    f"Foreshadow '{fs.id}': plant_chapter {fs.plant_chapter} does not exist in outline"
                )
        if fs.status != "resolved" and fs.due_chapter not in chapter_numbers:
            if fs.cross_volume and not project.meta.is_final_volume:
                continue
            if project.meta.is_final_volume:
                raise ProjectValidationError(
                    f"Final volume forbids unresolved foreshadow '{fs.id}' ({fs.title}) "
                    f"with due_chapter {fs.due_chapter} outside outline"
                )
            raise ProjectValidationError(
                f"Foreshadow '{fs.id}': due_chapter {fs.due_chapter} does not exist in outline"
            )
