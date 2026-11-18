"""Audit regression tests for companion publish-trust, sentinel coverage, cover upload and daemon resilience."""

from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Tuple
from unittest import mock

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import xiaoshuo_bot.companion as companion_pkg
from xiaoshuo_bot.companion import (
    BrowserLaunchConfig,
    CaptchaAuthPauseError,
    CompanionWorkerError,
    TomatoBrowserWorker,
    Woff2FontDecoder,
)
from xiaoshuo_bot.companion.font_decoder import FontDecodeError
from xiaoshuo_bot.cli import run_companion_daemon
from xiaoshuo_bot.contracts import (
    CompanionLeaseResponse,
    CompanionTaskType,
    StockpileStatus,
)
from xiaoshuo_bot.models import SHANGHAI_TZ


NOW = datetime(2026, 11, 18, 13, 5, 0, tzinfo=SHANGHAI_TZ)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

_IDENTITY = (
    '<div id="header">'
    '<span data-testid="author-name">墨山行</span>'
    '<span data-testid="book-id" data-book-id="fq_book_881">fq_book_881</span>'
    '<span data-testid="book-title">我在十万大山开盲盒</span>'
    "</div>"
)


def _publish_page(*, with_save: bool = True, body_extra: str = "") -> str:
    save_btn = (
        '<button id="save-draft-btn" data-testid="save-draft-btn" data-remote-chapter-id="rm-draft-6" '
        'type="button">存草稿</button>'
        if with_save
        else ""
    )
    return f"""<!doctype html><html><body>
    {_IDENTITY}
    {body_extra}
    <div data-testid="recent-chapters"></div>
    <input id="chapter-number-input" name="chapter_no" />
    <input id="chapter-title-input" name="chapter_title" />
    <div class="ProseMirror" contenteditable="true" style="min-height:120px;border:1px solid #ccc;"></div>
    <span data-testid="editor-word-count">0</span>
    <textarea id="author-note-input" name="author_note"></textarea>
    <input id="schedule-time-input" name="scheduled_publish_at" />
    {save_btn}
    <button id="publish-submit-btn" data-testid="publish-submit-btn" type="button">定时发布</button>
    <script>
      const pm = document.querySelector('.ProseMirror');
      const wc = document.querySelector('[data-testid="editor-word-count"]');
      pm.addEventListener('input', () => {{
        wc.textContent = String(pm.innerText.replace(/\\s+/g, '').length);
      }});
      const post = (path, extra) => fetch(path, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(Object.assign({{
          chapter_no: document.getElementById('chapter-number-input').value,
          scheduled_publish_at: document.getElementById('schedule-time-input').value,
        }}, extra || {{}}))
      }});
      const sb = document.getElementById('save-draft-btn');
      if (sb) sb.addEventListener('click', () => post('/api/save-draft'));
      document.getElementById('publish-submit-btn').addEventListener('click', () => post('/api/publish'));
    </script>
    </body></html>"""


class _SimState:
    def __init__(self) -> None:
        self.posts: Dict[str, List[Dict[str, Any]]] = {}
        self.pages: Dict[str, Tuple[int, str]] = {}


def _start_server(sim: _SimState) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            sim.posts.setdefault(self.path, []).append(json.loads(raw.decode("utf-8") or "{}"))
            self._send(200, json.dumps({"ok": True}), "application/json")

        def do_GET(self) -> None:
            if self.path.startswith("/forbidden"):
                self._send(403, "forbidden", "text/plain")
                return
            code, html = sim.pages.get(self.path, (404, "<html><body>Not Found</body></html>"))
            self._send(code, html)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, t, f"http://{host}:{port}"


def _task(**overrides: Any) -> CompanionLeaseResponse:
    base: Dict[str, Any] = dict(
        task_id="task-audit-ch6",
        task_type=CompanionTaskType.PUBLISH_CHAPTER.value,
        book_id="bk_881",
        book_title="我在十万大山开盲盒",
        author_name="墨山行",
        fanqie_book_id="fq_book_881",
        chapter=6,
        public_title="第6章：深夜响起的旧铜钟",
        body="陈青推开木门时，屋檐下的铜铃猛地响了一声。\n\n柜台后面的账本翻到第三页，墨迹还没干透。",
        author_note="",
        sha256="sha-ch6",
        scheduled_publish_at="",
    )
    base.update(overrides)
    return CompanionLeaseResponse(**base)


