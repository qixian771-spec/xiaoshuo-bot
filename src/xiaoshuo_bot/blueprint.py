from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
import yaml

from .genres import UnknownGenreError, get_genre
from .models import ProjectValidationError
from .namer import CharacterNamer, NamerError, to_toneless_pinyin


SCALE_POLICIES: Tuple[str, ...] = ("bounded_realistic", "unbounded_fantasy")
MIN_VOLUMES = 3
MAX_VOLUMES = 12
MIN_CAST_COUNT = 2
MIN_SELLING_POINTS = 1
MIN_SELLING_POINT_KEYWORDS = 3
MIN_BENCHMARK_REFS = 1
MIN_SCALE_RULES = 1
MAX_CATCHPHRASES = 1
MIN_COOL_POINT_MODES = 1
MIN_VOLUME_KEY_SCENES = 2
MAX_TOTAL_CHARS_DEVIATION = 0.15


class BlueprintValidationError(ProjectValidationError):
    """Raised when a BookBlueprint fails schema or validation checks."""

    def __init__(self, errors: Sequence[str] | str) -> None:
        if isinstance(errors, str):
            error_list = [errors]
        else:
            error_list = [str(e) for e in errors if str(e).strip()]
        if not error_list:
            raise ValueError("BlueprintValidationError requires at least one error message")
        self.errors: Tuple[str, ...] = tuple(error_list)
        if len(self.errors) == 1:
            message = f"BookBlueprint validation failed: {self.errors[0]}"
        else:
            bullets = "\n".join(f"  - {err}" for err in self.errors)
            message = f"BookBlueprint validation failed ({len(self.errors)} errors):\n{bullets}"
        super().__init__(message)


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_key(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
) -> Tuple[bool, Any]:
    if key not in mapping:
        errors.append(f"{path}: missing required field")
        return False, None
    return True, mapping[key]


def _parse_str(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
) -> str:
    present, val = _require_key(mapping, key, path, errors)
    if not present:
        return ""
    if not isinstance(val, str):
        errors.append(f"{path}: expected str, got {type(val).__name__}")
        return ""
    return val


def _parse_bool(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
) -> bool:
    present, val = _require_key(mapping, key, path, errors)
    if not present:
        return False
    if not isinstance(val, bool):
        errors.append(f"{path}: expected bool, got {type(val).__name__}")
        return False
    return val


def _parse_int(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
    *,
    nullable: bool = False,
) -> Optional[int]:
    present, val = _require_key(mapping, key, path, errors)
    if not present:
        return None
    if val is None:
        if nullable:
            return None
        errors.append(f"{path}: expected int, got NoneType")
        return None
    if not _is_strict_int(val):
        errors.append(f"{path}: expected int, got {type(val).__name__}")
        return None
    return int(val)


def _parse_number(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
    *,
    nullable: bool = False,
) -> Optional[float | int]:
    present, val = _require_key(mapping, key, path, errors)
    if not present:
        return None
    if val is None:
        if nullable:
            return None
        errors.append(f"{path}: expected number, got NoneType")
        return None
    if not _is_strict_finite_number(val):
        errors.append(f"{path}: expected finite number, got {type(val).__name__}")
        return None
    return val if isinstance(val, int) else float(val)


def _parse_str_list(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
) -> List[str]:
    present, val = _require_key(mapping, key, path, errors)
    if not present:
        return []
    if not isinstance(val, list):
        errors.append(f"{path}: expected list, got {type(val).__name__}")
        return []
    out: List[str] = []
    for idx, item in enumerate(val):
        if not isinstance(item, str):
            errors.append(f"{path}[{idx}]: expected str, got {type(item).__name__}")
        else:
            out.append(item)
    return out


def _parse_mapping_field(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
    *,
    nullable: bool = False,
) -> Optional[Mapping[str, Any]]:
    present, val = _require_key(mapping, key, path, errors)
    if not present:
        return None
    if val is None:
        if nullable:
            return None
        errors.append(f"{path}: expected mapping, got NoneType")
        return None
    if not isinstance(val, Mapping):
        errors.append(f"{path}: expected mapping, got {type(val).__name__}")
        return None
    return val


def _parse_list_field(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    errors: List[str],
) -> Optional[List[Any]]:
    present, val = _require_key(mapping, key, path, errors)
    if not present:
        return None
    if not isinstance(val, list):
        errors.append(f"{path}: expected list, got {type(val).__name__}")
        return None
    return val


@dataclass
class BenchmarkRef:
    """Reference benchmark title and rationale for premise positioning."""

    title: str
    why: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> BenchmarkRef:
        return cls(
            title=_parse_str(data, "title", f"{path}.title", errors),
            why=_parse_str(data, "why", f"{path}.why", errors),
        )


@dataclass
class PremiseSpec:
    """Core book premise, selling points, drift-detection keywords, and reader positioning."""

    logline: str
    selling_points: List[str]
    selling_point_keywords: List[str]
    benchmark_refs: List[BenchmarkRef]
    target_readers: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logline": self.logline,
            "selling_points": list(self.selling_points),
            "selling_point_keywords": list(self.selling_point_keywords),
            "benchmark_refs": [b.to_dict() for b in self.benchmark_refs],
            "target_readers": self.target_readers,
        }

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> PremiseSpec:
        refs_raw = _parse_list_field(data, "benchmark_refs", f"{path}.benchmark_refs", errors)
        refs: List[BenchmarkRef] = []
        if refs_raw is not None:
            for idx, item in enumerate(refs_raw):
                item_path = f"{path}.benchmark_refs[{idx}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{item_path}: expected mapping, got {type(item).__name__}")
                else:
                    refs.append(BenchmarkRef._from_mapping(item, item_path, errors))
        return cls(
            logline=_parse_str(data, "logline", f"{path}.logline", errors),
            selling_points=_parse_str_list(data, "selling_points", f"{path}.selling_points", errors),
            selling_point_keywords=_parse_str_list(
                data, "selling_point_keywords", f"{path}.selling_point_keywords", errors
            ),
            benchmark_refs=refs,
            target_readers=_parse_str(data, "target_readers", f"{path}.target_readers", errors),
        )


@dataclass
class VoiceCard:
    """Speech style descriptor and optional single catchphrase for a character."""

    speech_patterns: str
    catchphrases: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> VoiceCard:
        return cls(
            speech_patterns=_parse_str(data, "speech_patterns", f"{path}.speech_patterns", errors),
            catchphrases=_parse_str_list(data, "catchphrases", f"{path}.catchphrases", errors),
        )


