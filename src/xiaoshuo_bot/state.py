from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import (
    AssetState,
    CharacterMutation,
    CharacterWorldState,
    ExpectationDirective,
    ExpectationState,
    ForeshadowItem,
    LedgerMutation,
    NovelProject,
    SceneOutline,
    VolumeCapLock,
    WorldStateSnapshot,
    resolve_chapter_path,
)


class WorldStateError(RuntimeError):
    """Base error for 5D world-state invariant violations."""


class LedgerUnderflowError(WorldStateError):
    """Raised when a numeric ledger deduction would drive balance below zero."""


class VolumeCapViolationError(WorldStateError):
    """Raised when a numeric ledger balance exceeds the current volume cap without breakthrough."""


class CharacterStateViolationError(WorldStateError):
    """Raised when an irreversible character lifecycle constraint (e.g. DECEASED) is violated."""


class ForeshadowEvidenceMissingError(WorldStateError):
    """Raised when required physical foreshadow/expectation keywords are absent from chapter text."""


class LockedChapterTamperedError(WorldStateError):
    """Raised when a locked prior chapter's SHA256 hash mismatches or is tampered."""


class ForeshadowConfigurationError(WorldStateError):
    """Raised at preflight when a planted/due foreshadow declares no usable evidence keywords."""


SEASONS: Tuple[str, ...] = ("春", "夏", "秋", "冬")

_FLASHBACK_RE = re.compile(
    r"(?:\[|【|\(|（)?\b(?:flashback|memory)\b(?:\]|】|\)|）)?|"
    r"(?:\[|【|\(|（)?(?:回忆|闪回)(?:片段)?(?:\]|】|\)|）)?",
    re.IGNORECASE,
)

_MARRIAGE_OLD_RELATIONS = {
    "married",
    "spouse",
    "husband",
    "wife",
    "夫妻",
    "配偶",
    "丈夫",
    "妻子",
    "老公",
    "老婆",
    "婚配",
}
_DIVORCE_NEW_RELATIONS = {
    "divorced",
    "ex_spouse",
    "ex-spouse",
    "离婚",
    "离异",
    "和离",
    "休妻",
    "休夫",
    "前妻",
    "前夫",
}
_ENGAGED_OLD_RELATIONS = {"engaged", "fiance", "fiancee", "未婚夫", "未婚妻", "婚约"}
_BROKEN_ENGAGEMENT_RELATIONS = {"broken_engagement", "退婚", "解除婚约", "毁约"}

_MASTER_DISCIPLE_OLD_RELATIONS = {
    "master",
    "disciple",
    "apprentice",
    "master_disciple",
    "师父",
    "师傅",
    "师尊",
    "徒弟",
    "徒儿",
    "弟子",
    "师徒",
}
_SECT_EXPULSION_RELATIONS = {
    "expelled",
    "expelled_from_sect",
    "betrayed",
    "estranged",
    "叛出师门",
    "逐出师门",
    "逐出宗门",
    "叛宗",
    "断绝师徒",
    "脱离宗门",
    "叛门",
}


def _extract_sentence_with_keyword(text: str, keywords: List[str]) -> str:
    if not keywords:
        return ""
    sentences = re.split(r"(?<=[。！？])", text)
    for kw in keywords:
        if not kw:
            continue
        for sent in sentences:
            s = sent.strip()
            if kw in s:
                return s[:120]
    return ""


def _compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe_extend(target: List[str], additions: Iterable[str]) -> None:
    seen = set(target)
    for item in additions:
        val = str(item).strip()
        if val and val not in seen:
            seen.add(val)
            target.append(val)


def _infer_obsolete_titles(
    old_relation: str,
    new_relation: str,
    old_faction: str = "",
    new_faction: Optional[str] = None,
) -> List[str]:
    old_norm = old_relation.strip().lower()
    new_norm = new_relation.strip().lower()
    titles: List[str] = []

    if new_norm in _DIVORCE_NEW_RELATIONS or (
        old_norm in _MARRIAGE_OLD_RELATIONS and new_norm not in _MARRIAGE_OLD_RELATIONS and new_norm
    ):
        if old_norm in {"husband", "丈夫", "老公"}:
            titles.extend(["老公", "丈夫", "夫君"])
        elif old_norm in {"wife", "妻子", "老婆"}:
            titles.extend(["老婆", "妻子", "夫人"])
        else:
            titles.extend(["老公", "丈夫", "夫君", "老婆", "妻子", "夫人"])

    if new_norm in _BROKEN_ENGAGEMENT_RELATIONS or (
        old_norm in _ENGAGED_OLD_RELATIONS and new_norm not in _ENGAGED_OLD_RELATIONS and new_norm not in _MARRIAGE_OLD_RELATIONS and new_norm
    ):
        titles.extend(["未婚夫", "未婚妻"])

    if new_norm in _SECT_EXPULSION_RELATIONS or (
        old_norm in _MASTER_DISCIPLE_OLD_RELATIONS
        and new_norm not in _MASTER_DISCIPLE_OLD_RELATIONS
        and new_norm
    ):
        if old_norm in {"master", "师父", "师傅", "师尊"}:
            titles.extend(["师父", "师傅", "师尊"])
        elif old_norm in {"disciple", "apprentice", "徒弟", "徒儿", "弟子"}:
            titles.extend(["徒弟", "徒儿", "弟子"])
        else:
            titles.extend(["师父", "师傅", "师尊", "徒弟", "徒儿", "弟子"])

    if new_norm in {"expelled_from_sect", "逐出宗门", "逐出师门", "叛出师门", "叛宗", "脱离宗门", "叛门"}:
        titles.extend(["同门", "师兄", "师姐", "师弟", "师妹"])

    if new_faction is not None and old_faction and new_faction != old_faction:
        titles.extend([f"{old_faction}弟子", f"{old_faction}门人"])

    return titles


