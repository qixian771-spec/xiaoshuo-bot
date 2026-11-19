"""Public read-only "创作演播厅 · AI 写作流水线实时指挥中心" view.

Iron rules enforced by this module:
1. Real-event driven only: state reflects real backend events or honest idle ("待机 / 等待下一章")
   with the real timestamp of the last recorded event. Zero fabricated/random metrics or animations.
2. Approved chapter text only: chapter prose comes ONLY from the stockpile DB (chapters that passed
   all gates). Drafts, rewrites, prompt text, critic prose snippets, and author notes are NEVER exposed.
3. Zero secrets / tokens / admin routes / server IPs / internal filesystem paths in any payload.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import threading
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence

from ..contracts import (
    SSE_BOOK_ISOLATED,
    SSE_CHAPTER_STARTED,
    SSE_CHAPTER_STOCKPILED,
    SSE_GATE_PASSED,
    SSE_PIPELINE_CHAPTER_LOCKED,
    SSE_PIPELINE_GATE_REPORT,
    SSE_PIPELINE_MODEL_EVENT,
    SSE_PIPELINE_STAGE,
    SSE_PIPELINE_TOKEN_USAGE,
    SSEEventPayload,
)
from ..genres import get_genre
from ..llm.base import redact_secrets
from ..loader import load_project
from ..models import NovelProject
from .stockpile_db import StockpileDB, shanghai_now

PIPELINE_STAGES: tuple[str, ...] = (
    "plan",
    "outline",
    "compile",
    "draft",
    "gate",
    "polish",
    "lock",
    "export",
)

PIPELINE_STAGE_LABELS: Dict[str, str] = {
    "plan": "立项",
    "outline": "章纲",
    "compile": "组装提示词",
    "draft": "写稿",
    "gate": "门禁质检",
    "polish": "定点精修",
    "lock": "锁章",
    "export": "导出",
}

PUBLIC_EVENT_NAMES = frozenset(
    {
        SSE_CHAPTER_STARTED,
        SSE_GATE_PASSED,
        SSE_CHAPTER_STOCKPILED,
        SSE_BOOK_ISOLATED,
        SSE_PIPELINE_STAGE,
        SSE_PIPELINE_TOKEN_USAGE,
        SSE_PIPELINE_GATE_REPORT,
        SSE_PIPELINE_MODEL_EVENT,
        SSE_PIPELINE_CHAPTER_LOCKED,
    }
)

_STATUS_TEXT = {
    "ACTIVE": "正在写作",
    "COMPLETED": "已完本",
    "ISOLATED_GATE_FAILURE": "暂停检修中",
    "PAUSED_QUOTA_EXHAUSTED": "额度用完，暂停中",
    "PAUSED_MANUAL": "手动暂停",
}

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")
_UNIX_INTERNAL_PATH_RE = re.compile(
    r"(?:/(?:opt|Users|root|etc|var|home|tmp|private|usr)/[^\s'\"<>),;]+)"
)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"<>),;]+")
_ADMIN_ROUTE_RE = re.compile(
    r"(?:/admin(?:/[^\s'\"<>),;]*)?|/api/(?:books|cpa|companion|wizard|model-switches|model-quotas|production)(?:/[^\s'\"<>),;]*)?)",
    re.IGNORECASE,
)
_ENV_SECRET_KEY_RE = re.compile(
    r"\b(?:XIAOSHUO_ADMIN_TOKEN|XIAOSHUO_API_KEY|CPA_MANAGEMENT_KEY|COMPANION_SECRET|OPENAI_API_KEY)\b",
    re.IGNORECASE,
)


def sanitize_public_text(text: Any, *, max_len: int = 200) -> str:
    """Redact secrets, server IPs, admin paths, and internal filesystem paths for public display."""
    raw = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if not raw:
        return ""
    cleaned = redact_secrets(raw)
    cleaned = _IPV4_RE.sub("[HOST]", cleaned)
    cleaned = _UNIX_INTERNAL_PATH_RE.sub("[PATH]", cleaned)
    cleaned = _WIN_PATH_RE.sub("[PATH]", cleaned)
    cleaned = _ADMIN_ROUTE_RE.sub("[INTERNAL]", cleaned)
    cleaned = _ENV_SECRET_KEY_RE.sub("[SECRET]", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def _format_hms(ts: str) -> str:
    raw = str(ts or "").strip()
    if "T" in raw:
        return raw.split("T", 1)[1][:8]
    if len(raw) >= 19 and raw[10] == " ":
        return raw[11:19]
    return raw[:8] if raw else "--:--:--"


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
    # Active books stay pinned ahead of non-active books; within each group newest updated_at comes first.
    out.sort(key=lambda b: (str(b["updated_at"]), str(b["book_id"])), reverse=True)
    out.sort(key=lambda b: b["status"] != "ACTIVE")
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
                "model": ch.model,
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
        "model": str(ch.get("model") or ""),
    }


def _sanitize_gate_rules(raw_rules: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)):
        return []
    safe_rules: List[Dict[str, Any]] = []
    for item in raw_rules:
        if not isinstance(item, Mapping):
            continue
        rule_id = re.sub(r"[^A-Za-z0-9_\-]", "", str(item.get("rule_id") or ""))[:40]
        if not rule_id:
            continue
        severity = str(item.get("severity") or "CRITICAL").upper()
        if severity not in ("CRITICAL", "WARNING", "INFO"):
            severity = "CRITICAL"
        safe_rules.append(
            {
                "rule_id": rule_id,
                "label": sanitize_public_text(item.get("label") or rule_id, max_len=36),
                "passed": bool(item.get("passed", False)),
                "metric": sanitize_public_text(item.get("metric") or "", max_len=64),
                "severity": severity,
            }
        )
    return safe_rules


def _sanitize_public_quota_item(raw: Mapping[str, Any]) -> Dict[str, Any]:
    pct_raw = raw.get("percent")
    has_quota = bool(raw.get("has_quota")) and pct_raw is not None
    return {
        "slot": sanitize_public_text(raw.get("slot") or "", max_len=40),
        "model": sanitize_public_text(raw.get("model") or "", max_len=80),
        "label": sanitize_public_text(raw.get("label") or raw.get("model") or "", max_len=60),
        "in_rotation": bool(raw.get("in_rotation", False)),
        "manual_only": bool(raw.get("manual_only", False)),
        "has_quota": has_quota,
        "percent": round(float(pct_raw), 2) if has_quota else None,
        "reset_at": sanitize_public_text(raw.get("reset_at") or "", max_len=40),
        "updated_at": sanitize_public_text(raw.get("updated_at") or "", max_len=40),
    }


def _build_log_entry(pub: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    ev = str(pub.get("event") or "")
    ts = str(pub.get("timestamp") or "")
    ch = int(pub.get("chapter") or 0)
    ch_tag = f"ch{ch:04d}" if ch > 0 else "sys"
    book_id = str(pub.get("book_id") or "")

    if ev == SSE_PIPELINE_STAGE:
        stage = str(pub.get("stage") or "")
        status = str(pub.get("status") or "")
        att = int(pub.get("attempt") or 0)
        max_att = int(pub.get("max_attempts") or 0)
        model = str(pub.get("model") or "")
        detail = str(pub.get("detail") or "")
        if stage == "draft" and status == "running":
            att_str = f"attempt {att}/{max_att}" if att and max_att else "draft"
            m_str = f" → {model} thinking…" if model else " thinking…"
            return {"timestamp": ts, "level": "info", "tag": "THINK", "text": f"{ch_tag} {att_str}{m_str}", "book_id": book_id, "chapter": ch}
        if stage == "draft" and status == "rewriting":
            att_str = f"{att}/{max_att}" if att and max_att else str(att)
            return {"timestamp": ts, "level": "warn", "tag": "REWRITE", "text": f"{ch_tag} gate→draft rollback · attempt {att_str}", "book_id": book_id, "chapter": ch}
        if stage == "idle":
            return {"timestamp": ts, "level": "muted", "tag": "IDLE", "text": detail or "待机 / 等待下一章", "book_id": book_id, "chapter": ch}
        stage_lbl = PIPELINE_STAGE_LABELS.get(stage, stage)
        lvl = "pass" if status == "done" else ("fail" if status == "failed" else "info")
        return {
            "timestamp": ts,
            "level": lvl,
            "tag": stage.upper()[:8] or "STAGE",
            "text": f"{ch_tag} [{stage_lbl}] {status}" + (f" · {detail}" if detail else ""),
            "book_id": book_id,
            "chapter": ch,
        }

    if ev == SSE_PIPELINE_TOKEN_USAGE:
        role = str(pub.get("role") or "llm")
        model = str(pub.get("model") or "")
        p = int(pub.get("prompt_tokens") or 0)
        c = int(pub.get("completion_tokens") or 0)
        r = int(pub.get("reasoning_tokens") or 0)
        t = int(pub.get("total_tokens") or 0)
        parts = [f"prompt={p}", f"comp={c}"]
        if r > 0:
            parts.append(f"reason={r}")
        parts.append(f"total={t}")
        return {
            "timestamp": ts,
            "level": "info",
            "tag": "USAGE",
            "text": f"{ch_tag} {role} {model} {' '.join(parts)}".strip(),
            "book_id": book_id,
            "chapter": ch,
        }

    if ev == SSE_PIPELINE_GATE_REPORT:
        passed = bool(pub.get("passed"))
        rules = list(pub.get("rules") or [])
        highlights: List[str] = []
        for r in rules:
            if not r.get("passed") or r.get("rule_id") in ("LEN_001", "RHYTHM_NARRATION_WALL", "FORESHADOW"):
                sym = "✓" if r.get("passed") else "✗"
                highlights.append(f"{r.get('rule_id')} {sym} {r.get('metric')}")
        summary = " · ".join(highlights[:4]) or (f"{pub.get('char_count', 0)} chars")
        return {
            "timestamp": ts,
            "level": "pass" if passed else "fail",
            "tag": "GATE",
            "text": f"{ch_tag} {summary}",
            "book_id": book_id,
            "chapter": ch,
        }

    if ev == SSE_PIPELINE_MODEL_EVENT:
        kind = str(pub.get("kind") or "")
        if kind == "quota_updated":
            q = pub.get("quota") or {}
            if not q or not q.get("has_quota"):
                return None
            return {
                "timestamp": ts,
                "level": "muted",
                "tag": "QUOTA",
                "text": f"{q.get('model')} used={q.get('percent')}% reset={_format_hms(str(q.get('reset_at') or ''))}",
                "book_id": book_id,
                "chapter": ch,
            }
        if kind == "model_switched":
            return {
                "timestamp": ts,
                "level": "warn",
                "tag": "SWITCH",
                "text": f"{pub.get('from_model')} → {pub.get('to_model')} ({pub.get('reason')})",
                "book_id": book_id,
                "chapter": ch,
            }
        if kind == "rate_limit_retry":
            return {
                "timestamp": ts,
                "level": "warn",
                "tag": "BACKOFF",
                "text": f"{pub.get('model')} 429 retry {pub.get('retry_attempt')}/{pub.get('max_retries')} wait {pub.get('wait_seconds')}s",
                "book_id": book_id,
                "chapter": ch,
            }
        if kind == "model_restored":
            return {
                "timestamp": ts,
                "level": "pass",
                "tag": "RESTORE",
                "text": f"restored → {pub.get('to_model')} ({pub.get('reason')})",
                "book_id": book_id,
                "chapter": ch,
            }
        if kind == "quota_exhausted":
            return {
                "timestamp": ts,
                "level": "fail",
                "tag": "HALT",
                "text": f"circuit-breaker · {pub.get('reason')}",
                "book_id": book_id,
                "chapter": ch,
            }
        return None

    if ev == SSE_PIPELINE_CHAPTER_LOCKED:
        sha = str(pub.get("sha256_prefix") or "")
        chars = int(pub.get("char_count") or 0)
        dur = float(pub.get("duration_sec") or 0.0)
        model = str(pub.get("model") or "")
        return {
            "timestamp": ts,
            "level": "pass",
            "tag": "LOCKED",
            "text": f"{ch_tag} sha256:{sha} · {chars}字 · {dur:.1f}s · {model}".strip(" ·"),
            "book_id": book_id,
            "chapter": ch,
        }

    if ev in (SSE_CHAPTER_STARTED, SSE_GATE_PASSED, SSE_CHAPTER_STOCKPILED, SSE_BOOK_ISOLATED):
        lvl = "fail" if ev == SSE_BOOK_ISOLATED else ("pass" if ev in (SSE_GATE_PASSED, SSE_CHAPTER_STOCKPILED) else "info")
        tag = {
            SSE_CHAPTER_STARTED: "START",
            SSE_GATE_PASSED: "PASSED",
            SSE_CHAPTER_STOCKPILED: "SAVED",
            SSE_BOOK_ISOLATED: "ISOLATED",
        }[ev]
        return {
            "timestamp": ts,
            "level": lvl,
            "tag": tag,
            "text": str(pub.get("message") or ""),
            "book_id": book_id,
            "chapter": ch,
        }
    return None


def public_event(
    evt: SSEEventPayload,
    titles: Dict[str, str],
    *,
    replay: bool = False,
) -> Optional[Dict[str, Any]]:
    """Filter and strictly sanitize an SSEEventPayload for public broadcast.

    Legacy SSE_GATE_FAILED / SSE_CHAPTER_ATTEMPT remain blocked (returns None) so raw internal
    violations/snippets can never leak. Fine-grained SSE_PIPELINE_* events are rebuilt from an
    explicit metadata-only allowlist.
    """
    if evt.event not in PUBLIC_EVENT_NAMES:
        return None
    title = sanitize_public_text(titles.get(evt.book_id, evt.book_id), max_len=80)
    ch = int(evt.chapter)
    d = evt.data if isinstance(evt.data, Mapping) else {}

    # Preserve exact legacy schema & message wording for existing 4 public stage events
    if evt.event in (SSE_CHAPTER_STARTED, SSE_GATE_PASSED, SSE_CHAPTER_STOCKPILED, SSE_BOOK_ISOLATED):
        text = {
            SSE_CHAPTER_STARTED: f"《{title}》开始构思第 {ch} 章",
            SSE_GATE_PASSED: f"《{title}》第 {ch} 章通过全部审校门禁",
            SSE_CHAPTER_STOCKPILED: f"《{title}》第 {ch} 章成稿",
            SSE_BOOK_ISOLATED: f"《{title}》第 {ch} 章暂停检修，机器人在等待处理",
        }[evt.event]
        out: Dict[str, Any] = {
            "event": evt.event,
            "book_id": evt.book_id,
            "chapter": ch,
            "message": text,
            "timestamp": evt.timestamp,
        }
        if replay:
            out["replay"] = True
        return out

    if evt.event == SSE_PIPELINE_STAGE:
        stage = str(d.get("stage") or "").strip().lower()
        if stage not in PIPELINE_STAGES and stage != "idle":
            return None
        status = str(d.get("status") or "running").strip().lower()
        if status not in ("running", "done", "failed", "rewriting", "idle", "skipped"):
            status = "running"
        from_stage = str(d.get("from_stage") or "").strip().lower()
        if from_stage not in PIPELINE_STAGES:
            from_stage = ""
        out = {
            "event": evt.event,
            "book_id": evt.book_id,
            "book_title": title,
            "chapter": ch,
            "stage": stage,
            "stage_label": PIPELINE_STAGE_LABELS.get(stage, "待机"),
            "status": status,
            "from_stage": from_stage,
            "attempt": max(0, int(d.get("attempt") or 0)),
            "max_attempts": max(0, int(d.get("max_attempts") or 0)),
            "purpose": sanitize_public_text(d.get("purpose") or "", max_len=24),
            "model": sanitize_public_text(d.get("model") or "", max_len=80),
            "thinking": bool(d.get("thinking", False)),
            "char_count": max(0, int(d.get("char_count") or 0)),
            "min_chars": max(0, int(d.get("min_chars") or 0)),
            "max_chars": max(0, int(d.get("max_chars") or 0)),
            "scene_title": sanitize_public_text(d.get("scene_title") or "", max_len=60),
            "sha256_prefix": re.sub(r"[^a-fA-F0-9]", "", str(d.get("sha256_prefix") or ""))[:12],
            "latency_sec": round(float(d["latency_sec"]), 2) if d.get("latency_sec") is not None else None,
            "overall_score": round(float(d["overall_score"]), 2) if d.get("overall_score") is not None else None,
            "replaced_count": max(0, int(d.get("replaced_count") or 0)),
            "will_rewrite": bool(d.get("will_rewrite", False)),
            "detail": sanitize_public_text(d.get("detail") or "", max_len=120),
            "timestamp": evt.timestamp,
        }
        log_entry = _build_log_entry(out)
        if log_entry is not None:
            out["log"] = log_entry
        if replay:
            out["replay"] = True
        return out

    if evt.event == SSE_PIPELINE_TOKEN_USAGE:
        out = {
            "event": evt.event,
            "book_id": evt.book_id,
            "book_title": title,
            "chapter": ch,
            "role": sanitize_public_text(d.get("role") or "", max_len=24),
            "model": sanitize_public_text(d.get("model") or "", max_len=80),
            "model_label": sanitize_public_text(d.get("model_label") or "", max_len=80),
            "purpose": sanitize_public_text(d.get("purpose") or "", max_len=24),
            "attempt": max(0, int(d.get("attempt") or 0)),
            "prompt_tokens": max(0, int(d.get("prompt_tokens") or 0)),
            "completion_tokens": max(0, int(d.get("completion_tokens") or 0)),
            "reasoning_tokens": max(0, int(d.get("reasoning_tokens") or 0)),
            "total_tokens": max(0, int(d.get("total_tokens") or 0)),
            "latency_sec": round(float(d.get("latency_sec") or 0.0), 2),
            "truncated": bool(d.get("truncated", False)),
            "timestamp": evt.timestamp,
        }
        log_entry = _build_log_entry(out)
        if log_entry is not None:
            out["log"] = log_entry
        if replay:
            out["replay"] = True
        return out

    if evt.event == SSE_PIPELINE_GATE_REPORT:
        out = {
            "event": evt.event,
            "book_id": evt.book_id,
            "book_title": title,
            "chapter": ch,
            "attempt": max(0, int(d.get("attempt") or 0)),
            "max_attempts": max(0, int(d.get("max_attempts") or 0)),
            "passed": bool(d.get("passed", False)),
            "char_count": max(0, int(d.get("char_count") or 0)),
            "min_chars": max(0, int(d.get("min_chars") or 0)),
            "max_chars": max(0, int(d.get("max_chars") or 0)),
            "will_rewrite": bool(d.get("will_rewrite", False)),
            "rules": _sanitize_gate_rules(d.get("rules")),
            "timestamp": evt.timestamp,
        }
        log_entry = _build_log_entry(out)
        if log_entry is not None:
            out["log"] = log_entry
        if replay:
            out["replay"] = True
        return out

    if evt.event == SSE_PIPELINE_MODEL_EVENT:
        kind = str(d.get("kind") or "").strip().lower()
        if kind not in ("model_switched", "rate_limit_retry", "model_restored", "quota_exhausted", "quota_updated"):
            return None
        out = {
            "event": evt.event,
            "book_id": evt.book_id,
            "book_title": title,
            "chapter": ch,
            "kind": kind,
            "role": sanitize_public_text(d.get("role") or "", max_len=24),
            "model": sanitize_public_text(d.get("model") or "", max_len=80),
            "from_model": sanitize_public_text(d.get("from_model") or "", max_len=80),
            "to_model": sanitize_public_text(d.get("to_model") or "", max_len=80),
            "retry_attempt": max(0, int(d.get("retry_attempt") or 0)),
            "max_retries": max(0, int(d.get("max_retries") or 0)),
            "wait_seconds": round(float(d.get("wait_seconds") or 0.0), 1),
            "reason": sanitize_public_text(d.get("reason") or "", max_len=160),
            "timestamp": evt.timestamp,
        }
        if kind == "quota_updated" and isinstance(d.get("quota"), Mapping):
            out["quota"] = _sanitize_public_quota_item(d["quota"])
        log_entry = _build_log_entry(out)
        if log_entry is not None:
            out["log"] = log_entry
        if replay:
            out["replay"] = True
        return out

    if evt.event == SSE_PIPELINE_CHAPTER_LOCKED:
        out = {
            "event": evt.event,
            "book_id": evt.book_id,
            "book_title": title,
            "chapter": ch,
            "title": sanitize_public_text(d.get("title") or "", max_len=80),
            "char_count": max(0, int(d.get("char_count") or 0)),
            "sha256_prefix": re.sub(r"[^a-fA-F0-9]", "", str(d.get("sha256_prefix") or ""))[:12],
            "model": sanitize_public_text(d.get("model") or "", max_len=80),
            "duration_sec": round(float(d.get("duration_sec") or 0.0), 1),
            "attempts_used": max(0, int(d.get("attempts_used") or 0)),
            "rewrites_triggered": max(0, int(d.get("rewrites_triggered") or 0)),
            "timestamp": evt.timestamp,
        }
        log_entry = _build_log_entry(out)
        if log_entry is not None:
            out["log"] = log_entry
        if replay:
            out["replay"] = True
        return out

    return None


def _blank_stage_states() -> Dict[str, Dict[str, Any]]:
    return {
        s: {
            "stage": s,
            "label": PIPELINE_STAGE_LABELS[s],
            "status": "idle",
            "detail": "",
            "model": "",
            "attempt": 0,
            "max_attempts": 0,
            "updated_at": "",
        }
        for s in PIPELINE_STAGES
    }


class PublicPipelineTracker:
    """Thread-safe real-event state tracker for GET /api/public/pipeline and SSE reconnect recovery."""

    def __init__(
        self,
        *,
        replay_mode: bool = False,
        replay_label: str = "",
        log_limit: int = 80,
    ) -> None:
        self._lock = threading.Lock()
        self.replay_mode = bool(replay_mode)
        self.replay_label = sanitize_public_text(replay_label, max_len=80) if replay_mode else ""
        self._state: str = "idle"
        self._idle_reason: str = "待机 / 等待下一章"
        self._last_event_at: str = ""
        self._active_book_id: str = ""
        self._active_book_title: str = ""
        self._active_chapter: int = 0
        self._active_stage: str = ""
        self._active_stage_status: str = "idle"
        self._from_stage: str = ""
        self._stage_states: Dict[str, Dict[str, Any]] = _blank_stage_states()
        self._attempt: int = 0
        self._max_attempts: int = 4
        self._rewrite_count: int = 0
        self._current_model: str = ""
        self._current_model_label: str = ""
        self._thinking: bool = False
        self._thinking_started_at: str = ""
        self._chapter_tokens: Dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "has_usage": False,
        }
        self._gate_rules: List[Dict[str, Any]] = []
        self._gate_summary: Dict[str, Any] = {}
        self._model_alert: Optional[Dict[str, Any]] = None
        self._recent_logs: Deque[Dict[str, Any]] = deque(maxlen=max(10, int(log_limit)))

    def set_replay_mode(self, enabled: bool, label: str = "回放 · 服务器真实历史事件") -> None:
        with self._lock:
            self.replay_mode = bool(enabled)
            self.replay_label = sanitize_public_text(label, max_len=80) if enabled else ""

    def reset_for_idle(self, *, reason: str = "待机 / 等待下一章", last_event_at: str = "") -> None:
        with self._lock:
            self._state = "idle"
            self._idle_reason = sanitize_public_text(reason, max_len=80) or "待机 / 等待下一章"
            self._active_stage = ""
            self._active_stage_status = "idle"
            self._from_stage = ""
            self._thinking = False
            self._thinking_started_at = ""
            self._stage_states = _blank_stage_states()
            if last_event_at:
                self._last_event_at = last_event_at

    def observe(self, evt: SSEEventPayload, titles: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Sanitize an event and fold it into the live command-center state machine."""
        pub = public_event(evt, titles, replay=self.replay_mode)
        if pub is None:
            return None

        ev_name = pub["event"]
        ts = str(pub.get("timestamp") or "")
        book_id = str(pub.get("book_id") or "")
        book_title = sanitize_public_text(titles.get(book_id, book_id), max_len=80) if book_id else ""
        ch = int(pub.get("chapter") or 0)

        with self._lock:
            if ts:
                self._last_event_at = ts
            if book_id:
                self._active_book_id = book_id
                self._active_book_title = book_title or self._active_book_title

            log_item = pub.get("log") or _build_log_entry(pub)
            if log_item is not None:
                self._recent_logs.append(dict(log_item))

            if ev_name == SSE_CHAPTER_STARTED:
                self._active_chapter = ch
                self._state = "running"
                self._idle_reason = ""
                self._attempt = 1
                self._rewrite_count = 0
                self._thinking = False
                self._from_stage = ""
                self._gate_rules = []
                self._gate_summary = {}
                self._chapter_tokens = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                    "has_usage": False,
                }
                self._stage_states = _blank_stage_states()
                self._stage_states["plan"]["status"] = "done"
                self._stage_states["plan"]["detail"] = f"《{book_title}》立项就绪" if book_title else "立项就绪"
                self._stage_states["plan"]["updated_at"] = ts

            elif ev_name == SSE_PIPELINE_STAGE:
                stage = str(pub.get("stage") or "")
                status = str(pub.get("status") or "running")
                if ch > 0 and ch != self._active_chapter and stage in ("outline", "compile", "draft"):
                    self._active_chapter = ch
                    self._rewrite_count = 0
                    self._gate_rules = []
                    self._gate_summary = {}
                    self._chapter_tokens = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 0,
                        "calls": 0,
                        "has_usage": False,
                    }
                    self._stage_states = _blank_stage_states()
                    self._stage_states["plan"]["status"] = "done"
                    self._stage_states["plan"]["updated_at"] = ts

                if stage == "idle":
                    self._state = "idle"
                    self._idle_reason = str(pub.get("detail") or "待机 / 等待下一章")
                    self._active_stage = ""
                    self._active_stage_status = "idle"
                    self._from_stage = ""
                    self._thinking = False
                    self._thinking_started_at = ""
                    for s_key in PIPELINE_STAGES:
                        if self._stage_states[s_key]["status"] in ("running", "rewriting"):
                            self._stage_states[s_key]["status"] = "idle"
                else:
                    if stage == "plan":
                        self._stage_states = _blank_stage_states()
                    else:
                        if self._stage_states["plan"]["status"] == "idle":
                            self._stage_states["plan"]["status"] = "done"
                            self._stage_states["plan"]["detail"] = f"《{self._active_book_title}》" if self._active_book_title else "已立项"
                    self._active_stage = stage
                    self._active_stage_status = status
                    self._from_stage = str(pub.get("from_stage") or "")
                    if pub.get("attempt"):
                        self._attempt = int(pub["attempt"])
                        self._rewrite_count = max(self._rewrite_count, max(0, self._attempt - 1))
                    if pub.get("max_attempts"):
                        self._max_attempts = int(pub["max_attempts"])
                    if pub.get("model"):
                        self._current_model = str(pub["model"])
                    if status == "rewriting":
                        self._state = "rewriting"
                        self._thinking = False
                    elif status in ("running", "done", "failed", "skipped"):
                        self._state = "running"
                        self._idle_reason = ""
                    if stage == "draft" and status == "running" and pub.get("thinking"):
                        self._thinking = True
                        self._thinking_started_at = ts
                    elif stage == "draft" and status != "running":
                        self._thinking = False
                        self._thinking_started_at = ""
                    self._stage_states[stage] = {
                        "stage": stage,
                        "label": PIPELINE_STAGE_LABELS[stage],
                        "status": status,
                        "detail": str(pub.get("detail") or ""),
                        "model": str(pub.get("model") or self._current_model),
                        "attempt": int(pub.get("attempt") or self._attempt),
                        "max_attempts": int(pub.get("max_attempts") or self._max_attempts),
                        "updated_at": ts,
                    }

            elif ev_name == SSE_PIPELINE_TOKEN_USAGE:
                if ch > 0 and self._active_chapter == 0:
                    self._active_chapter = ch
                if pub.get("model"):
                    self._current_model = str(pub["model"])
                if pub.get("model_label"):
                    self._current_model_label = str(pub["model_label"])
                self._chapter_tokens["prompt_tokens"] += int(pub.get("prompt_tokens") or 0)
                self._chapter_tokens["completion_tokens"] += int(pub.get("completion_tokens") or 0)
                self._chapter_tokens["reasoning_tokens"] += int(pub.get("reasoning_tokens") or 0)
                self._chapter_tokens["total_tokens"] += int(pub.get("total_tokens") or 0)
                self._chapter_tokens["calls"] += 1
                self._chapter_tokens["has_usage"] = True

            elif ev_name == SSE_PIPELINE_GATE_REPORT:
                self._gate_rules = list(pub.get("rules") or [])
                self._gate_summary = {
                    "chapter": ch,
                    "attempt": int(pub.get("attempt") or 0),
                    "max_attempts": int(pub.get("max_attempts") or 0),
                    "passed": bool(pub.get("passed")),
                    "char_count": int(pub.get("char_count") or 0),
                    "min_chars": int(pub.get("min_chars") or 0),
                    "max_chars": int(pub.get("max_chars") or 0),
                    "will_rewrite": bool(pub.get("will_rewrite")),
                    "timestamp": ts,
                }

            elif ev_name == SSE_PIPELINE_MODEL_EVENT:
                kind = str(pub.get("kind") or "")
                if kind in ("model_switched", "rate_limit_retry", "model_restored", "quota_exhausted"):
                    self._model_alert = {
                        "kind": kind,
                        "role": str(pub.get("role") or ""),
                        "model": str(pub.get("model") or ""),
                        "from_model": str(pub.get("from_model") or ""),
                        "to_model": str(pub.get("to_model") or ""),
                        "retry_attempt": int(pub.get("retry_attempt") or 0),
                        "max_retries": int(pub.get("max_retries") or 0),
                        "wait_seconds": float(pub.get("wait_seconds") or 0.0),
                        "reason": str(pub.get("reason") or ""),
                        "timestamp": ts,
                    }
                    if kind in ("model_switched", "model_restored") and pub.get("to_model"):
                        self._current_model = str(pub["to_model"])
                    if kind == "quota_exhausted":
                        self._state = "paused_quota"
                        self._thinking = False
                        self._idle_reason = "全部白名单额度用尽 / 超预算熔断停机"

            elif ev_name == SSE_PIPELINE_CHAPTER_LOCKED:
                self._stage_states["lock"]["status"] = "done"
                self._stage_states["lock"]["detail"] = f"sha256:{pub.get('sha256_prefix', '')}"
                self._stage_states["lock"]["updated_at"] = ts
                self._thinking = False

            elif ev_name == SSE_BOOK_ISOLATED:
                self._state = "isolated"
                self._thinking = False
                self._idle_reason = "单书门禁隔离，等待检修"
                if self._active_stage in self._stage_states:
                    self._stage_states[self._active_stage]["status"] = "failed"

        return pub

    def snapshot(self, db: StockpileDB, scheduler: Any = None) -> Dict[str, Any]:
        books = list_public_books(db)
        primary_book = books[0] if books else None
        primary_bid = primary_book["book_id"] if primary_book else None
        prod = db.get_public_production_metrics(primary_bid)
        raw_quotas = scheduler.get_model_quotas() if scheduler is not None and hasattr(scheduler, "get_model_quotas") else []
        safe_quotas = [_sanitize_public_quota_item(q) for q in raw_quotas]

        router = getattr(scheduler, "provider", None) if scheduler is not None else None
        router_model = ""
        router_label = ""
        if router is not None and hasattr(router, "has_role") and router.has_role("writer"):
            router_model = sanitize_public_text(router.get_active_model("writer"), max_len=80)
            cfg = router.get_role_config("writer")
            router_label = sanitize_public_text(cfg.model_labels.get(router_model, ""), max_len=60)

        now_iso = shanghai_now(getattr(db, "_now_fn", None)).isoformat()

        with self._lock:
            state = self._state
            idle_reason = self._idle_reason
            last_event_at = self._last_event_at
            active_bid = self._active_book_id or (primary_book["book_id"] if primary_book else "")
            active_title = self._active_book_title or (primary_book["title"] if primary_book else "")
            current_model = self._current_model or router_model or sanitize_public_text(prod.get("latest_model") or "", max_len=80)
            quota_label = next(
                (str(q.get("label") or "") for q in safe_quotas if q.get("model") == current_model and q.get("label")),
                "",
            )
            current_label = quota_label or (router_label if current_model == router_model else "") or self._current_model_label

            # Honest fallback from persisted book state when no in-flight event has arrived yet
            if state == "idle" and primary_book is not None:
                if primary_book["status"] == "PAUSED_QUOTA_EXHAUSTED":
                    state = "paused_quota"
                    idle_reason = "额度用完，熔断停机"
                elif primary_book["status"] == "ISOLATED_GATE_FAILURE":
                    state = "isolated"
                    idle_reason = "单书门禁隔离，等待检修"
                elif primary_book["status"] == "PAUSED_MANUAL":
                    state = "paused_manual"
                    idle_reason = "手动暂停"
                elif primary_book["status"] == "COMPLETED":
                    idle_reason = "本书已完本 · 待机中"
                else:
                    idle_reason = "待机 / 等待下一章"

            if not last_event_at:
                last_event_at = prod.get("latest_produced_at") or (primary_book["updated_at"] if primary_book else "")

            return {
                "mode": "replay" if self.replay_mode else "live",
                "replay_label": self.replay_label if self.replay_mode else "",
                "server_time": now_iso,
                "state": state,
                "idle_reason": idle_reason,
                "last_event_at": last_event_at,
                "active_book_id": active_bid,
                "active_book_title": active_title,
                "active_chapter": int(self._active_chapter),
                "active_stage": self._active_stage,
                "active_stage_status": self._active_stage_status,
                "from_stage": self._from_stage,
                "stage_states": {k: dict(v) for k, v in self._stage_states.items()},
                "attempt": int(self._attempt),
                "max_attempts": int(self._max_attempts),
                "rewrite_count": int(self._rewrite_count),
                "current_model": current_model,
                "current_model_label": current_label,
                "thinking": bool(self._thinking),
                "thinking_started_at": self._thinking_started_at,
                "chapter_tokens": dict(self._chapter_tokens),
                "gate_rules": list(self._gate_rules),
                "gate_summary": dict(self._gate_summary),
                "model_alert": dict(self._model_alert) if self._model_alert is not None else None,
                "model_quotas": safe_quotas,
                "metrics": {
                    "today_date": prod["today_date"],
                    "today_chapters": int(prod["today_chapters"]),
                    "total_chapters_written": int(prod["total_chapters_written"]),
                    "avg_duration_sec": float(prod["avg_duration_sec"]),
                    "first_pass_rate": float(prod["first_pass_rate"]),
                    "first_pass_count": int(prod["first_pass_count"]),
                    "avg_attempts": float(prod["avg_attempts"]),
                    "total_chars": int(prod["total_chars"]),
                    "book_written_chapters": int(primary_book["written_chapters"]) if primary_book else 0,
                    "book_total_chapters": int(primary_book["total_chapters"]) if primary_book else 0,
                    "current_model": current_model,
                },
                "recent_logs": list(self._recent_logs),
            }


__all__ = [
    "PIPELINE_STAGES",
    "PIPELINE_STAGE_LABELS",
    "PUBLIC_EVENT_NAMES",
    "PublicPipelineTracker",
    "get_public_book",
    "get_public_chapter",
    "list_public_books",
    "public_event",
    "sanitize_public_text",
]