@dataclass
class ProtagonistSpec:
    """Protagonist motivation, flaw, growth arc, and voice profile."""

    character_id: str
    desire: str
    flaw: str
    arc: str
    voice_card: VoiceCard

    def to_dict(self) -> Dict[str, Any]:
        return {
            "character_id": self.character_id,
            "desire": self.desire,
            "flaw": self.flaw,
            "arc": self.arc,
            "voice_card": self.voice_card.to_dict(),
        }

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> ProtagonistSpec:
        vc_raw = _parse_mapping_field(data, "voice_card", f"{path}.voice_card", errors)
        voice_card = (
            VoiceCard._from_mapping(vc_raw, f"{path}.voice_card", errors)
            if vc_raw is not None
            else VoiceCard(speech_patterns="", catchphrases=[])
        )
        return cls(
            character_id=_parse_str(data, "character_id", f"{path}.character_id", errors),
            desire=_parse_str(data, "desire", f"{path}.desire", errors),
            flaw=_parse_str(data, "flaw", f"{path}.flaw", errors),
            arc=_parse_str(data, "arc", f"{path}.arc", errors),
            voice_card=voice_card,
        )


@dataclass
class GoldenFingerUpgrade:
    """Per-volume golden finger unlock milestone."""

    volume: int
    unlock: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> GoldenFingerUpgrade:
        vol = _parse_int(data, "volume", f"{path}.volume", errors)
        return cls(
            volume=vol if vol is not None else 0,
            unlock=_parse_str(data, "unlock", f"{path}.unlock", errors),
        )


@dataclass
class GoldenFingerSpec:
    """Core golden finger rules, cost, limits, and volume upgrade trajectory."""

    rules: str
    cost: str
    limits: str
    upgrade_path: List[GoldenFingerUpgrade]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rules": self.rules,
            "cost": self.cost,
            "limits": self.limits,
            "upgrade_path": [u.to_dict() for u in self.upgrade_path],
        }

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> GoldenFingerSpec:
        up_raw = _parse_list_field(data, "upgrade_path", f"{path}.upgrade_path", errors)
        upgrades: List[GoldenFingerUpgrade] = []
        if up_raw is not None:
            for idx, item in enumerate(up_raw):
                item_path = f"{path}.upgrade_path[{idx}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{item_path}: expected mapping, got {type(item).__name__}")
                else:
                    upgrades.append(GoldenFingerUpgrade._from_mapping(item, item_path, errors))
        return cls(
            rules=_parse_str(data, "rules", f"{path}.rules", errors),
            cost=_parse_str(data, "cost", f"{path}.cost", errors),
            limits=_parse_str(data, "limits", f"{path}.limits", errors),
            upgrade_path=upgrades,
        )


@dataclass
class ScaleRule:
    """Numerical ledger boundary rule distinguishing realistic caps from fantasy progression."""

    ledger_key: str
    policy: str
    hard_max: Optional[float | int]
    unit: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> ScaleRule:
        return cls(
            ledger_key=_parse_str(data, "ledger_key", f"{path}.ledger_key", errors),
            policy=_parse_str(data, "policy", f"{path}.policy", errors),
            hard_max=_parse_number(data, "hard_max", f"{path}.hard_max", errors, nullable=True),
            unit=_parse_str(data, "unit", f"{path}.unit", errors),
        )


@dataclass
class VolumeExit:
    """Character exit record within a volume."""

    id: str
    how: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> VolumeExit:
        return cls(
            id=_parse_str(data, "id", f"{path}.id", errors),
            how=_parse_str(data, "how", f"{path}.how", errors),
        )


@dataclass
class VolumeMap:
    """Primary map/location transition for a volume."""

    location_id: str
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> VolumeMap:
        return cls(
            location_id=_parse_str(data, "location_id", f"{path}.location_id", errors),
            name=_parse_str(data, "name", f"{path}.name", errors),
        )


@dataclass
class VolumeSpec:
    """Single volume card specifying chapter span, antagonist, beats, cast flow, and foreshadowing."""

    index: int
    title: str
    chapter_start: int
    chapter_end: int
    protagonist_goal: str
    antagonist_id: str
    cool_point_modes: List[str]
    climax: str
    payoff: str
    enters: List[str]
    exits: List[VolumeExit]
    map: Optional[VolumeMap]
    key_scenes: List[str]
    foreshadows_plant: List[str]
    foreshadows_payoff: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "chapter_start": self.chapter_start,
            "chapter_end": self.chapter_end,
            "protagonist_goal": self.protagonist_goal,
            "antagonist_id": self.antagonist_id,
            "cool_point_modes": list(self.cool_point_modes),
            "climax": self.climax,
            "payoff": self.payoff,
            "enters": list(self.enters),
            "exits": [e.to_dict() for e in self.exits],
            "map": self.map.to_dict() if self.map is not None else None,
            "key_scenes": list(self.key_scenes),
            "foreshadows_plant": list(self.foreshadows_plant),
            "foreshadows_payoff": list(self.foreshadows_payoff),
        }

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> VolumeSpec:
        idx_val = _parse_int(data, "index", f"{path}.index", errors)
        ch_start = _parse_int(data, "chapter_start", f"{path}.chapter_start", errors)
        ch_end = _parse_int(data, "chapter_end", f"{path}.chapter_end", errors)

        exits_raw = _parse_list_field(data, "exits", f"{path}.exits", errors)
        exits: List[VolumeExit] = []
        if exits_raw is not None:
            for j, item in enumerate(exits_raw):
                ex_path = f"{path}.exits[{j}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{ex_path}: expected mapping, got {type(item).__name__}")
                else:
                    exits.append(VolumeExit._from_mapping(item, ex_path, errors))

        map_raw = _parse_mapping_field(data, "map", f"{path}.map", errors, nullable=True)
        vol_map = (
            VolumeMap._from_mapping(map_raw, f"{path}.map", errors)
            if map_raw is not None
            else None
        )

        return cls(
            index=idx_val if idx_val is not None else 0,
            title=_parse_str(data, "title", f"{path}.title", errors),
            chapter_start=ch_start if ch_start is not None else 0,
            chapter_end=ch_end if ch_end is not None else 0,
            protagonist_goal=_parse_str(data, "protagonist_goal", f"{path}.protagonist_goal", errors),
            antagonist_id=_parse_str(data, "antagonist_id", f"{path}.antagonist_id", errors),
            cool_point_modes=_parse_str_list(
                data, "cool_point_modes", f"{path}.cool_point_modes", errors
            ),
            climax=_parse_str(data, "climax", f"{path}.climax", errors),
            payoff=_parse_str(data, "payoff", f"{path}.payoff", errors),
            enters=_parse_str_list(data, "enters", f"{path}.enters", errors),
            exits=exits,
            map=vol_map,
            key_scenes=_parse_str_list(data, "key_scenes", f"{path}.key_scenes", errors),
            foreshadows_plant=_parse_str_list(
                data, "foreshadows_plant", f"{path}.foreshadows_plant", errors
            ),
            foreshadows_payoff=_parse_str_list(
                data, "foreshadows_payoff", f"{path}.foreshadows_payoff", errors
            ),
        )


