from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CharacterAnchor:
    id: str
    name: str
    role: str
    personality: str
    speech_style: str
    behavioral_anchors: Dict[str, str] = field(default_factory=dict)
    forbidden_ooc: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StyleProfile:
    id: str
    name: str
    granularity_notes: str
    sample_text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AssetMutation:
    asset_id: str
    location: Optional[str] = None
    holder: Optional[str] = None
    condition: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
class ProjectMeta:
    project_id: str
    title: str
    genre: str
    synopsis: str
    default_pov_character: str
    max_rewrite_retries: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NovelProject:
    meta: ProjectMeta
    characters: Dict[str, CharacterAnchor]
    assets: Dict[str, AssetState]
    foreshadows: Dict[str, ForeshadowItem]
    style_profiles: Dict[str, StyleProfile]
    scenes: List[SceneOutline]