def _wait_for(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class _BrowserTestBase(unittest.TestCase):
    sim: _SimState
    srv: ThreadingHTTPServer
    thread: threading.Thread
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.sim = _SimState()
        cls.srv, cls.thread, cls.base_url = _start_server(cls.sim)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        self.sim.posts.clear()
        self._tmp = tempfile.TemporaryDirectory(prefix="xsb_audit_cmp_")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cfg(self, **kw: Any) -> BrowserLaunchConfig:
        params: Dict[str, Any] = dict(
            profile_root=self.tmp / "profiles",
            channel=None,
            headless=True,
            review_pause_sec=0.05,
            timeout_ms=4000,
            screenshot_dir=self.tmp / "shots",
        )
        params.update(kw)
        return BrowserLaunchConfig(**params)


class TestPublishResultTrust(_BrowserTestBase):
    def test_no_schedule_only_saves_draft_and_never_clicks_publish(self) -> None:
        self.sim.pages["/pub_ok"] = (200, _publish_page())
        with TomatoBrowserWorker("acc_a", self._cfg(), now_provider=lambda: NOW) as worker:
            outcome = worker.publish_chapter(_task(), target_url=f"{self.base_url}/pub_ok")
        self.assertEqual(outcome.status, StockpileStatus.DRAFT_SAVED.value)
        self.assertTrue(_wait_for(lambda: len(self.sim.posts.get("/api/save-draft", [])) == 1))
        self.assertNotIn("/api/publish", self.sim.posts)
        self.assertEqual(outcome.receipt.scheduled_at, "")
        self.assertEqual(outcome.receipt.remote_chapter_id, "rm-draft-6")
        self.assertFalse(outcome.details["post_verify"])

    def test_valid_future_schedule_clicks_publish(self) -> None:
        self.sim.pages["/pub_sched"] = (200, _publish_page())
        with TomatoBrowserWorker("acc_b", self._cfg(), now_provider=lambda: NOW) as worker:
            outcome = worker.publish_chapter(
                _task(scheduled_publish_at="2026-11-19T08:30:00+08:00"),
                target_url=f"{self.base_url}/pub_sched",
            )
        self.assertEqual(outcome.status, StockpileStatus.SCHEDULED.value)
        self.assertTrue(_wait_for(lambda: len(self.sim.posts.get("/api/publish", [])) == 1))
        self.assertEqual(self.sim.posts["/api/publish"][0]["scheduled_publish_at"], "2026-11-19 08:30")
        # No remote id attribute on the publish button -> deterministic fallback, never inner_text.
        self.assertEqual(outcome.receipt.remote_chapter_id, "tomato-ch-6")

    def test_schedule_in_past_or_too_soon_rejected_before_any_write(self) -> None:
        self.sim.pages["/pub_past"] = (200, _publish_page())
        with TomatoBrowserWorker("acc_c", self._cfg(), now_provider=lambda: NOW) as worker:
            for sched in ("2026-11-18T13:00:00+08:00", "2026-11-18 13:07", "2026-11-18T13:05:00+08:00"):
                with self.assertRaisesRegex(CompanionWorkerError, "schedule in the past"):
                    worker.publish_chapter(
                        _task(scheduled_publish_at=sched),
                        target_url=f"{self.base_url}/pub_past",
                    )
        time.sleep(0.2)
        self.assertEqual(self.sim.posts, {})

    def test_missing_save_button_raises(self) -> None:
        self.sim.pages["/pub_nosave"] = (200, _publish_page(with_save=False))
        with TomatoBrowserWorker("acc_d", self._cfg(), now_provider=lambda: NOW) as worker:
            with self.assertRaises(CompanionWorkerError):
                worker.publish_chapter(_task(), target_url=f"{self.base_url}/pub_nosave")
        time.sleep(0.2)
        self.assertEqual(self.sim.posts, {})

    def test_missing_submit_buttons_raise_for_other_tasks(self) -> None:
        self.sim.pages["/empty_form"] = (200, f"<!doctype html><html><body>{_IDENTITY}</body></html>")
        with TomatoBrowserWorker("acc_e", self._cfg(), now_provider=lambda: NOW) as worker:
            with self.assertRaises(CompanionWorkerError):
                worker.create_star_cards(
                    _task(task_type=CompanionTaskType.CREATE_STAR_CARDS.value, metadata={"star_cards": [{"name": "陈青"}]}),
                    target_url=f"{self.base_url}/empty_form",
                )
            with self.assertRaises(CompanionWorkerError):
                worker.submit_signing_draft(
                    _task(
                        task_type=CompanionTaskType.SUBMIT_SIGNING_DRAFT.value,
                        metadata={"signing_application": {"core_hook": "x"}},
                    ),
                    target_url=f"{self.base_url}/empty_form",
                )
            with self.assertRaises(CompanionWorkerError):
                worker.rotate_packaging(
                    _task(task_type=CompanionTaskType.ROTATE_PACKAGING.value, metadata={"new_title": "新书名"}),
                    target_url=f"{self.base_url}/empty_form",
                )


class TestCoverUploadConfinement(_BrowserTestBase):
    def _rotate_task(self, cover_path: str) -> CompanionLeaseResponse:
        return _task(
            task_type=CompanionTaskType.ROTATE_PACKAGING.value,
            metadata={"new_title": "盲盒开出仙帝剑", "cover_path": cover_path},
        )

    def test_cover_validation_rules(self) -> None:
        cover_root = self.tmp / "covers"
        cover_root.mkdir()
        good = cover_root / "good.png"
        good.write_bytes(PNG_BYTES)
        outside = self.tmp / "secret.png"
        outside.write_bytes(PNG_BYTES)
        fake = cover_root / "fake.png"
        fake.write_bytes(b"not really a png at all")
        wrong_ext = cover_root / "notes.txt"
        wrong_ext.write_bytes(PNG_BYTES)
        big = cover_root / "big.png"
        big.write_bytes(PNG_BYTES + b"\x00" * (5 * 1024 * 1024))
        link = cover_root / "link.png"
        os.symlink(outside, link)

        # No cover_root configured -> uploads disabled entirely.
        no_root = TomatoBrowserWorker("acc_cov0", self._cfg())
        with self.assertRaisesRegex(CompanionWorkerError, "no cover_root configured"):
            no_root._validate_cover_path(str(good))

        worker = TomatoBrowserWorker("acc_cov1", self._cfg(cover_root=cover_root))
        self.assertEqual(worker._validate_cover_path(str(good)), good.resolve())
        self.assertEqual(worker._validate_cover_path("good.png"), good.resolve())
        for bad in (
            str(outside),
            str(cover_root / ".." / "secret.png"),
            str(fake),
            str(wrong_ext),
            str(big),
            str(link),
            "/etc/passwd",
            str(cover_root / "missing.png"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(CompanionWorkerError):
                    worker._validate_cover_path(bad)

    def test_rotate_rejects_outside_cover_before_navigation_and_uploads_valid_cover(self) -> None:
        cover_root = self.tmp / "covers"
        cover_root.mkdir()
        good = cover_root / "good.png"
        good.write_bytes(PNG_BYTES)
        outside = self.tmp / "outside.png"
        outside.write_bytes(PNG_BYTES)
        self.sim.pages["/rotate"] = (
            200,
            f"""<!doctype html><html><body>{_IDENTITY}
            <input id="rotate-new-title" />
            <input id="rotate-cover-input" type="file" />
            <button id="rotate-submit" type="button">提交换皮</button>
            <script>
              document.getElementById('rotate-submit').addEventListener('click', () => {{
                const f = document.getElementById('rotate-cover-input').files;
                fetch('/api/rotate', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                  body: JSON.stringify({{title: document.getElementById('rotate-new-title').value,
                                         file: f.length ? f[0].name : ''}})}});
              }});
            </script></body></html>""",
        )
        with TomatoBrowserWorker("acc_cov2", self._cfg(cover_root=cover_root)) as worker:
            with self.assertRaises(CompanionWorkerError):
                worker.rotate_packaging(self._rotate_task(str(outside)), target_url=f"{self.base_url}/rotate")
            self.assertNotIn("/api/rotate", self.sim.posts)
            outcome = worker.rotate_packaging(self._rotate_task(str(good)), target_url=f"{self.base_url}/rotate")
        self.assertTrue(outcome.details["cover_uploaded"])
        self.assertTrue(_wait_for(lambda: len(self.sim.posts.get("/api/rotate", [])) == 1))
        self.assertEqual(self.sim.posts["/api/rotate"][0]["file"], "good.png")


class TestSentinelCoverage(_BrowserTestBase):
    def test_marker_text_inside_editor_and_form_fields_is_ignored(self) -> None:
        with TomatoBrowserWorker("acc_s1", self._cfg()) as worker:
            sentinel = worker._sentinel
            worker.page.set_content(
                f"""<html><body>{_IDENTITY}
                <div class="ProseMirror" contenteditable="true"><p>他看着屏幕：登录已过期，请扫码登录。</p></div>
                <textarea>滑块验证 请完成安全验证</textarea>
                <input value="账号验证" />
                <div contenteditable="true">账号验证</div>
                <div style="display:none">登录已过期</div>
                </body></html>"""
            )
            self.assertIsNone(sentinel.check_passive_signals(worker.page))
            worker.page.set_content("<html><body><div>登录已过期，请重新登录</div></body></html>")
            self.assertEqual(sentinel.check_passive_signals(worker.page), "DOM_TEXT:登录已过期")

    def test_third_party_and_non_xhr_403_do_not_trigger_but_first_party_fetch_does(self) -> None:
        port = self.base_url.rsplit(":", 1)[1]
        self.sim.pages["/net"] = (200, f"<!doctype html><html><body>{_IDENTITY}</body></html>")
        with TomatoBrowserWorker("acc_s2", self._cfg()) as worker:
            worker.page.goto(f"{self.base_url}/net")
            sentinel = worker._sentinel
            sentinel.auth_http_errors.clear()
            # Same-host image 403 (not document/xhr/fetch) -> ignored.
            worker.page.evaluate(
                """(u) => new Promise((res) => { const i = new Image(); i.onerror = res; i.onload = res; i.src = u; })""",
                f"{self.base_url}/forbidden-img",
            )
            # Third-party host fetch 403 -> ignored.
            worker.page.evaluate(
                "(u) => fetch(u, {mode: 'no-cors'}).catch(() => null)",
                f"http://localhost:{port}/forbidden-3p",
            )
            worker.page.wait_for_timeout(300)
            self.assertEqual(sentinel.auth_http_errors, [])
            self.assertIsNone(sentinel.check_passive_signals(worker.page))
            # First-party XHR/fetch 403 -> counted.
            worker.page.evaluate("(u) => fetch(u).catch(() => null)", f"{self.base_url}/forbidden-api")
            self.assertTrue(_wait_for(lambda: bool(sentinel.auth_http_errors)))
            self.assertTrue(str(sentinel.check_passive_signals(worker.page)).startswith("AUTH_HTTP_403"))

    def test_guarded_click_converts_timeout_plus_captcha_into_pause(self) -> None:
        html = """<html><body>
          <button id="target" disabled>提交</button>
          <div class="secsdk-captcha" style="width:300px;height:150px;background:#eee;">安全验证</div>
        </body></html>"""
        with TomatoBrowserWorker("acc_s3", self._cfg(timeout_ms=800)) as worker:
            worker.page.set_content(html)
            real_check = worker._sentinel.check_passive_signals
            calls = {"n": 0}

            def first_clean_then_real(page: Any) -> Any:
                calls["n"] += 1
                if calls["n"] == 1:
                    return None  # captcha "appears" after the pre-action inspection
                return real_check(page)

            with mock.patch.object(worker._sentinel, "check_passive_signals", side_effect=first_clean_then_real):
                with self.assertRaises(CaptchaAuthPauseError) as ctx:
                    worker._guarded_click("#target", book_id="bk_881")
            self.assertIn("secsdk-captcha", ctx.exception.report.reason)
            self.assertGreaterEqual(calls["n"], 2)

            # Plain timeout without captcha is re-raised unchanged.
            worker.page.set_content('<html><body><button id="target" disabled>提交</button></body></html>')
            with self.assertRaises(PlaywrightTimeoutError):
                worker._guarded_click("#target")

    def test_guarded_fill_halts_before_action_when_captcha_visible(self) -> None:
        with TomatoBrowserWorker("acc_s4", self._cfg()) as worker:
            worker.page.set_content(
                """<html><body><input id="f" />
                <div class="secsdk-captcha" style="width:300px;height:150px;">x</div>
                <script>window.__typed = false; document.getElementById('f').addEventListener('input', () => { window.__typed = true; });</script>
                </body></html>"""
            )
            with self.assertRaises(CaptchaAuthPauseError):
                worker._guarded_fill("#f", "hello")
            self.assertFalse(worker.page.evaluate("() => window.__typed"))

    def test_captcha_report_send_failure_still_raises_pause(self) -> None:
        failing_client = mock.Mock()
        failing_client.send_captcha_report.side_effect = RuntimeError("network down")
        with TomatoBrowserWorker("acc_s5", self._cfg(), client=failing_client) as worker:
            worker.page.set_content('<html><body><div class="secsdk-captcha" style="width:10px;height:10px;">x</div></body></html>')
            with self.assertRaises(CaptchaAuthPauseError):
                worker._halt_if_flagged(book_id="bk")
        failing_client.send_captcha_report.assert_called_once()


class TestWoff2AndFontLimits(_BrowserTestBase):
    def test_woff2_fetch_rejects_cross_origin_http(self) -> None:
        self.sim.pages["/m"] = (200, "<html><body>m</body></html>")
        with TomatoBrowserWorker("acc_w1", self._cfg()) as worker:
            worker.page.goto(f"{self.base_url}/m")
            with self.assertRaises(CompanionWorkerError):
                worker._fetch_woff2_bytes_from_url("http://evil.example.com/font.woff2")
            with self.assertRaises(CompanionWorkerError):
                worker._fetch_woff2_bytes_from_url("file:///etc/passwd")

    def test_font_decoder_wraps_garbage_and_caps_size(self) -> None:
        decoder = Woff2FontDecoder()
        with self.assertRaises(FontDecodeError):
            decoder.build_font_mapping(b"wOF2" + b"\x00" * 200, use_cache=False)
        with self.assertRaises(FontDecodeError):
            decoder.build_font_mapping(b"\x00" * (2 * 1024 * 1024 + 1), use_cache=False)


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.poll_calls = 0
        self.receipts: List[Any] = []
        self.closed = False
        _FakeClient.instance = self

    instance: "_FakeClient"
    stop_event: threading.Event

    def flush_outbox(self) -> None:
        return None

    def poll_tasks(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        self.poll_calls += 1
        if self.poll_calls <= 2:
            raise ConnectionError("server unreachable")
        if self.poll_calls == 3:
            return [
                {"task_id": "t-boom", "task_type": CompanionTaskType.PUBLISH_CHAPTER.value, "book_id": "bk", "chapter": 1},
                {"task_id": "t-ok", "task_type": CompanionTaskType.PUBLISH_CHAPTER.value, "book_id": "bk", "chapter": 2},
            ]
        _FakeClient.stop_event.set()
        return []

    def lease_task(self, req: Any) -> CompanionLeaseResponse:
        return _task(task_id=req.task_id, book_id=req.book_id, chapter=req.chapter)

    def send_receipt(self, receipt: Any) -> None:
        self.receipts.append(receipt)

    def close(self) -> None:
        self.closed = True


class _FakeWorker:
    executed: List[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_FakeWorker":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute_task(self, lease: CompanionLeaseResponse, *, target_url: str) -> Any:
        _FakeWorker.executed.append(lease.task_id)
        if lease.task_id == "t-boom":
            raise RuntimeError("unexpected DOM explosion")
        outcome = mock.Mock()
        outcome.to_dict.return_value = {"task_id": lease.task_id, "status": "DRAFT_SAVED"}
        return outcome


class TestCompanionDaemonResilience(unittest.TestCase):
    def test_daemon_backs_off_on_poll_errors_and_survives_task_exceptions(self) -> None:
        stop = threading.Event()
        _FakeClient.stop_event = stop
        _FakeWorker.executed = []
        with tempfile.TemporaryDirectory(prefix="xsb_audit_daemon_") as tmpdir:
            args = argparse.Namespace(
                server_url="http://127.0.0.1:9",
                hmac_secret="secret-secret-secret-123",
                account_id="acc_daemon",
                target_url="http://127.0.0.1:9/publish",
                profile_root=tmpdir,
                outbox_db=str(Path(tmpdir) / "outbox.db"),
                chrome_channel="none",
                headless=True,
                once=False,
                poll_interval=0.1,
                poll_timeout=0,
                cover_root=None,
            )
            with mock.patch.object(companion_pkg, "CompanionHttpClient", _FakeClient), mock.patch.object(
                companion_pkg, "TomatoBrowserWorker", _FakeWorker
            ), mock.patch("random.uniform", return_value=0.0), mock.patch(
                "builtins.print"
            ):
                rc = run_companion_daemon(args, stop_event=stop)
        self.assertEqual(rc, 0)
        client = _FakeClient.instance
        self.assertGreaterEqual(client.poll_calls, 4)
        self.assertEqual(_FakeWorker.executed, ["t-boom", "t-ok"])
        self.assertEqual(len(client.receipts), 1)
        self.assertEqual(client.receipts[0].task_id, "t-boom")
        self.assertEqual(client.receipts[0].status, StockpileStatus.FAILED_HARD_MISMATCH.value)
        self.assertEqual(client.receipts[0].error_code, "COMPANION_TASK_ERROR")
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