@dataclass
class CastMember:
    """Named cast entry with volume lifecycle, appearance card, voice card, and arc."""

    id: str
    name: str
    role: str
    first_volume: int
    exit_volume: Optional[int]
    appearance_card: str
    voice_card: VoiceCard
    arc: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "first_volume": self.first_volume,
            "exit_volume": self.exit_volume,
            "appearance_card": self.appearance_card,
            "voice_card": self.voice_card.to_dict(),
            "arc": self.arc,
        }

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> CastMember:
        first_vol = _parse_int(data, "first_volume", f"{path}.first_volume", errors)
        exit_vol = _parse_int(data, "exit_volume", f"{path}.exit_volume", errors, nullable=True)
        vc_raw = _parse_mapping_field(data, "voice_card", f"{path}.voice_card", errors)
        voice_card = (
            VoiceCard._from_mapping(vc_raw, f"{path}.voice_card", errors)
            if vc_raw is not None
            else VoiceCard(speech_patterns="", catchphrases=[])
        )
        return cls(
            id=_parse_str(data, "id", f"{path}.id", errors),
            name=_parse_str(data, "name", f"{path}.name", errors),
            role=_parse_str(data, "role", f"{path}.role", errors),
            first_volume=first_vol if first_vol is not None else 0,
            exit_volume=exit_vol,
            appearance_card=_parse_str(data, "appearance_card", f"{path}.appearance_card", errors),
            voice_card=voice_card,
            arc=_parse_str(data, "arc", f"{path}.arc", errors),
        )


@dataclass
class ForeshadowPlanItem:
    """Book-level foreshadowing plan entry tying plant volume to payoff volume."""

    id: str
    title: str
    plant_volume: int
    payoff_volume: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> ForeshadowPlanItem:
        plant_vol = _parse_int(data, "plant_volume", f"{path}.plant_volume", errors)
        payoff_vol = _parse_int(data, "payoff_volume", f"{path}.payoff_volume", errors)
        return cls(
            id=_parse_str(data, "id", f"{path}.id", errors),
            title=_parse_str(data, "title", f"{path}.title", errors),
            plant_volume=plant_vol if plant_vol is not None else 0,
            payoff_volume=payoff_vol if payoff_vol is not None else 0,
        )


@dataclass
class StyleConfig:
    """Book-level genre, narrative rules, and style reference excerpt."""

    genre_id: str
    allow_pov_inner_monologue: bool
    humor: bool
    narration_rules: str
    sample_text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> StyleConfig:
        return cls(
            genre_id=_parse_str(data, "genre_id", f"{path}.genre_id", errors),
            allow_pov_inner_monologue=_parse_bool(
                data, "allow_pov_inner_monologue", f"{path}.allow_pov_inner_monologue", errors
            ),
            humor=_parse_bool(data, "humor", f"{path}.humor", errors),
            narration_rules=_parse_str(data, "narration_rules", f"{path}.narration_rules", errors),
            sample_text=_parse_str(data, "sample_text", f"{path}.sample_text", errors),
        )


@dataclass
class AdaptationSpec:
    """Visual adaptation anchors for comic/short-drama export."""

    visual_hook: str
    protagonist_look: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def _from_mapping(
        cls,
        data: Mapping[str, Any],
        path: str,
        errors: List[str],
    ) -> AdaptationSpec:
        return cls(
            visual_hook=_parse_str(data, "visual_hook", f"{path}.visual_hook", errors),
            protagonist_look=_parse_str(data, "protagonist_look", f"{path}.protagonist_look", errors),
        )


