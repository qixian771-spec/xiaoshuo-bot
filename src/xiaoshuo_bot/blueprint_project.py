from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import shutil
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
import yaml

from .blueprint import (
    BlueprintValidationError,
    BookBlueprint,
    VoiceCard,
    load_blueprint,
    validate_blueprint,
)
from .loader import load_project
from .models import (
    AUTHOR_LEVELS,
    CHARACTER_STATUSES,
    CONTRACT_TRACKS,
    PANEL_POLICIES,
    POV_MODES,
    SHANGHAI_TZ,
)


_REQUIRED_PROJECT_META_STR_FIELDS: Tuple[str, ...] = (
    "project_id",
    "title",
    "genre",
    "synopsis",
    "default_pov_character",
    "author_level",
    "contract_track",
    "pov_mode",
    "panel_policy",
    "genre_id",
    "pen_name",
)

_PROJECT_META_ENUM_CONSTRAINTS: Dict[str, Tuple[str, ...]] = {
    "author_level": AUTHOR_LEVELS,
    "contract_track": CONTRACT_TRACKS,
    "pov_mode": POV_MODES,
    "panel_policy": PANEL_POLICIES,
}

_OPTIONAL_PROJECT_META_FIELDS: Tuple[str, ...] = (
    "tags",
    "target_word_count",
    "initial_balances",
    "volume_cap_locks",
    "author_name",
    "book_id",
    "primary_category",
    "alternate_titles",
    "alternate_blurbs",
    "tweet_aliases",
    "signed",
    "max_published_chapter",
    "volume_index",
    "is_final_volume",
)

_VALID_FORESHADOW_STATUSES: Tuple[str, ...] = ("pending", "planted", "resolved")


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_non_empty_str(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
) -> Optional[str]:
    if key not in mapping:
        errors.append(f"{path}: missing required field")
        return None
    val = mapping[key]
    if not isinstance(val, str) or not val.strip():
        errors.append(f"{path}: expected non-empty str, got {type(val).__name__}")
        return None
    return val


def _require_str_present(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
) -> Optional[str]:
    if key not in mapping:
        errors.append(f"{path}: missing required field")
        return None
    val = mapping[key]
    if not isinstance(val, str):
        errors.append(f"{path}: expected str, got {type(val).__name__}")
        return None
    return val


def _require_strict_int(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
    *,
    min_value: int = 0,
) -> Optional[int]:
    if key not in mapping:
        errors.append(f"{path}: missing required field")
        return None
    val = mapping[key]
    if not _is_strict_int(val):
        errors.append(f"{path}: expected int, got {type(val).__name__}")
        return None
    if val < min_value:
        errors.append(f"{path}: must be >= {min_value}, got {val}")
        return None
    return int(val)


