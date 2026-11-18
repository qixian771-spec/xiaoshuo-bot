from __future__ import annotations

import base64
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional
import unittest
from unittest import mock
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.contracts import (
    AutoCreateBookRequest,
    BookRunStatus,
    BookSummaryResponse,
    CompanionCaptchaReport,
    MatrixControlPort,
    MatrixDailyDigest,
)
from xiaoshuo_bot.models import SHANGHAI_TZ
from xiaoshuo_bot.telegram_bot import (
    CallbackDataError,
    DEFAULT_POLL_TIMEOUT_SEC,
    DEFAULT_READ_TIMEOUT_SEC,
    MAX_CALLBACK_DATA_BYTES,
    MAX_HTML_MESSAGE_CHARS,
    MAX_PHOTO_CAPTION_CHARS,
    TelegramAPIError,
    TelegramBotConfig,
    TelegramConfigError,
    TelegramControlBot,
    TelegramHTTPSClient,
    escape_html,
    make_inline_button,
    paginate_html_chunks,
    validate_callback_data,
)

SAMPLE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, str, bytes]]]:
    match = re.search(r"boundary=([^\s;]+)", content_type)
    if not match:
        raise ValueError("Missing multipart boundary")
    boundary = match.group(1).strip('"').encode("ascii")
    delimiter = b"--" + boundary
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, str, bytes]] = {}
    for part in body.split(delimiter):
        if b"Content-Disposition:" not in part:
            continue
        header_blob, _, payload = part.partition(b"\r\n\r\n")
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        header_text = header_blob.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]+)"', header_text)
        if not name_match:
            continue
        field_name = name_match.group(1)
        fn_match = re.search(r'filename="([^"]+)"', header_text)
        if fn_match:
            ct_match = re.search(r"Content-Type:\s*([^\r\n]+)", header_text, re.IGNORECASE)
            mime = ct_match.group(1).strip() if ct_match else "application/octet-stream"
            files[field_name] = (fn_match.group(1), mime, payload)
        else:
            fields[field_name] = payload.decode("utf-8", errors="replace")
    return fields, files


