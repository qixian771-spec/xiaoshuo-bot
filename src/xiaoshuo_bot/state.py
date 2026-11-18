from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from .models import AssetState, ForeshadowItem, NovelProject, SceneOutline


def _extract_sentence_with_keyword(text: str, keywords: List[str]) -> str:
    sentences = re.split(r"(?<=[。！？])", text)
    for kw in keywords:
        for sent in sentences:
            s = sent.strip()
            if kw in s:
                return s[:120]
    return ""


class WorldStateManager:
    """Tracks mutable physical asset states, foreshadowing ledger, and chapter continuity tail."""

    def __init__(self, project: NovelProject) -> None:
        self.assets: Dict[str, AssetState] = copy.deepcopy(project.assets)
        self.foreshadows: Dict[str, ForeshadowItem] = copy.deepcopy(project.foreshadows)
        self.previous_chapter_tail: str = ""
        self.completed_chapters: List[int] = []

    def get_scene_assets(self, scene: SceneOutline) -> List[AssetState]:
        return [self.assets[aid] for aid in scene.active_assets if aid in self.assets]

    def get_due_foreshadows(self, chapter: int) -> List[ForeshadowItem]:
        return [
            fs
            for fs in self.foreshadows.values()
            if fs.due_chapter == chapter and fs.status != "resolved"
        ]

    def get_plant_foreshadows(self, chapter: int) -> List[ForeshadowItem]:
        return [
            fs
            for fs in self.foreshadows.values()
            if fs.plant_chapter == chapter and fs.due_chapter > chapter and fs.status == "pending"
        ]

    def advance_after_chapter(self, scene: SceneOutline, chapter_text: str) -> Dict[str, Any]:
        """Apply physical asset mutations & mark planted/resolved foreshadows with text receipts."""
        changes: Dict[str, Any] = {
            "chapter": scene.chapter,
            "asset_updates": [],
            "foreshadow_updates": [],
        }

        # 1. Update planted foreshadows
        for fs in self.get_plant_foreshadows(scene.chapter):
            evidence = _extract_sentence_with_keyword(chapter_text, fs.plant_keywords)
            fs.status = "planted"
            fs.planted_evidence = evidence or f"Ch.{scene.chapter} 已埋设"
            changes["foreshadow_updates"].append(
                {
                    "id": fs.id,
                    "transition": "pending -> planted",
                    "evidence": fs.planted_evidence,
                }
            )

        # 2. Resolve due foreshadows
        for fs in self.get_due_foreshadows(scene.chapter):
            evidence = _extract_sentence_with_keyword(chapter_text, fs.payoff_keywords)
            fs.status = "resolved"
            fs.payoff_evidence = evidence or f"Ch.{scene.chapter} 已物理回收"
            changes["foreshadow_updates"].append(
                {
                    "id": fs.id,
                    "transition": "planted -> resolved",
                    "evidence": fs.payoff_evidence,
                }
            )

        # 3. Apply post-scene physical asset mutations
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

        # 4. Save continuity tail (last ~220 chars of clean prose)
        clean_lines = [ln.strip() for ln in chapter_text.strip().splitlines() if ln.strip()]
        tail = "\n".join(clean_lines[-2:]) if len(clean_lines) >= 2 else chapter_text.strip()[-220:]
        self.previous_chapter_tail = tail[-260:]
        self.completed_chapters.append(scene.chapter)
        return changes

    def snapshot_dict(self) -> Dict[str, Any]:
        return {
            "completed_chapters": list(self.completed_chapters),
            "previous_chapter_tail": self.previous_chapter_tail,
            "assets": {aid: a.to_dict() for aid, a in self.assets.items()},
            "foreshadows": {fid: f.to_dict() for fid, f in self.foreshadows.items()},
        }

    def save_snapshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.snapshot_dict(), f, ensure_ascii=False, indent=2)