def _validate_str_list(
    value: Any,
    path: str,
    errors: List[str],
    *,
    allow_empty_items: bool = False,
) -> Optional[List[str]]:
    if not isinstance(value, list):
        errors.append(f"{path}: expected list, got {type(value).__name__}")
        return None
    out: List[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or (not allow_empty_items and not item.strip()):
            desc = "str" if allow_empty_items else "non-empty str"
            errors.append(f"{path}[{idx}]: expected {desc}, got {type(item).__name__}")
        else:
            out.append(item)
    return out


def _validate_str_mapping(
    value: Any,
    path: str,
    errors: List[str],
    *,
    allow_empty_values: bool = False,
) -> Optional[Dict[str, str]]:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: expected mapping, got {type(value).__name__}")
        return None
    out: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            errors.append(f"{path}: mapping keys must be non-empty str, got {k!r}")
            continue
        if not isinstance(v, str) or (not allow_empty_values and not v.strip()):
            desc = "str" if allow_empty_values else "non-empty str"
            errors.append(f"{path}.{k}: expected {desc}, got {type(v).__name__}")
            continue
        out[k] = v
    return out


def _validate_initial_balances(
    value: Any,
    path: str,
    errors: List[str],
) -> Optional[Dict[str, float | int]]:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: expected mapping, got {type(value).__name__}")
        return None
    out: Dict[str, float | int] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            errors.append(f"{path}: balance key must be non-empty str, got {k!r}")
            continue
        if not _is_strict_finite_number(v):
            errors.append(f"{path}.{k}: expected finite number, got {type(v).__name__}")
            continue
        out[k] = v if _is_strict_int(v) else float(v)
    return out


def _validate_volume_cap_locks(
    value: Any,
    path: str,
    errors: List[str],
) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(value, list):
        errors.append(f"{path}: expected list, got {type(value).__name__}")
        return None
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(value):
        item_path = f"{path}[{idx}]"
        if not isinstance(item, Mapping):
            errors.append(f"{item_path}: expected mapping, got {type(item).__name__}")
            continue
        ledger_key = _require_non_empty_str(item, "ledger_key", f"{item_path}.ledger_key", errors)
        volume = _require_strict_int(item, "volume", f"{item_path}.volume", errors, min_value=1)
        if "max_value" not in item:
            errors.append(f"{item_path}.max_value: missing required field")
            max_value = None
        elif not _is_strict_finite_number(item["max_value"]):
            errors.append(
                f"{item_path}.max_value: expected finite number, got {type(item['max_value']).__name__}"
            )
            max_value = None
        else:
            raw_max = item["max_value"]
            max_value = raw_max if _is_strict_int(raw_max) else float(raw_max)

        entry: Dict[str, Any] = {}
        if ledger_key is not None and volume is not None and max_value is not None:
            entry = {
                "ledger_key": ledger_key,
                "volume": volume,
                "max_value": max_value,
            }
        if "unlocked" in item:
            if not isinstance(item["unlocked"], bool):
                errors.append(
                    f"{item_path}.unlocked: expected bool, got {type(item['unlocked']).__name__}"
                )
            elif entry:
                entry["unlocked"] = item["unlocked"]
        if "unit" in item:
            if not isinstance(item["unit"], str):
                errors.append(
                    f"{item_path}.unit: expected str, got {type(item['unit']).__name__}"
                )
            elif entry:
                entry["unit"] = item["unit"]
        if entry:
            out.append(entry)
    return out


def format_character_speech_style(voice_card: VoiceCard) -> str:
    """Format a CastMember's VoiceCard into CharacterAnchor.speech_style deterministically."""
    patterns = voice_card.speech_patterns.strip()
    phrases = [p.strip() for p in voice_card.catchphrases if p.strip()]
    if phrases:
        return f"{patterns}；口头禅：{'、'.join(phrases)}"
    return f"{patterns}；口头禅：无"


def _load_extras_mapping(extras_path: Path) -> Mapping[str, Any]:
    if not extras_path.is_file():
        raise BlueprintValidationError(f"Extras file does not exist: {extras_path}")
    try:
        raw_text = extras_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise BlueprintValidationError(f"Invalid YAML in extras {extras_path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise BlueprintValidationError(
            f"Extras YAML root must be a mapping in {extras_path}, got {type(data).__name__}"
        )
    return data


def _validate_project_meta(
    raw_meta: Mapping[str, Any],
    bp: BookBlueprint,
    errors: List[str],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    for field in _REQUIRED_PROJECT_META_STR_FIELDS:
        val = _require_non_empty_str(raw_meta, field, f"extras.project_meta.{field}", errors)
        if val is not None:
            if field in _PROJECT_META_ENUM_CONSTRAINTS:
                allowed = _PROJECT_META_ENUM_CONSTRAINTS[field]
                if val not in allowed:
                    errors.append(
                        f"extras.project_meta.{field}: unknown value {val!r}; expected one of {allowed}"
                    )
                    continue
            out[field] = val

    retries = _require_strict_int(
        raw_meta,
        "max_rewrite_retries",
        "extras.project_meta.max_rewrite_retries",
        errors,
        min_value=0,
    )
    if retries is not None:
        out["max_rewrite_retries"] = retries

    if "genre_id" in out and out["genre_id"] != bp.style.genre_id:
        errors.append(
            f"extras.project_meta.genre_id: {out['genre_id']!r} does not match "
            f"blueprint style.genre_id {bp.style.genre_id!r}"
        )

    for opt_field in _OPTIONAL_PROJECT_META_FIELDS:
        if opt_field not in raw_meta:
            continue
        val = raw_meta[opt_field]
        path = f"extras.project_meta.{opt_field}"
        if opt_field in ("tags", "alternate_titles", "alternate_blurbs", "tweet_aliases"):
            parsed_list = _validate_str_list(val, path, errors, allow_empty_items=False)
            if parsed_list is not None:
                out[opt_field] = parsed_list
        elif opt_field in ("target_word_count", "max_published_chapter"):
            if not _is_strict_int(val) or val < 0:
                errors.append(f"{path}: expected non-negative int, got {val!r}")
            else:
                out[opt_field] = int(val)
        elif opt_field == "volume_index":
            if not _is_strict_int(val) or val < 1:
                errors.append(f"{path}: expected int >= 1, got {val!r}")
            else:
                out[opt_field] = int(val)
        elif opt_field in ("signed", "is_final_volume"):
            if not isinstance(val, bool):
                errors.append(f"{path}: expected bool, got {type(val).__name__}")
            else:
                out[opt_field] = val
        elif opt_field in ("author_name", "book_id", "primary_category"):
            if not isinstance(val, str):
                errors.append(f"{path}: expected str, got {type(val).__name__}")
            else:
                out[opt_field] = val
        elif opt_field == "initial_balances":
            parsed_bal = _validate_initial_balances(val, path, errors)
            if parsed_bal is not None:
                out[opt_field] = parsed_bal
        elif opt_field == "volume_cap_locks":
            parsed_locks = _validate_volume_cap_locks(val, path, errors)
            if parsed_locks is not None:
                out[opt_field] = parsed_locks

    return out


def _validate_characters(
    raw_chars: Mapping[str, Any],
    bp: BookBlueprint,
    errors: List[str],
) -> List[Dict[str, Any]]:
    cast_ids = [member.id for member in bp.cast]
    cast_id_set = set(cast_ids)
    extra_keys = set(raw_chars.keys())

    missing_ids = [cid for cid in cast_ids if cid not in extra_keys]
    if missing_ids:
        errors.append(
            f"extras.characters: missing entries for blueprint cast ids: {missing_ids}"
        )

    unexpected_ids = sorted(str(k) for k in extra_keys if k not in cast_id_set)
    if unexpected_ids:
        errors.append(
            f"extras.characters: unexpected character ids not in blueprint cast: {unexpected_ids}"
        )

    characters_out: List[Dict[str, Any]] = []
    for member in bp.cast:
        if member.id not in raw_chars:
            continue
        c_path = f"extras.characters.{member.id}"
        c_raw = raw_chars[member.id]
        if not isinstance(c_raw, Mapping):
            errors.append(f"{c_path}: expected mapping, got {type(c_raw).__name__}")
            continue

        personality = _require_non_empty_str(
            c_raw, "personality", f"{c_path}.personality", errors
        )

        if "behavioral_anchors" not in c_raw:
            errors.append(f"{c_path}.behavioral_anchors: missing required field")
            behavioral_anchors = None
        else:
            behavioral_anchors = _validate_str_mapping(
                c_raw["behavioral_anchors"],
                f"{c_path}.behavioral_anchors",
                errors,
                allow_empty_values=False,
            )

        if "forbidden_ooc" not in c_raw:
            errors.append(f"{c_path}.forbidden_ooc: missing required field")
            forbidden_ooc = None
        else:
            forbidden_ooc = _validate_str_list(
                c_raw["forbidden_ooc"],
                f"{c_path}.forbidden_ooc",
                errors,
                allow_empty_items=False,
            )

        initial_status = _require_non_empty_str(
            c_raw, "initial_status", f"{c_path}.initial_status", errors
        )
        if initial_status is not None and initial_status not in CHARACTER_STATUSES:
            errors.append(
                f"{c_path}.initial_status: unknown status {initial_status!r}; "
                f"expected one of {CHARACTER_STATUSES}"
            )

        faction = _require_str_present(c_raw, "faction", f"{c_path}.faction", errors)

        if "relations" not in c_raw:
            errors.append(f"{c_path}.relations: missing required field")
            relations = None
        else:
            relations = _validate_str_mapping(
                c_raw["relations"],
                f"{c_path}.relations",
                errors,
                allow_empty_values=False,
            )

        entry: Dict[str, Any] = {
            "id": member.id,
            "name": member.name,
            "role": member.role,
            "personality": personality or "",
            "speech_style": format_character_speech_style(member.voice_card),
            "behavioral_anchors": behavioral_anchors if behavioral_anchors is not None else {},
            "forbidden_ooc": forbidden_ooc if forbidden_ooc is not None else [],
            "initial_status": initial_status or "",
            "faction": faction if faction is not None else "",
            "relations": relations if relations is not None else {},
        }

        if "forbidden_titles" in c_raw:
            ft_list = _validate_str_list(
                c_raw["forbidden_titles"],
                f"{c_path}.forbidden_titles",
                errors,
                allow_empty_items=False,
            )
            if ft_list is not None:
                entry["forbidden_titles"] = ft_list

        characters_out.append(entry)

    return characters_out


def _validate_assets(raw_assets: Any, errors: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(raw_assets, list):
        errors.append(f"extras.assets: expected list, got {type(raw_assets).__name__}")
        return []

    seen_ids: set[str] = set()
    assets_out: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_assets):
        path = f"extras.assets[{idx}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path}: expected mapping, got {type(item).__name__}")
            continue
        aid = _require_non_empty_str(item, "id", f"{path}.id", errors)
        if aid is not None:
            if aid in seen_ids:
                errors.append(f"{path}.id: duplicate asset id {aid!r}")
            else:
                seen_ids.add(aid)
        name = _require_non_empty_str(item, "name", f"{path}.name", errors)
        location = _require_non_empty_str(item, "location", f"{path}.location", errors)
        holder = _require_non_empty_str(item, "holder", f"{path}.holder", errors)
        condition = _require_non_empty_str(item, "condition", f"{path}.condition", errors)
        physical_details = _require_non_empty_str(
            item, "physical_details", f"{path}.physical_details", errors
        )

        entry: Dict[str, Any] = {
            "id": aid or "",
            "name": name or "",
            "location": location or "",
            "holder": holder or "",
            "condition": condition or "",
            "physical_details": physical_details or "",
        }
        if "last_updated_chapter" in item:
            luc = _require_strict_int(
                item,
                "last_updated_chapter",
                f"{path}.last_updated_chapter",
                errors,
                min_value=0,
            )
            if luc is not None:
                entry["last_updated_chapter"] = luc
        if "history" in item:
            hist = _validate_str_list(
                item["history"], f"{path}.history", errors, allow_empty_items=False
            )
            if hist is not None:
                entry["history"] = hist

        assets_out.append(entry)

    return assets_out


def _validate_foreshadows(raw_foreshadows: Any, errors: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(raw_foreshadows, list):
        errors.append(
            f"extras.foreshadows: expected list, got {type(raw_foreshadows).__name__}"
        )
        return []

    seen_ids: set[str] = set()
    foreshadows_out: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_foreshadows):
        path = f"extras.foreshadows[{idx}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path}: expected mapping, got {type(item).__name__}")
            continue
        fid = _require_non_empty_str(item, "id", f"{path}.id", errors)
        if fid is not None:
            if fid in seen_ids:
                errors.append(f"{path}.id: duplicate foreshadow id {fid!r}")
            else:
                seen_ids.add(fid)
        title = _require_non_empty_str(item, "title", f"{path}.title", errors)
        plant_ch = _require_strict_int(
            item, "plant_chapter", f"{path}.plant_chapter", errors, min_value=1
        )
        due_ch = _require_strict_int(
            item, "due_chapter", f"{path}.due_chapter", errors, min_value=1
        )
        plant_inst = _require_non_empty_str(
            item, "plant_instruction", f"{path}.plant_instruction", errors
        )
        payoff_inst = _require_non_empty_str(
            item, "payoff_instruction", f"{path}.payoff_instruction", errors
        )

        entry: Dict[str, Any] = {
            "id": fid or "",
            "title": title or "",
            "plant_chapter": plant_ch or 0,
            "due_chapter": due_ch or 0,
            "plant_instruction": plant_inst or "",
            "payoff_instruction": payoff_inst or "",
        }
        for kw_key in ("plant_keywords", "payoff_keywords"):
            if kw_key in item:
                kws = _validate_str_list(
                    item[kw_key], f"{path}.{kw_key}", errors, allow_empty_items=False
                )
                if kws is not None:
                    entry[kw_key] = kws
        if "status" in item:
            st = _require_non_empty_str(item, "status", f"{path}.status", errors)
            if st is not None:
                if st not in _VALID_FORESHADOW_STATUSES:
                    errors.append(
                        f"{path}.status: unknown status {st!r}; "
                        f"expected one of {_VALID_FORESHADOW_STATUSES}"
                    )
                else:
                    entry["status"] = st
        for ev_key in ("planted_evidence", "payoff_evidence"):
            if ev_key in item:
                if not isinstance(item[ev_key], str):
                    errors.append(
                        f"{path}.{ev_key}: expected str, got {type(item[ev_key]).__name__}"
                    )
                else:
                    entry[ev_key] = item[ev_key]
        if "cross_volume" in item:
            if not isinstance(item["cross_volume"], bool):
                errors.append(
                    f"{path}.cross_volume: expected bool, got {type(item['cross_volume']).__name__}"
                )
            else:
                entry["cross_volume"] = item["cross_volume"]

        foreshadows_out.append(entry)

    return foreshadows_out


