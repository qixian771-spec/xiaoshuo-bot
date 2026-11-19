from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..contracts import (
    BookRunStatus,
    BookSummaryResponse,
    ChapterStatusItem,
    CompanionLeaseRequest,
    CompanionLeaseResponse,
    CompanionMetricsRequest,
    CompanionReceiptRequest,
    CompanionTaskType,
    StockpileStatus,
    VALID_STOCKPILE_TRANSITIONS,
)
from ..evolution import (
    BookMetrics,
    MetricsNotSettledError,
    MotifWeightStore,
    PackagingPoolExhaustedError,
    ReviewFeedbackOptimizer,
    evaluate_book_racing,
)
from ..models import AUTHOR_LEVELS, SHANGHAI_TZ

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 1800
LV0_1_DAILY_CHAR_CAP = 8500
LV2_PLUS_DAILY_CHAR_CAP = 17000
SAFETY_CUSHION_TARGET = 10
SAFETY_CUSHION_CRITICAL = 6

_WS_RE = re.compile(r"\s+")


class StockpileError(RuntimeError):
    """Base exception for cloud stockpile SQLite persistence and protocol locks."""


class BookNotFoundError(StockpileError, KeyError):
    """Raised when referencing an unregistered book_id."""


class ChapterNotFoundError(StockpileError, KeyError):
    """Raised when referencing an unknown chapter for a book."""


class SequentialLeaseLockError(StockpileError):
    """Raised when trying to lease chapter N (N>1) before chapter N-1 is SCHEDULED or PUBLISHED."""


class InvalidStatusTransitionError(StockpileError):
    """Raised when a chapter status change violates VALID_STOCKPILE_TRANSITIONS."""


class DailyQuotaExceededError(StockpileError):
    """Raised when leasing/scheduling would exceed the account's CST daily character safety cap."""

    def __init__(
        self,
        message: str,
        *,
        account_id: str,
        author_level: str,
        cst_date: str,
        deferred_to_date: str,
        used_chars: int,
        daily_cap: int,
        requested_chars: int,
    ) -> None:
        super().__init__(message)
        self.account_id = account_id
        self.author_level = author_level
        self.cst_date = cst_date
        self.deferred_to_date = deferred_to_date
        self.used_chars = used_chars
        self.daily_cap = daily_cap
        self.requested_chars = requested_chars


class CutConfirmationConflictError(StockpileError):
    """Raised on two-step cut confirmation CAS version mismatch."""


def count_non_ws_chars(text: str) -> int:
    return len(_WS_RE.sub("", text or ""))


def daily_char_cap_for_level(author_level: str) -> int:
    level = str(author_level or "").strip()
    if level == "lv0_1":
        return LV0_1_DAILY_CHAR_CAP
    if level == "lv2_plus":
        return LV2_PLUS_DAILY_CHAR_CAP
    raise ValueError(f"Unknown author_level {author_level!r}; expected one of {AUTHOR_LEVELS}")