class _SimulatedTelegramServer:
    """In-process 127.0.0.1:0 HTTP server simulating Telegram Bot API."""

    def __init__(self, expected_token: str) -> None:
        self.expected_token = expected_token
        self.lock = threading.Lock()
        self.pending_updates: List[Dict[str, Any]] = []
        self.api_calls: List[Dict[str, Any]] = []
        self.force_error_method: Optional[str] = None
        self.force_error_status: int = 500
        self.next_message_id: int = 100
        self.get_updates_event = threading.Event()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, status: int, payload: object) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                prefix = f"/bot{outer.expected_token}/"
                if not parsed.path.startswith(prefix):
                    self._send_json(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                    return
                method = parsed.path[len(prefix) :]
                length = int(self.headers.get("Content-Length") or "0")
                raw_body = self.rfile.read(length) if length > 0 else b""
                content_type = self.headers.get("Content-Type") or ""

                if "multipart/form-data" in content_type:
                    fields, files = _parse_multipart(content_type, raw_body)
                    payload: Dict[str, Any] = {"fields": fields, "files": files}
                else:
                    payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}

                with outer.lock:
                    if method == outer.force_error_method:
                        self._send_json(
                            outer.force_error_status,
                            {
                                "ok": False,
                                "error_code": outer.force_error_status,
                                "description": f"Forced error on /bot{outer.expected_token}/{method}",
                            },
                        )
                        return

                    outer.api_calls.append({"method": method, "payload": payload})

                    if method == "getUpdates":
                        offset = payload.get("offset")
                        if offset is not None:
                            delivered = [
                                u for u in outer.pending_updates if int(u["update_id"]) >= int(offset)
                            ]
                            outer.pending_updates = [
                                u for u in outer.pending_updates if int(u["update_id"]) >= int(offset)
                            ]
                        else:
                            delivered = list(outer.pending_updates)
                        outer.get_updates_event.set()
                        self._send_json(200, {"ok": True, "result": delivered})
                        return

                    if method == "answerCallbackQuery":
                        self._send_json(200, {"ok": True, "result": True})
                        return

                    if method in {"sendMessage", "sendPhoto"}:
                        outer.next_message_id += 1
                        mid = outer.next_message_id
                        self._send_json(
                            200,
                            {
                                "ok": True,
                                "result": {
                                    "message_id": mid,
                                    "date": 1700000000,
                                },
                            },
                        )
                        return

                    if method in {"editMessageReplyMarkup", "editMessageText"}:
                        self._send_json(200, {"ok": True, "result": True})
                        return

                    self._send_json(404, {"ok": False, "error_code": 404, "description": "Unknown method"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def push_update(self, update: Dict[str, Any]) -> None:
        with self.lock:
            self.pending_updates.append(update)

    def calls_excluding_poll(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [c for c in self.api_calls if c["method"] != "getUpdates"]

    def clear_calls(self) -> None:
        with self.lock:
            self.api_calls.clear()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


class _InMemoryMatrixPort:
    """Stateful genuine MatrixControlPort implementation for testing TelegramControlBot."""

    def __init__(self) -> None:
        self.books: Dict[str, BookSummaryResponse] = {
            "b12": BookSummaryResponse(
                book_id="b12",
                title="我在规则怪谈当宿管 & <破局>",
                genre_id="suspense_rules",
                author_level="lv0_1",
                contract_track="complete_150k",
                status=BookRunStatus.ACTIVE.value,
                stockpiled_count=12,
                safety_cushion_target=10,
                max_published_chapter=8,
                total_chapters=80,
                daily_target_chapters=2,
                account_id="ac1",
            )
        }
        self.digest_override: Optional[MatrixDailyDigest] = None
        self.cut_requests: List[Dict[str, Any]] = []
        self.cut_confirms: List[Dict[str, Any]] = []
        self.cut_cancels: List[str] = []
        self.promotions: List[str] = []
        self.resumed_companions: List[Dict[str, str]] = []
        self.paused_books: List[Dict[str, str]] = []
        self.resumed_books: List[str] = []
        self.created_requests: List[AutoCreateBookRequest] = []
        self._cut_versions: Dict[str, int] = {}

    def get_daily_digest(self) -> MatrixDailyDigest:
        if self.digest_override is not None:
            return self.digest_override
        summaries = list(self.books.values())
        return MatrixDailyDigest(
            total_books=len(summaries),
            active_books=sum(1 for b in summaries if b.status == BookRunStatus.ACTIVE.value),
            low_cushion_books=sum(1 for b in summaries if b.low_cushion_alert),
            isolated_books=sum(
                1 for b in summaries if b.status == BookRunStatus.ISOLATED_GATE_FAILURE.value
            ),
            daily_chars_generated=18400,
            daily_cost_usd=0.42,
            book_summaries=summaries,
        )

    def list_books(self) -> List[BookSummaryResponse]:
        return list(self.books.values())

    def get_book_status(self, book_id: str) -> BookSummaryResponse:
        if book_id not in self.books:
            raise KeyError(f"Unknown book_id: {book_id}")
        return self.books[book_id]

    def auto_create_book(self, request: AutoCreateBookRequest) -> BookSummaryResponse:
        self.created_requests.append(request)
        created = BookSummaryResponse(
            book_id=request.project_id,
            title=f"自动立项_{request.genre_id}",
            genre_id=request.genre_id,
            author_level=request.author_level,
            contract_track="complete_150k" if request.author_level == "lv0_1" else "serial_20k",
            status=BookRunStatus.ACTIVE.value,
            stockpiled_count=0,
            safety_cushion_target=10,
            total_chapters=80,
            account_id=request.account_id,
        )
        self.books[created.book_id] = created
        return created

    def approve_promotion(self, book_id: str) -> Dict[str, Any]:
        if book_id not in self.books:
            raise KeyError(f"Unknown book_id: {book_id}")
        self.promotions.append(book_id)
        self.books[book_id].daily_target_chapters += 1
        return {"book_id": book_id, "daily_target_chapters": self.books[book_id].daily_target_chapters}

    def request_cut_book(self, book_id: str, reason: str = "") -> Dict[str, Any]:
        if book_id not in self.books:
            raise KeyError(f"Unknown book_id: {book_id}")
        ver = self._cut_versions.get(book_id, 0) + 1
        self._cut_versions[book_id] = ver
        self.cut_requests.append({"book_id": book_id, "version": ver, "reason": reason})
        return {"book_id": book_id, "confirmation_version": ver, "pending": True}

    def confirm_cut_book(
        self, book_id: str, confirmation_version: int, reason: str = ""
    ) -> Dict[str, Any]:
        if book_id not in self.books:
            raise KeyError(f"Unknown book_id: {book_id}")
        expected = self._cut_versions.get(book_id)
        if expected != confirmation_version:
            raise ValueError(f"CAS mismatch: expected {expected}, got {confirmation_version}")
        self.cut_confirms.append(
            {"book_id": book_id, "confirmation_version": confirmation_version, "reason": reason}
        )
        self.books[book_id].status = BookRunStatus.CUT_LOSS.value
        self.books[book_id].paused_reason = reason
        return {"book_id": book_id, "status": BookRunStatus.CUT_LOSS.value}

    def cancel_cut_book(self, book_id: str) -> Dict[str, Any]:
        self.cut_cancels.append(book_id)
        return {"book_id": book_id, "cancelled": True}

    def report_captcha_pause(self, report: CompanionCaptchaReport) -> Dict[str, Any]:
        return {"account_id": report.account_id, "status": "PAUSED_AUTH_CAPTCHA"}

    def resume_companion(self, account_id: str, book_id: str = "") -> Dict[str, Any]:
        self.resumed_companions.append({"account_id": account_id, "book_id": book_id})
        return {"account_id": account_id, "book_id": book_id, "status": "RESUMED"}

    def pause_book(self, book_id: str, reason: str = "") -> Dict[str, Any]:
        if book_id not in self.books:
            raise KeyError(f"Unknown book_id: {book_id}")
        self.paused_books.append({"book_id": book_id, "reason": reason})
        self.books[book_id].paused_reason = reason
        self.books[book_id].status = BookRunStatus.ISOLATED_GATE_FAILURE.value
        return {"book_id": book_id, "status": "PAUSED"}

    def resume_book(self, book_id: str) -> Dict[str, Any]:
        if book_id not in self.books:
            raise KeyError(f"Unknown book_id: {book_id}")
        self.resumed_books.append(book_id)
        self.books[book_id].paused_reason = ""
        self.books[book_id].status = BookRunStatus.ACTIVE.value
        return {"book_id": book_id, "status": "ACTIVE"}


class TestTelegramBot(unittest.TestCase):
    OWNER_ID = 8800112233
    BOT_TOKEN = "99887766:SECRET_TELEGRAM_TOKEN_XYZ_DO_NOT_LEAK"

    def setUp(self) -> None:
        self.port = _InMemoryMatrixPort()
        self.assertIsInstance(self.port, MatrixControlPort)
        self.server = _SimulatedTelegramServer(self.BOT_TOKEN)

    def tearDown(self) -> None:
        self.server.close()

    def _make_bot(self, *, state_path: Optional[Path] = None, poll_timeout: int = 30, read_timeout: int = 45) -> TelegramControlBot:
        return TelegramControlBot(
            self.port,
            bot_token=self.BOT_TOKEN,
            owner_user_id=self.OWNER_ID,
            api_base_url=self.server.base_url,
            poll_timeout=poll_timeout,
            read_timeout=read_timeout,
            state_path=state_path,
        )

    def test_missing_or_invalid_config_raises_and_token_never_leaks(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_USER_ID": "", "TELEGRAM_API_BASE_URL": ""},
            clear=False,
        ):
            with self.assertRaises(TelegramConfigError):
                TelegramBotConfig.from_env()
            with self.assertRaises(TelegramConfigError):
                TelegramControlBot(self.port, bot_token="", owner_user_id=self.OWNER_ID)
            with self.assertRaises(TelegramConfigError):
                TelegramControlBot(self.port, bot_token=self.BOT_TOKEN, owner_user_id=None)
            with self.assertRaises(TelegramConfigError):
                TelegramControlBot(self.port, bot_token=self.BOT_TOKEN, owner_user_id="not-a-number")
            with self.assertRaises(TelegramConfigError):
                TelegramControlBot(self.port, bot_token=self.BOT_TOKEN, owner_user_id=0)
            with self.assertRaises(TelegramConfigError):
                TelegramControlBot(self.port, bot_token=self.BOT_TOKEN, owner_user_id=True)
            with self.assertRaises(TelegramConfigError):
                TelegramControlBot(
                    self.port,
                    bot_token=self.BOT_TOKEN,
                    owner_user_id=self.OWNER_ID,
                    poll_timeout=30,
                    read_timeout=20,
                )

        cfg = TelegramBotConfig(
            bot_token=self.BOT_TOKEN,
            owner_user_id=self.OWNER_ID,
            api_base_url=self.server.base_url,
        )
        self.assertNotIn(self.BOT_TOKEN, repr(cfg))

        # Verify HTTP error & transport error & logging never leak bot_token
        self.server.force_error_method = "sendMessage"
        self.server.force_error_status = 500
        bot = self._make_bot()
        with self.assertRaises(TelegramAPIError) as ctx:
            bot.push_alert("测试", "内容")
        self.assertNotIn(self.BOT_TOKEN, str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))
        self.server.force_error_method = None

        # Closed port transport failure
        bad_client = TelegramHTTPSClient(
            self.BOT_TOKEN,
            api_base_url="http://127.0.0.1:1",
            default_timeout=1,
            read_timeout=2,
        )
        with self.assertRaises(TelegramAPIError) as tx_ctx:
            bad_client.send_message(self.OWNER_ID, "hello")
        self.assertNotIn(self.BOT_TOKEN, str(tx_ctx.exception))

        # run_forever warning log redaction
        bad_bot = TelegramControlBot(
            self.port,
            bot_token=self.BOT_TOKEN,
            owner_user_id=self.OWNER_ID,
            api_base_url="http://127.0.0.1:1",
            poll_timeout=1,
            read_timeout=2,
            client=bad_client,
        )
        stop_ev = threading.Event()
        log_records: List[str] = []

        class _MemHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(self.format(record))

        tg_logger = logging.getLogger("xiaoshuo_bot.telegram_bot")
        handler = _MemHandler()
        tg_logger.addHandler(handler)
        try:
            t = threading.Thread(
                target=bad_bot.run_forever,
                kwargs={"stop_event": stop_ev, "error_backoff_sec": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.2)
            stop_ev.set()
            t.join(timeout=2.0)
        finally:
            tg_logger.removeHandler(handler)

        self.assertTrue(len(log_records) >= 1)
        for line in log_records:
            self.assertNotIn(self.BOT_TOKEN, line)

    def test_non_owner_messages_and_callbacks_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "tg_state.json"
            bot = self._make_bot(state_path=state_file)
            attacker_id = 999999999

            self.server.push_update(
                {
                    "update_id": 10,
                    "message": {
                        "message_id": 1,
                        "from": {"id": attacker_id, "is_bot": False},
                        "chat": {"id": attacker_id, "type": "private"},
                        "text": "/digest",
                    },
                }
            )
            self.server.push_update(
                {
                    "update_id": 11,
                    "callback_query": {
                        "id": "cb_attacker_1",
                        "from": {"id": attacker_id, "is_bot": False},
                        "message": {"message_id": 2, "chat": {"id": attacker_id}},
                        "data": "cut_do:b12:1",
                    },
                }
            )
            self.server.push_update(
                {
                    "update_id": 12,
                    "message": {
                        "message_id": 3,
                        "chat": {"id": attacker_id, "type": "private"},
                        "text": "/status",
                    },
                }
            )

            handled = bot.poll_once()
            self.assertEqual(handled, 0)
            # Zero outbound calls (only getUpdates was sent)
            self.assertEqual(self.server.calls_excluding_poll(), [])
            self.assertEqual(self.port.cut_requests, [])
            self.assertEqual(self.port.cut_confirms, [])
            # Offset STILL advances to prevent infinite replay loop
            self.assertEqual(bot.last_update_id, 12)
            self.assertEqual(bot.next_offset(), 13)

    def test_long_polling_timeouts_and_atomic_offset_persistence_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "nested" / "bot_state.json"
            bot1 = self._make_bot(state_path=state_file)
            self.assertEqual(bot1.config.poll_timeout, DEFAULT_POLL_TIMEOUT_SEC)
            self.assertEqual(bot1.config.read_timeout, DEFAULT_READ_TIMEOUT_SEC)

            self.server.push_update(
                {
                    "update_id": 501,
                    "message": {
                        "message_id": 11,
                        "from": {"id": self.OWNER_ID},
                        "chat": {"id": self.OWNER_ID},
                        "text": "/start",
                    },
                }
            )
            self.server.push_update(
                {
                    "update_id": 502,
                    "message": {
                        "message_id": 12,
                        "from": {"id": self.OWNER_ID},
                        "chat": {"id": self.OWNER_ID},
                        "text": "/status b12",
                    },
                }
            )

            handled = bot1.poll_once()
            self.assertEqual(handled, 2)
            self.assertEqual(bot1.last_update_id, 502)

            # Verify getUpdates sent timeout=30
            poll_calls = [c for c in self.server.api_calls if c["method"] == "getUpdates"]
            self.assertEqual(poll_calls[0]["payload"]["timeout"], 30)

            # Verify state file persisted atomically with SHANGHAI_TZ (+08:00) and no leftover .tmp
            self.assertTrue(state_file.exists())
            self.assertFalse(state_file.with_name(f"{state_file.name}.tmp").exists())
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["last_update_id"], 502)
            self.assertTrue(saved["updated_at"].endswith("+08:00"))

            # Simulate restart with bot2 reading same state_file
            self.server.clear_calls()
            bot2 = self._make_bot(state_path=state_file)
            self.assertEqual(bot2.last_update_id, 502)
            self.assertEqual(bot2.next_offset(), 503)

            # Replayed update_id <= 502 must be ignored even if API returns it
            self.assertFalse(
                bot2.process_update(
                    {
                        "update_id": 502,
                        "message": {
                            "message_id": 12,
                            "from": {"id": self.OWNER_ID},
                            "chat": {"id": self.OWNER_ID},
                            "text": "/status b12",
                        },
                    }
                )
            )
            self.assertEqual(self.server.calls_excluding_poll(), [])

            bot2.poll_once()
            poll_calls_2 = [c for c in self.server.api_calls if c["method"] == "getUpdates"]
            self.assertEqual(poll_calls_2[0]["payload"]["offset"], 503)

    def test_callback_data_pure_ascii_and_byte_limits(self) -> None:
        with self.assertRaises(CallbackDataError):
            validate_callback_data("切书:b12")
        with self.assertRaises(CallbackDataError):
            validate_callback_data("cut:" + "a" * 65)
        with self.assertRaises(CallbackDataError):
            validate_callback_data("cut ask:b12")
        with self.assertRaises(CallbackDataError):
            make_inline_button("ok", "中文按钮")

        # Long non-ASCII book_id gets mapped to a compact pure ASCII ref <= 32B
        long_id = "都市脑洞_测试超长标识_" + ("x" * 50)
        self.port.books[long_id] = BookSummaryResponse(
            book_id=long_id,
            title="测试长ID书",
            genre_id="urban_brainhole",
        )
        bot = self._make_bot()
        bot.push_cut_recommendation(long_id, title="测试长ID书", reason="CTR低")
        bot.push_promotion_alert(long_id, title="测试长ID书", stats_summary="读完68%")
        bot._handle_message(
            {
                "message_id": 1,
                "from": {"id": self.OWNER_ID},
                "chat": {"id": self.OWNER_ID},
                "text": "/new",
            }
        )
        bot._handle_message(
            {
                "message_id": 2,
                "from": {"id": self.OWNER_ID},
                "chat": {"id": self.OWNER_ID},
                "text": "/start",
            }
        )

        # Inspect every single inline button emitted
        button_count = 0
        for call in self.server.calls_excluding_poll():
            markup = call["payload"].get("reply_markup")
            if isinstance(markup, dict):
                for row in markup.get("inline_keyboard", []):
                    for btn in row:
                        cb = btn["callback_data"]
                        self.assertTrue(cb.isascii(), f"Non-ASCII callback_data: {cb!r}")
                        self.assertLessEqual(
                            len(cb.encode("utf-8")),
                            MAX_CALLBACK_DATA_BYTES,
                            f"callback_data > 64B: {cb!r}",
                        )
                        self.assertLessEqual(len(cb.encode("utf-8")), 32, f"Expected <=32B: {cb!r}")
                        button_count += 1
        self.assertGreaterEqual(button_count, 10)

    def test_cut_loss_two_step_cas_answer_first_and_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "cas_state.json"
            bot = self._make_bot(state_path=state_file)
            bot.push_cut_recommendation("b12", title="怪谈宿管", reason="二章读完率<45%")
            self.server.clear_calls()

            # Step 1: Single click on cut_ask:b12
            self.server.push_update(
                {
                    "update_id": 201,
                    "callback_query": {
                        "id": "cb_cut_ask_1",
                        "from": {"id": self.OWNER_ID},
                        "message": {"message_id": 900, "chat": {"id": self.OWNER_ID}},
                        "data": "cut_ask:b12",
                    },
                }
            )
            bot.poll_once()

            calls_step1 = self.server.calls_excluding_poll()
            self.assertGreaterEqual(len(calls_step1), 2)
            # Rule 2: answerCallbackQuery MUST be called FIRST
            self.assertEqual(calls_step1[0]["method"], "answerCallbackQuery")
            self.assertEqual(calls_step1[0]["payload"]["callback_query_id"], "cb_cut_ask_1")
            # Rule 3: In-place keyboard replacement, ZERO confirm_cut_book execution
            self.assertEqual(calls_step1[1]["method"], "editMessageReplyMarkup")
            rows = calls_step1[1]["payload"]["reply_markup"]["inline_keyboard"]
            self.assertEqual(rows[0][0]["callback_data"], "cut_do:b12:1")
            self.assertEqual(rows[0][1]["callback_data"], "cut_no:b12")
            self.assertEqual(len(self.port.cut_confirms), 0)
            self.assertEqual(len(self.port.cut_requests), 1)

            # Stale version click cut_do:b12:99 must be rejected idempotently
            self.server.clear_calls()
            self.server.push_update(
                {
                    "update_id": 202,
                    "callback_query": {
                        "id": "cb_cut_stale",
                        "from": {"id": self.OWNER_ID},
                        "message": {"message_id": 900, "chat": {"id": self.OWNER_ID}},
                        "data": "cut_do:b12:99",
                    },
                }
            )
            bot.poll_once()
            self.assertEqual(self.server.calls_excluding_poll()[0]["method"], "answerCallbackQuery")
            self.assertEqual(len(self.port.cut_confirms), 0)

            # Step 2: Confirm click cut_do:b12:1 -> executes confirm_cut_book ONCE
            self.server.clear_calls()
            self.server.push_update(
                {
                    "update_id": 203,
                    "callback_query": {
                        "id": "cb_cut_do_1",
                        "from": {"id": self.OWNER_ID},
                        "message": {"message_id": 900, "chat": {"id": self.OWNER_ID}},
                        "data": "cut_do:b12:1",
                    },
                }
            )
            bot.poll_once()
            calls_step2 = self.server.calls_excluding_poll()
            self.assertEqual(calls_step2[0]["method"], "answerCallbackQuery")
            self.assertEqual(len(self.port.cut_confirms), 1)
            self.assertEqual(self.port.cut_confirms[0]["book_id"], "b12")
            self.assertEqual(self.port.cut_confirms[0]["confirmation_version"], 1)
            self.assertEqual(self.port.books["b12"].status, BookRunStatus.CUT_LOSS.value)

            # Replay click of cut_do:b12:1 (even after bot restart) -> idempotent, count stays 1
            restarted_bot = self._make_bot(state_path=state_file)
            self.server.clear_calls()
            self.server.push_update(
                {
                    "update_id": 204,
                    "callback_query": {
                        "id": "cb_cut_do_replay",
                        "from": {"id": self.OWNER_ID},
                        "message": {"message_id": 900, "chat": {"id": self.OWNER_ID}},
                        "data": "cut_do:b12:1",
                    },
                }
            )
            restarted_bot.poll_once()
            self.assertEqual(
                self.server.calls_excluding_poll()[0]["method"], "answerCallbackQuery"
            )
            self.assertEqual(len(self.port.cut_confirms), 1)

    def test_cut_cancel_invalidates_pending_confirmation(self) -> None:
        bot = self._make_bot()
        self.server.push_update(
            {
                "update_id": 301,
                "callback_query": {
                    "id": "cb_ask",
                    "from": {"id": self.OWNER_ID},
                    "message": {"message_id": 901, "chat": {"id": self.OWNER_ID}},
                    "data": "cut_ask:b12",
                },
            }
        )
        self.server.push_update(
            {
                "update_id": 302,
                "callback_query": {
                    "id": "cb_cancel",
                    "from": {"id": self.OWNER_ID},
                    "message": {"message_id": 901, "chat": {"id": self.OWNER_ID}},
                    "data": "cut_no:b12",
                },
            }
        )
        self.server.push_update(
            {
                "update_id": 303,
                "callback_query": {
                    "id": "cb_late_do",
                    "from": {"id": self.OWNER_ID},
                    "message": {"message_id": 901, "chat": {"id": self.OWNER_ID}},
                    "data": "cut_do:b12:1",
                },
            }
        )
        bot.poll_once()
        self.assertEqual(self.port.cut_cancels, ["b12"])
        self.assertEqual(self.port.cut_confirms, [])

    def test_html_escaping_and_auto_pagination_over_3800_chars(self) -> None:
        self.assertEqual(escape_html('A & <script>"x"</script> > B'), "A &amp; &lt;script&gt;&quot;x&quot;&lt;/script&gt; &gt; B")

        many_books: List[BookSummaryResponse] = []
        for idx in range(1, 36):
            many_books.append(
                BookSummaryResponse(
                    book_id=f"book_{idx:02d}",
                    title=f"第{idx}本 <爆款> & 奇遇記_" + ("书名" * 5),
                    genre_id="urban_brainhole",
                    author_level="lv2_plus",
                    contract_track="serial_20k",
                    status=BookRunStatus.ACTIVE.value,
                    stockpiled_count=10 + (idx % 5),
                    safety_cushion_target=10,
                    max_published_chapter=idx * 2,
                    total_chapters=200,
                    daily_target_chapters=4,
                    paused_reason="测试<标签>&提示" if idx % 7 == 0 else "",
                )
            )
        digest = MatrixDailyDigest(
            total_books=len(many_books),
            active_books=len(many_books),
            low_cushion_books=2,
            daily_chars_generated=180000,
            daily_cost_usd=3.55,
            book_summaries=many_books,
            alerts=["<警告> 账号池触发限流 & 已切换"],
            pending_cut_recommendations=[
                {"book_id": "book_05", "title": "第5本 <爆款>", "reason": "读完率 < 40%"}
            ],
        )
        self.port.digest_override = digest

        bot = self._make_bot()
        sent = bot.send_daily_digest()
        self.assertGreaterEqual(len(sent), 2)

        msg_calls = [c for c in self.server.calls_excluding_poll() if c["method"] == "sendMessage"]
        self.assertEqual(len(msg_calls), len(sent))
        combined_text = ""
        for idx, call in enumerate(msg_calls, start=1):
            txt = call["payload"]["text"]
            self.assertEqual(call["payload"]["parse_mode"], "HTML")
            self.assertLessEqual(len(txt), MAX_HTML_MESSAGE_CHARS)
            self.assertIn(f"第 {idx}/{len(msg_calls)} 页", txt)
            self.assertNotIn("<script>", txt)
            self.assertNotIn("<警告>", txt)
            combined_text += txt
        self.assertIn("&lt;警告&gt;", combined_text)
        self.assertIn("book_35", combined_text)
        # Cut button attached on final page
        last_markup = msg_calls[-1]["payload"].get("reply_markup")
        self.assertIsNotNone(last_markup)
        self.assertEqual(last_markup["inline_keyboard"][0][0]["callback_data"], "cut_ask:book_05")

        # Also verify paginate_html_chunks handles single oversized block
        huge = "\n".join(f"行{i}: 内容" + ("x" * 120) for i in range(60))
        chunks = paginate_html_chunks([huge], max_chars=3800)
        self.assertGreaterEqual(len(chunks), 2)
        for ch in chunks:
            self.assertLessEqual(len(ch), 3800)

    def test_captcha_send_photo_multipart_and_resume_companion(self) -> None:
        bot = self._make_bot()
        report = CompanionCaptchaReport(
            account_id="ac_tomato_01",
            book_id="b12",
            chapter=9,
            reason="CAPTCHA_DETECTED <secsdk>",
            page_url="https://fanqienovel.com/writer/zone?x=1&y=2",
            screenshot_base64=base64.b64encode(SAMPLE_PNG_BYTES).decode("ascii"),
            screenshot_mime="image/png",
        )
        bot.push_captcha_alert(report)

        photo_calls = [c for c in self.server.calls_excluding_poll() if c["method"] == "sendPhoto"]
        self.assertEqual(len(photo_calls), 1)
        fields = photo_calls[0]["payload"]["fields"]
        files = photo_calls[0]["payload"]["files"]
        self.assertEqual(fields["chat_id"], str(self.OWNER_ID))
        self.assertEqual(fields["parse_mode"], "HTML")
        self.assertIn("&lt;secsdk&gt;", fields["caption"])
        self.assertIn("x=1&amp;y=2", fields["caption"])
        self.assertIn("photo", files)
        filename, mime, uploaded_bytes = files["photo"]
        self.assertTrue(filename.endswith(".png"))
        self.assertEqual(mime, "image/png")
        self.assertEqual(uploaded_bytes, SAMPLE_PNG_BYTES)

        markup = json.loads(fields["reply_markup"])
        btn = markup["inline_keyboard"][0][0]
        self.assertIn("恢复运行", btn["text"])
        cb_data = btn["callback_data"]
        self.assertTrue(cb_data.startswith("cap_ok:ac_tomato_01:"))
        self.assertLessEqual(len(cb_data.encode("utf-8")), 32)

        # Owner clicks resume button twice -> answerCallbackQuery first, resume_companion called once
        self.server.clear_calls()
        self.server.push_update(
            {
                "update_id": 401,
                "callback_query": {
                    "id": "cb_cap_1",
                    "from": {"id": self.OWNER_ID},
                    "message": {"message_id": 950, "chat": {"id": self.OWNER_ID}},
                    "data": cb_data,
                },
            }
        )
        self.server.push_update(
            {
                "update_id": 402,
                "callback_query": {
                    "id": "cb_cap_2",
                    "from": {"id": self.OWNER_ID},
                    "message": {"message_id": 950, "chat": {"id": self.OWNER_ID}},
                    "data": cb_data,
                },
            }
        )
        bot.poll_once()
        calls = self.server.calls_excluding_poll()
        self.assertEqual(calls[0]["method"], "answerCallbackQuery")
        self.assertEqual(len(self.port.resumed_companions), 1)
        self.assertEqual(
            self.port.resumed_companions[0],
            {"account_id": "ac_tomato_01", "book_id": "b12"},
        )

    def test_commands_new_pause_resume_promote_and_background_thread(self) -> None:
        bot = self._make_bot(poll_timeout=1, read_timeout=5)
        bot.start_background(error_backoff_sec=0.05)
        try:
            self.server.push_update(
                {
                    "update_id": 601,
                    "callback_query": {
                        "id": "cb_new_urban",
                        "from": {"id": self.OWNER_ID},
                        "message": {"message_id": 1001, "chat": {"id": self.OWNER_ID}},
                        "data": "new:urban_brainhole",
                    },
                }
            )
            self.server.push_update(
                {
                    "update_id": 602,
                    "message": {
                        "message_id": 1002,
                        "from": {"id": self.OWNER_ID},
                        "chat": {"id": self.OWNER_ID},
                        "text": "/pause b12 人工调整大纲",
                    },
                }
            )
            self.server.push_update(
                {
                    "update_id": 603,
                    "message": {
                        "message_id": 1003,
                        "from": {"id": self.OWNER_ID},
                        "chat": {"id": self.OWNER_ID},
                        "text": "/resume b12",
                    },
                }
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if (
                    len(self.port.created_requests) == 1
                    and len(self.port.paused_books) == 1
                    and len(self.port.resumed_books) == 1
                ):
                    break
                time.sleep(0.05)
        finally:
            bot.stop(timeout=3.0)

        self.assertEqual(len(self.port.created_requests), 1)
        self.assertEqual(self.port.created_requests[0].genre_id, "urban_brainhole")
        self.assertEqual(self.port.paused_books, [{"book_id": "b12", "reason": "人工调整大纲"}])
        self.assertEqual(self.port.resumed_books, ["b12"])

        # Promotion approval CAS
        bot.push_promotion_alert("b12", title="怪谈宿管", stats_summary="完读率 65%")
        promo_calls = [c for c in self.server.calls_excluding_poll() if c["method"] == "sendMessage"]
        apr_cb = promo_calls[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(apr_cb.startswith("apr:b12:"))
        bot.process_update(
            {
                "update_id": 604,
                "callback_query": {
                    "id": "cb_apr_1",
                    "from": {"id": self.OWNER_ID},
                    "message": {"message_id": 1004, "chat": {"id": self.OWNER_ID}},
                    "data": apr_cb,
                },
            }
        )
        bot.process_update(
            {
                "update_id": 605,
                "callback_query": {
                    "id": "cb_apr_2",
                    "from": {"id": self.OWNER_ID},
                    "message": {"message_id": 1004, "chat": {"id": self.OWNER_ID}},
                    "data": apr_cb,
                },
            }
        )
        self.assertEqual(self.port.promotions, ["b12"])

        # Unknown genre via /new does not crash or call auto_create_book
        bot.process_update(
            {
                "update_id": 606,
                "message": {
                    "message_id": 1006,
                    "from": {"id": self.OWNER_ID},
                    "chat": {"id": self.OWNER_ID},
                    "text": "/new nonexistent_genre",
                },
            }
        )
        self.assertEqual(len(self.port.created_requests), 1)

    def _assert_balanced_telegram_html(self, text: str) -> None:
        stack: List[str] = []

        class _StrictHTMLValidator(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=False)

            def handle_starttag(self, tag: str, attrs: Any) -> None:
                stack.append(tag.lower())

            def handle_endtag(self, tag: str) -> None:
                t = tag.lower()
                if not stack or stack[-1] != t:
                    raise AssertionError(f"Mismatched closing tag </{t}> with stack={stack} in {text!r}")
                stack.pop()

            def handle_data(self, data: str) -> None:
                # In HTML escaped text passed to our validator, check no truncated '&amp' without ';'
                if re.search(r"&(?:amp|lt|gt|quot)(?![;\w])", data):
                    raise AssertionError(f"Truncated HTML entity in {data!r}")

        parser = _StrictHTMLValidator()
        parser.feed(text)
        parser.close()
        self.assertEqual(stack, [], f"Unclosed HTML tags {stack} in {text!r}")

    def test_save_state_multithreaded_concurrency_no_errors_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "concurrent_state.json"
            bot = self._make_bot(state_path=state_file)
            num_threads = 6
            iters = 35
            barrier = threading.Barrier(num_threads)
            errors: List[BaseException] = []
            errors_lock = threading.Lock()
            expected_ids: List[tuple[str, str]] = []
            ids_lock = threading.Lock()

            def _worker(tid: int) -> None:
                try:
                    barrier.wait(timeout=5.0)
                    for idx in range(iters):
                        book_id = f"并发书目_{tid}_{idx}_" + ("X" * 28)
                        ref = bot._compact_ref(book_id)
                        with ids_lock:
                            expected_ids.append((ref, book_id))
                        if idx % 5 == 0:
                            bot.process_update(
                                {
                                    "update_id": 1000 + tid * 100 + idx,
                                    "message": {
                                        "message_id": idx + 1,
                                        "from": {"id": 999999999},  # non-owner still advances offset & saves
                                        "chat": {"id": 999999999},
                                        "text": "/status",
                                    },
                                }
                            )
                except BaseException as exc:
                    with errors_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=_worker, args=(t,)) for t in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15.0)
                self.assertFalse(t.is_alive(), "Worker thread timed out")

            self.assertEqual(errors, [], f"Concurrent _save_state raised errors: {errors!r}")
            self.assertTrue(state_file.exists())
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIsInstance(persisted, dict)
            self.assertEqual(len(persisted["refs"]), num_threads * iters)
            for ref, original_id in expected_ids:
                self.assertEqual(persisted["refs"].get(ref), original_id)

            reloaded = self._make_bot(state_path=state_file)
            for ref, original_id in expected_ids:
                self.assertEqual(reloaded._resolve_ref(ref), original_id)

    def test_paginate_html_chunks_balanced_tags_with_long_header_and_sections(self) -> None:
        # Exact reproducer: long HTML header + long HTML section exceeding effective_body_limit
        header = "<b>" + ("H" * 300) + "</b>"
        sections = ["<b>" + ("a" * 3600) + "</b>", "<b>" + ("b" * 200) + "</b>"]
        pages = paginate_html_chunks(sections, header=header, max_chars=3800)
        self.assertGreaterEqual(len(pages), 2)
        for idx, page in enumerate(pages, start=1):
            self.assertLessEqual(len(page), 3800)
            self.assertEqual(page.count("<b>"), page.count("</b>"), f"Page {idx} unbalanced <b>")
            self.assertEqual(page.count("<i>"), page.count("</i>"), f"Page {idx} unbalanced <i>")
            self._assert_balanced_telegram_html(page)

        # Single massive nested HTML section with entities and no newlines
        nested_sec = "<b><i>" + ("测试&amp;&lt;标签&gt;" * 400) + "</i></b>"
        huge_header = "<code>" + ("头部&amp;" * 600) + "</code>"
        nested_pages = paginate_html_chunks(
            [nested_sec],
            header=huge_header,
            footer="<b>尾注</b>",
            max_chars=3800,
        )
        self.assertGreaterEqual(len(nested_pages), 3)
        for page in nested_pages:
            self.assertLessEqual(len(page), 3800)
            self._assert_balanced_telegram_html(page)

        # Empty sections with oversized HTML header
        header_only_pages = paginate_html_chunks([], header="<b>" + ("Z" * 5000) + "</b>", max_chars=3800)
        self.assertGreaterEqual(len(header_only_pages), 2)
        for page in header_only_pages:
            self.assertLessEqual(len(page), 3800)
            self._assert_balanced_telegram_html(page)

    def test_captcha_alert_and_send_photo_clamp_preserve_balanced_html(self) -> None:
        bot = self._make_bot()
        long_reason = ("验证码拦截 <secsdk> & \"滑块\" " * 80).strip()
        self.assertGreater(len(long_reason), 1000)
        report = CompanionCaptchaReport(
            account_id="acct_<1>&2_" + ("A" * 120),
            book_id="book_<3>&4_" + ("B" * 120),
            chapter=12,
            reason=long_reason,
            page_url="https://fanqienovel.com/writer/zone?a=1&b=2&token=" + ("z" * 250),
            screenshot_base64=base64.b64encode(SAMPLE_PNG_BYTES).decode("ascii"),
            screenshot_mime="image/png",
        )
        bot.push_captcha_alert(report)

        photo_calls = [c for c in self.server.calls_excluding_poll() if c["method"] == "sendPhoto"]
        self.assertEqual(len(photo_calls), 1)
        caption = photo_calls[0]["payload"]["fields"]["caption"]
        self.assertLessEqual(len(caption), MAX_PHOTO_CAPTION_CHARS)
        self.assertEqual(caption.count("<code>"), caption.count("</code>"))
        self.assertGreaterEqual(caption.count("<code>"), 3)
        self.assertEqual(caption.count("<b>"), caption.count("</b>"))
        self._assert_balanced_telegram_html(caption)

        # Direct send_photo with oversized HTML caption also preserves balanced tags & entities
        self.server.clear_calls()
        direct_caption = "<b>" + ("字段&amp;&lt;值&gt;" * 200) + "</b>"
        bot.client.send_photo(
            self.OWNER_ID,
            SAMPLE_PNG_BYTES,
            caption=direct_caption,
            parse_mode="HTML",
        )
        direct_calls = [c for c in self.server.calls_excluding_poll() if c["method"] == "sendPhoto"]
        self.assertEqual(len(direct_calls), 1)
        clamped = direct_calls[0]["payload"]["fields"]["caption"]
        self.assertLessEqual(len(clamped), MAX_PHOTO_CAPTION_CHARS)
        self.assertEqual(clamped.count("<b>"), clamped.count("</b>"))
        self._assert_balanced_telegram_html(clamped)

    def test_run_forever_unexpected_exception_uses_error_and_redacts_token(self) -> None:
        bot = self._make_bot(poll_timeout=1, read_timeout=5)
        stop_ev = threading.Event()
        records: List[logging.LogRecord] = []
        formatted: List[str] = []

        class _MemHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)
                formatted.append(self.format(record))

        tg_logger = logging.getLogger("xiaoshuo_bot.telegram_bot")
        handler = _MemHandler()
        tg_logger.addHandler(handler)
        try:
            def _boom() -> int:
                stop_ev.set()
                raise RuntimeError(f"unexpected failure at https://api.telegram.org/bot{self.BOT_TOKEN}/getUpdates")

            with mock.patch.object(bot, "poll_once", side_effect=_boom):
                bot.run_forever(stop_event=stop_ev, error_backoff_sec=0.01)
        finally:
            tg_logger.removeHandler(handler)

        self.assertGreaterEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.ERROR)
        self.assertIsNone(records[0].exc_info)
        for line in formatted:
            self.assertNotIn(self.BOT_TOKEN, line)
            self.assertIn("[REDACTED]", line)


if __name__ == "__main__":
    unittest.main()