@dataclass
class BookBlueprint:
    """Whole-book structural blueprint governing volumes, cast, scale caps, and style."""

    premise: PremiseSpec
    target_total_chars: int
    chapter_chars_range: List[int]
    protagonist: ProtagonistSpec
    golden_finger: GoldenFingerSpec
    scale_rules: List[ScaleRule]
    volumes: List[VolumeSpec]
    cast: List[CastMember]
    foreshadow_plan: List[ForeshadowPlanItem]
    ending: str
    style: StyleConfig
    adaptation: AdaptationSpec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "premise": self.premise.to_dict(),
            "target_total_chars": self.target_total_chars,
            "chapter_chars_range": list(self.chapter_chars_range),
            "protagonist": self.protagonist.to_dict(),
            "golden_finger": self.golden_finger.to_dict(),
            "scale_rules": [r.to_dict() for r in self.scale_rules],
            "volumes": [v.to_dict() for v in self.volumes],
            "cast": [c.to_dict() for c in self.cast],
            "foreshadow_plan": [f.to_dict() for f in self.foreshadow_plan],
            "ending": self.ending,
            "style": self.style.to_dict(),
            "adaptation": self.adaptation.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BookBlueprint:
        if not isinstance(data, Mapping):
            raise BlueprintValidationError(
                f"Blueprint root must be a mapping, got {type(data).__name__}"
            )
        errors: List[str] = []

        premise_raw = _parse_mapping_field(data, "premise", "premise", errors)
        premise = (
            PremiseSpec._from_mapping(premise_raw, "premise", errors)
            if premise_raw is not None
            else PremiseSpec("", [], [], [], "")
        )

        target_total_chars = _parse_int(data, "target_total_chars", "target_total_chars", errors)

        range_raw = _parse_list_field(data, "chapter_chars_range", "chapter_chars_range", errors)
        chapter_chars_range: List[int] = []
        if range_raw is not None:
            for idx, item in enumerate(range_raw):
                if not _is_strict_int(item):
                    errors.append(
                        f"chapter_chars_range[{idx}]: expected int, got {type(item).__name__}"
                    )
                else:
                    chapter_chars_range.append(int(item))

        protag_raw = _parse_mapping_field(data, "protagonist", "protagonist", errors)
        protagonist = (
            ProtagonistSpec._from_mapping(protag_raw, "protagonist", errors)
            if protag_raw is not None
            else ProtagonistSpec("", "", "", "", VoiceCard("", []))
        )

        gf_raw = _parse_mapping_field(data, "golden_finger", "golden_finger", errors)
        golden_finger = (
            GoldenFingerSpec._from_mapping(gf_raw, "golden_finger", errors)
            if gf_raw is not None
            else GoldenFingerSpec("", "", "", [])
        )

        scale_raw = _parse_list_field(data, "scale_rules", "scale_rules", errors)
        scale_rules: List[ScaleRule] = []
        if scale_raw is not None:
            for idx, item in enumerate(scale_raw):
                item_path = f"scale_rules[{idx}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{item_path}: expected mapping, got {type(item).__name__}")
                else:
                    scale_rules.append(ScaleRule._from_mapping(item, item_path, errors))

        vols_raw = _parse_list_field(data, "volumes", "volumes", errors)
        volumes: List[VolumeSpec] = []
        if vols_raw is not None:
            for idx, item in enumerate(vols_raw):
                item_path = f"volumes[{idx}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{item_path}: expected mapping, got {type(item).__name__}")
                else:
                    volumes.append(VolumeSpec._from_mapping(item, item_path, errors))

        cast_raw = _parse_list_field(data, "cast", "cast", errors)
        cast: List[CastMember] = []
        if cast_raw is not None:
            for idx, item in enumerate(cast_raw):
                item_path = f"cast[{idx}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{item_path}: expected mapping, got {type(item).__name__}")
                else:
                    cast.append(CastMember._from_mapping(item, item_path, errors))

        fs_raw = _parse_list_field(data, "foreshadow_plan", "foreshadow_plan", errors)
        foreshadow_plan: List[ForeshadowPlanItem] = []
        if fs_raw is not None:
            for idx, item in enumerate(fs_raw):
                item_path = f"foreshadow_plan[{idx}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{item_path}: expected mapping, got {type(item).__name__}")
                else:
                    foreshadow_plan.append(ForeshadowPlanItem._from_mapping(item, item_path, errors))

        ending = _parse_str(data, "ending", "ending", errors)

        style_raw = _parse_mapping_field(data, "style", "style", errors)
        style = (
            StyleConfig._from_mapping(style_raw, "style", errors)
            if style_raw is not None
            else StyleConfig("", False, False, "", "")
        )

        adapt_raw = _parse_mapping_field(data, "adaptation", "adaptation", errors)
        adaptation = (
            AdaptationSpec._from_mapping(adapt_raw, "adaptation", errors)
            if adapt_raw is not None
            else AdaptationSpec("", "")
        )

        if errors:
            raise BlueprintValidationError(errors)

        assert target_total_chars is not None
        return cls(
            premise=premise,
            target_total_chars=target_total_chars,
            chapter_chars_range=chapter_chars_range,
            protagonist=protagonist,
            golden_finger=golden_finger,
            scale_rules=scale_rules,
            volumes=volumes,
            cast=cast,
            foreshadow_plan=foreshadow_plan,
            ending=ending,
            style=style,
            adaptation=adaptation,
        )


def load_blueprint(path: str | Path) -> BookBlueprint:
    """Load book_blueprint.yaml into a BookBlueprint without silent defaults."""
    bp_path = Path(path)
    if not bp_path.is_file():
        raise BlueprintValidationError(f"Blueprint file does not exist: {bp_path}")
    try:
        raw_text = bp_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise BlueprintValidationError(f"Invalid YAML in blueprint {bp_path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise BlueprintValidationError(
            f"Blueprint YAML root must be a mapping in {bp_path}, got {type(data).__name__}"
        )
    return BookBlueprint.from_dict(data)