def _dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _quarantine_failed_outputs(out_root: Path, written_paths: List[Path]) -> None:
    """Move all written files in out_root into _failed_<timestamp> without deleting anything."""
    if not written_paths:
        return
    stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d_%H%M%S_%f")
    failed_dir = out_root / f"_failed_{stamp}"
    while failed_dir.exists():
        stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d_%H%M%S_%f")
        failed_dir = out_root / f"_failed_{stamp}"
    failed_dir.mkdir(parents=True, exist_ok=True)
    for file_path in written_paths:
        if file_path.exists():
            shutil.move(str(file_path), str(failed_dir / file_path.name))


def materialize_project(
    *,
    blueprint_path: str | Path,
    extras_path: str | Path,
    outline_path: str | Path,
    out_dir: str | Path,
    existing_cast_names: Iterable[str],
) -> Path:
    """Materialize a BookBlueprint + extras + outline into a validated NovelProject directory."""
    out_root = Path(out_dir).resolve()
    if out_root.exists() or out_root.is_symlink():
        if not out_root.is_dir():
            raise FileExistsError(
                f"Output path exists and is not a directory: {out_root}"
            )
        if any(out_root.iterdir()):
            raise FileExistsError(
                f"Output directory already exists and is non-empty: {out_root}"
            )

    bp_file = Path(blueprint_path)
    bp = load_blueprint(bp_file)
    validate_blueprint(bp, existing_cast_names=existing_cast_names)

    extras_file = Path(extras_path)
    extras = _load_extras_mapping(extras_file)

    errors: List[str] = []

    if "project_meta" not in extras or not isinstance(extras["project_meta"], Mapping):
        errors.append("extras.project_meta: missing or non-mapping required section")
        project_yaml_data: Dict[str, Any] = {}
    else:
        project_yaml_data = _validate_project_meta(extras["project_meta"], bp, errors)

    if "characters" not in extras or not isinstance(extras["characters"], Mapping):
        errors.append("extras.characters: missing or non-mapping required section")
        characters_list: List[Dict[str, Any]] = []
    else:
        characters_list = _validate_characters(extras["characters"], bp, errors)

    if "assets" not in extras:
        errors.append("extras.assets: missing required field")
        assets_list: List[Dict[str, Any]] = []
    else:
        assets_list = _validate_assets(extras["assets"], errors)

    if "foreshadows" not in extras:
        errors.append("extras.foreshadows: missing required field")
        foreshadows_list: List[Dict[str, Any]] = []
    else:
        foreshadows_list = _validate_foreshadows(extras["foreshadows"], errors)

    assets_yaml_data: Dict[str, Any] = {"assets": assets_list}
    if "initial_balances" in extras:
        top_bal = _validate_initial_balances(
            extras["initial_balances"], "extras.initial_balances", errors
        )
        if top_bal is not None:
            assets_yaml_data["initial_balances"] = top_bal
    if "volume_cap_locks" in extras:
        top_locks = _validate_volume_cap_locks(
            extras["volume_cap_locks"], "extras.volume_cap_locks", errors
        )
        if top_locks is not None:
            assets_yaml_data["volume_cap_locks"] = top_locks

    ol_file = Path(outline_path)
    if not ol_file.is_file():
        errors.append(f"outline_path: file does not exist: {ol_file}")
    else:
        try:
            ol_text = ol_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"outline_path: failed to decode UTF-8 in {ol_file}: {exc}")
        else:
            if not ol_text.strip():
                errors.append(f"outline_path: file is empty: {ol_file}")

    if errors:
        raise BlueprintValidationError(errors)

    style_profiles_yaml_data: Dict[str, Any] = {
        "style_profiles": [
            {
                "id": "book_style",
                "name": project_yaml_data["title"],
                "granularity_notes": bp.style.narration_rules,
                "sample_text": bp.style.sample_text,
            }
        ]
    }

    out_root.mkdir(parents=True, exist_ok=True)
    written_paths: List[Path] = []
    try:
        bp_dst = out_root / "blueprint.yaml"
        written_paths.append(bp_dst)
        shutil.copyfile(bp_file, bp_dst)

        yaml_outputs: List[Tuple[str, Dict[str, Any]]] = [
            ("project.yaml", project_yaml_data),
            ("characters.yaml", {"characters": characters_list}),
            ("assets.yaml", assets_yaml_data),
            ("foreshadows.yaml", {"foreshadows": foreshadows_list}),
            ("style_profiles.yaml", style_profiles_yaml_data),
        ]
        for filename, payload in yaml_outputs:
            dst = out_root / filename
            written_paths.append(dst)
            dst.write_text(_dump_yaml(payload), encoding="utf-8", newline="\n")

        outline_dst = out_root / "outline.yaml"
        written_paths.append(outline_dst)
        shutil.copyfile(ol_file, outline_dst)

        load_project(out_root)
    except Exception:
        _quarantine_failed_outputs(out_root, written_paths)
        raise

    return out_root


__all__ = [
    "BlueprintValidationError",
    "format_character_speech_style",
    "materialize_project",
]
