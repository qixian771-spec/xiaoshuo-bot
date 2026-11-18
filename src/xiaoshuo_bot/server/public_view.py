"""Public read-only "创作演播厅" view (owner Web UI grill-me answers).

Q6=B   完全透明: visitors see settings (characters, key items, style) and the robot's chapter plan.
Q8=A   只展示已通过审计的成稿: chapter text comes ONLY from the stockpile DB, which only holds
       chapters that passed every gate. Drafts, rewrites and failure details are never exposed.
Q2=C   结果导向: the public event stream forwards stage-completion events only.
Q10=C  完全封闭: no visitor input of any kind.

Never returned: account ids, filesystem paths, platform book ids, author notes, isolation internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..contracts import (
    SSE_BOOK_ISOLATED,
    SSE_CHAPTER_STARTED,
    SSE_CHAPTER_STOCKPILED,
    SSE_GATE_PASSED,
    SSEEventPayload,
)
from ..genres import get_genre
from ..loader import load_project
from ..models import NovelProject
from .stockpile_db import StockpileDB

PUBLIC_EVENT_NAMES = frozenset(
    {SSE_CHAPTER_STARTED, SSE_GATE_PASSED, SSE_CHAPTER_STOCKPILED, SSE_BOOK_ISOLATED}
)

_STATUS_TEXT = {
    "ACTIVE": "正在写作",
    "COMPLETED": "已完本",
    "ISOLATED_GATE_FAILURE": "暂停检修中",
    "PAUSED_QUOTA_EXHAUSTED": "额度用完，暂停中",
}


def _status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, "暂停中")


def _genre_name(genre_id: str) -> str:
    if not genre_id:
        return ""
    return get_genre(genre_id).display_name


def _written_chapters(db: StockpileDB, book_id: str) -> List[int]:
    return sorted(db.get_existing_chapter_numbers(book_id))


def list_public_books(db: StockpileDB) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for book in db.list_books():
        written = _written_chapters(db, book["book_id"])
        out.append(
            {
                "book_id": book["book_id"],
                "title": book["title"],
                "genre": _genre_name(book["genre_id"]),
                "status": book["status"],
                "status_text": _status_text(book["status"]),
                "written_chapters": len(written),
                "total_chapters": int(book["total_chapters"]),
                "latest_chapter": written[-1] if written else 0,
                "updated_at": book["updated_at"],
            }
        )
    out.sort(key=lambda b: (b["status"] != "ACTIVE", b["updated_at"]), reverse=False)
    return out


def _scene_plan(project: NovelProject, chapter: int) -> Optional[Dict[str, Any]]:
    scene = next((s for s in project.scenes if s.chapter == chapter), None)
    if scene is None:
        return None
    names = {cid: c.name for cid, c in project.characters.items()}
    return {
        "chapter": scene.chapter,
        "title": scene.public_title or scene.internal_title,
        "location_time": scene.location_time,
        "pov": names.get(scene.pov_character, scene.pov_character),
        "characters": [names.get(c, c) for c in scene.active_characters],
        "events": list(scene.events),
        "hook": scene.cliffhanger_hook,
    }


def get_public_book(db: StockpileDB, book_id: str) -> Dict[str, Any]:
    book = db.get_book(book_id)
    project = load_project(Path(book["project_dir"]))
    written = _written_chapters(db, book_id)
    written_set = set(written)
    next_chapter = next(
        (s.chapter for s in sorted(project.scenes, key=lambda s: s.chapter) if s.chapter not in written_set),
        0,
    )
    blurbs = list(project.meta.alternate_blurbs)
    return {
        "book_id": book_id,
        "title": book["title"],
        "genre": _genre_name(book["genre_id"]),
        "status": book["status"],
        "status_text": _status_text(book["status"]),
        "synopsis": project.meta.synopsis,
        "blurb": blurbs[0] if blurbs else "",
        "written_chapters": len(written),
        "total_chapters": len(project.scenes),
        "characters": [
            {
                "name": c.name,
                "role": c.role,
                "personality": c.personality,
                "speech_style": c.speech_style,
            }
            for c in project.characters.values()
        ],
        "key_items": [
            {"name": a.name, "details": a.physical_details, "condition": a.condition}
            for a in project.assets.values()
        ],
        "styles": [
            {"name": p.name, "notes": p.granularity_notes} for p in project.style_profiles.values()
        ],
        "chapters": [
            {
                "chapter": ch.chapter,
                "title": ch.public_title,
                "char_count": ch.char_count,
            }
            for ch in db.list_chapters(book_id)
        ],
        # The plan the robot is working on right now (完全透明). Future chapters stay hidden.
        "current_plan": _scene_plan(project, next_chapter) if book["status"] == "ACTIVE" else None,
    }


def get_public_chapter(db: StockpileDB, book_id: str, chapter: int) -> Dict[str, Any]:
    db.get_book(book_id)
    ch = db.get_chapter(book_id, chapter)
    return {
        "book_id": book_id,
        "chapter": ch["chapter"],
        "title": ch["public_title"],
        "body": ch["body"],
        "char_count": ch["char_count"],
    }


def public_event(evt: SSEEventPayload, titles: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if evt.event not in PUBLIC_EVENT_NAMES:
        return None
    title = titles.get(evt.book_id, evt.book_id)
    ch = int(evt.chapter)
    text = {
        SSE_CHAPTER_STARTED: f"《{title}》开始构思第 {ch} 章",
        SSE_GATE_PASSED: f"《{title}》第 {ch} 章通过全部审校门禁",
        SSE_CHAPTER_STOCKPILED: f"《{title}》第 {ch} 章成稿",
        SSE_BOOK_ISOLATED: f"《{title}》第 {ch} 章暂停检修，机器人在等待处理",
    }[evt.event]
    return {
        "event": evt.event,
        "book_id": evt.book_id,
        "chapter": ch,
        "message": text,
        "timestamp": evt.timestamp,
    }


__all__ = [
    "PUBLIC_EVENT_NAMES",
    "get_public_book",
    "get_public_chapter",
    "list_public_books",
    "public_event",
]