def collect_existing_cast_names(
    projects_root: str | Path,
    exclude_project_id: Optional[str] = None,
) -> List[str]:
    """Collect character names from all project characters.yaml files under projects_root."""
    root = Path(projects_root)
    if not root.exists() or not root.is_dir():
        raise BlueprintValidationError(f"projects_root directory does not exist: {root}")

    excluded = exclude_project_id.strip() if isinstance(exclude_project_id, str) else None
    collected: List[str] = []
    seen: Set[str] = set()

    for subdir in sorted(root.iterdir(), key=lambda p: p.name):
        if not subdir.is_dir() or subdir.name.startswith(("_", ".")):
            continue
        if excluded and subdir.name == excluded:
            continue

        project_yaml = subdir / "project.yaml"
        if project_yaml.is_file():
            try:
                meta = yaml.safe_load(project_yaml.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise BlueprintValidationError(
                    f"Invalid YAML in {project_yaml}: {exc}"
                ) from exc
            if not isinstance(meta, Mapping):
                raise BlueprintValidationError(
                    f"project.yaml root must be a mapping in {project_yaml}"
                )
            proj_id = meta.get("project_id")
            if excluded and isinstance(proj_id, str) and proj_id.strip() == excluded:
                continue

        chars_yaml = subdir / "characters.yaml"
        if not chars_yaml.is_file():
            continue

        try:
            chars_data = yaml.safe_load(chars_yaml.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise BlueprintValidationError(
                f"Invalid YAML in {chars_yaml}: {exc}"
            ) from exc
        if not isinstance(chars_data, Mapping) or not isinstance(chars_data.get("characters"), list):
            raise BlueprintValidationError(
                f"{chars_yaml}: must be a mapping containing a 'characters' list"
            )

        for idx, entry in enumerate(chars_data["characters"]):
            if not isinstance(entry, Mapping):
                raise BlueprintValidationError(
                    f"{chars_yaml}: characters[{idx}] must be a mapping"
                )
            raw_name = entry.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise BlueprintValidationError(
                    f"{chars_yaml}: characters[{idx}].name must be a non-empty string"
                )
            clean_name = raw_name.strip()
            if clean_name not in seen:
                seen.add(clean_name)
                collected.append(clean_name)

    return collected


def _check_non_empty_str(value: Any, path: str, errors: List[str]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: must be a non-empty string")
        return None
    return value.strip()


def _check_non_empty_str_list(
    value: Any,
    path: str,
    errors: List[str],
    *,
    min_len: int = 0,
) -> Optional[List[str]]:
    if not isinstance(value, list):
        errors.append(f"{path}: expected list, got {type(value).__name__}")
        return None
    if len(value) < min_len:
        errors.append(f"{path}: expected at least {min_len} items, got {len(value)}")
    cleaned: List[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{path}[{idx}]: must be a non-empty string")
        else:
            cleaned.append(item.strip())
    return cleaned


def _validate_voice_card(value: Any, path: str, errors: List[str]) -> None:
    if not isinstance(value, VoiceCard):
        errors.append(f"{path}: expected VoiceCard, got {type(value).__name__}")
        return
    _check_non_empty_str(value.speech_patterns, f"{path}.speech_patterns", errors)
    cp_path = f"{path}.catchphrases"
    if not isinstance(value.catchphrases, list):
        errors.append(f"{cp_path}: expected list, got {type(value.catchphrases).__name__}")
        return
    if len(value.catchphrases) > MAX_CATCHPHRASES:
        errors.append(
            f"{cp_path}: expected at most {MAX_CATCHPHRASES} catchphrase, got {len(value.catchphrases)}"
        )
    for idx, phrase in enumerate(value.catchphrases):
        _check_non_empty_str(phrase, f"{cp_path}[{idx}]", errors)


def _validate_premise(premise: Any, errors: List[str]) -> None:
    if not isinstance(premise, PremiseSpec):
        errors.append(f"premise: expected PremiseSpec, got {type(premise).__name__}")
        return
    _check_non_empty_str(premise.logline, "premise.logline", errors)
    _check_non_empty_str_list(
        premise.selling_points, "premise.selling_points", errors, min_len=MIN_SELLING_POINTS
    )
    _check_non_empty_str_list(
        premise.selling_point_keywords,
        "premise.selling_point_keywords",
        errors,
        min_len=MIN_SELLING_POINT_KEYWORDS,
    )
    if not isinstance(premise.benchmark_refs, list):
        errors.append(
            f"premise.benchmark_refs: expected list, got {type(premise.benchmark_refs).__name__}"
        )
    else:
        if len(premise.benchmark_refs) < MIN_BENCHMARK_REFS:
            errors.append(
                f"premise.benchmark_refs: expected at least {MIN_BENCHMARK_REFS} item, "
                f"got {len(premise.benchmark_refs)}"
            )
        for idx, ref in enumerate(premise.benchmark_refs):
            ref_path = f"premise.benchmark_refs[{idx}]"
            if not isinstance(ref, BenchmarkRef):
                errors.append(f"{ref_path}: expected BenchmarkRef, got {type(ref).__name__}")
            else:
                _check_non_empty_str(ref.title, f"{ref_path}.title", errors)
                _check_non_empty_str(ref.why, f"{ref_path}.why", errors)
    _check_non_empty_str(premise.target_readers, "premise.target_readers", errors)


def _validate_scale_rules(scale_rules: Any, errors: List[str]) -> None:
    if not isinstance(scale_rules, list):
        errors.append(f"scale_rules: expected list, got {type(scale_rules).__name__}")
        return
    if len(scale_rules) < MIN_SCALE_RULES:
        errors.append(f"scale_rules: expected at least {MIN_SCALE_RULES} rule, got {len(scale_rules)}")
    for idx, rule in enumerate(scale_rules):
        path = f"scale_rules[{idx}]"
        if not isinstance(rule, ScaleRule):
            errors.append(f"{path}: expected ScaleRule, got {type(rule).__name__}")
            continue
        _check_non_empty_str(rule.ledger_key, f"{path}.ledger_key", errors)
        _check_non_empty_str(rule.unit, f"{path}.unit", errors)

        policy_ok = isinstance(rule.policy, str) and rule.policy in SCALE_POLICIES
        if not policy_ok:
            errors.append(
                f"{path}.policy: invalid policy {rule.policy!r}; expected one of {SCALE_POLICIES}"
            )

        if rule.policy == "bounded_realistic":
            if rule.hard_max is None:
                errors.append(
                    f"{path}.hard_max: required and must be > 0 when policy is 'bounded_realistic'"
                )
            elif not _is_strict_finite_number(rule.hard_max) or float(rule.hard_max) <= 0:
                errors.append(
                    f"{path}.hard_max: must be a finite number > 0, got {rule.hard_max!r}"
                )
        elif rule.hard_max is not None:
            if not _is_strict_finite_number(rule.hard_max) or float(rule.hard_max) <= 0:
                errors.append(
                    f"{path}.hard_max: must be null or a finite number > 0, got {rule.hard_max!r}"
                )


def _validate_volumes(
    volumes: Any,
    errors: List[str],
) -> Tuple[Set[int], Optional[int]]:
    if not isinstance(volumes, list):
        errors.append(f"volumes: expected list, got {type(volumes).__name__}")
        return set(), None

    if not (MIN_VOLUMES <= len(volumes) <= MAX_VOLUMES):
        errors.append(
            f"volumes: expected {MIN_VOLUMES}..{MAX_VOLUMES} volumes, got {len(volumes)}"
        )

    valid_volume_indices = set(range(1, len(volumes) + 1))
    prev_end: Optional[int] = None
    total_chapters = 0
    all_chapter_spans_valid = len(volumes) > 0

    for idx, vol in enumerate(volumes):
        path = f"volumes[{idx}]"
        if not isinstance(vol, VolumeSpec):
            errors.append(f"{path}: expected VolumeSpec, got {type(vol).__name__}")
            all_chapter_spans_valid = False
            prev_end = None
            continue

        expected_index = idx + 1
        if not _is_strict_int(vol.index) or vol.index != expected_index:
            errors.append(
                f"{path}.index: expected continuous index {expected_index}, got {vol.index!r}"
            )

        _check_non_empty_str(vol.title, f"{path}.title", errors)

        start_ok = _is_strict_int(vol.chapter_start) and vol.chapter_start >= 1
        end_ok = _is_strict_int(vol.chapter_end) and vol.chapter_end >= 1
        if not start_ok:
            errors.append(
                f"{path}.chapter_start: must be an integer >= 1, got {vol.chapter_start!r}"
            )
        if not end_ok:
            errors.append(
                f"{path}.chapter_end: must be an integer >= 1, got {vol.chapter_end!r}"
            )

        span_ok = start_ok and end_ok
        if span_ok and vol.chapter_start > vol.chapter_end:
            errors.append(
                f"{path}.chapter_end: chapter_end ({vol.chapter_end}) < chapter_start ({vol.chapter_start})"
            )
            span_ok = False

        if start_ok:
            if idx == 0 and vol.chapter_start != 1:
                errors.append(
                    f"{path}.chapter_start: first volume must start at chapter 1, got {vol.chapter_start}"
                )
            elif idx > 0 and prev_end is not None and vol.chapter_start != prev_end + 1:
                errors.append(
                    f"{path}.chapter_start: expected {prev_end + 1} (contiguous after previous chapter_end={prev_end}), "
                    f"got {vol.chapter_start}"
                )

        if span_ok:
            total_chapters += vol.chapter_end - vol.chapter_start + 1
            prev_end = vol.chapter_end
        else:
            all_chapter_spans_valid = False
            prev_end = vol.chapter_end if end_ok else None

        _check_non_empty_str(vol.protagonist_goal, f"{path}.protagonist_goal", errors)
        _check_non_empty_str(vol.antagonist_id, f"{path}.antagonist_id", errors)
        _check_non_empty_str_list(
            vol.cool_point_modes, f"{path}.cool_point_modes", errors, min_len=MIN_COOL_POINT_MODES
        )
        _check_non_empty_str(vol.climax, f"{path}.climax", errors)
        _check_non_empty_str(vol.payoff, f"{path}.payoff", errors)

        min_enters = 1 if idx >= 1 else 0
        _check_non_empty_str_list(vol.enters, f"{path}.enters", errors, min_len=min_enters)

        if not isinstance(vol.exits, list):
            errors.append(f"{path}.exits: expected list, got {type(vol.exits).__name__}")
        else:
            for j, ex in enumerate(vol.exits):
                ex_path = f"{path}.exits[{j}]"
                if not isinstance(ex, VolumeExit):
                    errors.append(f"{ex_path}: expected VolumeExit, got {type(ex).__name__}")
                else:
                    _check_non_empty_str(ex.id, f"{ex_path}.id", errors)
                    _check_non_empty_str(ex.how, f"{ex_path}.how", errors)

        if vol.map is not None:
            if not isinstance(vol.map, VolumeMap):
                errors.append(f"{path}.map: expected VolumeMap or None, got {type(vol.map).__name__}")
            else:
                _check_non_empty_str(vol.map.location_id, f"{path}.map.location_id", errors)
                _check_non_empty_str(vol.map.name, f"{path}.map.name", errors)

        _check_non_empty_str_list(
            vol.key_scenes, f"{path}.key_scenes", errors, min_len=MIN_VOLUME_KEY_SCENES
        )
        _check_non_empty_str_list(vol.foreshadows_plant, f"{path}.foreshadows_plant", errors)
        _check_non_empty_str_list(vol.foreshadows_payoff, f"{path}.foreshadows_payoff", errors)

    return valid_volume_indices, (total_chapters if all_chapter_spans_valid else None)


def _validate_cast(
    cast: Any,
    valid_volume_indices: Set[int],
    existing_exact: Set[str],
    existing_pinyin: Dict[Tuple[str, ...], str],
    errors: List[str],
) -> Set[str]:
    if not isinstance(cast, list):
        errors.append(f"cast: expected list, got {type(cast).__name__}")
        return set()
    if len(cast) < MIN_CAST_COUNT:
        errors.append(f"cast: expected at least {MIN_CAST_COUNT} members, got {len(cast)}")

    cast_ids: Set[str] = set()
    cast_namer = CharacterNamer()
    registered_names: Set[str] = set()
    seen_intra_pinyin: Dict[Tuple[str, ...], str] = {}

    for idx, member in enumerate(cast):
        path = f"cast[{idx}]"
        if not isinstance(member, CastMember):
            errors.append(f"{path}: expected CastMember, got {type(member).__name__}")
            continue

        cid = _check_non_empty_str(member.id, f"{path}.id", errors)
        if cid is not None:
            if cid in cast_ids:
                errors.append(f"{path}.id: duplicate cast id {cid!r}")
            else:
                cast_ids.add(cid)

        cname = _check_non_empty_str(member.name, f"{path}.name", errors)
        if cname is not None:
            namer_ok = True
            try:
                cast_namer.validate_name(cname)
            except NamerError as exc:
                namer_ok = False
                errors.append(f"{path}.name: {exc}")

            py: Optional[Tuple[str, ...]] = None
            try:
                py = to_toneless_pinyin(cname)
            except NamerError:
                py = None

            if py is not None:
                if py in seen_intra_pinyin:
                    # validate_name already reports collisions against registered_names;
                    # only check unregistered earlier names (e.g. blacklisted ones) here.
                    if seen_intra_pinyin[py] not in registered_names:
                        errors.append(
                            f"{path}.name: 人名 {cname!r} ({' '.join(py)}) 与同书已有角色 "
                            f"{seen_intra_pinyin[py]!r} 同音或重名冲突"
                        )
                else:
                    seen_intra_pinyin[py] = cname

            if namer_ok:
                cast_namer.register_name(cname)
                registered_names.add(cname)

            if cname in existing_exact:
                errors.append(
                    f"{path}.name: 人名 {cname!r} 与已有作品角色完全同名冲突"
                )
            elif py is not None and py in existing_pinyin:
                errors.append(
                    f"{path}.name: 人名 {cname!r} ({' '.join(py)}) 与已有作品角色 "
                    f"{existing_pinyin[py]!r} 同音冲突"
                )

        _check_non_empty_str(member.role, f"{path}.role", errors)

        first_vol_ok = _is_strict_int(member.first_volume) and member.first_volume >= 1
        if not first_vol_ok:
            errors.append(
                f"{path}.first_volume: must be an integer >= 1, got {member.first_volume!r}"
            )
        elif valid_volume_indices and member.first_volume not in valid_volume_indices:
            errors.append(
                f"{path}.first_volume: volume {member.first_volume} outside valid volumes "
                f"{sorted(valid_volume_indices)}"
            )

        if member.exit_volume is not None:
            exit_vol_ok = _is_strict_int(member.exit_volume) and member.exit_volume >= 1
            if not exit_vol_ok:
                errors.append(
                    f"{path}.exit_volume: must be None or an integer >= 1, got {member.exit_volume!r}"
                )
            else:
                if valid_volume_indices and member.exit_volume not in valid_volume_indices:
                    errors.append(
                        f"{path}.exit_volume: volume {member.exit_volume} outside valid volumes "
                        f"{sorted(valid_volume_indices)}"
                    )
                if first_vol_ok and member.exit_volume < member.first_volume:
                    errors.append(
                        f"{path}.exit_volume: exit_volume ({member.exit_volume}) < "
                        f"first_volume ({member.first_volume})"
                    )

        _check_non_empty_str(member.appearance_card, f"{path}.appearance_card", errors)
        _validate_voice_card(member.voice_card, f"{path}.voice_card", errors)
        _check_non_empty_str(member.arc, f"{path}.arc", errors)

    return cast_ids


def _validate_foreshadow_plan(
    foreshadow_plan: Any,
    valid_volume_indices: Set[int],
    errors: List[str],
) -> Set[str]:
    if not isinstance(foreshadow_plan, list):
        errors.append(f"foreshadow_plan: expected list, got {type(foreshadow_plan).__name__}")
        return set()

    foreshadow_ids: Set[str] = set()
    for idx, item in enumerate(foreshadow_plan):
        path = f"foreshadow_plan[{idx}]"
        if not isinstance(item, ForeshadowPlanItem):
            errors.append(f"{path}: expected ForeshadowPlanItem, got {type(item).__name__}")
            continue

        fid = _check_non_empty_str(item.id, f"{path}.id", errors)
        if fid is not None:
            if fid in foreshadow_ids:
                errors.append(f"{path}.id: duplicate foreshadow id {fid!r}")
            else:
                foreshadow_ids.add(fid)

        _check_non_empty_str(item.title, f"{path}.title", errors)

        plant_ok = _is_strict_int(item.plant_volume) and item.plant_volume >= 1
        payoff_ok = _is_strict_int(item.payoff_volume) and item.payoff_volume >= 1
        if not plant_ok:
            errors.append(
                f"{path}.plant_volume: must be an integer >= 1, got {item.plant_volume!r}"
            )
        elif valid_volume_indices and item.plant_volume not in valid_volume_indices:
            errors.append(
                f"{path}.plant_volume: volume {item.plant_volume} outside valid volumes "
                f"{sorted(valid_volume_indices)}"
            )

        if not payoff_ok:
            errors.append(
                f"{path}.payoff_volume: must be an integer >= 1, got {item.payoff_volume!r}"
            )
        elif valid_volume_indices and item.payoff_volume not in valid_volume_indices:
            errors.append(
                f"{path}.payoff_volume: volume {item.payoff_volume} outside valid volumes "
                f"{sorted(valid_volume_indices)}"
            )

        if plant_ok and payoff_ok and item.plant_volume > item.payoff_volume:
            errors.append(
                f"{path}.payoff_volume: payoff_volume ({item.payoff_volume}) < "
                f"plant_volume ({item.plant_volume})"
            )

    return foreshadow_ids


def _prepare_existing_cast_index(
    existing_cast_names: Iterable[str],
    errors: List[str],
) -> Tuple[Set[str], Dict[Tuple[str, ...], str]]:
    existing_exact: Set[str] = set()
    existing_pinyin: Dict[Tuple[str, ...], str] = {}
    for idx, raw_name in enumerate(existing_cast_names):
        if not isinstance(raw_name, str) or not raw_name.strip():
            errors.append(f"existing_cast_names[{idx}]: invalid cast name {raw_name!r}")
            continue
        clean = raw_name.strip()
        existing_exact.add(clean)
        try:
            py = to_toneless_pinyin(clean)
        except NamerError as exc:
            errors.append(f"existing_cast_names[{idx}]: {exc}")
            continue
        existing_pinyin.setdefault(py, clean)
    return existing_exact, existing_pinyin


def validate_blueprint(
    bp: BookBlueprint,
    *,
    existing_cast_names: Iterable[str],
) -> None:
    """Validate all structural, referential, sizing, genre, and naming constraints on BookBlueprint."""
    if not isinstance(bp, BookBlueprint):
        raise BlueprintValidationError(f"bp: expected BookBlueprint, got {type(bp).__name__}")
    if existing_cast_names is None or isinstance(existing_cast_names, (str, bytes)):
        raise BlueprintValidationError(
            "existing_cast_names: expected an iterable of strings (not str/bytes/None)"
        )

    errors: List[str] = []
    existing_exact, existing_pinyin = _prepare_existing_cast_index(existing_cast_names, errors)

    _validate_premise(bp.premise, errors)

    target_ok = _is_strict_int(bp.target_total_chars) and bp.target_total_chars > 0
    if not target_ok:
        errors.append(
            f"target_total_chars: must be an integer > 0, got {bp.target_total_chars!r}"
        )

    range_ok = False
    min_c = 0
    max_c = 0
    if not isinstance(bp.chapter_chars_range, (list, tuple)):
        errors.append(
            f"chapter_chars_range: expected [min, max] list, got {type(bp.chapter_chars_range).__name__}"
        )
    elif len(bp.chapter_chars_range) != 2:
        errors.append(
            f"chapter_chars_range: expected exactly 2 ints [min, max], got {len(bp.chapter_chars_range)}"
        )
    else:
        c0, c1 = bp.chapter_chars_range[0], bp.chapter_chars_range[1]
        c0_ok = _is_strict_int(c0) and c0 > 0
        c1_ok = _is_strict_int(c1) and c1 > 0
        if not c0_ok:
            errors.append(f"chapter_chars_range[0]: must be an integer > 0, got {c0!r}")
        if not c1_ok:
            errors.append(f"chapter_chars_range[1]: must be an integer > 0, got {c1!r}")
        if c0_ok and c1_ok:
            if c0 >= c1:
                errors.append(f"chapter_chars_range: expected min < max, got [{c0}, {c1}]")
            else:
                min_c, max_c = int(c0), int(c1)
                range_ok = True

    if not isinstance(bp.protagonist, ProtagonistSpec):
        errors.append(
            f"protagonist: expected ProtagonistSpec, got {type(bp.protagonist).__name__}"
        )
    else:
        _check_non_empty_str(bp.protagonist.character_id, "protagonist.character_id", errors)
        _check_non_empty_str(bp.protagonist.desire, "protagonist.desire", errors)
        _check_non_empty_str(bp.protagonist.flaw, "protagonist.flaw", errors)
        _check_non_empty_str(bp.protagonist.arc, "protagonist.arc", errors)
        _validate_voice_card(bp.protagonist.voice_card, "protagonist.voice_card", errors)

    valid_volume_indices, total_chapters = _validate_volumes(bp.volumes, errors)

    if not isinstance(bp.golden_finger, GoldenFingerSpec):
        errors.append(
            f"golden_finger: expected GoldenFingerSpec, got {type(bp.golden_finger).__name__}"
        )
    else:
        _check_non_empty_str(bp.golden_finger.rules, "golden_finger.rules", errors)
        _check_non_empty_str(bp.golden_finger.cost, "golden_finger.cost", errors)
        _check_non_empty_str(bp.golden_finger.limits, "golden_finger.limits", errors)
        if not isinstance(bp.golden_finger.upgrade_path, list):
            errors.append(
                f"golden_finger.upgrade_path: expected list, got {type(bp.golden_finger.upgrade_path).__name__}"
            )
        else:
            for idx, up in enumerate(bp.golden_finger.upgrade_path):
                up_path = f"golden_finger.upgrade_path[{idx}]"
                if not isinstance(up, GoldenFingerUpgrade):
                    errors.append(
                        f"{up_path}: expected GoldenFingerUpgrade, got {type(up).__name__}"
                    )
                    continue
                if not _is_strict_int(up.volume) or up.volume < 1:
                    errors.append(
                        f"{up_path}.volume: must be an integer >= 1, got {up.volume!r}"
                    )
                elif valid_volume_indices and up.volume not in valid_volume_indices:
                    errors.append(
                        f"{up_path}.volume: volume {up.volume} outside valid volumes "
                        f"{sorted(valid_volume_indices)}"
                    )
                _check_non_empty_str(up.unlock, f"{up_path}.unlock", errors)

    _validate_scale_rules(bp.scale_rules, errors)

    if target_ok and range_ok and total_chapters is not None and total_chapters > 0:
        estimated = total_chapters * ((min_c + max_c) / 2.0)
        deviation = abs(estimated - bp.target_total_chars) / bp.target_total_chars
        if deviation > MAX_TOTAL_CHARS_DEVIATION:
            errors.append(
                f"target_total_chars / chapter_chars_range: estimated {estimated:.0f} chars "
                f"({total_chapters} chapters * {(min_c + max_c) / 2.0:.1f}) deviates by "
                f"{deviation:.1%} from target_total_chars={bp.target_total_chars} "
                f"(exceeds 15.0% tolerance)"
            )

    cast_ids = _validate_cast(
        bp.cast, valid_volume_indices, existing_exact, existing_pinyin, errors
    )
    foreshadow_ids = _validate_foreshadow_plan(bp.foreshadow_plan, valid_volume_indices, errors)

    # Cross-entity referential integrity checks
    if isinstance(bp.protagonist, ProtagonistSpec):
        protag_id = bp.protagonist.character_id.strip() if isinstance(bp.protagonist.character_id, str) else ""
        if protag_id and protag_id not in cast_ids:
            errors.append(
                f"protagonist.character_id: character {protag_id!r} not found in cast"
            )

    if isinstance(bp.volumes, list):
        for idx, vol in enumerate(bp.volumes):
            if not isinstance(vol, VolumeSpec):
                continue
            path = f"volumes[{idx}]"
            ant_id = vol.antagonist_id.strip() if isinstance(vol.antagonist_id, str) else ""
            if ant_id and ant_id not in cast_ids:
                errors.append(f"{path}.antagonist_id: character {ant_id!r} not found in cast")
            if isinstance(vol.enters, list):
                for j, cid in enumerate(vol.enters):
                    if isinstance(cid, str) and cid.strip() and cid.strip() not in cast_ids:
                        errors.append(f"{path}.enters[{j}]: character {cid.strip()!r} not found in cast")
            if isinstance(vol.exits, list):
                for j, ex in enumerate(vol.exits):
                    if isinstance(ex, VolumeExit) and isinstance(ex.id, str) and ex.id.strip():
                        if ex.id.strip() not in cast_ids:
                            errors.append(
                                f"{path}.exits[{j}].id: character {ex.id.strip()!r} not found in cast"
                            )
            if isinstance(vol.foreshadows_plant, list):
                for j, fid in enumerate(vol.foreshadows_plant):
                    if isinstance(fid, str) and fid.strip() and fid.strip() not in foreshadow_ids:
                        errors.append(
                            f"{path}.foreshadows_plant[{j}]: foreshadow {fid.strip()!r} not found in foreshadow_plan"
                        )
            if isinstance(vol.foreshadows_payoff, list):
                for j, fid in enumerate(vol.foreshadows_payoff):
                    if isinstance(fid, str) and fid.strip() and fid.strip() not in foreshadow_ids:
                        errors.append(
                            f"{path}.foreshadows_payoff[{j}]: foreshadow {fid.strip()!r} not found in foreshadow_plan"
                        )

    _check_non_empty_str(bp.ending, "ending", errors)

    if not isinstance(bp.style, StyleConfig):
        errors.append(f"style: expected StyleConfig, got {type(bp.style).__name__}")
    else:
        genre_id = _check_non_empty_str(bp.style.genre_id, "style.genre_id", errors)
        if genre_id is not None:
            try:
                get_genre(genre_id)
            except UnknownGenreError as exc:
                errors.append(f"style.genre_id: {exc}")
        if not isinstance(bp.style.allow_pov_inner_monologue, bool):
            errors.append(
                f"style.allow_pov_inner_monologue: expected bool, got "
                f"{type(bp.style.allow_pov_inner_monologue).__name__}"
            )
        if not isinstance(bp.style.humor, bool):
            errors.append(
                f"style.humor: expected bool, got {type(bp.style.humor).__name__}"
            )
        _check_non_empty_str(bp.style.narration_rules, "style.narration_rules", errors)
        _check_non_empty_str(bp.style.sample_text, "style.sample_text", errors)

    if not isinstance(bp.adaptation, AdaptationSpec):
        errors.append(f"adaptation: expected AdaptationSpec, got {type(bp.adaptation).__name__}")
    else:
        _check_non_empty_str(bp.adaptation.visual_hook, "adaptation.visual_hook", errors)
        _check_non_empty_str(bp.adaptation.protagonist_look, "adaptation.protagonist_look", errors)

    if errors:
        raise BlueprintValidationError(errors)


__all__ = [
    "AdaptationSpec",
    "BenchmarkRef",
    "BlueprintValidationError",
    "BookBlueprint",
    "CastMember",
    "ForeshadowPlanItem",
    "GoldenFingerSpec",
    "GoldenFingerUpgrade",
    "MAX_CATCHPHRASES",
    "MAX_TOTAL_CHARS_DEVIATION",
    "MAX_VOLUMES",
    "MIN_VOLUMES",
    "PremiseSpec",
    "ProtagonistSpec",
    "SCALE_POLICIES",
    "ScaleRule",
    "StyleConfig",
    "VoiceCard",
    "VolumeExit",
    "VolumeMap",
    "VolumeSpec",
    "collect_existing_cast_names",
    "load_blueprint",
    "validate_blueprint",
]
