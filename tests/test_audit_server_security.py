from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xiaoshuo_bot.contracts import CompanionLeaseRequest
from xiaoshuo_bot.llm.router import RoleEndpointConfig, RoleModelRouter
from xiaoshuo_bot.server import InsecureBindError, create_server, validate_bind_security
from xiaoshuo_bot.server.auth import INSECURE_NO_AUTH_ENV, is_placeholder_secret, validate_companion_secret
from xiaoshuo_bot.server.matrix_scheduler import CPABindingOriginChangeError, MatrixScheduler
from xiaoshuo_bot.server.stockpile_db import MAX_LEASE_SECONDS, StockpileDB


STRONG_TOKEN = "t0k3n-" + "a1b2c3d4" * 4


class AuditBindSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(
            os.environ,
            {"XIAOSHUO_ADMIN_TOKEN": "", "XIAOSHUO_COMPANION_SECRET": "", INSECURE_NO_AUTH_ENV: ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_empty_token_on_loopback_rejected(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            with self.assertRaises(InsecureBindError):
                validate_bind_security(host, "")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(InsecureBindError):
                create_server(
                    host="127.0.0.1",
                    port=0,
                    db_path=root / "s.db",
                    projects_root=root / "projects",
                    admin_token="",
                )

    def test_short_token_on_loopback_rejected(self) -> None:
        with self.assertRaises(InsecureBindError):
            validate_bind_security("127.0.0.1", "a" * 23)
        self.assertEqual(validate_bind_security("127.0.0.1", STRONG_TOKEN), STRONG_TOKEN)

    def test_placeholder_tokens_rejected(self) -> None:
        for bad in (
            "__REPLACE_WITH_64_HEX_ADMIN_TOKEN__",
            "please-replace-me-with-a-real-token",
            "__" + "x" * 30 + "__",
        ):
            self.assertTrue(is_placeholder_secret(bad), bad)
            for host in ("127.0.0.1", "0.0.0.0"):
                with self.assertRaises(InsecureBindError):
                    validate_bind_security(host, bad)
            # opt-out never whitelists a placeholder
            with self.assertRaises(InsecureBindError):
                validate_bind_security("127.0.0.1", bad, allow_insecure_loopback_no_auth=True)
        self.assertFalse(is_placeholder_secret(STRONG_TOKEN))

    def test_placeholder_companion_secret_rejected(self) -> None:
        with self.assertRaises(InsecureBindError):
            validate_companion_secret("__REPLACE_WITH_64_HEX_COMPANION_HMAC_SECRET__")
        self.assertEqual(validate_companion_secret(""), "")
        self.assertEqual(validate_companion_secret("s" * 40), "s" * 40)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(InsecureBindError):
                create_server(
                    host="127.0.0.1",
                    port=0,
                    db_path=root / "s.db",
                    projects_root=root / "projects",
                    admin_token=STRONG_TOKEN,
                    companion_secret="__REPLACE_WITH_SECRET__",
                )

    def test_explicit_opt_out_only_on_loopback(self) -> None:
        self.assertEqual(validate_bind_security("127.0.0.1", "", allow_insecure_loopback_no_auth=True), "")
        with self.assertRaises(InsecureBindError):
            validate_bind_security("0.0.0.0", "", allow_insecure_loopback_no_auth=True)
        # env opt-out is consulted when the parameter is not given
        with mock.patch.dict(os.environ, {INSECURE_NO_AUTH_ENV: "1"}):
            self.assertEqual(validate_bind_security("localhost", ""), "")
            with self.assertRaises(InsecureBindError):
                validate_bind_security("0.0.0.0", "")
            # explicit False overrides env
            with self.assertRaises(InsecureBindError):
                validate_bind_security("127.0.0.1", "", allow_insecure_loopback_no_auth=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=root / "s.db",
                projects_root=root / "projects",
                admin_token="",
                allow_insecure_loopback_no_auth=True,
            )
            try:
                self.assertFalse(srv.admin_auth.auth_enabled)
            finally:
                # serve_forever() was never started, so shutdown() would block forever.
                srv.server_close()
                srv.db.close()


class AuditCPABindingOriginTests(unittest.TestCase):
    def _make_scheduler(self, root: Path) -> MatrixScheduler:
        router = RoleModelRouter(
            {
                "writer": RoleEndpointConfig(
                    role="writer",
                    base_url="http://127.0.0.1:8317/v1",
                    model="model-a",
                    api_keys=["secret-key-1"],
                )
            }
        )
        db = StockpileDB(root / "s.db")
        self.addCleanup(db.close)
        return MatrixScheduler(db=db, projects_root=root / "projects", provider=router)

    def test_same_origin_model_change_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sched = self._make_scheduler(Path(tmpdir))
            updated = sched.update_cpa_bindings(
                [{"role": "writer", "model": "model-b", "base_url": "http://127.0.0.1:8317/v2"}]
            )
            writer = [b for b in updated if b.role == "writer"][0]
            self.assertEqual(writer.model, "model-b")
            self.assertEqual(writer.base_url, "http://127.0.0.1:8317/v2")
            sched.db.close()

    def test_base_url_host_change_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sched = self._make_scheduler(Path(tmpdir))
            for evil in (
                "https://attacker.example.com/v1",
                "http://127.0.0.1:9999/v1",
                "https://127.0.0.1:8317/v1",
                "file:///etc/passwd",
                "gopher://127.0.0.1:8317/",
            ):
                with self.assertRaises(CPABindingOriginChangeError):
                    sched.update_cpa_bindings([{"role": "writer", "model": "m", "base_url": evil}])
                # maps to HTTP 400 via the app's ValueError handler
                self.assertTrue(issubclass(CPABindingOriginChangeError, ValueError))
            writer = [b for b in sched.get_cpa_bindings() if b.role == "writer"][0]
            self.assertEqual(writer.base_url, "http://127.0.0.1:8317/v1")
            self.assertEqual(writer.model, "model-a")
            sched.db.close()


class AuditLeaseTests(unittest.TestCase):
    def test_lease_scheduled_publish_at_empty_and_lease_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = StockpileDB(root / "s.db")
            try:
                db.register_book(book_id="b1", project_dir=root / "b1", title="排期测试", account_id="acc")
                db.add_stockpiled_chapter("b1", 1, "第一章", "这是第一章正文内容" * 50)
                with self.assertRaises(ValueError):
                    db.lease_chapter(
                        CompanionLeaseRequest(
                            task_id="t1",
                            book_id="b1",
                            chapter=1,
                            account_id="acc",
                            lease_seconds=MAX_LEASE_SECONDS + 1,
                        )
                    )
                lease = db.lease_chapter(
                    CompanionLeaseRequest(task_id="t1", book_id="b1", chapter=1, account_id="acc")
                )
                self.assertEqual(lease.scheduled_publish_at, "")
                self.assertTrue(lease.metadata.get("scheduled_cst_date"))
                # idempotent re-lease path also sends no fabricated schedule
                again = db.lease_chapter(
                    CompanionLeaseRequest(task_id="t1", book_id="b1", chapter=1, account_id="acc")
                )
                self.assertEqual(again.scheduled_publish_at, "")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