def _has_flashback_marker(scene: SceneOutline) -> bool:
    if scene.beat_type and _FLASHBACK_RE.search(scene.beat_type):
        return True
    if scene.location_time and _FLASHBACK_RE.search(scene.location_time):
        return True
    if scene.internal_title and _FLASHBACK_RE.search(scene.internal_title):
        return True
    if scene.scene_id and _FLASHBACK_RE.search(scene.scene_id):
        return True
    return False


class WorldStateManager:
    """5D anti-retcon world-state machine with O(1) snapshot persistence and SHA256 lock checks."""

    def __init__(self, project: NovelProject) -> None:
        self.project = project
        self.current_chapter: int = 0
        self.current_volume: int = project.meta.volume_index
        self.assets: Dict[str, AssetState] = copy.deepcopy(project.assets)
        self.foreshadows: Dict[str, ForeshadowItem] = copy.deepcopy(project.foreshadows)
        self.previous_chapter_tail: str = ""
        self.completed_chapters: List[int] = []

        # Dimension 1: Hard numeric ledger & volume cap locks
        self.numeric_balances: Dict[str, float] = {
            str(k): float(v) for k, v in project.initial_balances.items()
        }
        self.volume_cap_locks: Dict[str, VolumeCapLock] = copy.deepcopy(project.volume_cap_locks)

        # Dimension 2: Character lifecycle & relationship directed graph
        self.character_states: Dict[str, CharacterWorldState] = {}
        self.relationship_edges: List[Dict[str, str]] = []
        self.forbidden_titles: Dict[str, List[str]] = {}
        for cid, char in project.characters.items():
            c_state = CharacterWorldState(
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
            self.character_states[cid] = c_state
            if char.forbidden_titles:
                self.forbidden_titles[cid] = list(char.forbidden_titles)
            for target_id, rel_label in char.relations.items():
                self.relationship_edges.append(
                    {"source": cid, "target": str(target_id), "relation": str(rel_label)}
                )

        # Dimension 3: Expectation countdown ledger
        self.active_expectations: Dict[str, ExpectationState] = {}

        # Dimension 4: Virtual calendar, injury cooldowns, and consecutive locations
        self.virtual_day: int = 1
        self.virtual_season: str = "秋"
        self.virtual_date_label: str = "第1天（秋）"
        self.unhealed_injuries: Dict[str, Dict[str, int]] = {}
        self.recent_locations: List[str] = []

        # Dimension 5: Chapter SHA256 hashes & lock registry
        self.chapter_hashes: Dict[int, str] = {}
        self.locked_chapters: List[int] = []

    # ------------------------------------------------------------------
    # Query Helpers (v1-compatible + v2 5D queries)
    # ------------------------------------------------------------------
    def get_scene_assets(self, scene: SceneOutline) -> List[AssetState]:
        return [self.assets[aid] for aid in scene.active_assets if aid in self.assets]

    def get_due_foreshadows(self, chapter: int) -> List[ForeshadowItem]:
        """Return all unresolved foreshadows whose due_chapter <= chapter (including overdue)."""
        return [
            fs
            for fs in self.foreshadows.values()
            if fs.due_chapter <= chapter and fs.status != "resolved"
        ]

    def get_plant_foreshadows(self, chapter: int) -> List[ForeshadowItem]:
        """Return all pending foreshadows scheduled to be planted in chapter (including same-chapter payoff)."""
        return [
            fs
            for fs in self.foreshadows.values()
            if fs.plant_chapter == chapter and fs.due_chapter >= chapter and fs.status == "pending"
        ]

    def get_balance(self, ledger_key: str) -> float:
        return self.numeric_balances.get(ledger_key, 0.0)

    def get_volume_cap_lock(self, ledger_key: str, volume: Optional[int] = None) -> Optional[VolumeCapLock]:
        vol = self.current_volume if volume is None else volume
        key = f"{vol}:{ledger_key}"
        if key in self.volume_cap_locks:
            return self.volume_cap_locks[key]
        for lock in self.volume_cap_locks.values():
            if lock.ledger_key == ledger_key and lock.volume == vol:
                return lock
        return None

    def get_due_expectations(self, chapter: Optional[int] = None) -> List[ExpectationState]:
        ch = self.current_chapter if chapter is None else chapter
        return [
            exp
            for exp in self.active_expectations.values()
            if exp.status != "fulfilled" and exp.due_chapter <= ch
        ]

    def get_active_expectations(self) -> List[ExpectationState]:
        return [exp for exp in self.active_expectations.values() if exp.status != "fulfilled"]

    def get_consecutive_location_count(self, location_id: Optional[str] = None) -> int:
        if not self.recent_locations:
            return 0
        target = location_id if location_id is not None else self.recent_locations[-1]
        if not target:
            return 0
        count = 0
        for loc in reversed(self.recent_locations):
            if loc == target:
                count += 1
            else:
                break
        return count

    # ------------------------------------------------------------------
    # Direct Mutators & Validators
    # ------------------------------------------------------------------
    def _validate_ledger_mutation(
        self,
        mutation: LedgerMutation,
        balances: Dict[str, float],
        locks: Dict[str, VolumeCapLock],
        volume: int,
    ) -> Tuple[float, float, Optional[str]]:
        current = float(balances.get(mutation.ledger_key, 0.0))
        new_val = current + float(mutation.delta)
        if new_val < 0:
            raise LedgerUnderflowError(
                f"Ledger '{mutation.ledger_key}' underflow: {current} + ({mutation.delta}) = {new_val} < 0"
                + (f" ({mutation.reason})" if mutation.reason else "")
            )

        lock_key: Optional[str] = None
        composite_key = f"{volume}:{mutation.ledger_key}"
        if composite_key in locks:
            lock_key = composite_key
        else:
            for k, v in locks.items():
                if v.ledger_key == mutation.ledger_key and v.volume == volume:
                    lock_key = k
                    break

        if lock_key is not None:
            lock = locks[lock_key]
            if new_val > lock.max_value and not lock.unlocked and not mutation.breakthrough:
                raise VolumeCapViolationError(
                    f"Ledger '{mutation.ledger_key}' in volume {volume} would reach {new_val}, "
                    f"exceeding cap {lock.max_value} without breakthrough unlock"
                )
        return current, new_val, lock_key

    def apply_ledger_mutation(
        self, mutation: LedgerMutation, volume: Optional[int] = None
    ) -> Dict[str, Any]:
        vol = self.current_volume if volume is None else volume
        before, after, lock_key = self._validate_ledger_mutation(
            mutation, self.numeric_balances, self.volume_cap_locks, vol
        )
        self.numeric_balances[mutation.ledger_key] = after
        unlocked_now = False
        if lock_key is not None and mutation.breakthrough:
            if not self.volume_cap_locks[lock_key].unlocked:
                self.volume_cap_locks[lock_key].unlocked = True
                unlocked_now = True
        return {
            "ledger_key": mutation.ledger_key,
            "before": before,
            "after": after,
            "delta": mutation.delta,
            "volume": vol,
            "breakthrough_unlocked": unlocked_now,
            "reason": mutation.reason,
        }

    def adjust_balance(
        self,
        ledger_key: str,
        delta: float,
        *,
        reason: str = "",
        breakthrough: bool = False,
        volume: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.apply_ledger_mutation(
            LedgerMutation(
                ledger_key=ledger_key,
                delta=delta,
                reason=reason,
                breakthrough=breakthrough,
            ),
            volume=volume,
        )

    def validate_scene_characters(
        self, scene: SceneOutline, *, is_flashback: bool = False
    ) -> None:
        flashback = is_flashback or _has_flashback_marker(scene)
        seen: set[str] = set()
        candidates = [scene.pov_character] + list(scene.active_characters)
        for cid in candidates:
            if not cid or cid in seen:
                continue
            seen.add(cid)
            cs = self.character_states.get(cid)
            if cs is not None and cs.status == "deceased" and not flashback:
                raise CharacterStateViolationError(
                    f"Character '{cs.name}' ({cid}) died in chapter {cs.deceased_chapter} and cannot appear "
                    f"in chapter {scene.chapter} without an explicit flashback/memory marker"
                )

    def _validate_character_mutation(
        self, mutation: CharacterMutation, states: Dict[str, CharacterWorldState]
    ) -> None:
        cs = states.get(mutation.character_id)
        if cs is None:
            raise CharacterStateViolationError(
                f"Unknown character '{mutation.character_id}' in CharacterMutation"
            )
        if cs.status == "deceased" and mutation.status is not None and mutation.status != "deceased":
            raise CharacterStateViolationError(
                f"Character '{cs.name}' ({cs.character_id}) is DECEASED (chapter {cs.deceased_chapter}); "
                f"cannot transition to {mutation.status!r}"
            )
        if mutation.injury_recovery_chapters < 0:
            raise CharacterStateViolationError(
                f"Character '{cs.character_id}' injury_recovery_chapters cannot be negative: "
                f"{mutation.injury_recovery_chapters}"
            )
        if mutation.role is not None and not str(mutation.role).strip():
            raise CharacterStateViolationError(
                f"Character '{cs.character_id}' role mutation cannot be empty or whitespace"
            )

    def apply_character_mutation(
        self, mutation: CharacterMutation, chapter: Optional[int] = None
    ) -> Dict[str, Any]:
        ch = self.current_chapter if chapter is None else chapter
        self._validate_character_mutation(mutation, self.character_states)
        cs = self.character_states[mutation.character_id]

        if mutation.status is not None and mutation.status != cs.status:
            cs.status = mutation.status
            if mutation.status == "deceased":
                cs.deceased_chapter = ch

        old_faction = cs.faction
        if mutation.faction is not None:
            cs.faction = mutation.faction

        newly_forbidden: List[str] = []
        if mutation.add_forbidden_titles:
            newly_forbidden.extend(mutation.add_forbidden_titles)

        if mutation.relation_target_id and mutation.relation_label is not None:
            old_rel = cs.relations.get(mutation.relation_target_id, "")
            new_rel = mutation.relation_label
            cs.relations[mutation.relation_target_id] = new_rel
            self._upsert_relationship_edge(
                cs.character_id, mutation.relation_target_id, new_rel
            )
            newly_forbidden.extend(
                _infer_obsolete_titles(
                    old_relation=old_rel,
                    new_relation=new_rel,
                    old_faction=old_faction,
                    new_faction=mutation.faction,
                )
            )
        elif mutation.faction is not None and old_faction and mutation.faction != old_faction:
            newly_forbidden.extend(
                _infer_obsolete_titles(
                    old_relation="",
                    new_relation="",
                    old_faction=old_faction,
                    new_faction=mutation.faction,
                )
            )

        if newly_forbidden:
            _dedupe_extend(cs.forbidden_titles, newly_forbidden)
            bucket = self.forbidden_titles.setdefault(cs.character_id, [])
            _dedupe_extend(bucket, newly_forbidden)

        if mutation.role is not None:
            new_role = str(mutation.role).strip()
            if not new_role:
                raise CharacterStateViolationError(
                    f"Character '{cs.character_id}' role mutation cannot be empty or whitespace"
                )
            cs.current_role = new_role

        if mutation.injury:
            if mutation.injury_recovery_chapters > 0:
                cs.injuries[mutation.injury] = mutation.injury_recovery_chapters
                self.unhealed_injuries.setdefault(cs.character_id, {})[
                    mutation.injury
                ] = mutation.injury_recovery_chapters
            else:
                cs.injuries.pop(mutation.injury, None)
                if cs.character_id in self.unhealed_injuries:
                    self.unhealed_injuries[cs.character_id].pop(mutation.injury, None)
                    if not self.unhealed_injuries[cs.character_id]:
                        self.unhealed_injuries.pop(cs.character_id, None)

        return {
            "character_id": cs.character_id,
            "status": cs.status,
            "faction": cs.faction,
            "current_role": cs.current_role,
            "relations": dict(cs.relations),
            "added_forbidden_titles": list(newly_forbidden),
            "injuries": dict(cs.injuries),
        }

    def _upsert_relationship_edge(self, source: str, target: str, relation: str) -> None:
        for edge in self.relationship_edges:
            if edge.get("source") == source and edge.get("target") == target:
                edge["relation"] = relation
                return
        self.relationship_edges.append({"source": source, "target": target, "relation": relation})

    def lock_chapter(self, chapter: int, chapter_text: Optional[str] = None) -> str:
        if chapter_text is not None:
            ch_hash = _compute_sha256(chapter_text)
            if chapter in self.locked_chapters and chapter in self.chapter_hashes:
                if self.chapter_hashes[chapter] != ch_hash:
                    raise LockedChapterTamperedError(
                        f"Locked chapter {chapter} hash mismatch: expected {self.chapter_hashes[chapter]}, got {ch_hash}"
                    )
            self.chapter_hashes[chapter] = ch_hash
        elif chapter not in self.chapter_hashes:
            raise LockedChapterTamperedError(
                f"Cannot lock chapter {chapter}: missing chapter SHA256 hash"
            )
        if chapter not in self.locked_chapters:
            self.locked_chapters.append(chapter)
        return self.chapter_hashes[chapter]

    # ------------------------------------------------------------------
    # Chapter Advancement (Transactional / Atomic Validation First)
    # ------------------------------------------------------------------
    def preflight_scene(self, scene: SceneOutline, *, is_flashback: bool = False) -> None:
        """Outline-only (text-independent) checks run BEFORE spending LLM tokens on a chapter.

        Validates calendar delta, deceased-on-stage, staged character mutations, staged ledger
        mutations (underflow / volume cap), asset references, and that every foreshadow planted
        or due in this chapter declares non-empty physical keywords (otherwise no chapter text
        could ever satisfy the evidence gate).
        """
        if scene.calendar_days_elapsed < 0:
            raise WorldStateError(
                f"Chapter {scene.chapter}: calendar_days_elapsed cannot be negative ({scene.calendar_days_elapsed})"
            )
        self.validate_scene_characters(scene, is_flashback=is_flashback)
        self._stage_outline_mutations(scene)
        for fs in self.get_plant_foreshadows(scene.chapter):
            if not [kw for kw in fs.plant_keywords if str(kw).strip()]:
                raise ForeshadowConfigurationError(
                    f"Chapter {scene.chapter}: foreshadow [{fs.id}] '{fs.title}' is scheduled to be planted "
                    f"but declares no non-empty plant_keywords"
                )
        for fs in self.get_due_foreshadows(scene.chapter):
            if not [kw for kw in fs.payoff_keywords if str(kw).strip()]:
                raise ForeshadowConfigurationError(
                    f"Chapter {scene.chapter}: foreshadow [{fs.id}] '{fs.title}' is due "
                    f"(Ch.{fs.due_chapter}) but declares no non-empty payoff_keywords"
                )
        for mut in scene.post_scene_asset_mutations:
            if mut.asset_id not in self.assets:
                raise WorldStateError(
                    f"Chapter {scene.chapter}: unknown asset_id '{mut.asset_id}' in post_scene_asset_mutations"
                )

    def _stage_outline_mutations(self, scene: SceneOutline) -> None:
        # Pre-validate character mutations on a staged copy
        staged_chars = copy.deepcopy(self.character_states)
        for cm in scene.character_mutations:
            self._validate_character_mutation(cm, staged_chars)
            if cm.status is not None:
                staged_chars[cm.character_id].status = cm.status

        # Pre-validate ledger mutations on staged copies
        staged_balances = dict(self.numeric_balances)
        staged_locks = copy.deepcopy(self.volume_cap_locks)
        for lm in scene.ledger_mutations:
            _, new_val, lock_key = self._validate_ledger_mutation(
                lm, staged_balances, staged_locks, scene.volume
            )
            staged_balances[lm.ledger_key] = new_val
            if lock_key is not None and lm.breakthrough:
                staged_locks[lock_key].unlocked = True

    def validate_chapter(
        self,
        scene: SceneOutline,
        chapter_text: str,
        *,
        is_flashback: bool = False,
    ) -> Dict[str, Any]:
        """Run every 5D invariant check for (scene, chapter_text) WITHOUT mutating state.

        Returns the computed evidences/staged data consumed by advance_after_chapter.
        Raises a WorldStateError subclass on the first violation.
        """
        if scene.calendar_days_elapsed < 0:
            raise WorldStateError(
                f"Chapter {scene.chapter}: calendar_days_elapsed cannot be negative ({scene.calendar_days_elapsed})"
            )

        ch_hash = _compute_sha256(chapter_text)
        if scene.chapter in self.locked_chapters and scene.chapter in self.chapter_hashes:
            if self.chapter_hashes[scene.chapter] != ch_hash:
                raise LockedChapterTamperedError(
                    f"Locked chapter {scene.chapter} cannot be overwritten with different content "
                    f"(expected {self.chapter_hashes[scene.chapter]}, got {ch_hash})"
                )

        # 0. Pre-validate character lifecycle & deceased on-stage constraint
        self.validate_scene_characters(scene, is_flashback=is_flashback)

        # Pre-validate character & ledger mutations on staged copies
        self._stage_outline_mutations(scene)

        # Pre-validate foreshadow plant & due/overdue evidence (ZERO fake fallback)
        plant_list = self.get_plant_foreshadows(scene.chapter)
        plant_evidences: Dict[str, str] = {}
        for fs in plant_list:
            ev = _extract_sentence_with_keyword(chapter_text, fs.plant_keywords)
            if not ev:
                raise ForeshadowEvidenceMissingError(
                    f"Chapter {scene.chapter}: missing planting evidence keywords {fs.plant_keywords!r} "
                    f"for foreshadow [{fs.id}] '{fs.title}'"
                )
            plant_evidences[fs.id] = ev

        # Include foreshadows planted in this very chapter if due_chapter == scene.chapter!
        due_list = list(self.get_due_foreshadows(scene.chapter))
        due_evidences: Dict[str, str] = {}
        for fs in due_list:
            ev = _extract_sentence_with_keyword(chapter_text, fs.payoff_keywords)
            if not ev:
                raise ForeshadowEvidenceMissingError(
                    f"Chapter {scene.chapter}: missing payoff evidence keywords {fs.payoff_keywords!r} "
                    f"for foreshadow [{fs.id}] '{fs.title}' (due Ch.{fs.due_chapter})"
                )
            due_evidences[fs.id] = ev

        # Pre-validate expectation directive evidence if keywords are specified
        directive_evidences: Dict[Tuple[int, str], str] = {}
        for idx, ed in enumerate(scene.expectation_directives):
            if ed.evidence_keywords:
                ev = _extract_sentence_with_keyword(chapter_text, ed.evidence_keywords)
                if not ev:
                    raise ForeshadowEvidenceMissingError(
                        f"Chapter {scene.chapter}: missing evidence keywords {ed.evidence_keywords!r} "
                        f"for expectation [{ed.id}] '{ed.title}' (action={ed.action})"
                    )
                directive_evidences[(idx, ed.id)] = ev

        # Pre-validate asset references
        for mut in scene.post_scene_asset_mutations:
            if mut.asset_id not in self.assets:
                raise WorldStateError(
                    f"Chapter {scene.chapter}: unknown asset_id '{mut.asset_id}' in post_scene_asset_mutations"
                )

        return {
            "chapter_sha256": ch_hash,
            "plant_list": plant_list,
            "plant_evidences": plant_evidences,
            "due_list": due_list,
            "due_evidences": due_evidences,
            "directive_evidences": directive_evidences,
        }

    def advance_after_chapter(
        self,
        scene: SceneOutline,
        chapter_text: str,
        *,
        is_flashback: bool = False,
    ) -> Dict[str, Any]:
        """Validate all 5D invariants and advance world state atomically after a chapter completes."""
        validated = self.validate_chapter(scene, chapter_text, is_flashback=is_flashback)
        ch_hash: str = validated["chapter_sha256"]
        plant_list: List[ForeshadowItem] = validated["plant_list"]
        plant_evidences: Dict[str, str] = validated["plant_evidences"]
        due_list: List[ForeshadowItem] = validated["due_list"]
        due_evidences: Dict[str, str] = validated["due_evidences"]
        directive_evidences: Dict[Tuple[int, str], str] = validated["directive_evidences"]

        # ---- All validations passed; commit state changes ----
        changes: Dict[str, Any] = {
            "chapter": scene.chapter,
            "volume": scene.volume,
            "asset_updates": [],
            "foreshadow_updates": [],
            "ledger_updates": [],
            "character_updates": [],
            "expectation_updates": [],
            "healed_injuries": [],
        }

        # 1. Plant foreshadows
        for fs in plant_list:
            fs.status = "planted"
            fs.planted_evidence = plant_evidences[fs.id]
            changes["foreshadow_updates"].append(
                {
                    "id": fs.id,
                    "transition": "pending -> planted",
                    "evidence": fs.planted_evidence,
                }
            )

        # 2. Resolve due/overdue foreshadows
        for fs in due_list:
            prev_status = fs.status
            fs.status = "resolved"
            fs.payoff_evidence = due_evidences[fs.id]
            changes["foreshadow_updates"].append(
                {
                    "id": fs.id,
                    "transition": f"{prev_status} -> resolved",
                    "evidence": fs.payoff_evidence,
                }
            )

        # 3. Apply physical asset mutations
        for mut in scene.post_scene_asset_mutations:
            asset = self.assets[mut.asset_id]
            before = {
                "location": asset.location,
                "holder": asset.holder,
                "condition": asset.condition,
            }
            if mut.location is not None:
                asset.location = mut.location
            if mut.holder is not None:
                asset.holder = mut.holder
            if mut.condition is not None:
                asset.condition = mut.condition
            asset.last_updated_chapter = scene.chapter
            summary = mut.note or f"第{scene.chapter}章状态变更：{asset.location} / {asset.condition}"
            asset.history.append(f"[第{scene.chapter}章] {summary}")
            after = {
                "location": asset.location,
                "holder": asset.holder,
                "condition": asset.condition,
            }
            changes["asset_updates"].append(
                {
                    "asset_id": asset.id,
                    "name": asset.name,
                    "before": before,
                    "after": after,
                    "note": summary,
                }
            )

        # 4. Apply numeric ledger mutations
        for lm in scene.ledger_mutations:
            changes["ledger_updates"].append(self.apply_ledger_mutation(lm, volume=scene.volume))

        # 5. Cooldown existing injuries before applying new chapter injuries
        healed: List[Dict[str, str]] = []
        for cid in list(self.unhealed_injuries.keys()):
            injs = self.unhealed_injuries[cid]
            for inj_name in list(injs.keys()):
                rem = injs[inj_name] - 1
                if rem <= 0:
                    injs.pop(inj_name, None)
                    if cid in self.character_states:
                        self.character_states[cid].injuries.pop(inj_name, None)
                    healed.append({"character_id": cid, "injury": inj_name})
                else:
                    injs[inj_name] = rem
                    if cid in self.character_states:
                        self.character_states[cid].injuries[inj_name] = rem
            if not injs:
                self.unhealed_injuries.pop(cid, None)
        changes["healed_injuries"] = healed

        # 6. Apply character lifecycle, relation graph, forbidden titles, and new injuries
        for cm in scene.character_mutations:
            changes["character_updates"].append(
                self.apply_character_mutation(cm, chapter=scene.chapter)
            )

        # 7. Apply expectation directives & update overdue statuses
        for idx, ed in enumerate(scene.expectation_directives):
            ev = directive_evidences.get((idx, ed.id), "")
            if ed.action == "open":
                self.active_expectations[ed.id] = ExpectationState(
                    id=ed.id,
                    title=ed.title,
                    planted_chapter=scene.chapter,
                    due_chapter=ed.due_chapter,
                    description=ed.description,
                    status="overdue" if ed.due_chapter < scene.chapter else "active",
                    evidence=ev,
                )
                changes["expectation_updates"].append(
                    {"id": ed.id, "action": "open", "due_chapter": ed.due_chapter, "evidence": ev}
                )
            elif ed.action == "fulfill":
                existing = self.active_expectations.get(ed.id)
                if existing is not None:
                    existing.status = "fulfilled"
                    if ev:
                        existing.evidence = ev
                else:
                    self.active_expectations[ed.id] = ExpectationState(
                        id=ed.id,
                        title=ed.title,
                        planted_chapter=scene.chapter,
                        due_chapter=ed.due_chapter,
                        description=ed.description,
                        status="fulfilled",
                        evidence=ev,
                    )
                changes["expectation_updates"].append(
                    {"id": ed.id, "action": "fulfill", "evidence": ev}
                )

        for exp in self.active_expectations.values():
            if exp.status == "active" and exp.due_chapter <= scene.chapter:
                exp.status = "overdue"

        # 8. Advance virtual calendar & recent locations
        old_day = self.virtual_day
        self.virtual_day += int(scene.calendar_days_elapsed)
        season_steps = ((self.virtual_day - 1) // 90) - ((old_day - 1) // 90)
        if season_steps > 0:
            cur_idx = SEASONS.index(self.virtual_season) if self.virtual_season in SEASONS else 2
            self.virtual_season = SEASONS[(cur_idx + season_steps) % len(SEASONS)]
        self.virtual_date_label = f"第{self.virtual_day}天（{self.virtual_season}）"

        loc = scene.location_id.strip() if scene.location_id else ""
        if loc:
            self.recent_locations.append(loc)
            if len(self.recent_locations) > 20:
                self.recent_locations = self.recent_locations[-20:]

        # 9. Save continuity tail, hashes, locks, and chapter progress
        clean_lines = [ln.strip() for ln in chapter_text.strip().splitlines() if ln.strip()]
        tail = "\n".join(clean_lines[-2:]) if len(clean_lines) >= 2 else chapter_text.strip()[-220:]
        self.previous_chapter_tail = tail[-260:]

        self.current_chapter = max(self.current_chapter, scene.chapter)
        self.current_volume = scene.volume
        if scene.chapter not in self.completed_chapters:
            self.completed_chapters.append(scene.chapter)
        self.chapter_hashes[scene.chapter] = ch_hash
        if scene.locked and scene.chapter not in self.locked_chapters:
            self.locked_chapters.append(scene.chapter)

        changes["virtual_day"] = self.virtual_day
        changes["virtual_season"] = self.virtual_season
        changes["virtual_date_label"] = self.virtual_date_label
        changes["chapter_sha256"] = ch_hash
        return changes

    # ------------------------------------------------------------------
    # O(1) Snapshot Persistence & Tamper Verification
    # ------------------------------------------------------------------
    def to_snapshot(self) -> WorldStateSnapshot:
        return WorldStateSnapshot(
            current_chapter=self.current_chapter,
            current_volume=self.current_volume,
            completed_chapters=list(self.completed_chapters),
            previous_chapter_tail=self.previous_chapter_tail,
            assets=copy.deepcopy(self.assets),
            foreshadows=copy.deepcopy(self.foreshadows),
            numeric_balances=dict(self.numeric_balances),
            volume_cap_locks=copy.deepcopy(self.volume_cap_locks),
            character_states=copy.deepcopy(self.character_states),
            relationship_edges=[dict(edge) for edge in self.relationship_edges],
            forbidden_titles={k: list(v) for k, v in self.forbidden_titles.items()},
            active_expectations=copy.deepcopy(self.active_expectations),
            virtual_day=self.virtual_day,
            virtual_season=self.virtual_season,
            virtual_date_label=self.virtual_date_label,
            unhealed_injuries=copy.deepcopy(self.unhealed_injuries),
            recent_locations=list(self.recent_locations),
            chapter_hashes=dict(self.chapter_hashes),
            locked_chapters=list(self.locked_chapters),
        )

    def snapshot_dict(self) -> Dict[str, Any]:
        return self.to_snapshot().to_dict()

    def save_snapshot(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.snapshot_dict(), ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
            tmp_path = fh.name
        os.replace(tmp_path, target)

    def verify_chapter_hashes(
        self,
        *,
        chapters_dir: Optional[str | Path] = None,
        chapter_texts: Optional[Dict[int, str]] = None,
        locked_only: bool = True,
    ) -> None:
        target_chapters = list(self.locked_chapters) if locked_only else list(self.chapter_hashes.keys())
        for ch in self.locked_chapters:
            expected = self.chapter_hashes.get(ch, "")
            if not expected or len(expected) != 64:
                raise LockedChapterTamperedError(
                    f"Locked chapter {ch} is missing a valid SHA256 hash in snapshot"
                )

        if chapter_texts is not None:
            for ch, text in chapter_texts.items():
                if ch in target_chapters or ch in self.locked_chapters:
                    expected = self.chapter_hashes.get(ch)
                    actual = _compute_sha256(text)
                    if expected != actual:
                        raise LockedChapterTamperedError(
                            f"Locked chapter {ch} tampered: expected SHA256 {expected}, got {actual}"
                        )

        if chapters_dir is not None:
            base = Path(chapters_dir)
            for ch in target_chapters:
                expected = self.chapter_hashes.get(ch)
                if not expected:
                    continue
                ch_path = resolve_chapter_path(base, ch)
                if ch_path is None:
                    raise LockedChapterTamperedError(
                        f"Locked chapter {ch} file missing from '{base}'"
                    )
                actual = _compute_sha256(ch_path.read_text(encoding="utf-8"))
                if actual != expected:
                    raise LockedChapterTamperedError(
                        f"Locked chapter {ch} at '{ch_path}' tampered: expected SHA256 {expected}, got {actual}"
                    )

    def apply_snapshot(
        self,
        snapshot: WorldStateSnapshot,
        *,
        chapters_dir: Optional[str | Path] = None,
        chapter_texts: Optional[Dict[int, str]] = None,
        expected_hashes: Optional[Dict[int, str]] = None,
        verify_locked: bool = True,
    ) -> WorldStateSnapshot:
        if verify_locked:
            for ch in snapshot.locked_chapters:
                exp_hash = snapshot.chapter_hashes.get(ch, "")
                if not exp_hash or len(exp_hash) != 64:
                    raise LockedChapterTamperedError(
                        f"Locked chapter {ch} is missing a valid SHA256 hash in snapshot"
                    )
            if expected_hashes is not None:
                for ch, expected in expected_hashes.items():
                    if ch in snapshot.locked_chapters or ch in snapshot.chapter_hashes:
                        actual = snapshot.chapter_hashes.get(ch)
                        if actual != expected:
                            raise LockedChapterTamperedError(
                                f"Locked chapter {ch} snapshot hash tampered: expected {expected}, got {actual}"
                            )
            if chapter_texts is not None:
                for ch, text in chapter_texts.items():
                    if ch in snapshot.locked_chapters:
                        expected = snapshot.chapter_hashes.get(ch)
                        actual = _compute_sha256(text)
                        if actual != expected:
                            raise LockedChapterTamperedError(
                                f"Locked chapter {ch} tampered: expected SHA256 {expected}, got {actual}"
                            )
            if chapters_dir is not None:
                base = Path(chapters_dir)
                for ch in snapshot.locked_chapters:
                    expected = snapshot.chapter_hashes.get(ch)
                    ch_path = resolve_chapter_path(base, ch)
                    if ch_path is None:
                        raise LockedChapterTamperedError(
                            f"Locked chapter {ch} file missing from '{base}'"
                        )
                    actual = _compute_sha256(ch_path.read_text(encoding="utf-8"))
                    if actual != expected:
                        raise LockedChapterTamperedError(
                            f"Locked chapter {ch} at '{ch_path}' tampered: expected SHA256 {expected}, got {actual}"
                        )

        migrated_character_states = copy.deepcopy(snapshot.character_states)
        logger = logging.getLogger(__name__)
        for cid, cs in migrated_character_states.items():
            if cs.current_role is None:
                if cid not in self.project.characters:
                    raise WorldStateError(
                        f"Legacy snapshot migration failed: character '{cid}' is missing current_role "
                        f"and does not exist in project.characters"
                    )
                cs.current_role = self.project.characters[cid].role
                logger.warning("legacy snapshot migrated current_role for %s", cid)

        self.current_chapter = snapshot.current_chapter
        self.current_volume = snapshot.current_volume
        self.completed_chapters = list(snapshot.completed_chapters)
        self.previous_chapter_tail = snapshot.previous_chapter_tail
        self.assets = copy.deepcopy(snapshot.assets)
        self.foreshadows = copy.deepcopy(snapshot.foreshadows)
        self.numeric_balances = dict(snapshot.numeric_balances)
        self.volume_cap_locks = copy.deepcopy(snapshot.volume_cap_locks)
        self.character_states = migrated_character_states
        self.relationship_edges = [dict(e) for e in snapshot.relationship_edges]
        self.forbidden_titles = {k: list(v) for k, v in snapshot.forbidden_titles.items()}
        self.active_expectations = copy.deepcopy(snapshot.active_expectations)
        self.virtual_day = snapshot.virtual_day
        self.virtual_season = snapshot.virtual_season
        self.virtual_date_label = snapshot.virtual_date_label
        self.unhealed_injuries = copy.deepcopy(snapshot.unhealed_injuries)
        self.recent_locations = list(snapshot.recent_locations)
        self.chapter_hashes = dict(snapshot.chapter_hashes)
        self.locked_chapters = list(snapshot.locked_chapters)
        return self.to_snapshot()

    def load_snapshot(
        self,
        source: str | Path | WorldStateSnapshot,
        *,
        chapters_dir: Optional[str | Path] = None,
        chapter_texts: Optional[Dict[int, str]] = None,
        expected_hashes: Optional[Dict[int, str]] = None,
        verify_locked: bool = True,
    ) -> WorldStateSnapshot:
        if isinstance(source, WorldStateSnapshot):
            snap = source
        else:
            p = Path(source)
            data = json.loads(p.read_text(encoding="utf-8"))
            snap = WorldStateSnapshot.from_dict(data)
        return self.apply_snapshot(
            snap,
            chapters_dir=chapters_dir,
            chapter_texts=chapter_texts,
            expected_hashes=expected_hashes,
            verify_locked=verify_locked,
        )

    @classmethod
    def from_snapshot(
        cls,
        project: NovelProject,
        source: str | Path | WorldStateSnapshot,
        *,
        chapters_dir: Optional[str | Path] = None,
        chapter_texts: Optional[Dict[int, str]] = None,
        expected_hashes: Optional[Dict[int, str]] = None,
        verify_locked: bool = True,
    ) -> WorldStateManager:
        mgr = cls(project)
        mgr.load_snapshot(
            source,
            chapters_dir=chapters_dir,
            chapter_texts=chapter_texts,
            expected_hashes=expected_hashes,
            verify_locked=verify_locked,
        )
        return mgr
