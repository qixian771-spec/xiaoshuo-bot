"""T3: export-only scheduling (owner decision 23A) and visible circuit breakers.

- No publishing companion consumes the stockpile, so active books must not stop at the
  10-chapter safety cushion.
- At the end of the outline: complete_150k books complete; serial books extend the next volume.
- Upstream model failures and failed volume extensions stop the book visibly (book_isolated)
  instead of the background loop retrying forever.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List
import unittest
from unittest import mock

from xiaoshuo_bot.contracts import SSE_BOOK_ISOLATED, SSE_CPA_QUOTA_ALERT, BookRunStatus
from xiaoshuo_bot.llm.base import BudgetCircuitBreakerError, LLMRequestError, QuotaExhaustedError
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.server import matrix_scheduler as ms
from xiaoshuo_bot.server.matrix_scheduler import MatrixScheduler
from xiaoshuo_bot.server.stockpile_db import StockpileDB

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROJECT = REPO_ROOT / "projects" / "baiwu_dukou"


class _RaisingEngine:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def run(self, **_: Any) -> Dict[str, Any]:
        raise self.exc


class ExportOnlySchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="xsb_t3_"))
        self.project_dir = self.tmp / "projects" / "baiwu_dukou"
        shutil.copytree(SOURCE_PROJECT, self.project_dir, ignore=shutil.ignore_patterns("output"))
        self.db = StockpileDB(self.tmp / "m.db")
        self.db.register_book(book_id="b1", project_dir=self.project_dir, title="白雾渡口", total_chapters=5)
        self.events: List[Any] = []

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp)

    def _scheduler(self, *, export_only: bool, engine_exc: Exception | None = None) -> MatrixScheduler:
        factory = None
        if engine_exc is not None:
            factory = lambda project, output_dir, cb: _RaisingEngine(engine_exc)  # noqa: E731
        sched = MatrixScheduler(
            db=self.db, projects_root=self.tmp / "projects", engine_factory=factory, export_only=export_only
        )
        sched.event_broker.publish = lambda payload: self.events.append(payload) or payload  # type: ignore[method-assign]
        return sched

    def _stockpile(self, chapters: range) -> None:
        for n in chapters:
            self.db.add_stockpiled_chapter("b1", n, f"t{n}", "正文。" * 100)

    def test_cushion_mode_stops_queueing_at_ten(self) -> None:
        self._stockpile(range(1, 11))
        self.assertEqual(self._scheduler(export_only=False).inspect_safety_cushions(emit_alerts=False), [])

    def test_export_only_keeps_queueing_full_books(self) -> None:
        self._stockpile(range(1, 11))
        queue = self._scheduler(export_only=True).inspect_safety_cushions(emit_alerts=False)
        self.assertEqual([q.book_id for q in queue], ["b1"])

    def test_cushion_mode_end_of_outline_unchanged(self) -> None:
        self._stockpile(range(1, 6))
        res = self._scheduler(export_only=False).produce_next_chapter("b1")
        self.assertEqual(res["status"], "NO_OUTLINED_CHAPTERS")

    def test_complete_track_finishes_book(self) -> None:
        self._stockpile(range(1, 6))
        real = load_project

        def complete_track(path: Path):
            p = real(path)
            return replace(p, meta=replace(p.meta, contract_track="complete_150k"))

        with mock.patch.object(ms, "load_project", side_effect=complete_track):
            res = self._scheduler(export_only=True).produce_next_chapter("b1")
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(self.db.get_book("b1")["status"], BookRunStatus.COMPLETED.value)

    def test_serial_track_extends_and_failure_isolates_visibly(self) -> None:
        self._stockpile(range(1, 6))
        sched = self._scheduler(export_only=True)
        with mock.patch.object(sched, "extend_book_volume", return_value={"added": 15}) as ext:
            res = sched.produce_next_chapter("b1")
        ext.assert_called_once_with("b1")
        self.assertEqual(res["status"], "EXTENDED")

        with mock.patch.object(sched, "extend_book_volume", side_effect=RuntimeError("planner JSON invalid")):
            res = sched.produce_next_chapter("b1")
        self.assertEqual(res["status"], BookRunStatus.ISOLATED_GATE_FAILURE.value)
        book = self.db.get_book("b1")
        self.assertEqual(book["status"], BookRunStatus.ISOLATED_GATE_FAILURE.value)
        self.assertIn("planner JSON invalid", book["paused_reason"])
        self.assertEqual(self.events[-1].event, SSE_BOOK_ISOLATED)
        self.assertEqual(self.events[-1].chapter, 6)

    def test_upstream_model_error_isolates_instead_of_looping(self) -> None:
        sched = self._scheduler(export_only=True, engine_exc=LLMRequestError("HTTP 503 from CPA", status_code=503))
        res = sched.produce_next_chapter("b1")
        self.assertEqual(res["status"], BookRunStatus.ISOLATED_GATE_FAILURE.value)
        self.assertEqual(res["detail"]["error_type"], "LLMRequestError")
        self.assertIn("HTTP 503 from CPA", self.db.get_book("b1")["paused_reason"])
        self.assertEqual(self.events[-1].event, SSE_BOOK_ISOLATED)
        # The isolated book is no longer queued, so the background loop cannot hammer it.
        self.assertEqual(sched.inspect_safety_cushions(emit_alerts=False), [])

    def test_p1_2_manual_pause_distinct_from_quota_and_budget_circuit_breaker(self) -> None:
        sched = self._scheduler(export_only=True)
        res_pause = sched.pause_book("b1", reason="老板手动暂停测试")
        self.assertEqual(res_pause["status"], BookRunStatus.PAUSED_MANUAL.value)
        self.assertEqual(self.db.get_book("b1")["status"], BookRunStatus.PAUSED_MANUAL.value)
        self.assertEqual(self.db.get_book("b1")["paused_reason"], "老板手动暂停测试")
        self.assertEqual(self.events, [])
        self.assertEqual(sched.inspect_safety_cushions(emit_alerts=False), [])

        res_resume = sched.resume_book("b1")
        self.assertEqual(res_resume["status"], BookRunStatus.ACTIVE.value)
        self.assertEqual(self.db.get_book("b1")["paused_reason"], "")

        # Budget circuit breaker sets PAUSED_QUOTA_EXHAUSTED and emits structured cpa_quota_alert
        sched_budget = self._scheduler(
            export_only=True,
            engine_exc=BudgetCircuitBreakerError("Daily budget cap $5.00 exceeded"),
        )
        res_budget = sched_budget.produce_next_chapter("b1")
        self.assertEqual(res_budget["status"], BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value)
        self.assertEqual(self.db.get_book("b1")["status"], BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value)
        self.assertEqual(self.events[-1].event, SSE_CPA_QUOTA_ALERT)
        self.assertEqual(self.events[-1].data.get("kind"), "quota_exhausted")
        self.assertEqual(self.events[-1].data.get("error_type"), "BudgetCircuitBreakerError")
        self.assertIn("Daily budget cap", self.events[-1].data.get("reason", ""))

        # QuotaExhaustedError behaves identically and stops queueing
        sched_budget.resume_book("b1")
        sched_quota = self._scheduler(
            export_only=True,
            engine_exc=QuotaExhaustedError("All whitelist models exhausted (HTTP 429)"),
        )
        res_quota = sched_quota.produce_next_chapter("b1")
        self.assertEqual(res_quota["status"], BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value)
        self.assertEqual(self.events[-1].event, SSE_CPA_QUOTA_ALERT)
        self.assertEqual(self.events[-1].data.get("error_type"), "QuotaExhaustedError")
        self.assertEqual(sched_quota.inspect_safety_cushions(emit_alerts=False), [])

    def test_p1_2_legacy_db_migrates_manual_pause_without_touching_real_quota_exhausted(self) -> None:
        db_path = self.tmp / "legacy_p1_2.db"
        legacy_db = StockpileDB(db_path)
        for bid, title in (
            ("b_manual_en", "手动暂停英文原因"),
            ("b_manual_zh", "手动暂停中文原因"),
            ("b_legacy_paused", "旧PAUSED状态"),
            ("b_real_budget", "真超预算熔断"),
            ("b_real_quota", "真429额度耗尽"),
        ):
            legacy_db.register_book(book_id=bid, project_dir=self.project_dir, title=title)

        with legacy_db._lock:
            legacy_db._conn.execute(
                "UPDATE books SET status = 'PAUSED_QUOTA_EXHAUSTED', paused_reason = 'Manually paused' WHERE book_id = 'b_manual_en'"
            )
            legacy_db._conn.execute(
                "UPDATE books SET status = 'PAUSED_QUOTA_EXHAUSTED', paused_reason = '控制台手动暂停' WHERE book_id = 'b_manual_zh'"
            )
            legacy_db._conn.execute(
                "UPDATE books SET status = 'PAUSED', paused_reason = '旧版手动停笔' WHERE book_id = 'b_legacy_paused'"
            )
            legacy_db._conn.execute(
                "UPDATE books SET status = 'PAUSED_QUOTA_EXHAUSTED', paused_reason = 'Daily budget exceeded (BudgetCircuitBreakerError)' WHERE book_id = 'b_real_budget'"
            )
            legacy_db._conn.execute(
                "UPDATE books SET status = 'PAUSED_QUOTA_EXHAUSTED', paused_reason = 'All credentials HTTP 429 quota exhausted' WHERE book_id = 'b_real_quota'"
            )
        legacy_db.close()

        # Re-open triggers automatic _init_schema() migration
        migrated_db = StockpileDB(db_path)
        try:
            self.assertEqual(migrated_db.get_book("b_manual_en")["status"], BookRunStatus.PAUSED_MANUAL.value)
            self.assertEqual(migrated_db.get_book("b_manual_zh")["status"], BookRunStatus.PAUSED_MANUAL.value)
            self.assertEqual(migrated_db.get_book("b_legacy_paused")["status"], BookRunStatus.PAUSED_MANUAL.value)
            self.assertEqual(
                migrated_db.get_book("b_real_budget")["status"],
                BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value,
            )
            self.assertEqual(
                migrated_db.get_book("b_real_quota")["status"],
                BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value,
            )
        finally:
            migrated_db.close()


if __name__ == "__main__":
    unittest.main()
