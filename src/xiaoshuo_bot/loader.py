from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import yaml

from .models import (
    AssetMutation,
    AssetState,
    CharacterAnchor,
    ForeshadowItem,
    NovelProject,
    ProjectMeta,
    SceneOutline,
    StyleProfile,
)


class ProjectValidationError(ValueError):
    """Raised when novel project configuration fails referential integrity checks."""


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        raise ProjectValidationError(f"Missing required project file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_project(project_dir: str | Path) -> NovelProject:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise ProjectValidationError(f"Project directory does not exist: {root}")

    meta_raw = _load_yaml(root / "project.yaml")
    chars_raw = _load_yaml(root / "characters.yaml")
    assets_raw = _load_yaml(root / "assets.yaml")
    foreshadows_raw = _load_yaml(root / "foreshadows.yaml")
    styles_raw = _load_yaml(root / "style_profiles.yaml")
    outline_raw = _load_yaml(root / "outline.yaml")

    meta = ProjectMeta(
        project_id=str(meta_raw["project_id"]),
        title=str(meta_raw["title"]),
        genre=str(meta_raw.get("genre", "现实悬疑")),
        synopsis=str(meta_raw.get("synopsis", "")),
        default_pov_character=str(meta_raw["default_pov_character"]),
        max_rewrite_retries=int(meta_raw.get("max_rewrite_retries", 3)),
    )

    characters: Dict[str, CharacterAnchor] = {}
    for item in chars_raw.get("characters", []):
        c = CharacterAnchor(
            id=str(item["id"]),
            name=str(item["name"]),
            role=str(item["role"]),
            personality=str(item["personality"]),
            speech_style=str(item["speech_style"]),
            behavioral_anchors=dict(item.get("behavioral_anchors", {})),
            forbidden_ooc=list(item.get("forbidden_ooc", [])),
        )
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

    foreshadows: Dict[str, ForeshadowItem] = {}
    for item in foreshadows_raw.get("foreshadows", []):
        fs = ForeshadowItem(
            id=str(item["id"]),
            title=str(item["title"]),
            plant_chapter=int(item["plant_chapter"]),
            due_chapter=int(item["due_chapter"]),
            plant_instruction=str(item["plant_instruction"]),
            payoff_instruction=str(item["payoff_instruction"]),
            plant_keywords=list(item.get("plant_keywords", [])),
            payoff_keywords=list(item.get("payoff_keywords", [])),
            status=str(item.get("status", "pending")),
        )
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

    scenes: List[SceneOutline] = []
    for item in outline_raw.get("scenes", []):
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
        scene = SceneOutline(
            chapter=int(item["chapter"]),
            scene_id=str(item["scene_id"]),
            internal_title=str(item["internal_title"]),
            pov_character=str(item.get("pov_character", meta.default_pov_character)),
            active_characters=list(item["active_characters"]),
            active_assets=list(item["active_assets"]),
            style_profile_ids=list(item["style_profile_ids"]),
            location_time=str(item["location_time"]),
            events=list(item["events"]),
            stop_condition=str(item["stop_condition"]),
            min_chars=int(item.get("min_chars", 1600)),
            max_chars=int(item.get("max_chars", 4500)),
            post_scene_asset_mutations=mutations,
        )
        scenes.append(scene)

    scenes.sort(key=lambda s: s.chapter)
    project = NovelProject(
        meta=meta,
        characters=characters,
        assets=assets,
        foreshadows=foreshadows,
        style_profiles=style_profiles,
        scenes=scenes,
    )
    validate_project(project)
    return project


def validate_project(project: NovelProject) -> None:
    if not project.scenes:
        raise ProjectValidationError("Project must contain at least one scene in outline.yaml")

    if project.meta.default_pov_character not in project.characters:
        raise ProjectValidationError(
            f"default_pov_character '{project.meta.default_pov_character}' not found in characters.yaml"
        )

    chapter_numbers = {s.chapter for s in project.scenes}
    if len(chapter_numbers) != len(project.scenes):
        raise ProjectValidationError("Duplicate chapter numbers found in outline.yaml")

    for s in project.scenes:
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

    for fs in project.foreshadows.values():
        if fs.plant_chapter not in chapter_numbers:
            raise ProjectValidationError(
                f"Foreshadow '{fs.id}': plant_chapter {fs.plant_chapter} does not exist in outline"
            )
        if fs.due_chapter not in chapter_numbers:
            raise ProjectValidationError(
                f"Foreshadow '{fs.id}': due_chapter {fs.due_chapter} does not exist in outline"
            )
        if fs.due_chapter < fs.plant_chapter:
            raise ProjectValidationError(
                f"Foreshadow '{fs.id}': due_chapter ({fs.due_chapter}) < plant_chapter ({fs.plant_chapter})"
            )