def shanghai_now(now_fn: Optional[Callable[[], datetime]] = None) -> datetime:
    dt = now_fn() if now_fn is not None else datetime.now(SHANGHAI_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


def parse_to_shanghai(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=SHANGHAI_TZ)
        return value.astimezone(SHANGHAI_TZ)
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Timestamp string cannot be empty")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


def shanghai_date_str(
    dt_or_iso: Optional[str | datetime] = None,
    *,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> str:
    if dt_or_iso is None or (isinstance(dt_or_iso, str) and not dt_or_iso.strip()):
        dt = shanghai_now(now_fn)
    else:
        dt = parse_to_shanghai(dt_or_iso)
    return dt.strftime("%Y-%m-%d")


def next_shanghai_date_str(cst_date: str) -> str:
    dt = datetime.strptime(cst_date, "%Y-%m-%d").replace(tzinfo=SHANGHAI_TZ)
    return (dt + timedelta(days=1)).strftime("%Y-%m-%d")


class StockpileDB:
    """SQLite WAL stockpile, lease-lock, CST daily quota, and T-1 evolution ledger."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        now_fn: Optional[Callable[[], datetime]] = None,
        safety_cushion_target: int = SAFETY_CUSHION_TARGET,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self.safety_cushion_target = int(safety_cushion_target)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _now_dt(self) -> datetime:
        return shanghai_now(self._now_fn)

    def _now_iso(self) -> str:
        return self._now_dt().isoformat()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA busy_timeout=10000;")
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    book_id TEXT PRIMARY KEY,
                    project_dir TEXT NOT NULL,
                    title TEXT NOT NULL,
                    genre_id TEXT NOT NULL DEFAULT '',
                    author_level TEXT NOT NULL DEFAULT 'lv0_1',
                    contract_track TEXT NOT NULL DEFAULT 'complete_150k',
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    account_id TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    fanqie_book_id TEXT NOT NULL DEFAULT '',
                    daily_target_chapters INTEGER NOT NULL DEFAULT 2,
                    max_published_chapter INTEGER NOT NULL DEFAULT 0,
                    total_chapters INTEGER NOT NULL DEFAULT 0,
                    signed INTEGER NOT NULL DEFAULT 0,
                    paused_reason TEXT NOT NULL DEFAULT '',
                    isolation_detail_json TEXT NOT NULL DEFAULT '{}',
                    alternate_titles_json TEXT NOT NULL DEFAULT '[]',
                    alternate_blurbs_json TEXT NOT NULL DEFAULT '[]',
                    cover_path TEXT NOT NULL DEFAULT '',
                    opening_motif TEXT NOT NULL DEFAULT '',
                    golden_finger_type TEXT NOT NULL DEFAULT '',
                    cut_confirmation_version INTEGER NOT NULL DEFAULT 0,
                    cut_pending_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chapters (
                    book_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL,
                    public_title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    author_note TEXT NOT NULL DEFAULT '',
                    char_count INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    locked INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'STOCKPILED',
                    task_id TEXT NOT NULL DEFAULT '',
                    leased_by_account TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    lease_expires_epoch REAL NOT NULL DEFAULT 0.0,
                    remote_chapter_id TEXT NOT NULL DEFAULT '',
                    scheduled_at TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    scheduled_cst_date TEXT NOT NULL DEFAULT '',
                    quota_deferred_to_date TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (book_id, chapter),
                    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_chapters_status ON chapters (book_id, status, chapter);
                CREATE INDEX IF NOT EXISTS idx_chapters_cst ON chapters (leased_by_account, scheduled_cst_date, status);

                CREATE TABLE IF NOT EXISTS companion_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS metrics_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL,
                    metrics_date TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    settled INTEGER NOT NULL,
                    settlement_status TEXT NOT NULL,
                    ch2_read_through REAL,
                    word100k_completion REAL,
                    click_through_rate REAL,
                    bookshelf_rate REAL,
                    impressions INTEGER,
                    daily_revenue_cny REAL,
                    comments_json TEXT NOT NULL DEFAULT '[]',
                    decoded_hotlist_json TEXT NOT NULL DEFAULT '[]',
                    racing_decision_json TEXT NOT NULL DEFAULT '{}',
                    optimization_report_json TEXT NOT NULL DEFAULT '{}',
                    payload_sha256 TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (book_id, metrics_date),
                    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS hotlist_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL,
                    metrics_date TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (book_id, metrics_date)
                );

                CREATE TABLE IF NOT EXISTS chapter_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL,
                    metrics_date TEXT NOT NULL,
                    chapter INTEGER NOT NULL DEFAULT 1,
                    comment_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    likes INTEGER NOT NULL DEFAULT 0,
                    sentiment TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outline_directives (
                    directive_id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL,
                    metrics_date TEXT NOT NULL,
                    target_chapter INTEGER NOT NULL,
                    max_published_chapter INTEGER NOT NULL,
                    signal_type TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    instruction TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cut_recommendations (
                    book_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION',
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promotion_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL,
                    metrics_date TEXT NOT NULL,
                    previous_daily_chapters INTEGER NOT NULL,
                    new_daily_chapters INTEGER NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 1,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS captcha_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT '',
                    chapter INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL,
                    page_url TEXT NOT NULL DEFAULT '',
                    screenshot_base64 TEXT NOT NULL DEFAULT '',
                    screenshot_mime TEXT NOT NULL DEFAULT 'image/png',
                    resolved INTEGER NOT NULL DEFAULT 0,
                    detected_at TEXT NOT NULL,
                    resolved_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS production_stats (
                    book_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL,
                    duration_sec REAL NOT NULL,
                    attempts_used INTEGER NOT NULL,
                    rewrites_triggered INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    produced_at TEXT NOT NULL,
                    PRIMARY KEY (book_id, chapter)
                );

                CREATE TABLE IF NOT EXISTS wizard_sessions (
                    session_id TEXT PRIMARY KEY,
                    idea TEXT NOT NULL,
                    status TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    choices_json TEXT NOT NULL DEFAULT '[]',
                    options_json TEXT NOT NULL DEFAULT '[]',
                    book_id TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Legacy schema migration for existing matrix.db databases created before T5.
            wizard_cols = {
                row[1] for row in cur.execute("PRAGMA table_info(wizard_sessions)").fetchall()
            }
            if "progress_json" not in wizard_cols:
                cur.execute(
                    "ALTER TABLE wizard_sessions ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "error_message" not in wizard_cols:
                cur.execute(
                    "ALTER TABLE wizard_sessions ADD COLUMN error_message TEXT NOT NULL DEFAULT ''"
                )
            self._migrate_legacy_manual_pause_books_unlocked()

    def _migrate_legacy_manual_pause_books_unlocked(self) -> int:
        """Migrate legacy manually paused books (old PAUSED or manual PAUSED_QUOTA_EXHAUSTED) to PAUSED_MANUAL."""
        manual_markers = (
            "manually paused",
            "manual pause",
            "手动暂停",
            "人工暂停",
            "t5 试跑完成",
        )
        quota_markers = (
            "quota",
            "budget",
            "429",
            "exhausted",
            "exceeded",
            "circuit",
            "额度",
            "预算",
        )
        rows = self._conn.execute(
            """
            SELECT book_id, status, paused_reason
            FROM books
            WHERE status IN ('PAUSED', ?)
            """,
            (BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value,),
        ).fetchall()
        migrated = 0
        now_iso = self._now_iso()
        for row in rows:
            status = str(row["status"] or "")
            reason = str(row["paused_reason"] or "")
            low = reason.strip().lower()
            should_migrate = False
            if status == "PAUSED":
                should_migrate = True
            elif status == BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value:
                has_manual = any(m in low for m in manual_markers)
                has_quota = any(q in low for q in quota_markers)
                if has_manual and not has_quota:
                    should_migrate = True
            if should_migrate:
                self._conn.execute(
                    """
                    UPDATE books
                    SET status = ?, updated_at = ?
                    WHERE book_id = ?
                    """,
                    (BookRunStatus.PAUSED_MANUAL.value, now_iso, str(row["book_id"])),
                )
                migrated += 1
        return migrated

    def migrate_legacy_manual_pause_books(self) -> int:
        with self._lock:
            return self._migrate_legacy_manual_pause_books_unlocked()

    def get_journal_mode(self) -> str:
        with self._lock:
            row = self._conn.execute("PRAGMA journal_mode;").fetchone()
            return str(row[0]).lower() if row else ""

    # -----------------------------------------------------------------------
    # Production KPI (写稿耗时 vs 重写率)
    # -----------------------------------------------------------------------
    def record_production_stat(
        self,
        book_id: str,
        chapter: int,
        *,
        duration_sec: float,
        attempts_used: int,
        rewrites_triggered: int,
        char_count: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO production_stats
                    (book_id, chapter, duration_sec, attempts_used, rewrites_triggered, char_count, produced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id, chapter) DO UPDATE SET
                    duration_sec = excluded.duration_sec,
                    attempts_used = excluded.attempts_used,
                    rewrites_triggered = excluded.rewrites_triggered,
                    char_count = excluded.char_count,
                    produced_at = excluded.produced_at
                """,
                (
                    book_id,
                    int(chapter),
                    float(duration_sec),
                    int(attempts_used),
                    int(rewrites_triggered),
                    int(char_count),
                    self._now_iso(),
                ),
            )

    def get_production_kpi(self, book_id: Optional[str] = None) -> Dict[str, Any]:
        where = "WHERE book_id = ?" if book_id else ""
        params: Tuple[Any, ...] = (book_id,) if book_id else ()
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COUNT(*) AS chapters,
                       COALESCE(AVG(duration_sec), 0) AS avg_duration_sec,
                       COALESCE(SUM(CASE WHEN rewrites_triggered > 0 THEN 1 ELSE 0 END), 0) AS rewritten,
                       COALESCE(AVG(attempts_used), 0) AS avg_attempts,
                       COALESCE(SUM(char_count), 0) AS total_chars
                FROM production_stats {where}
                """,
                params,
            ).fetchone()
        chapters = int(row["chapters"])
        return {
            "chapters": chapters,
            "avg_duration_sec": round(float(row["avg_duration_sec"]), 1),
            "rewrite_rate": round(int(row["rewritten"]) / chapters, 4) if chapters else 0.0,
            "avg_attempts": round(float(row["avg_attempts"]), 2),
            "total_chars": int(row["total_chars"]),
        }

    # -----------------------------------------------------------------------
    # Admin new-book wizard sessions
    # -----------------------------------------------------------------------
    def create_wizard_session(self, session_id: str, idea: str, options: List[Dict[str, Any]]) -> None:
        now = self._now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO wizard_sessions
                    (session_id, idea, status, step_index, choices_json, options_json, progress_json, created_at, updated_at)
                VALUES (?, ?, 'IN_PROGRESS', 0, '[]', ?, '{}', ?, ?)
                """,
                (session_id, idea, json.dumps(options, ensure_ascii=False), now, now),
            )

    @staticmethod
    def _row_to_wizard_session(row: sqlite3.Row) -> Dict[str, Any]:
        raw_prog = row["progress_json"] if "progress_json" in row.keys() else "{}"
        progress = json.loads(raw_prog) if raw_prog else {}
        return {
            "session_id": row["session_id"],
            "idea": row["idea"],
            "status": row["status"],
            "step_index": int(row["step_index"]),
            "choices": json.loads(row["choices_json"]),
            "options": json.loads(row["options_json"]),
            "book_id": row["book_id"],
            "error_message": row["error_message"],
            "progress": progress,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_wizard_session(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wizard_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Wizard session '{session_id}' not found")
        return self._row_to_wizard_session(row)

    def update_wizard_session(
        self,
        session_id: str,
        *,
        status: str,
        step_index: int,
        choices: List[Dict[str, Any]],
        options: List[Dict[str, Any]],
        book_id: str = "",
        error_message: str = "",
        progress: Optional[Mapping[str, Any]] = None,
    ) -> None:
        now = self._now_iso()
        with self._lock:
            if progress is None:
                cur = self._conn.execute(
                    """
                    UPDATE wizard_sessions
                    SET status = ?, step_index = ?, choices_json = ?, options_json = ?,
                        book_id = ?, error_message = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        status,
                        int(step_index),
                        json.dumps(choices, ensure_ascii=False),
                        json.dumps(options, ensure_ascii=False),
                        book_id,
                        error_message,
                        now,
                        session_id,
                    ),
                )
            else:
                cur = self._conn.execute(
                    """
                    UPDATE wizard_sessions
                    SET status = ?, step_index = ?, choices_json = ?, options_json = ?,
                        book_id = ?, error_message = ?, progress_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        status,
                        int(step_index),
                        json.dumps(choices, ensure_ascii=False),
                        json.dumps(options, ensure_ascii=False),
                        book_id,
                        error_message,
                        json.dumps(dict(progress), ensure_ascii=False),
                        now,
                        session_id,
                    ),
                )
            if cur.rowcount != 1:
                raise KeyError(f"Wizard session '{session_id}' not found")

    def update_wizard_progress(self, session_id: str, progress: Mapping[str, Any]) -> None:
        now = self._now_iso()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE wizard_sessions
                SET progress_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (json.dumps(dict(progress), ensure_ascii=False), now, session_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"Wizard session '{session_id}' not found")

    def fail_interrupted_planning_sessions(
        self, reason: str = "服务重启中断了立项，请重新点确认"
    ) -> int:
        now = self._now_iso()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE wizard_sessions
                SET status = 'FAILED', error_message = ?, updated_at = ?
                WHERE status = 'PLANNING'
                """,
                (reason, now),
            )
            return int(cur.rowcount)

    def try_start_wizard_planning(
        self,
        session_id: str,
        *,
        initial_progress: Mapping[str, Any],
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Atomically transition READY_TO_CONFIRM/FAILED -> PLANNING if no session is currently PLANNING.

        Returns (code, session_or_none) where code is:
        - "OK": transitioned and session returned
        - "BUSY_SELF": this session is already PLANNING
        - "BUSY_OTHER": another session is currently PLANNING
        - "INVALID_STATUS:<status>": session is not READY_TO_CONFIRM or FAILED
        """
        now = self._now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wizard_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Wizard session '{session_id}' not found")
            current_status = str(row["status"])
            if current_status == "PLANNING":
                return "BUSY_SELF", None
            if current_status not in ("READY_TO_CONFIRM", "FAILED"):
                return f"INVALID_STATUS:{current_status}", None
            busy_other = self._conn.execute(
                "SELECT session_id FROM wizard_sessions WHERE status = 'PLANNING' AND session_id != ? LIMIT 1",
                (session_id,),
            ).fetchone()
            if busy_other is not None:
                return "BUSY_OTHER", None
            self._conn.execute(
                """
                UPDATE wizard_sessions
                SET status = 'PLANNING', options_json = '[]', error_message = '',
                    progress_json = ?, updated_at = ?
                WHERE session_id = ? AND status IN ('READY_TO_CONFIRM', 'FAILED')
                """,
                (json.dumps(dict(initial_progress), ensure_ascii=False), now, session_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM wizard_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert updated is not None
            return "OK", self._row_to_wizard_session(updated)

    # -----------------------------------------------------------------------
    # Book CRUD & Status
    # -----------------------------------------------------------------------
    def register_book(
        self,
        *,
        book_id: str,
        project_dir: str | Path,
        title: str,
        genre_id: str = "",
        author_level: str = "lv0_1",
        contract_track: str = "complete_150k",
        status: BookRunStatus | str = BookRunStatus.ACTIVE,
        account_id: str = "",
        author_name: str = "",
        fanqie_book_id: str = "",
        daily_target_chapters: int = 2,
        max_published_chapter: int = 0,
        total_chapters: int = 0,
        signed: bool = False,
        alternate_titles: Optional[Sequence[str]] = None,
        alternate_blurbs: Optional[Sequence[str]] = None,
        cover_path: str = "",
        opening_motif: str = "",
        golden_finger_type: str = "",
    ) -> Dict[str, Any]:
        if not book_id or not str(book_id).strip():
            raise ValueError("book_id cannot be empty")
        if author_level not in AUTHOR_LEVELS:
            raise ValueError(f"Unknown author_level {author_level!r}; expected {AUTHOR_LEVELS}")
        run_status = BookRunStatus(status.value if isinstance(status, BookRunStatus) else str(status))
        now_iso = self._now_iso()
        proj_dir_str = str(Path(project_dir).resolve())

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO books (
                    book_id, project_dir, title, genre_id, author_level, contract_track,
                    status, account_id, author_name, fanqie_book_id, daily_target_chapters,
                    max_published_chapter, total_chapters, signed, paused_reason,
                    isolation_detail_json, alternate_titles_json, alternate_blurbs_json,
                    cover_path, opening_motif, golden_finger_type, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '{}', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    project_dir = excluded.project_dir,
                    title = excluded.title,
                    genre_id = excluded.genre_id,
                    author_level = excluded.author_level,
                    contract_track = excluded.contract_track,
                    account_id = excluded.account_id,
                    author_name = excluded.author_name,
                    fanqie_book_id = excluded.fanqie_book_id,
                    total_chapters = excluded.total_chapters,
                    signed = excluded.signed,
                    alternate_titles_json = excluded.alternate_titles_json,
                    alternate_blurbs_json = excluded.alternate_blurbs_json,
                    cover_path = CASE WHEN excluded.cover_path != '' THEN excluded.cover_path ELSE books.cover_path END,
                    opening_motif = CASE WHEN excluded.opening_motif != '' THEN excluded.opening_motif ELSE books.opening_motif END,
                    golden_finger_type = CASE WHEN excluded.golden_finger_type != '' THEN excluded.golden_finger_type ELSE books.golden_finger_type END,
                    updated_at = excluded.updated_at
                """,
                (
                    book_id.strip(),
                    proj_dir_str,
                    title,
                    genre_id,
                    author_level,
                    contract_track,
                    run_status.value,
                    account_id,
                    author_name,
                    fanqie_book_id,
                    int(daily_target_chapters),
                    int(max_published_chapter),
                    int(total_chapters),
                    1 if signed else 0,
                    json.dumps(list(alternate_titles or []), ensure_ascii=False),
                    json.dumps(list(alternate_blurbs or []), ensure_ascii=False),
                    cover_path,
                    opening_motif,
                    golden_finger_type,
                    now_iso,
                    now_iso,
                ),
            )
            return self.get_book(book_id.strip())

    def _row_to_book_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["signed"] = bool(d["signed"])
        d["isolation_detail"] = json.loads(d.pop("isolation_detail_json") or "{}")
        d["alternate_titles"] = json.loads(d.pop("alternate_titles_json") or "[]")
        d["alternate_blurbs"] = json.loads(d.pop("alternate_blurbs_json") or "[]")
        return d

    def get_book(self, book_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone()
            if row is None:
                raise BookNotFoundError(f"Book '{book_id}' not found")
            return self._row_to_book_dict(row)

    def list_books(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM books ORDER BY created_at ASC, book_id ASC").fetchall()
            return [self._row_to_book_dict(r) for r in rows]

    def update_book_status(
        self,
        book_id: str,
        status: BookRunStatus | str,
        *,
        paused_reason: str = "",
        isolation_detail: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        run_status = BookRunStatus(status.value if isinstance(status, BookRunStatus) else str(status))
        now_iso = self._now_iso()
        with self._lock:
            self.get_book(book_id)
            if isolation_detail is not None:
                iso_json = json.dumps(dict(isolation_detail), ensure_ascii=False)
                self._conn.execute(
                    """
                    UPDATE books
                    SET status = ?, paused_reason = ?, isolation_detail_json = ?, updated_at = ?
                    WHERE book_id = ?
                    """,
                    (run_status.value, paused_reason, iso_json, now_iso, book_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE books
                    SET status = ?, paused_reason = ?, updated_at = ?
                    WHERE book_id = ?
                    """,
                    (run_status.value, paused_reason, now_iso, book_id),
                )
            return self.get_book(book_id)

    def set_book_cover_path(self, book_id: str, cover_path: str | Path) -> Dict[str, Any]:
        now_iso = self._now_iso()
        with self._lock:
            self.get_book(book_id)
            self._conn.execute(
                "UPDATE books SET cover_path = ?, updated_at = ? WHERE book_id = ?",
                (str(Path(cover_path).resolve()), now_iso, book_id),
            )
            return self.get_book(book_id)

    def update_book_total_chapters(self, book_id: str, total_chapters: int) -> Dict[str, Any]:
        now_iso = self._now_iso()
        with self._lock:
            self.get_book(book_id)
            self._conn.execute(
                "UPDATE books SET total_chapters = ?, updated_at = ? WHERE book_id = ?",
                (int(total_chapters), now_iso, book_id),
            )
            return self.get_book(book_id)

    def update_book_daily_target(self, book_id: str, daily_target_chapters: int) -> Dict[str, Any]:
        now_iso = self._now_iso()
        with self._lock:
            self.get_book(book_id)
            self._conn.execute(
                "UPDATE books SET daily_target_chapters = ?, updated_at = ? WHERE book_id = ?",
                (max(1, int(daily_target_chapters)), now_iso, book_id),
            )
            return self.get_book(book_id)

    def get_book_summary(self, book_id: str) -> BookSummaryResponse:
        with self._lock:
            book = self.get_book(book_id)
            stockpiled = self.count_stockpiled(book_id)
            low_alert = (
                book["status"] == BookRunStatus.ACTIVE.value
                and stockpiled < self.safety_cushion_target
            )
            cover_url = f"/api/books/{book_id}/cover" if book.get("cover_path") else ""
            return BookSummaryResponse(
                book_id=book["book_id"],
                title=book["title"],
                genre_id=book["genre_id"],
                author_level=book["author_level"],
                contract_track=book["contract_track"],
                status=book["status"],
                stockpiled_count=stockpiled,
                safety_cushion_target=self.safety_cushion_target,
                low_cushion_alert=low_alert,
                max_published_chapter=int(book["max_published_chapter"]),
                total_chapters=int(book["total_chapters"]),
                daily_target_chapters=int(book["daily_target_chapters"]),
                account_id=book["account_id"],
                paused_reason=book["paused_reason"],
                alternate_titles=list(book["alternate_titles"]),
                cover_url=cover_url,
                updated_at=book["updated_at"],
            )

    def list_book_summaries(self) -> List[BookSummaryResponse]:
        with self._lock:
            return [self.get_book_summary(b["book_id"]) for b in self.list_books()]

    # -----------------------------------------------------------------------
    # Two-Step Cut Confirmation State (CAS)
    # -----------------------------------------------------------------------
    def request_cut_book(self, book_id: str, reason: str = "") -> Dict[str, Any]:
        now_iso = self._now_iso()
        with self._lock:
            book = self.get_book(book_id)
            next_ver = int(book["cut_confirmation_version"]) + 1
            self._conn.execute(
                """
                UPDATE books
                SET cut_confirmation_version = ?, cut_pending_reason = ?, updated_at = ?
                WHERE book_id = ?
                """,
                (next_ver, reason, now_iso, book_id),
            )
            return {
                "book_id": book_id,
                "status": "PENDING_CONFIRMATION",
                "confirmation_version": next_ver,
                "reason": reason,
            }

    def confirm_cut_book(
        self, book_id: str, confirmation_version: int, reason: str = ""
    ) -> Dict[str, Any]:
        now_iso = self._now_iso()
        with self._lock:
            book = self.get_book(book_id)
            current_ver = int(book["cut_confirmation_version"])
            if current_ver <= 0 or int(confirmation_version) != current_ver:
                raise CutConfirmationConflictError(
                    f"Cut confirmation version mismatch for '{book_id}': "
                    f"expected {current_ver}, got {confirmation_version}"
                )
            eff_reason = reason.strip() or book["cut_pending_reason"] or "Confirmed cut-loss"
            self._conn.execute(
                """
                UPDATE books
                SET status = ?, paused_reason = ?, cut_confirmation_version = ?,
                    cut_pending_reason = '', updated_at = ?
                WHERE book_id = ?
                """,
                (BookRunStatus.CUT_LOSS.value, eff_reason, current_ver + 1, now_iso, book_id),
            )
            self._conn.execute(
                """
                UPDATE cut_recommendations
                SET status = 'CONFIRMED_CUT', updated_at = ?
                WHERE book_id = ?
                """,
                (now_iso, book_id),
            )
            return {
                "book_id": book_id,
                "status": BookRunStatus.CUT_LOSS.value,
                "confirmation_version": int(confirmation_version),
                "reason": eff_reason,
            }

    def cancel_cut_book(self, book_id: str) -> Dict[str, Any]:
        now_iso = self._now_iso()
        with self._lock:
            book = self.get_book(book_id)
            next_ver = int(book["cut_confirmation_version"]) + 1
            self._conn.execute(
                """
                UPDATE books
                SET cut_confirmation_version = ?, cut_pending_reason = '', updated_at = ?
                WHERE book_id = ?
                """,
                (next_ver, now_iso, book_id),
            )
            self._conn.execute(
                """
                UPDATE cut_recommendations
                SET status = 'CANCELLED', updated_at = ?
                WHERE book_id = ?
                """,
                (now_iso, book_id),
            )
            return {"book_id": book_id, "status": "CANCELLED"}

    # -----------------------------------------------------------------------
    # Stockpile Chapters & Locking
    # -----------------------------------------------------------------------
    def add_stockpiled_chapter(
        self,
        book_id: str,
        chapter: int,
        public_title: str,
        body: str,
        *,
        author_note: str = "",
        sha256: str = "",
        locked: bool = False,
        char_count: Optional[int] = None,
        overwrite_if_stockpiled: bool = True,
    ) -> ChapterStatusItem:
        if chapter <= 0:
            raise ValueError(f"chapter must be positive, got {chapter}")
        if not body or not body.strip():
            raise ValueError(f"Chapter {chapter} body cannot be empty")
        computed_sha = sha256.strip() or hashlib.sha256(body.encode("utf-8")).hexdigest()
        computed_chars = int(char_count) if char_count is not None else count_non_ws_chars(body)
        now_iso = self._now_iso()

        with self._lock:
            self.get_book(book_id)
            existing = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? AND chapter = ?",
                (book_id, chapter),
            ).fetchone()
            if existing is not None:
                if existing["status"] != StockpileStatus.STOCKPILED.value:
                    raise InvalidStatusTransitionError(
                        f"Cannot overwrite chapter {chapter} of '{book_id}' in status {existing['status']!r}"
                    )
                if not overwrite_if_stockpiled:
                    return self._row_to_chapter_item(existing)
                self._conn.execute(
                    """
                    UPDATE chapters
                    SET public_title = ?, body = ?, author_note = ?, char_count = ?,
                        sha256 = ?, locked = ?, updated_at = ?
                    WHERE book_id = ? AND chapter = ?
                    """,
                    (
                        public_title.strip(),
                        body,
                        author_note.strip(),
                        computed_chars,
                        computed_sha,
                        1 if locked else 0,
                        now_iso,
                        book_id,
                        chapter,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO chapters (
                        book_id, chapter, public_title, body, author_note, char_count,
                        sha256, locked, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        book_id,
                        chapter,
                        public_title.strip(),
                        body,
                        author_note.strip(),
                        computed_chars,
                        computed_sha,
                        1 if locked else 0,
                        StockpileStatus.STOCKPILED.value,
                        now_iso,
                        now_iso,
                    ),
                )
            row = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? AND chapter = ?",
                (book_id, chapter),
            ).fetchone()
            return self._row_to_chapter_item(row)

    def _row_to_chapter_item(self, row: sqlite3.Row) -> ChapterStatusItem:
        return ChapterStatusItem(
            chapter=int(row["chapter"]),
            public_title=str(row["public_title"]),
            status=str(row["status"]),
            char_count=int(row["char_count"]),
            locked=bool(row["locked"]),
            sha256=str(row["sha256"]),
            author_note=str(row["author_note"]),
            scheduled_at=str(row["scheduled_at"]),
            published_at=str(row["published_at"]),
            error_message=str(row["error_message"]),
        )

    def list_chapters(self, book_id: str) -> List[ChapterStatusItem]:
        with self._lock:
            self.get_book(book_id)
            self.expire_stale_leases()
            rows = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? ORDER BY chapter ASC",
                (book_id,),
            ).fetchall()
            return [self._row_to_chapter_item(r) for r in rows]

    def get_chapter(self, book_id: str, chapter: int) -> Dict[str, Any]:
        with self._lock:
            self.get_book(book_id)
            self.expire_stale_leases()
            row = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? AND chapter = ?",
                (book_id, int(chapter)),
            ).fetchone()
            if row is None:
                raise ChapterNotFoundError(f"Chapter {chapter} for book '{book_id}' not found")
            item = self._row_to_chapter_item(row).to_dict()
            item["body"] = str(row["body"])
            item["book_id"] = str(row["book_id"])
            item["scheduled_cst_date"] = str(row["scheduled_cst_date"])
            item["quota_deferred_to_date"] = str(row["quota_deferred_to_date"])
            return item

    def set_chapter_lock(
        self, book_id: str, chapter: int, locked: bool, reason: str = ""
    ) -> ChapterStatusItem:
        now_iso = self._now_iso()
        with self._lock:
            self.get_book(book_id)
            row = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? AND chapter = ?",
                (book_id, int(chapter)),
            ).fetchone()
            if row is None:
                raise ChapterNotFoundError(f"Chapter {chapter} for book '{book_id}' not found")
            self._conn.execute(
                "UPDATE chapters SET locked = ?, updated_at = ? WHERE book_id = ? AND chapter = ?",
                (1 if locked else 0, now_iso, book_id, int(chapter)),
            )
            updated = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? AND chapter = ?",
                (book_id, int(chapter)),
            ).fetchone()
            return self._row_to_chapter_item(updated)

    def count_stockpiled(self, book_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM chapters WHERE book_id = ? AND status = ?",
                (book_id, StockpileStatus.STOCKPILED.value),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_existing_chapter_numbers(self, book_id: str) -> Set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chapter FROM chapters WHERE book_id = ?",
                (book_id,),
            ).fetchall()
            return {int(r["chapter"]) for r in rows}

    # -----------------------------------------------------------------------
    # Lease Expiry, Sequential Lock & CST Daily Word Quota Lock
    # -----------------------------------------------------------------------
    def expire_stale_leases(self) -> int:
        now_dt = self._now_dt()
        now_epoch = now_dt.timestamp()
        now_iso = now_dt.isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT book_id, chapter FROM chapters
                WHERE status = ? AND lease_expires_epoch > 0 AND lease_expires_epoch <= ?
                """,
                (StockpileStatus.LEASED.value, now_epoch),
            ).fetchall()
            if not rows:
                return 0
            for r in rows:
                self._conn.execute(
                    """
                    UPDATE chapters
                    SET status = ?, task_id = '', leased_by_account = '',
                        lease_expires_at = '', lease_expires_epoch = 0.0,
                        scheduled_cst_date = '', updated_at = ?
                    WHERE book_id = ? AND chapter = ?
                    """,
                    (
                        StockpileStatus.STOCKPILED.value,
                        now_iso,
                        r["book_id"],
                        int(r["chapter"]),
                    ),
                )
            return len(rows)

    def get_daily_used_chars(
        self,
        account_id: str,
        cst_date: Optional[str] = None,
    ) -> int:
        target_date = cst_date or shanghai_date_str(now_fn=self._now_fn)
        with self._lock:
            self.expire_stale_leases()
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(c.char_count), 0) AS total_chars
                FROM chapters c
                JOIN books b ON c.book_id = b.book_id
                WHERE COALESCE(NULLIF(c.leased_by_account, ''), b.account_id) = ?
                  AND c.scheduled_cst_date = ?
                  AND c.status IN (?, ?, ?, ?)
                """,
                (
                    account_id,
                    target_date,
                    StockpileStatus.LEASED.value,
                    StockpileStatus.DRAFT_SAVED.value,
                    StockpileStatus.SCHEDULED.value,
                    StockpileStatus.PUBLISHED.value,
                ),
            ).fetchone()
            return int(row["total_chars"]) if row else 0

    def get_daily_quota_status(
        self,
        account_id: str,
        author_level: str = "lv0_1",
        cst_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        target_date = cst_date or shanghai_date_str(now_fn=self._now_fn)
        cap = daily_char_cap_for_level(author_level)
        used = self.get_daily_used_chars(account_id, target_date)
        return {
            "account_id": account_id,
            "author_level": author_level,
            "cst_date": target_date,
            "used_chars": used,
            "daily_cap": cap,
            "remaining_chars": max(0, cap - used),
            "next_cst_date": next_shanghai_date_str(target_date),
        }

    def get_today_generated_chars(self, cst_date: Optional[str] = None) -> int:
        target_date = cst_date or shanghai_date_str(now_fn=self._now_fn)
        with self._lock:
            rows = self._conn.execute(
                "SELECT char_count, created_at FROM chapters",
            ).fetchall()
            total = 0
            for r in rows:
                try:
                    if shanghai_date_str(str(r["created_at"])) == target_date:
                        total += int(r["char_count"])
                except ValueError:
                    continue
            return total

    def _is_predecessor_ready_unlocked(self, book_id: str, chapter: int) -> Tuple[bool, str]:
        if chapter <= 1:
            return True, ""
        prev = self._conn.execute(
            "SELECT status FROM chapters WHERE book_id = ? AND chapter = ?",
            (book_id, chapter - 1),
        ).fetchone()
        if prev is None:
            return False, (
                f"Sequential lease lock: chapter {chapter} requires chapter {chapter - 1} "
                f"to exist in SCHEDULED or PUBLISHED status (missing)."
            )
        prev_status = str(prev["status"])
        if prev_status not in (
            StockpileStatus.SCHEDULED.value,
            StockpileStatus.PUBLISHED.value,
        ):
            return False, (
                f"Sequential lease lock: chapter {chapter} cannot be leased while "
                f"chapter {chapter - 1} is {prev_status!r} (requires SCHEDULED or PUBLISHED)."
            )
        return True, ""

    def poll_available_tasks(
        self,
        account_id: str,
        *,
        book_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return available companion tasks respecting sequential lock & CST daily word quota."""
        acct = (account_id or "").strip()
        if not acct:
            raise ValueError("account_id cannot be empty")
        allowed_books = {str(b).strip() for b in book_ids if str(b).strip()} if book_ids else None
        today_cst = shanghai_date_str(now_fn=self._now_fn)
        next_cst = next_shanghai_date_str(today_cst)

        with self._lock:
            self.expire_stale_leases()
            tasks: List[Dict[str, Any]] = []

            # 1. Pending non-chapter companion tasks (e.g. ROTATE_PACKAGING)
            pending_rows = self._conn.execute(
                """
                SELECT * FROM companion_tasks
                WHERE status = 'PENDING' AND (account_id = ? OR account_id = '')
                ORDER BY created_at ASC
                """,
                (acct,),
            ).fetchall()
            for tr in pending_rows:
                if allowed_books is not None and tr["book_id"] not in allowed_books:
                    continue
                payload = json.loads(tr["payload_json"] or "{}")
                tasks.append(
                    {
                        "task_id": str(tr["task_id"]),
                        "task_type": str(tr["task_type"]),
                        "book_id": str(tr["book_id"]),
                        "account_id": acct,
                        **payload,
                    }
                )

            # 2. Sequential stockpiled chapters eligible for leasing today (CST)
            book_rows = self._conn.execute(
                """
                SELECT * FROM books
                WHERE status IN (?, ?, ?)
                  AND (account_id = ? OR account_id = '')
                ORDER BY created_at ASC, book_id ASC
                """,
                (
                    BookRunStatus.ACTIVE.value,
                    BookRunStatus.ISOLATED_GATE_FAILURE.value,
                    BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value,
                    acct,
                ),
            ).fetchall()

            for brow in book_rows:
                bid = str(brow["book_id"])
                if allowed_books is not None and bid not in allowed_books:
                    continue
                ch_row = self._conn.execute(
                    """
                    SELECT * FROM chapters
                    WHERE book_id = ? AND status = ?
                    ORDER BY chapter ASC
                    LIMIT 1
                    """,
                    (bid, StockpileStatus.STOCKPILED.value),
                ).fetchone()
                if ch_row is None:
                    continue

                ch_no = int(ch_row["chapter"])
                ok_seq, _ = self._is_predecessor_ready_unlocked(bid, ch_no)
                if not ok_seq:
                    continue

                cap = daily_char_cap_for_level(str(brow["author_level"]))
                used_today = self.get_daily_used_chars(acct, today_cst)
                ch_chars = int(ch_row["char_count"])
                if used_today + ch_chars > cap:
                    self._conn.execute(
                        """
                        UPDATE chapters SET quota_deferred_to_date = ?, updated_at = ?
                        WHERE book_id = ? AND chapter = ?
                        """,
                        (next_cst, self._now_iso(), bid, ch_no),
                    )
                    continue

                tasks.append(
                    {
                        "task_id": f"pub_{bid}_ch{ch_no:04d}",
                        "task_type": CompanionTaskType.PUBLISH_CHAPTER.value,
                        "book_id": bid,
                        "book_title": str(brow["title"]),
                        "author_name": str(brow["author_name"]),
                        "fanqie_book_id": str(brow["fanqie_book_id"]),
                        "chapter": ch_no,
                        "public_title": str(ch_row["public_title"]),
                        "char_count": ch_chars,
                        "sha256": str(ch_row["sha256"]),
                        "scheduled_cst_date": today_cst,
                    }
                )
            return tasks

    def lease_chapter(self, req: CompanionLeaseRequest) -> CompanionLeaseResponse:
        if req.lease_seconds <= 0:
            raise ValueError(f"lease_seconds must be positive, got {req.lease_seconds}")
        if req.lease_seconds > MAX_LEASE_SECONDS:
            raise ValueError(
                f"lease_seconds must be <= {MAX_LEASE_SECONDS}, got {req.lease_seconds}"
            )
        now_dt = self._now_dt()
        now_iso = now_dt.isoformat()
        today_cst = shanghai_date_str(now_dt)
        next_cst = next_shanghai_date_str(today_cst)
        expires_dt = now_dt + timedelta(seconds=int(req.lease_seconds))

        with self._lock:
            self.expire_stale_leases()
            book = self.get_book(req.book_id)
            if book["status"] in (
                BookRunStatus.PAUSED_AUTH_CAPTCHA.value,
                BookRunStatus.CUT_LOSS.value,
            ):
                raise InvalidStatusTransitionError(
                    f"Book '{req.book_id}' is in status {book['status']!r}; refusing lease."
                )

            ch_row = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? AND chapter = ?",
                (req.book_id, int(req.chapter)),
            ).fetchone()
            if ch_row is None:
                raise ChapterNotFoundError(
                    f"Chapter {req.chapter} for book '{req.book_id}' not found"
                )

            current_status = StockpileStatus(str(ch_row["status"]))
            # Idempotent retry for active identical lease
            if (
                current_status == StockpileStatus.LEASED
                and str(ch_row["task_id"]) == req.task_id
                and str(ch_row["leased_by_account"]) == req.account_id
            ):
                return CompanionLeaseResponse(
                    task_id=req.task_id,
                    task_type=CompanionTaskType.PUBLISH_CHAPTER.value,
                    book_id=req.book_id,
                    book_title=book["title"],
                    author_name=book["author_name"],
                    fanqie_book_id=book["fanqie_book_id"],
                    chapter=int(req.chapter),
                    public_title=str(ch_row["public_title"]),
                    body=str(ch_row["body"]),
                    author_note=str(ch_row["author_note"]),
                    sha256=str(ch_row["sha256"]),
                    status=StockpileStatus.LEASED.value,
                    lease_expires_at=str(ch_row["lease_expires_at"]),
                    # No planned publish time is stored for leased chapters (only a CST date),
                    # so send "" (publish immediately) rather than fabricating a past timestamp.
                    scheduled_publish_at="",
                    metadata={"scheduled_cst_date": str(ch_row["scheduled_cst_date"])},
                )

            if StockpileStatus.LEASED not in VALID_STOCKPILE_TRANSITIONS[current_status]:
                raise InvalidStatusTransitionError(
                    f"Cannot transition chapter {req.chapter} of '{req.book_id}' "
                    f"from {current_status.value!r} to 'LEASED'"
                )

            ok_seq, seq_msg = self._is_predecessor_ready_unlocked(req.book_id, int(req.chapter))
            if not ok_seq:
                raise SequentialLeaseLockError(seq_msg)

            # Enforce CST daily safety word quota (85% safety margin)
            cap = daily_char_cap_for_level(book["author_level"])
            used_today = self.get_daily_used_chars(req.account_id, today_cst)
            ch_chars = int(ch_row["char_count"])
            if used_today + ch_chars > cap:
                self._conn.execute(
                    """
                    UPDATE chapters
                    SET quota_deferred_to_date = ?, updated_at = ?
                    WHERE book_id = ? AND chapter = ?
                    """,
                    (next_cst, now_iso, req.book_id, int(req.chapter)),
                )
                raise DailyQuotaExceededError(
                    f"Daily CST word quota exceeded for account {req.account_id!r} "
                    f"({book['author_level']} cap={cap} on {today_cst}): "
                    f"used={used_today} + chapter={ch_chars} > {cap}; deferred to {next_cst}.",
                    account_id=req.account_id,
                    author_level=book["author_level"],
                    cst_date=today_cst,
                    deferred_to_date=next_cst,
                    used_chars=used_today,
                    daily_cap=cap,
                    requested_chars=ch_chars,
                )

            self._conn.execute(
                """
                UPDATE chapters
                SET status = ?, task_id = ?, leased_by_account = ?,
                    lease_expires_at = ?, lease_expires_epoch = ?,
                    scheduled_cst_date = ?, quota_deferred_to_date = '',
                    error_code = '', error_message = '', updated_at = ?
                WHERE book_id = ? AND chapter = ?
                """,
                (
                    StockpileStatus.LEASED.value,
                    req.task_id,
                    req.account_id,
                    expires_dt.isoformat(),
                    expires_dt.timestamp(),
                    today_cst,
                    now_iso,
                    req.book_id,
                    int(req.chapter),
                ),
            )
            return CompanionLeaseResponse(
                task_id=req.task_id,
                task_type=CompanionTaskType.PUBLISH_CHAPTER.value,
                book_id=req.book_id,
                book_title=book["title"],
                author_name=book["author_name"],
                fanqie_book_id=book["fanqie_book_id"],
                chapter=int(req.chapter),
                public_title=str(ch_row["public_title"]),
                body=str(ch_row["body"]),
                author_note=str(ch_row["author_note"]),
                sha256=str(ch_row["sha256"]),
                status=StockpileStatus.LEASED.value,
                lease_expires_at=expires_dt.isoformat(),
                # See above: never send lease time as a schedule (companion would fill a past time).
                scheduled_publish_at="",
                metadata={"scheduled_cst_date": today_cst},
            )

    def record_receipt(self, req: CompanionReceiptRequest) -> Dict[str, Any]:
        target_status = StockpileStatus(req.status)
        now_dt = self._now_dt()
        now_iso = now_dt.isoformat()

        with self._lock:
            self.expire_stale_leases()
            book = self.get_book(req.book_id)
            ch_row = self._conn.execute(
                "SELECT * FROM chapters WHERE book_id = ? AND chapter = ?",
                (req.book_id, int(req.chapter)),
            ).fetchone()
            if ch_row is None:
                raise ChapterNotFoundError(
                    f"Chapter {req.chapter} for book '{req.book_id}' not found"
                )

            current_status = StockpileStatus(str(ch_row["status"]))
            if current_status == target_status and target_status in (
                StockpileStatus.DRAFT_SAVED,
                StockpileStatus.SCHEDULED,
                StockpileStatus.PUBLISHED,
                StockpileStatus.PAUSED_AUTH_CAPTCHA,
            ):
                return {
                    "book_id": req.book_id,
                    "chapter": int(req.chapter),
                    "status": target_status.value,
                    "idempotent_replay": True,
                    "scheduled_cst_date": str(ch_row["scheduled_cst_date"]),
                }

            if target_status not in VALID_STOCKPILE_TRANSITIONS[current_status]:
                raise InvalidStatusTransitionError(
                    f"Illegal stockpile status transition for '{req.book_id}' ch {req.chapter}: "
                    f"{current_status.value} -> {target_status.value}"
                )

            eff_ts = req.published_at.strip() or req.scheduled_at.strip()
            if eff_ts:
                cst_date = shanghai_date_str(parse_to_shanghai(eff_ts))
            elif str(ch_row["scheduled_cst_date"]).strip():
                cst_date = str(ch_row["scheduled_cst_date"]).strip()
            else:
                cst_date = shanghai_date_str(now_dt)

            eff_char_count = int(ch_row["char_count"])
            if req.char_count > 0:
                eff_char_count = int(req.char_count)

            clear_lease = target_status in (
                StockpileStatus.STOCKPILED,
                StockpileStatus.PAUSED_AUTH_CAPTCHA,
                StockpileStatus.FAILED_HARD_MISMATCH,
            )
            new_cst_date = "" if clear_lease else cst_date
            new_leased_acct = "" if clear_lease else (req.account_id or str(ch_row["leased_by_account"]))

            self._conn.execute(
                """
                UPDATE chapters
                SET status = ?,
                    remote_chapter_id = CASE WHEN ? != '' THEN ? ELSE remote_chapter_id END,
                    char_count = ?,
                    scheduled_at = CASE WHEN ? != '' THEN ? ELSE scheduled_at END,
                    published_at = CASE WHEN ? != '' THEN ? ELSE published_at END,
                    scheduled_cst_date = ?,
                    leased_by_account = ?,
                    lease_expires_at = CASE WHEN ? THEN '' ELSE lease_expires_at END,
                    lease_expires_epoch = CASE WHEN ? THEN 0.0 ELSE lease_expires_epoch END,
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE book_id = ? AND chapter = ?
                """,
                (
                    target_status.value,
                    req.remote_chapter_id,
                    req.remote_chapter_id,
                    eff_char_count,
                    req.scheduled_at,
                    req.scheduled_at,
                    req.published_at,
                    req.published_at,
                    new_cst_date,
                    new_leased_acct,
                    1 if clear_lease else 0,
                    1 if clear_lease else 0,
                    req.error_code,
                    req.error_message,
                    now_iso,
                    req.book_id,
                    int(req.chapter),
                ),
            )

            if target_status == StockpileStatus.PUBLISHED:
                new_max = max(int(book["max_published_chapter"]), int(req.chapter))
                self._conn.execute(
                    "UPDATE books SET max_published_chapter = ?, updated_at = ? WHERE book_id = ?",
                    (new_max, now_iso, req.book_id),
                )
            elif target_status == StockpileStatus.PAUSED_AUTH_CAPTCHA:
                self.update_book_status(
                    req.book_id,
                    BookRunStatus.PAUSED_AUTH_CAPTCHA,
                    paused_reason=req.error_message or "PAUSED_AUTH_CAPTCHA",
                )

            return {
                "book_id": req.book_id,
                "chapter": int(req.chapter),
                "status": target_status.value,
                "idempotent_replay": False,
                "scheduled_cst_date": new_cst_date,
            }

    # -----------------------------------------------------------------------
    # Captcha Pause / Resume
    # -----------------------------------------------------------------------
    def record_captcha_pause(
        self,
        *,
        account_id: str,
        book_id: str = "",
        chapter: int = 0,
        reason: str = "CAPTCHA_DETECTED",
        page_url: str = "",
        screenshot_base64: str = "",
        screenshot_mime: str = "image/png",
        detected_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_iso = detected_at or self._now_iso()
        affected_books: List[str] = []
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO captcha_events (
                    account_id, book_id, chapter, reason, page_url,
                    screenshot_base64, screenshot_mime, resolved, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    account_id,
                    book_id,
                    int(chapter),
                    reason,
                    page_url,
                    screenshot_base64,
                    screenshot_mime,
                    now_iso,
                ),
            )
            if book_id:
                target_books = [self.get_book(book_id)]
            else:
                target_books = [
                    b for b in self.list_books() if b["account_id"] == account_id
                ]
            for b in target_books:
                bid = b["book_id"]
                affected_books.append(bid)
                self.update_book_status(
                    bid,
                    BookRunStatus.PAUSED_AUTH_CAPTCHA,
                    paused_reason=reason,
                )
                if chapter > 0:
                    ch_row = self._conn.execute(
                        "SELECT status FROM chapters WHERE book_id = ? AND chapter = ?",
                        (bid, int(chapter)),
                    ).fetchone()
                    if ch_row is not None:
                        st = StockpileStatus(str(ch_row["status"]))
                        if StockpileStatus.PAUSED_AUTH_CAPTCHA in VALID_STOCKPILE_TRANSITIONS[st]:
                            self._conn.execute(
                                """
                                UPDATE chapters
                                SET status = ?, error_message = ?, lease_expires_at = '',
                                    lease_expires_epoch = 0.0, updated_at = ?
                                WHERE book_id = ? AND chapter = ?
                                """,
                                (
                                    StockpileStatus.PAUSED_AUTH_CAPTCHA.value,
                                    reason,
                                    self._now_iso(),
                                    bid,
                                    int(chapter),
                                ),
                            )
            return {
                "status": BookRunStatus.PAUSED_AUTH_CAPTCHA.value,
                "account_id": account_id,
                "affected_books": affected_books,
                "chapter": int(chapter),
                "reason": reason,
            }

    def resume_companion(self, account_id: str, book_id: str = "") -> Dict[str, Any]:
        now_iso = self._now_iso()
        resumed_books: List[str] = []
        with self._lock:
            self._conn.execute(
                """
                UPDATE captcha_events SET resolved = 1, resolved_at = ?
                WHERE account_id = ? AND resolved = 0
                """,
                (now_iso, account_id),
            )
            if book_id:
                candidates = [self.get_book(book_id)]
            else:
                candidates = [
                    b
                    for b in self.list_books()
                    if b["account_id"] == account_id
                    and b["status"] == BookRunStatus.PAUSED_AUTH_CAPTCHA.value
                ]
            for b in candidates:
                bid = b["book_id"]
                if b["status"] == BookRunStatus.PAUSED_AUTH_CAPTCHA.value:
                    self.update_book_status(bid, BookRunStatus.ACTIVE, paused_reason="")
                    resumed_books.append(bid)
                self._conn.execute(
                    """
                    UPDATE chapters
                    SET status = ?, error_message = '', lease_expires_at = '',
                        lease_expires_epoch = 0.0, scheduled_cst_date = '', updated_at = ?
                    WHERE book_id = ? AND status = ?
                    """,
                    (
                        StockpileStatus.STOCKPILED.value,
                        now_iso,
                        bid,
                        StockpileStatus.PAUSED_AUTH_CAPTCHA.value,
                    ),
                )
            return {
                "status": "RESUMED",
                "account_id": account_id,
                "resumed_books": resumed_books,
            }

    def get_paused_captcha_accounts(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT account_id FROM books
                WHERE status = ? AND account_id != ''
                ORDER BY account_id ASC
                """,
                (BookRunStatus.PAUSED_AUTH_CAPTCHA.value,),
            ).fetchall()
            return [str(r["account_id"]) for r in rows]

    # -----------------------------------------------------------------------
    # T-1 Metrics, Hotlists, Comments & T09 Evolution Linkage
    # -----------------------------------------------------------------------
    def record_metrics(
        self,
        req: CompanionMetricsRequest,
        *,
        motif_store: Optional[MotifWeightStore] = None,
    ) -> Dict[str, Any]:
        now_iso = self._now_iso()
        metrics_date = (
            req.metrics_date.strip()
            if req.metrics_date and req.metrics_date.strip()
            else shanghai_date_str(parse_to_shanghai(req.collected_at))
        )
        canonical_payload = json.dumps(req.to_dict(), ensure_ascii=False, sort_keys=True)
        payload_sha = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

        with self._lock:
            book = self.get_book(req.book_id)
            existing = self._conn.execute(
                "SELECT * FROM metrics_records WHERE book_id = ? AND metrics_date = ?",
                (req.book_id, metrics_date),
            ).fetchone()
            if existing is not None:
                same_payload = str(existing["payload_sha256"]) == payload_sha
                downgrade_attempt = bool(existing["settled"]) and not req.settled
                if same_payload or downgrade_attempt:
                    return {
                        "book_id": req.book_id,
                        "metrics_date": metrics_date,
                        "idempotent_replay": True,
                        "settled": bool(existing["settled"]),
                        "racing_decision": json.loads(existing["racing_decision_json"] or "{}"),
                        "optimization_report": json.loads(
                            existing["optimization_report_json"] or "{}"
                        ),
                        "hotlist_count": len(json.loads(existing["decoded_hotlist_json"] or "[]")),
                    }

            # Store decoded hotlist snapshot
            if req.decoded_hotlist:
                self._conn.execute(
                    """
                    INSERT INTO hotlist_snapshots (book_id, metrics_date, account_id, items_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(book_id, metrics_date) DO UPDATE SET
                        account_id = excluded.account_id,
                        items_json = excluded.items_json,
                        created_at = excluded.created_at
                    """,
                    (
                        req.book_id,
                        metrics_date,
                        req.account_id,
                        json.dumps(req.decoded_hotlist, ensure_ascii=False),
                        now_iso,
                    ),
                )

            # Evaluate T09 racing (never coerce unsettled metrics to 0.0!)
            total_words_row = self._conn.execute(
                "SELECT COALESCE(SUM(char_count), 0) AS w FROM chapters WHERE book_id = ?",
                (req.book_id,),
            ).fetchone()
            total_words = int(total_words_row["w"]) if total_words_row else 0
            alt_titles = list(book["alternate_titles"])
            alt_blurbs = list(book["alternate_blurbs"])

            book_metrics = BookMetrics(
                book_id=req.book_id,
                ch2_read_through=req.ch2_read_through,
                bookshelf_rate=req.bookshelf_rate,
                click_through_rate=req.click_through_rate,
                word100k_completion=req.word100k_read_through,
                impressions=req.impressions,
                settled=req.settled,
                settlement_status=req.settlement_status,
                signed=bool(book["signed"]),
                current_title=book["title"],
                current_daily_chapters=int(book["daily_target_chapters"]),
                total_words=total_words,
                debut_reached=(total_words >= 30000),
                collected_at=req.collected_at,
            )

            racing_result: Dict[str, Any]
            try:
                decision = evaluate_book_racing(
                    book_metrics,
                    alternate_titles=alt_titles,
                    alternate_blurbs=alt_blurbs,
                    mutate_pool=True,
                )
                racing_result = decision.to_dict()
                self._apply_racing_decision_unlocked(
                    book=book,
                    decision=decision,
                    remaining_titles=alt_titles,
                    remaining_blurbs=alt_blurbs,
                    metrics_date=metrics_date,
                    motif_store=motif_store,
                )
            except MetricsNotSettledError as exc:
                logger.warning(
                    "Skipping T09 racing evaluation for book=%s date=%s: %s",
                    req.book_id,
                    metrics_date,
                    exc,
                )
                racing_result = {
                    "status": "METRICS_NOT_SETTLED",
                    "action": None,
                    "error": str(exc),
                }
            except PackagingPoolExhaustedError as exc:
                logger.warning(
                    "Packaging pool exhausted for book=%s date=%s: %s",
                    req.book_id,
                    metrics_date,
                    exc,
                )
                racing_result = {
                    "status": "PACKAGING_POOL_EXHAUSTED",
                    "action": None,
                    "error": str(exc),
                }

            # Store reader comments & run T09 ReviewFeedbackOptimizer (> max_published_chapter)
            opt_result: Dict[str, Any] = {}
            if req.comments:
                self._conn.execute(
                    "DELETE FROM chapter_comments WHERE book_id = ? AND metrics_date = ?",
                    (req.book_id, metrics_date),
                )
                for c in req.comments:
                    self._conn.execute(
                        """
                        INSERT INTO chapter_comments (
                            book_id, metrics_date, chapter, comment_id, content, likes, sentiment, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            req.book_id,
                            metrics_date,
                            int(c.get("chapter", 1)),
                            str(c.get("comment_id", "")),
                            str(c.get("content", "")),
                            int(c.get("likes", 0)),
                            c.get("sentiment"),
                            now_iso,
                        ),
                    )
                optimizer = ReviewFeedbackOptimizer()
                report = optimizer.optimize(
                    req.book_id,
                    req.comments,
                    max_published_chapter=int(book["max_published_chapter"]),
                )
                opt_result = report.to_dict()
                for d in report.directives:
                    self._conn.execute(
                        """
                        INSERT INTO outline_directives (
                            directive_id, book_id, metrics_date, target_chapter,
                            max_published_chapter, signal_type, priority, instruction,
                            payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(directive_id) DO UPDATE SET
                            instruction = excluded.instruction,
                            payload_json = excluded.payload_json
                        """,
                        (
                            d.directive_id,
                            req.book_id,
                            metrics_date,
                            int(d.target_chapter),
                            int(d.max_published_chapter),
                            d.signal_type,
                            int(d.priority),
                            d.instruction,
                            json.dumps(d.to_dict(), ensure_ascii=False),
                            now_iso,
                        ),
                    )

            self._conn.execute(
                """
                INSERT INTO metrics_records (
                    book_id, metrics_date, account_id, settled, settlement_status,
                    ch2_read_through, word100k_completion, click_through_rate,
                    bookshelf_rate, impressions, daily_revenue_cny,
                    comments_json, decoded_hotlist_json, racing_decision_json,
                    optimization_report_json, payload_sha256, collected_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id, metrics_date) DO UPDATE SET
                    account_id = excluded.account_id,
                    settled = excluded.settled,
                    settlement_status = excluded.settlement_status,
                    ch2_read_through = excluded.ch2_read_through,
                    word100k_completion = excluded.word100k_completion,
                    click_through_rate = excluded.click_through_rate,
                    bookshelf_rate = excluded.bookshelf_rate,
                    impressions = excluded.impressions,
                    daily_revenue_cny = excluded.daily_revenue_cny,
                    comments_json = excluded.comments_json,
                    decoded_hotlist_json = excluded.decoded_hotlist_json,
                    racing_decision_json = excluded.racing_decision_json,
                    optimization_report_json = excluded.optimization_report_json,
                    payload_sha256 = excluded.payload_sha256,
                    collected_at = excluded.collected_at,
                    updated_at = excluded.updated_at
                """,
                (
                    req.book_id,
                    metrics_date,
                    req.account_id,
                    1 if req.settled else 0,
                    req.settlement_status,
                    req.ch2_read_through,
                    req.word100k_read_through,
                    req.click_through_rate,
                    req.bookshelf_rate,
                    req.impressions,
                    req.daily_revenue_cny,
                    json.dumps(req.comments, ensure_ascii=False),
                    json.dumps(req.decoded_hotlist, ensure_ascii=False),
                    json.dumps(racing_result, ensure_ascii=False),
                    json.dumps(opt_result, ensure_ascii=False),
                    payload_sha,
                    req.collected_at,
                    now_iso,
                ),
            )

            return {
                "book_id": req.book_id,
                "metrics_date": metrics_date,
                "idempotent_replay": False,
                "settled": bool(req.settled),
                "racing_decision": racing_result,
                "optimization_report": opt_result,
                "hotlist_count": len(req.decoded_hotlist),
            }

    def _apply_racing_decision_unlocked(
        self,
        *,
        book: Dict[str, Any],
        decision: Any,
        remaining_titles: List[str],
        remaining_blurbs: List[str],
        metrics_date: str,
        motif_store: Optional[MotifWeightStore],
    ) -> None:
        now_iso = self._now_iso()
        bid = book["book_id"]

        if decision.promotion is not None:
            self._conn.execute(
                "UPDATE books SET daily_target_chapters = ?, updated_at = ? WHERE book_id = ?",
                (int(decision.promotion.new_daily_chapters), now_iso, bid),
            )
            self._conn.execute(
                """
                INSERT INTO promotion_recommendations (
                    book_id, metrics_date, previous_daily_chapters,
                    new_daily_chapters, approved, reason, created_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    bid,
                    metrics_date,
                    int(decision.promotion.previous_daily_chapters),
                    int(decision.promotion.new_daily_chapters),
                    decision.promotion.reason,
                    now_iso,
                ),
            )

        if decision.packaging_task is not None:
            self._conn.execute(
                """
                UPDATE books
                SET alternate_titles_json = ?, alternate_blurbs_json = ?, updated_at = ?
                WHERE book_id = ?
                """,
                (
                    json.dumps(remaining_titles, ensure_ascii=False),
                    json.dumps(remaining_blurbs, ensure_ascii=False),
                    now_iso,
                    bid,
                ),
            )
            task_id = f"rot_{bid}_{metrics_date}"
            self._conn.execute(
                """
                INSERT INTO companion_tasks (
                    task_id, task_type, book_id, account_id, status, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    status = 'PENDING',
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    CompanionTaskType.ROTATE_PACKAGING.value,
                    bid,
                    book["account_id"],
                    json.dumps(decision.packaging_task.to_dict(), ensure_ascii=False),
                    now_iso,
                    now_iso,
                ),
            )

        if decision.cut_recommendation is not None:
            self._conn.execute(
                """
                INSERT INTO cut_recommendations (book_id, reason, status, payload_json, created_at, updated_at)
                VALUES (?, ?, 'PENDING_CONFIRMATION', ?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    reason = excluded.reason,
                    status = 'PENDING_CONFIRMATION',
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    bid,
                    decision.cut_recommendation.reason,
                    json.dumps(decision.cut_recommendation.to_dict(), ensure_ascii=False),
                    now_iso,
                    now_iso,
                ),
            )

        if (
            motif_store is not None
            and book.get("opening_motif")
            and book.get("golden_finger_type")
            and book.get("genre_id")
        ):
            try:
                motif_store.record_racing_decision(
                    decision,
                    genre_id=book["genre_id"],
                    opening_motif=book["opening_motif"],
                    golden_finger_type=book["golden_finger_type"],
                )
            except MetricsNotSettledError as exc:
                logger.warning("MotifWeightStore skipped unsettled decision for %s: %s", bid, exc)

    def get_latest_metrics(self, book_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM metrics_records WHERE book_id = ?
                ORDER BY metrics_date DESC, id DESC LIMIT 1
                """,
                (book_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "book_id": str(row["book_id"]),
                "metrics_date": str(row["metrics_date"]),
                "settled": bool(row["settled"]),
                "settlement_status": str(row["settlement_status"]),
                "ch2_read_through": row["ch2_read_through"],
                "word100k_completion": row["word100k_completion"],
                "click_through_rate": row["click_through_rate"],
                "bookshelf_rate": row["bookshelf_rate"],
                "impressions": row["impressions"],
                "daily_revenue_cny": row["daily_revenue_cny"],
                "racing_decision": json.loads(row["racing_decision_json"] or "{}"),
                "optimization_report": json.loads(row["optimization_report_json"] or "{}"),
                "collected_at": str(row["collected_at"]),
            }

    def get_pending_cut_recommendations(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM cut_recommendations
                WHERE status = 'PENDING_CONFIRMATION'
                ORDER BY updated_at DESC
                """
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                payload = json.loads(r["payload_json"] or "{}")
                out.append(
                    {
                        "book_id": str(r["book_id"]),
                        "reason": str(r["reason"]),
                        "status": str(r["status"]),
                        **payload,
                    }
                )
            return out

    def get_outline_directives(self, book_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM outline_directives
                WHERE book_id = ?
                ORDER BY target_chapter ASC, priority ASC
                """,
                (book_id,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def get_latest_hotlist(self) -> List[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT items_json FROM hotlist_snapshots ORDER BY metrics_date DESC, id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return []
            return list(json.loads(row["items_json"] or "[]"))


__all__ = [
    "BookNotFoundError",
    "ChapterNotFoundError",
    "CutConfirmationConflictError",
    "DEFAULT_LEASE_SECONDS",
    "DailyQuotaExceededError",
    "InvalidStatusTransitionError",
    "LV0_1_DAILY_CHAR_CAP",
    "LV2_PLUS_DAILY_CHAR_CAP",
    "MAX_LEASE_SECONDS",
    "SAFETY_CUSHION_CRITICAL",
    "SAFETY_CUSHION_TARGET",
    "SequentialLeaseLockError",
    "StockpileDB",
    "StockpileError",
    "count_non_ws_chars",
    "daily_char_cap_for_level",
    "next_shanghai_date_str",
    "parse_to_shanghai",
    "shanghai_date_str",
    "shanghai_now",
]
