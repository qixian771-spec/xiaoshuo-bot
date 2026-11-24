from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta, timezone
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

AUTHOR_LEVELS: Tuple[str, ...] = ("lv0_1", "lv2_plus")
CONTRACT_TRACKS: Tuple[str, ...] = ("complete_150k", "serial_20k", "legacy")
POV_MODES: Tuple[str, ...] = ("third_limited", "first_person")
PANEL_POLICIES: Tuple[str, ...] = ("forbid", "allow_system_tag")
EMOTION_POLARITIES: Tuple[str, ...] = ("suppress", "burst", "neutral", "mixed")
CHARACTER_STATUSES: Tuple[str, ...] = ("alive", "deceased", "missing")
EXPECTATION_ACTIONS: Tuple[str, ...] = ("open", "fulfill")


class ProjectValidationError(ValueError):
    """Raised when novel project configuration fails schema or referential integrity checks."""


class AmbiguousChapterArtifactError(RuntimeError):
    """Raised when both 4-digit (:04d) and legacy 2-digit (:02d) chapter files coexist."""


def format_chapter_filename(ch: int) -> str:
    if isinstance(ch, bool) or not isinstance(ch, int) or ch < 0:
        raise ValueError(f"Chapter number must be a non-negative int, got {ch!r}")
    return f"chapter_{ch:04d}.txt"


def resolve_chapter_path(directory: Path | str, ch: int) -> Optional[Path]:
    if isinstance(ch, bool) or not isinstance(ch, int) or ch < 0:
        raise ValueError(f"Chapter number must be a non-negative int, got {ch!r}")
    base = Path(directory)
    p4 = base / f"chapter_{ch:04d}.txt"
    p2 = base / f"chapter_{ch:02d}.txt"
    if p4 != p2 and p4.exists() and p2.exists():
        raise AmbiguousChapterArtifactError(
            f"Ambiguous chapter artifacts for chapter {ch}: both '{p4}' and '{p2}' exist"
        )
    if p4.exists():
        return p4
    if p2.exists():
        return p2
    return None


@dataclass
class CharacterAnchor:
    id: str
    name: str
    role: str
    personality: str
    speech_style: str
    behavioral_anchors: Dict[str, str] = field(default_factory=dict)
    forbidden_ooc: List[str] = field(default_factory=list)
    initial_status: str = "alive"
    faction: str = ""
    relations: Dict[str, str] = field(default_factory=dict)
    forbidden_titles: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.initial_status not in CHARACTER_STATUSES:
            raise ProjectValidationError(
                f"Character '{self.id}': unknown initial_status {self.initial_status!r}; "
                f"expected one of {CHARACTER_STATUSES}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CharacterAnchor:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            role=str(data["role"]),
            personality=str(data["personality"]),
            speech_style=str(data["speech_style"]),
            behavioral_anchors=dict(data.get("behavioral_anchors", {})),
            forbidden_ooc=list(data.get("forbidden_ooc", [])),
            initial_status=str(data.get("initial_status", "alive")),
            faction=str(data.get("faction", "")),
            relations=dict(data.get("relations", {})),
            forbidden_titles=list(data.get("forbidden_titles", [])),
        )


@dataclass
class AssetState:
    id: str
    name: str
    location: str
    holder: str
    condition: str
    physical_details: str
    last_updated_chapter: int = 0
    history: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AssetState:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            location=str(data["location"]),
            holder=str(data["holder"]),
            condition=str(data["condition"]),
            physical_details=str(data["physical_details"]),
            last_updated_chapter=int(data.get("last_updated_chapter", 0)),
            history=list(data.get("history", [])),
        )


_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；|\n]+")


