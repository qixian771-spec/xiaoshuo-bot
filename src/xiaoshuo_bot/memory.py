from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .models import SceneOutline


_logger = logging.getLogger(__name__)


class MemoryMigrationError(ValueError):
    """Raised when legacy memory data lacks required virtual_day or cannot be migrated."""


DEFAULT_MAX_CONTEXT_CHARS = 5200
L0_MAX_TAIL_CHARS = 260
L1_WINDOW_SIZE = 5
L2_BLOCK_SIZE = 10
L3_BLOCK_SIZE = 100


def _validate_positive_day(value: Any, field_name: str, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MemoryMigrationError(
            f"{context}: '{field_name}' must be an int >= 1, got {value!r}"
        )
    return value


def _truncate(text: str, max_len: int) -> str:
    s = text.strip()
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    if max_len <= 3:
        return s[:max_len]
    return s[: max_len - 1] + "…"


def _dedupe_list(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


@dataclass
class ChapterMemoryEntry:
    chapter: int
    virtual_day: int
    title: str = ""
    summary: str = ""
    character_ids: List[str] = field(default_factory=list)
    asset_ids: List[str] = field(default_factory=list)
    location_id: str = ""
    key_events: List[str] = field(default_factory=list)
    tail: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.chapter, bool) or not isinstance(self.chapter, int) or self.chapter < 1:
            raise ValueError(f"ChapterMemoryEntry chapter must be an int >= 1, got {self.chapter!r}")
        if (
            isinstance(self.virtual_day, bool)
            or not isinstance(self.virtual_day, int)
            or self.virtual_day < 1
        ):
            raise ValueError(
                f"ChapterMemoryEntry virtual_day must be an int >= 1, got {self.virtual_day!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ChapterMemoryEntry:
        if not isinstance(data, Mapping):
            raise MemoryMigrationError(
                f"ChapterMemoryEntry expects mapping, got {type(data).__name__}"
            )
        if "chapter" not in data:
            raise MemoryMigrationError("ChapterMemoryEntry missing required 'chapter' field")
        raw_ch = data["chapter"]
        if isinstance(raw_ch, bool) or not isinstance(raw_ch, int) or raw_ch < 1:
            raise MemoryMigrationError(
                f"ChapterMemoryEntry invalid 'chapter' (must be int >= 1): {raw_ch!r}"
            )
        if "virtual_day" not in data:
            raise MemoryMigrationError(
                f"Chapter {raw_ch}: ChapterMemoryEntry missing required 'virtual_day' field; "
                "use migrate_legacy_memory() to migrate legacy memory JSON"
            )
        virtual_day = _validate_positive_day(
            data["virtual_day"], "virtual_day", f"ChapterMemoryEntry(chapter={raw_ch})"
        )
        return cls(
            chapter=raw_ch,
            virtual_day=virtual_day,
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            character_ids=[str(x) for x in data.get("character_ids", [])],
            asset_ids=[str(x) for x in data.get("asset_ids", [])],
            location_id=str(data.get("location_id", "")),
            key_events=[str(x) for x in data.get("key_events", [])],
            tail=str(data.get("tail", "")),
        )


@dataclass
class StageChronicle:
    start_chapter: int
    end_chapter: int
    summary: str
    entity_ids: List[str] = field(default_factory=list)
    start_day: Optional[int] = None
    end_day: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StageChronicle:
        start_ch = int(data["start_chapter"])
        end_ch = int(data["end_chapter"])
        raw_start_day = data.get("start_day")
        raw_end_day = data.get("end_day")
        start_day = (
            _validate_positive_day(
                raw_start_day, "start_day", f"StageChronicle({start_ch}-{end_ch})"
            )
            if raw_start_day is not None
            else None
        )
        end_day = (
            _validate_positive_day(
                raw_end_day, "end_day", f"StageChronicle({start_ch}-{end_ch})"
            )
            if raw_end_day is not None
            else None
        )
        return cls(
            start_chapter=start_ch,
            end_chapter=end_ch,
            summary=str(data["summary"]),
            entity_ids=[str(x) for x in data.get("entity_ids", [])],
            start_day=start_day,
            end_day=end_day,
        )


@dataclass
class VolumeChronicle:
    start_chapter: int
    end_chapter: int
    summary: str
    entity_ids: List[str] = field(default_factory=list)
    start_day: Optional[int] = None
    end_day: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VolumeChronicle:
        start_ch = int(data["start_chapter"])
        end_ch = int(data["end_chapter"])
        raw_start_day = data.get("start_day")
        raw_end_day = data.get("end_day")
        start_day = (
            _validate_positive_day(
                raw_start_day, "start_day", f"VolumeChronicle({start_ch}-{end_ch})"
            )
            if raw_start_day is not None
            else None
        )
        end_day = (
            _validate_positive_day(
                raw_end_day, "end_day", f"VolumeChronicle({start_ch}-{end_ch})"
            )
            if raw_end_day is not None
            else None
        )
        return cls(
            start_chapter=start_ch,
            end_chapter=end_ch,
            summary=str(data["summary"]),
            entity_ids=[str(x) for x in data.get("entity_ids", [])],
            start_day=start_day,
            end_day=end_day,
        )


@dataclass
class EntityMemoryRecord:
    entity_id: str
    entity_type: str = "character"  # character | asset
    name: str = ""
    first_chapter: int = 0
    last_chapter: int = 0
    appearances: List[Dict[str, Any]] = field(default_factory=list)

    def record_appearance(self, chapter: int, fact: str, max_kept: int = 8) -> None:
        if max_kept < 1:
            raise ValueError(f"max_kept must be >= 1, got {max_kept}")
        if self.first_chapter == 0 or chapter < self.first_chapter:
            self.first_chapter = chapter
        if chapter > self.last_chapter:
            self.last_chapter = chapter

        clean_fact = _truncate(fact, 160)
        if not clean_fact:
            return

        # Replace existing entry for same chapter if already present
        for item in self.appearances:
            if int(item["chapter"]) == chapter:
                item["fact"] = clean_fact
                return

        self.appearances.append({"chapter": chapter, "fact": clean_fact})
        self.appearances.sort(key=lambda x: int(x["chapter"]))

        # Keep only the most recent max_kept appearances (do not permanently pin appearances[0])
        if len(self.appearances) > max_kept:
            self.appearances = self.appearances[-max_kept:]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EntityMemoryRecord:
        return cls(
            entity_id=str(data["entity_id"]),
            entity_type=str(data.get("entity_type", "character")),
            name=str(data.get("name", "")),
            first_chapter=int(data.get("first_chapter", 0)),
            last_chapter=int(data.get("last_chapter", 0)),
            appearances=[
                {"chapter": int(a["chapter"]), "fact": str(a["fact"])}
                for a in data.get("appearances", [])
            ],
        )


class MemoryPyramid:
    """Four-tier hierarchical memory pyramid (L0-L3) with strict context budget <= 5200 chars."""

    def __init__(self, max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS) -> None:
        if max_context_chars <= 0:
            raise ValueError(f"max_context_chars must be positive, got {max_context_chars}")
        self.max_context_chars = int(max_context_chars)
        self.last_chapter: int = 0

        # L0: Last <=260 chars of the immediately preceding chapter
        self.l0_tail: str = ""
        # L1: Structured summaries of the most recent 5 chapters
        self.l1_recent: List[ChapterMemoryEntry] = []
        # L2: Stage chronicles merged every 10 chapters + staging buffer
        self.l2_stages: List[StageChronicle] = []
        self._l2_staging: List[ChapterMemoryEntry] = []
        # L3: Grand volume chronicles merged every 100 chapters + staging buffer + precision entity index
        self.l3_volumes: List[VolumeChronicle] = []
        self._l3_staging: List[StageChronicle] = []
        self.entity_index: Dict[str, EntityMemoryRecord] = {}

    @staticmethod
    def _extract_tail(chapter_text: str) -> str:
        stripped = chapter_text.strip()
        if not stripped:
            return ""
        clean_lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        tail = "\n".join(clean_lines[-2:]) if len(clean_lines) >= 2 else stripped[-220:]
        return tail[-L0_MAX_TAIL_CHARS:]

    @staticmethod
    def _derive_summary(
        chapter: int,
        title: str,
        chapter_text: str,
        key_events: List[str],
        location_id: str,
    ) -> str:
        parts: List[str] = []
        if title:
            parts.append(f"《{title}》")
        if location_id:
            parts.append(f"@{location_id}")
        if key_events:
            parts.append("；".join(_truncate(ev, 60) for ev in key_events[:3]))
        elif chapter_text.strip():
            clean = " ".join(ln.strip() for ln in chapter_text.strip().splitlines() if ln.strip())
            parts.append(_truncate(clean, 140))
        if not parts:
            parts.append(f"第{chapter}章事件推进")
        return _truncate(" | ".join(parts), 200)

    def register_entity_name(
        self, entity_id: str, name: str, entity_type: str = "character"
    ) -> None:
        rec = self.entity_index.get(entity_id)
        if rec is None:
            self.entity_index[entity_id] = EntityMemoryRecord(
                entity_id=entity_id,
                entity_type=entity_type,
                name=name,
            )
        else:
            rec.name = name
            rec.entity_type = entity_type

    def record_chapter(
        self,
        chapter: int,
        chapter_text: str = "",
        *,
        virtual_day: int,
        title: str = "",
        summary: str = "",
        character_ids: Optional[List[str]] = None,
        asset_ids: Optional[List[str]] = None,
        location_id: str = "",
        key_events: Optional[List[str]] = None,
        entity_facts: Optional[Dict[str, str]] = None,
        entity_names: Optional[Dict[str, str]] = None,
        scene: Optional[SceneOutline] = None,
    ) -> ChapterMemoryEntry:
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise ValueError(f"Chapter number must be >= 1, got {chapter!r}")
        if isinstance(virtual_day, bool) or not isinstance(virtual_day, int) or virtual_day < 1:
            raise ValueError(f"virtual_day must be an int >= 1, got {virtual_day!r}")

        if scene is not None:
            if not title:
                title = scene.public_title or scene.internal_title
            if character_ids is None:
                character_ids = list(scene.active_characters)
            if asset_ids is None:
                asset_ids = list(scene.active_assets)
            if not location_id:
                location_id = scene.location_id
            if key_events is None:
                key_events = list(scene.events)

        chars = _dedupe_list(character_ids or [])
        assets = _dedupe_list(asset_ids or [])
        events = [str(e).strip() for e in (key_events or []) if str(e).strip()]
        tail = self._extract_tail(chapter_text)
        resolved_summary = (
            _truncate(summary, 220)
            if summary.strip()
            else self._derive_summary(chapter, title, chapter_text, events, location_id)
        )

        entry = ChapterMemoryEntry(
            chapter=chapter,
            virtual_day=virtual_day,
            title=title,
            summary=resolved_summary,
            character_ids=chars,
            asset_ids=assets,
            location_id=location_id,
            key_events=events[:4],
            tail=tail,
        )

        # Update L0 tail if this is the latest chapter
        if chapter >= self.last_chapter:
            self.last_chapter = chapter
            if tail:
                self.l0_tail = tail

        # Update L1 sliding window (keep last 5)
        self.l1_recent = [e for e in self.l1_recent if e.chapter != chapter]
        self.l1_recent.append(entry)
        self.l1_recent.sort(key=lambda e: e.chapter)
        if len(self.l1_recent) > L1_WINDOW_SIZE:
            self.l1_recent = self.l1_recent[-L1_WINDOW_SIZE:]

        # Update precision entity index for characters & assets
        names_map = entity_names or {}
        facts_map = entity_facts or {}
        for cid in chars:
            rec = self.entity_index.setdefault(
                cid,
                EntityMemoryRecord(
                    entity_id=cid,
                    entity_type="character",
                    name=names_map.get(cid, ""),
                ),
            )
            if cid in names_map and names_map[cid]:
                rec.name = names_map[cid]
            fact = facts_map.get(cid) or resolved_summary
            rec.record_appearance(chapter, fact)

        for aid in assets:
            rec = self.entity_index.setdefault(
                aid,
                EntityMemoryRecord(
                    entity_id=aid,
                    entity_type="asset",
                    name=names_map.get(aid, ""),
                ),
            )
            if aid in names_map and names_map[aid]:
                rec.name = names_map[aid]
            fact = facts_map.get(aid) or resolved_summary
            rec.record_appearance(chapter, fact)

        # Update L2 staging and merge every 10 chapters
        self._l2_staging = [e for e in self._l2_staging if e.chapter != chapter]
        self._l2_staging.append(entry)
        self._l2_staging.sort(key=lambda e: e.chapter)

        while len(self._l2_staging) >= L2_BLOCK_SIZE:
            block = self._l2_staging[:L2_BLOCK_SIZE]
            self._l2_staging = self._l2_staging[L2_BLOCK_SIZE:]
            stage = self._merge_to_stage(block)
            self.l2_stages.append(stage)
            self._l3_staging.append(stage)

            if len(self._l3_staging) >= (L3_BLOCK_SIZE // L2_BLOCK_SIZE):
                vol_stages = self._l3_staging[: (L3_BLOCK_SIZE // L2_BLOCK_SIZE)]
                self._l3_staging = self._l3_staging[(L3_BLOCK_SIZE // L2_BLOCK_SIZE) :]
                self.l3_volumes.append(self._merge_to_volume(vol_stages))

        return entry

    @staticmethod
    def _merge_to_stage(entries: List[ChapterMemoryEntry]) -> StageChronicle:
        start_ch = entries[0].chapter
        end_ch = entries[-1].chapter
        start_day = min(e.virtual_day for e in entries)
        end_day = max(e.virtual_day for e in entries)
        all_entities = _dedupe_list(
            ent for e in entries for ent in (e.character_ids + e.asset_ids)
        )
        # Keep key representative snippets across the 10-chapter stage
        highlights: List[str] = []
        for e in entries:
            label = f"Ch.{e.chapter}"
            if e.title:
                label += f"({e.title})"
            highlights.append(f"{label}:{_truncate(e.summary, 36)}")
        raw_summary = "；".join(highlights)
        return StageChronicle(
            start_chapter=start_ch,
            end_chapter=end_ch,
            summary=_truncate(raw_summary, 320),
            entity_ids=all_entities[:24],
            start_day=start_day,
            end_day=end_day,
        )

    @staticmethod
    def _merge_to_volume(stages: List[StageChronicle]) -> VolumeChronicle:
        start_ch = stages[0].start_chapter
        end_ch = stages[-1].end_chapter
        start_days = [st.start_day for st in stages if st.start_day is not None]
        end_days = [st.end_day for st in stages if st.end_day is not None]
        start_day = min(start_days) if len(start_days) == len(stages) and start_days else None
        end_day = max(end_days) if len(end_days) == len(stages) and end_days else None
        all_entities = _dedupe_list(ent for st in stages for ent in st.entity_ids)
        parts = [
            f"Ch.{st.start_chapter}-{st.end_chapter}:{_truncate(st.summary, 48)}"
            for st in stages
        ]
        return VolumeChronicle(
            start_chapter=start_ch,
            end_chapter=end_ch,
            summary=_truncate("；".join(parts), 460),
            entity_ids=all_entities[:36],
            start_day=start_day,
            end_day=end_day,
        )

    def recall_entity(self, entity_id: str, max_chars: int = 320) -> str:
        rec = self.entity_index.get(entity_id)
        if rec is None or not rec.appearances:
            return ""
        label = f"{rec.name}({rec.entity_id})" if rec.name else rec.entity_id
        # Only retain recent 3 appearances; first_chapter is kept numerically in the label
        selected = rec.appearances[-3:]
        facts = "；".join(
            f"第{item['chapter']}章:{_truncate(str(item['fact']), 90)}" for item in selected
        )
        return _truncate(
            f"- [{rec.entity_type}] {label}（首现Ch.{rec.first_chapter}，最近Ch.{rec.last_chapter}）：{facts}",
            max_chars,
        )

    def compile_context(
        self,
        scene: Optional[SceneOutline] = None,
        *,
        current_virtual_day: int,
        active_character_ids: Optional[Iterable[str]] = None,
        active_asset_ids: Optional[Iterable[str]] = None,
        active_entity_ids: Optional[Iterable[str]] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        """Compile L0-L3 memory pyramid context strictly bounded by <= max_chars (default 5200)."""
        if (
            isinstance(current_virtual_day, bool)
            or not isinstance(current_virtual_day, int)
            or current_virtual_day < 1
        ):
            raise ValueError(
                f"current_virtual_day must be an int >= 1, got {current_virtual_day!r}"
            )

        budget = self.max_context_chars if max_chars is None else int(max_chars)
        if budget <= 0:
            return ""

        req_entities: List[str] = []
        if scene is not None:
            req_entities.extend(scene.active_characters)
            req_entities.extend(scene.active_assets)
        if active_character_ids is not None:
            req_entities.extend(active_character_ids)
        if active_asset_ids is not None:
            req_entities.extend(active_asset_ids)
        if active_entity_ids is not None:
            req_entities.extend(active_entity_ids)
        target_entities = _dedupe_list(req_entities)

        # Sub-budgets tuned for <= 5200 chars total
        l0_budget = min(320, int(budget * 0.08))
        recall_budget = min(1450, int(budget * 0.28))
        l1_budget = min(1600, int(budget * 0.32))
        l2_budget = min(1050, int(budget * 0.20))
        l3_budget = max(200, budget - l0_budget - recall_budget - l1_budget - l2_budget - 80)

        sections: List[str] = [f"### [时间锚点] 当前第{current_virtual_day}天"]

        # L0: Immediate previous chapter tail
        if self.l0_tail:
            l0_body = f"### [L0 上章尾锚点]\n{_truncate(self.l0_tail, max(40, l0_budget - 20))}"
            sections.append(_truncate(l0_body, l0_budget))

        # L3 Precision Entity Recall (prioritize entities absent longest so old Ch.5 chars are never crowded out)
        recall_lines: List[str] = []
        if target_entities:
            known_targets = [eid for eid in target_entities if eid in self.entity_index]
            known_targets.sort(key=lambda eid: self.entity_index[eid].last_chapter)
            per_entity_cap = max(140, min(340, recall_budget // max(1, len(known_targets))))
            used_recall = len("### [L3 登场实体历史溯源]\n")
            for eid in known_targets:
                line = self.recall_entity(eid, max_chars=per_entity_cap)
                if not line:
                    continue
                if used_recall + len(line) + 1 > recall_budget:
                    break
                recall_lines.append(line)
                used_recall += len(line) + 1
        if recall_lines:
            sections.append("### [L3 登场实体历史溯源]\n" + "\n".join(recall_lines))

        # L1: Recent 5 chapters
        if self.l1_recent:
            l1_lines: List[str] = []
            per_ch_cap = max(120, (l1_budget - 24) // len(self.l1_recent))
            for entry in self.l1_recent:
                day_tag = f"（第{entry.virtual_day}天，距今{current_virtual_day - entry.virtual_day}天）"
                line = _truncate(f"- 第{entry.chapter}章{day_tag}：{entry.summary}", per_ch_cap)
                l1_lines.append(line)
            l1_block = "### [L1 近五章摘要]\n" + "\n".join(l1_lines)
            sections.append(_truncate(l1_block, l1_budget))

        # L2: Recent 10-chapter stage chronicles (+ unmerged staging chapters not in L1)
        l1_chapters = {e.chapter for e in self.l1_recent}
        unmerged_older = [e for e in self._l2_staging if e.chapter not in l1_chapters]
        l2_lines: List[str] = []
        # Include most recent 3 stage chronicles so medium-term arc stays sharp
        recent_stages = self.l2_stages[-3:]
        for st in recent_stages:
            day_tag = (
                f"（第{st.start_day}–{st.end_day}天）"
                if st.start_day is not None and st.end_day is not None
                else ""
            )
            l2_lines.append(f"- 第{st.start_chapter}-{st.end_chapter}章阶段{day_tag}：{st.summary}")
        if unmerged_older:
            st_temp = self._merge_to_stage(unmerged_older)
            day_tag = (
                f"（第{st_temp.start_day}–{st_temp.end_day}天）"
                if st_temp.start_day is not None and st_temp.end_day is not None
                else ""
            )
            l2_lines.append(
                f"- 第{st_temp.start_chapter}-{st_temp.end_chapter}章近期阶段{day_tag}：{st_temp.summary}"
            )
        if l2_lines:
            per_l2 = max(120, (l2_budget - 24) // len(l2_lines))
            trimmed_l2 = [_truncate(ln, per_l2) for ln in l2_lines]
            sections.append(_truncate("### [L2 十章阶段纪要]\n" + "\n".join(trimmed_l2), l2_budget))

        # L3: Grand 100-chapter volume chronicles
        if self.l3_volumes:
            vol_lines: List[str] = []
            per_vol = max(80, (l3_budget - 24) // len(self.l3_volumes))
            for vol in self.l3_volumes:
                day_tag = (
                    f"（第{vol.start_day}–{vol.end_day}天）"
                    if vol.start_day is not None and vol.end_day is not None
                    else ""
                )
                vol_lines.append(
                    _truncate(
                        f"- 第{vol.start_chapter}-{vol.end_chapter}章大卷{day_tag}：{vol.summary}",
                        per_vol,
                    )
                )
            sections.append(_truncate("### [L3 百章大卷纪要]\n" + "\n".join(vol_lines), l3_budget))

        compiled = "\n\n".join(sections)
        if len(compiled) > budget:
            compiled = _truncate(compiled, budget)
        return compiled

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_context_chars": self.max_context_chars,
            "last_chapter": self.last_chapter,
            "l0_tail": self.l0_tail,
            "l1_recent": [e.to_dict() for e in self.l1_recent],
            "l2_stages": [s.to_dict() for s in self.l2_stages],
            "_l2_staging": [e.to_dict() for e in self._l2_staging],
            "l3_volumes": [v.to_dict() for v in self.l3_volumes],
            "_l3_staging": [s.to_dict() for s in self._l3_staging],
            "entity_index": {k: v.to_dict() for k, v in self.entity_index.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MemoryPyramid:
        pyr = cls(max_context_chars=int(data.get("max_context_chars", DEFAULT_MAX_CONTEXT_CHARS)))
        pyr.last_chapter = int(data.get("last_chapter", 0))
        pyr.l0_tail = str(data.get("l0_tail", ""))
        pyr.l1_recent = [ChapterMemoryEntry.from_dict(e) for e in data.get("l1_recent", [])]
        pyr.l2_stages = [StageChronicle.from_dict(s) for s in data.get("l2_stages", [])]
        pyr._l2_staging = [ChapterMemoryEntry.from_dict(e) for e in data.get("_l2_staging", [])]
        pyr.l3_volumes = [VolumeChronicle.from_dict(v) for v in data.get("l3_volumes", [])]
        pyr._l3_staging = [StageChronicle.from_dict(s) for s in data.get("_l3_staging", [])]
        pyr.entity_index = {
            str(k): EntityMemoryRecord.from_dict(v)
            for k, v in dict(data.get("entity_index", {})).items()
        }
        return pyr

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f"{target.name}.tmp")
        tmp_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, target)

    @classmethod
    def load(cls, path: str | Path) -> MemoryPyramid:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)


def migrate_legacy_memory(
    data: Mapping[str, Any],
    chapter_day_map: Mapping[int, int],
) -> MemoryPyramid:
    """Explicitly migrate legacy MemoryPyramid JSON lacking virtual_day/start_day/end_day."""
    if not isinstance(data, Mapping):
        raise MemoryMigrationError(
            f"migrate_legacy_memory expects mapping data, got {type(data).__name__}"
        )
    if not isinstance(chapter_day_map, Mapping):
        raise MemoryMigrationError(
            f"migrate_legacy_memory expects mapping chapter_day_map, got {type(chapter_day_map).__name__}"
        )

    normalized_map: Dict[int, int] = {}
    for raw_ch, raw_day in chapter_day_map.items():
        ch = _validate_positive_day(raw_ch, "chapter", "chapter_day_map key")
        day = _validate_positive_day(raw_day, "virtual_day", f"chapter_day_map[{ch}]")
        normalized_map[ch] = day

    migrated: Dict[str, Any] = copy.deepcopy(dict(data))
    migrated_entries = 0
    migrated_stages = 0
    migrated_volumes = 0

    for key in ("l1_recent", "_l2_staging"):
        raw_list = migrated.get(key, [])
        if not isinstance(raw_list, list):
            raise MemoryMigrationError(f"Legacy memory field '{key}' must be a list")
        new_list: List[Dict[str, Any]] = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, Mapping):
                raise MemoryMigrationError(
                    f"Legacy memory '{key}[{idx}]' must be a mapping, got {type(item).__name__}"
                )
            entry = dict(item)
            ch = _validate_positive_day(entry.get("chapter"), "chapter", f"{key}[{idx}]")
            if "virtual_day" not in entry or entry["virtual_day"] is None:
                if ch not in normalized_map:
                    raise MemoryMigrationError(
                        f"Cannot migrate legacy memory '{key}' chapter {ch}: "
                        "missing chapter in chapter_day_map"
                    )
                entry["virtual_day"] = normalized_map[ch]
                migrated_entries += 1
            else:
                _validate_positive_day(entry["virtual_day"], "virtual_day", f"{key}[{idx}]")
            new_list.append(entry)
        migrated[key] = new_list

    for key in ("l2_stages", "_l3_staging", "l3_volumes"):
        raw_list = migrated.get(key, [])
        if not isinstance(raw_list, list):
            raise MemoryMigrationError(f"Legacy memory field '{key}' must be a list")
        new_list = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, Mapping):
                raise MemoryMigrationError(
                    f"Legacy memory '{key}[{idx}]' must be a mapping, got {type(item).__name__}"
                )
            block = dict(item)
            start_ch = _validate_positive_day(
                block.get("start_chapter"), "start_chapter", f"{key}[{idx}]"
            )
            end_ch = _validate_positive_day(
                block.get("end_chapter"), "end_chapter", f"{key}[{idx}]"
            )
            if end_ch < start_ch:
                raise MemoryMigrationError(
                    f"Legacy memory '{key}[{idx}]' invalid chapter span: {start_ch}-{end_ch}"
                )
            has_start = block.get("start_day") is not None
            has_end = block.get("end_day") is not None
            if not has_start or not has_end:
                if start_ch not in normalized_map or end_ch not in normalized_map:
                    raise MemoryMigrationError(
                        f"Cannot migrate legacy memory '{key}' span Ch.{start_ch}-{end_ch}: "
                        "missing boundary chapter in chapter_day_map"
                    )
                span_days = [
                    normalized_map[c] for c in range(start_ch, end_ch + 1) if c in normalized_map
                ]
                if not has_start:
                    block["start_day"] = min(span_days)
                else:
                    _validate_positive_day(block["start_day"], "start_day", f"{key}[{idx}]")
                if not has_end:
                    block["end_day"] = max(span_days)
                else:
                    _validate_positive_day(block["end_day"], "end_day", f"{key}[{idx}]")
                if key == "l3_volumes":
                    migrated_volumes += 1
                else:
                    migrated_stages += 1
            else:
                _validate_positive_day(block["start_day"], "start_day", f"{key}[{idx}]")
                _validate_positive_day(block["end_day"], "end_day", f"{key}[{idx}]")
            new_list.append(block)
        migrated[key] = new_list

    _logger.warning(
        "Migrated legacy MemoryPyramid: entries=%d, stages=%d, volumes=%d",
        migrated_entries,
        migrated_stages,
        migrated_volumes,
    )
    return MemoryPyramid.from_dict(migrated)