def _clean_keywords(raw: Any) -> List[str]:
    """Normalize a keyword list: drop None/blank entries and split packed comma-delimited strings."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    for k in raw:
        if k is None:
            continue
        for part in _KEYWORD_SPLIT_RE.split(str(k)):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


@dataclass
class ForeshadowItem:
    id: str
    title: str
    plant_chapter: int
    due_chapter: int
    plant_instruction: str
    payoff_instruction: str
    plant_keywords: List[str] = field(default_factory=list)
    payoff_keywords: List[str] = field(default_factory=list)
    status: str = "pending"  # pending | planted | resolved
    planted_evidence: str = ""
    payoff_evidence: str = ""
    cross_volume: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ForeshadowItem:
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            plant_chapter=int(data["plant_chapter"]),
            due_chapter=int(data["due_chapter"]),
            plant_instruction=str(data["plant_instruction"]),
            payoff_instruction=str(data["payoff_instruction"]),
            plant_keywords=_clean_keywords(data.get("plant_keywords")),
            payoff_keywords=_clean_keywords(data.get("payoff_keywords")),
            status=str(data.get("status", "pending")),
            planted_evidence=str(data.get("planted_evidence", "")),
            payoff_evidence=str(data.get("payoff_evidence", "")),
            cross_volume=bool(data.get("cross_volume", False)),
        )


@dataclass
class StyleProfile:
    id: str
    name: str
    granularity_notes: str
    sample_text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StyleProfile:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            granularity_notes=str(data["granularity_notes"]),
            sample_text=str(data["sample_text"]),
        )


@dataclass
class AssetMutation:
    asset_id: str
    location: Optional[str] = None
    holder: Optional[str] = None
    condition: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AssetMutation:
        return cls(
            asset_id=str(data["asset_id"]),
            location=data.get("location"),
            holder=data.get("holder"),
            condition=data.get("condition"),
            note=str(data.get("note", "")),
        )


@dataclass
class LedgerMutation:
    ledger_key: str
    delta: float
    reason: str = ""
    breakthrough: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LedgerMutation:
        return cls(
            ledger_key=str(data["ledger_key"]),
            delta=float(data["delta"]),
            reason=str(data.get("reason", "")),
            breakthrough=bool(data.get("breakthrough", False)),
        )


@dataclass
class CharacterMutation:
    character_id: str
    status: Optional[str] = None
    faction: Optional[str] = None
    relation_target_id: Optional[str] = None
    relation_label: Optional[str] = None
    add_forbidden_titles: List[str] = field(default_factory=list)
    injury: Optional[str] = None
    injury_recovery_chapters: int = 0
    note: str = ""
    role: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status is not None and self.status not in CHARACTER_STATUSES:
            raise ProjectValidationError(
                f"CharacterMutation '{self.character_id}': unknown status {self.status!r}; "
                f"expected one of {CHARACTER_STATUSES}"
            )
        if self.role is not None and not str(self.role).strip():
            raise ProjectValidationError(
                f"CharacterMutation '{self.character_id}': role cannot be empty or whitespace"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CharacterMutation:
        raw_role = data.get("role")
        return cls(
            character_id=str(data["character_id"]),
            status=data.get("status"),
            faction=data.get("faction"),
            relation_target_id=data.get("relation_target_id"),
            relation_label=data.get("relation_label"),
            add_forbidden_titles=list(data.get("add_forbidden_titles", [])),
            injury=data.get("injury"),
            injury_recovery_chapters=int(data.get("injury_recovery_chapters", 0)),
            note=str(data.get("note", "")),
            role=str(raw_role) if raw_role is not None else None,
        )


@dataclass
class ExpectationDirective:
    id: str
    title: str
    due_chapter: int
    description: str = ""
    action: str = "open"  # open | fulfill
    evidence_keywords: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.action not in EXPECTATION_ACTIONS:
            raise ProjectValidationError(
                f"ExpectationDirective '{self.id}': unknown action {self.action!r}; "
                f"expected one of {EXPECTATION_ACTIONS}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExpectationDirective:
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            due_chapter=int(data["due_chapter"]),
            description=str(data.get("description", "")),
            action=str(data.get("action", "open")),
            evidence_keywords=_clean_keywords(data.get("evidence_keywords")),
        )


@dataclass
class SceneOutline:
    chapter: int
    scene_id: str
    internal_title: str
    pov_character: str
    active_characters: List[str]
    active_assets: List[str]
    style_profile_ids: List[str]
    location_time: str
    events: List[str]
    stop_condition: str
    min_chars: int = 1600
    max_chars: int = 4500
    post_scene_asset_mutations: List[AssetMutation] = field(default_factory=list)
    public_title: str = ""
    beat_type: str = ""
    emotion_polarity: str = "neutral"
    cliffhanger_hook: str = ""
    segment_comment_cue: str = ""
    locked: bool = False
    volume: int = 1
    location_id: str = ""
    calendar_days_elapsed: int = 1
    ledger_mutations: List[LedgerMutation] = field(default_factory=list)
    character_mutations: List[CharacterMutation] = field(default_factory=list)
    expectation_directives: List[ExpectationDirective] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.emotion_polarity not in EMOTION_POLARITIES:
            raise ProjectValidationError(
                f"Chapter {self.chapter}: unknown emotion_polarity {self.emotion_polarity!r}; "
                f"expected one of {EMOTION_POLARITIES}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Violation:
    rule_id: str
    category: str
    severity: str  # CRITICAL | WARNING
    matched_text: str
    line_number: int
    context_snippet: str
    fix_hint: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScanReport:
    passed: bool
    char_count: int
    paragraph_count: int
    violations: List[Violation] = field(default_factory=list)

    @property
    def critical_violations(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == "CRITICAL"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "char_count": self.char_count,
            "paragraph_count": self.paragraph_count,
            "violation_count": len(self.violations),
            "critical_count": len(self.critical_violations),
            "violations": [v.to_dict() for v in self.violations],
        }


@dataclass
class ProductionIncident:
    incident_id: str
    chapter: int
    scene_id: str
    attempt: int
    timestamp: str
    violations: List[Violation]
    action_taken: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "chapter": self.chapter,
            "scene_id": self.scene_id,
            "attempt": self.attempt,
            "timestamp": self.timestamp,
            "violation_count": len(self.violations),
            "violations": [v.to_dict() for v in self.violations],
            "action_taken": self.action_taken,
        }


@dataclass
class VolumeCapLock:
    ledger_key: str
    volume: int
    max_value: float
    unlocked: bool = False
    unit: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VolumeCapLock:
        return cls(
            ledger_key=str(data["ledger_key"]),
            volume=int(data["volume"]),
            max_value=float(data["max_value"]),
            unlocked=bool(data.get("unlocked", False)),
            unit=str(data.get("unit", "")),
        )


@dataclass
class CharacterWorldState:
    character_id: str
    name: str
    status: str = "alive"  # alive | deceased | missing
    faction: str = ""
    relations: Dict[str, str] = field(default_factory=dict)
    forbidden_titles: List[str] = field(default_factory=list)
    injuries: Dict[str, int] = field(default_factory=dict)
    deceased_chapter: Optional[int] = None
    current_role: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in CHARACTER_STATUSES:
            raise ProjectValidationError(
                f"CharacterWorldState '{self.character_id}': unknown status {self.status!r}; "
                f"expected one of {CHARACTER_STATUSES}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CharacterWorldState:
        raw_role = data.get("current_role")
        return cls(
            character_id=str(data["character_id"]),
            name=str(data["name"]),
            status=str(data.get("status", "alive")),
            faction=str(data.get("faction", "")),
            relations=dict(data.get("relations", {})),
            forbidden_titles=list(data.get("forbidden_titles", [])),
            injuries={str(k): int(v) for k, v in dict(data.get("injuries", {})).items()},
            deceased_chapter=(
                int(data["deceased_chapter"]) if data.get("deceased_chapter") is not None else None
            ),
            current_role=str(raw_role) if raw_role is not None else None,
        )


@dataclass
class ExpectationState:
    id: str
    title: str
    planted_chapter: int
    due_chapter: int
    description: str = ""
    status: str = "active"  # active | fulfilled | overdue
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExpectationState:
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            planted_chapter=int(data["planted_chapter"]),
            due_chapter=int(data["due_chapter"]),
            description=str(data.get("description", "")),
            status=str(data.get("status", "active")),
            evidence=str(data.get("evidence", "")),
        )


@dataclass
class WorldStateSnapshot:
    current_chapter: int = 0
    current_volume: int = 1
    completed_chapters: List[int] = field(default_factory=list)
    previous_chapter_tail: str = ""
    assets: Dict[str, AssetState] = field(default_factory=dict)
    foreshadows: Dict[str, ForeshadowItem] = field(default_factory=dict)
    numeric_balances: Dict[str, float] = field(default_factory=dict)
    volume_cap_locks: Dict[str, VolumeCapLock] = field(default_factory=dict)
    character_states: Dict[str, CharacterWorldState] = field(default_factory=dict)
    relationship_edges: List[Dict[str, str]] = field(default_factory=list)
    forbidden_titles: Dict[str, List[str]] = field(default_factory=dict)
    active_expectations: Dict[str, ExpectationState] = field(default_factory=dict)
    virtual_day: int = 1
    virtual_season: str = "秋"
    virtual_date_label: str = ""
    unhealed_injuries: Dict[str, Dict[str, int]] = field(default_factory=dict)
    recent_locations: List[str] = field(default_factory=list)
    chapter_hashes: Dict[int, str] = field(default_factory=dict)
    locked_chapters: List[int] = field(default_factory=list)

    def get_deceased_names(self) -> List[str]:
        return [cs.name for cs in self.character_states.values() if cs.status == "deceased" and cs.name]

    def get_all_forbidden_titles(self) -> List[str]:
        seen: set[str] = set()
        result: List[str] = []
        for titles in self.forbidden_titles.values():
            for t in titles:
                if t and t not in seen:
                    seen.add(t)
                    result.append(t)
        for cs in self.character_states.values():
            for t in cs.forbidden_titles:
                if t and t not in seen:
                    seen.add(t)
                    result.append(t)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_chapter": self.current_chapter,
            "current_volume": self.current_volume,
            "completed_chapters": list(self.completed_chapters),
            "previous_chapter_tail": self.previous_chapter_tail,
            "assets": {aid: a.to_dict() for aid, a in self.assets.items()},
            "foreshadows": {fid: f.to_dict() for fid, f in self.foreshadows.items()},
            "numeric_balances": {k: float(v) for k, v in self.numeric_balances.items()},
            "volume_cap_locks": {k: v.to_dict() for k, v in self.volume_cap_locks.items()},
            "character_states": {k: v.to_dict() for k, v in self.character_states.items()},
            "relationship_edges": [dict(edge) for edge in self.relationship_edges],
            "forbidden_titles": {k: list(v) for k, v in self.forbidden_titles.items()},
            "active_expectations": {k: v.to_dict() for k, v in self.active_expectations.items()},
            "virtual_day": self.virtual_day,
            "virtual_season": self.virtual_season,
            "virtual_date_label": self.virtual_date_label,
            "unhealed_injuries": {
                cid: {inj: int(rem) for inj, rem in injs.items()}
                for cid, injs in self.unhealed_injuries.items()
            },
            "recent_locations": list(self.recent_locations),
            "chapter_hashes": {str(ch): h for ch, h in self.chapter_hashes.items()},
            "locked_chapters": list(self.locked_chapters),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorldStateSnapshot:
        return cls(
            current_chapter=int(data.get("current_chapter", 0)),
            current_volume=int(data.get("current_volume", 1)),
            completed_chapters=[int(c) for c in data.get("completed_chapters", [])],
            previous_chapter_tail=str(data.get("previous_chapter_tail", "")),
            assets={
                str(aid): AssetState.from_dict(a)
                for aid, a in dict(data.get("assets", {})).items()
            },
            foreshadows={
                str(fid): ForeshadowItem.from_dict(f)
                for fid, f in dict(data.get("foreshadows", {})).items()
            },
            numeric_balances={
                str(k): float(v)
                for k, v in dict(data.get("numeric_balances", {})).items()
            },
            volume_cap_locks={
                str(k): VolumeCapLock.from_dict(v)
                for k, v in dict(data.get("volume_cap_locks", {})).items()
            },
            character_states={
                str(k): CharacterWorldState.from_dict(v)
                for k, v in dict(data.get("character_states", {})).items()
            },
            relationship_edges=[dict(e) for e in data.get("relationship_edges", [])],
            forbidden_titles={
                str(k): list(v)
                for k, v in dict(data.get("forbidden_titles", {})).items()
            },
            active_expectations={
                str(k): ExpectationState.from_dict(v)
                for k, v in dict(data.get("active_expectations", {})).items()
            },
            virtual_day=int(data.get("virtual_day", 1)),
            virtual_season=str(data.get("virtual_season", "秋")),
            virtual_date_label=str(data.get("virtual_date_label", "")),
            unhealed_injuries={
                str(cid): {str(inj): int(rem) for inj, rem in dict(injs).items()}
                for cid, injs in dict(data.get("unhealed_injuries", {})).items()
            },
            recent_locations=[str(loc) for loc in data.get("recent_locations", [])],
            chapter_hashes={
                int(ch): str(h)
                for ch, h in dict(data.get("chapter_hashes", {})).items()
            },
            locked_chapters=[int(c) for c in data.get("locked_chapters", [])],
        )


@dataclass
class ChapterArtifact:
    chapter: int
    public_title: str = ""
    body: str = ""
    author_note: str = ""
    sha256: str = ""
    locked: bool = False
    scene_id: str = ""

    def __post_init__(self) -> None:
        if not self.sha256 and self.body:
            self.sha256 = self.compute_sha256()

    def compute_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ChapterArtifact:
        return cls(
            chapter=int(data["chapter"]),
            public_title=str(data.get("public_title", "")),
            body=str(data.get("body", "")),
            author_note=str(data.get("author_note", "")),
            sha256=str(data.get("sha256", "")),
            locked=bool(data.get("locked", False)),
            scene_id=str(data.get("scene_id", "")),
        )


@dataclass
class ProjectMeta:
    project_id: str
    title: str
    genre: str
    synopsis: str
    default_pov_character: str
    max_rewrite_retries: int = 3
    author_level: str = "lv0_1"
    contract_track: str = "legacy"
    pov_mode: str = "third_limited"
    panel_policy: str = "forbid"
    genre_id: str = ""
    pen_name: str = ""
    author_name: str = ""
    book_id: str = ""
    primary_category: str = ""
    tags: List[str] = field(default_factory=list)
    alternate_titles: List[str] = field(default_factory=list)
    alternate_blurbs: List[str] = field(default_factory=list)
    tweet_aliases: List[str] = field(default_factory=list)
    target_word_count: int = 0
    signed: bool = False
    max_published_chapter: int = 0
    volume_index: int = 1
    is_final_volume: bool = False

    def __post_init__(self) -> None:
        if self.author_level not in AUTHOR_LEVELS:
            raise ProjectValidationError(
                f"Unknown author_level {self.author_level!r}; expected one of {AUTHOR_LEVELS}"
            )
        if self.contract_track not in CONTRACT_TRACKS:
            raise ProjectValidationError(
                f"Unknown contract_track {self.contract_track!r}; expected one of {CONTRACT_TRACKS}"
            )
        if self.pov_mode not in POV_MODES:
            raise ProjectValidationError(
                f"Unknown pov_mode {self.pov_mode!r}; expected one of {POV_MODES}"
            )
        if self.panel_policy not in PANEL_POLICIES:
            raise ProjectValidationError(
                f"Unknown panel_policy {self.panel_policy!r}; expected one of {PANEL_POLICIES}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProjectMeta:
        return cls(
            project_id=str(data["project_id"]),
            title=str(data["title"]),
            genre=str(data.get("genre", "")),
            synopsis=str(data.get("synopsis", "")),
            default_pov_character=str(data["default_pov_character"]),
            max_rewrite_retries=int(data.get("max_rewrite_retries", 3)),
            author_level=str(data.get("author_level", "lv0_1")),
            contract_track=str(data.get("contract_track", "legacy")),
            pov_mode=str(data.get("pov_mode", "third_limited")),
            panel_policy=str(data.get("panel_policy", "forbid")),
            genre_id=str(data.get("genre_id", "")),
            pen_name=str(data.get("pen_name", "")),
            author_name=str(data.get("author_name", "")),
            book_id=str(data.get("book_id", "")),
            primary_category=str(data.get("primary_category", "")),
            tags=list(data.get("tags", [])),
            alternate_titles=list(data.get("alternate_titles", [])),
            alternate_blurbs=list(data.get("alternate_blurbs", [])),
            tweet_aliases=list(data.get("tweet_aliases", [])),
            target_word_count=int(data.get("target_word_count", 0)),
            signed=bool(data.get("signed", False)),
            max_published_chapter=int(data.get("max_published_chapter", 0)),
            volume_index=int(data.get("volume_index", 1)),
            is_final_volume=bool(data.get("is_final_volume", False)),
        )


@dataclass
class NovelProject:
    meta: ProjectMeta
    characters: Dict[str, CharacterAnchor]
    assets: Dict[str, AssetState]
    foreshadows: Dict[str, ForeshadowItem]
    style_profiles: Dict[str, StyleProfile]
    scenes: List[SceneOutline]
    initial_balances: Dict[str, float] = field(default_factory=dict)
    volume_cap_locks: Dict[str, VolumeCapLock] = field(default_factory=dict)
