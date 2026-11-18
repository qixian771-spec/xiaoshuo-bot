from __future__ import annotations

from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from xiaoshuo_bot.companion import (
    AccountBookMismatchError,
    AccountProfileLock,
    BrowserLaunchConfig,
    CaptchaAuthPauseError,
    CompanionAuthError,
    CompanionConfigError,
    CompanionHttpClient,
    EditorWordCountMismatchError,
    FontDecodeResidualError,
    MetricsSettlementWindowError,
    OutboxStore,
    ProfileLockError,
    TomatoBrowserWorker,
    Woff2FontDecoder,
    resolve_account_user_data_dir,
)
from xiaoshuo_bot.contracts import (
    API_COMPANION_CAPTCHA,
    API_COMPANION_LEASE,
    API_COMPANION_METRICS,
    API_COMPANION_POLL,
    API_COMPANION_RECEIPT,
    API_COMPANION_RESUME,
    API_HEALTH,
    CompanionLeaseRequest,
    CompanionLeaseResponse,
    CompanionMetricsRequest,
    CompanionReceiptRequest,
    CompanionCaptchaReport,
    CompanionTaskType,
    StockpileStatus,
    verify_hmac_headers,
)
from xiaoshuo_bot.evolution import MetricsNotSettledError
from xiaoshuo_bot.models import SHANGHAI_TZ


REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_FONT_PATH = REPO_ROOT / "src" / "xiaoshuo_bot" / "fonts" / "SmileySans-Oblique.ttf"
COMPANION_PKG_DIR = REPO_ROOT / "src" / "xiaoshuo_bot" / "companion"


def _build_obfuscated_woff2_bytes(
    reference_font_path: Path,
    char_to_pua: Dict[str, int],
    *,
    extra_unmapped_pua: Optional[int] = None,
    coord_shift: Tuple[float, float] = (0.0, 0.0),
    coord_scale: float = 1.0,
) -> bytes:
    """Build a real WOFF2 binary font cloning real outlines from SmileySans into PUA codepoints."""
    ref = TTFont(str(reference_font_path))
    try:
        ref_cmap = ref.getBestCmap()
        ref_glyph_set = ref.getGlyphSet()
        ref_hmtx = ref["hmtx"].metrics
        upm = int(round(int(ref["head"].unitsPerEm) * coord_scale))
        ascent = int(round(int(ref["hhea"].ascent) * coord_scale))
        descent = int(round(int(ref["hhea"].descent) * coord_scale))

        glyph_names: List[str] = [".notdef"]
        glyphs: Dict[str, Any] = {}
        metrics: Dict[str, Tuple[int, int]] = {}
        cmap: Dict[int, str] = {}

        notdef_pen = TTGlyphPen(None)
        notdef_pen.moveTo((50, 0))
        notdef_pen.lineTo((50, 700))
        notdef_pen.lineTo((450, 700))
        notdef_pen.lineTo((450, 0))
        notdef_pen.closePath()
        glyphs[".notdef"] = notdef_pen.glyph()
        metrics[".notdef"] = (500, 50)

        dx, dy = coord_shift
        for idx, (ch, pua_cp) in enumerate(char_to_pua.items()):
            src_gname = ref_cmap[ord(ch)]
            rec = RecordingPen()
            ref_glyph_set[src_gname].draw(rec)
            tt_pen = TTGlyphPen(None)
            for op, pts in rec.value:
                shifted_pts = tuple(
                    (
                        (round((pt[0] + dx) * coord_scale), round((pt[1] + dy) * coord_scale))
                        if pt is not None
                        else None
                    )
                    for pt in pts
                )
                getattr(tt_pen, op)(*shifted_pts)
            new_gname = f"pua_{pua_cp:04X}_{idx}"
            glyph_names.append(new_gname)
            glyphs[new_gname] = tt_pen.glyph()
            adv, lsb = ref_hmtx[src_gname]
            metrics[new_gname] = (int(round(adv * coord_scale)), int(round((lsb + dx) * coord_scale)))
            cmap[int(pua_cp)] = new_gname

        if extra_unmapped_pua is not None:
            tri_pen = TTGlyphPen(None)
            tri_pen.moveTo((100, 100))
            tri_pen.lineTo((350, 750))
            tri_pen.lineTo((600, 100))
            tri_pen.closePath()
            unmapped_name = f"pua_{extra_unmapped_pua:04X}_unmapped"
            glyph_names.append(unmapped_name)
            glyphs[unmapped_name] = tri_pen.glyph()
            metrics[unmapped_name] = (700, 100)
            cmap[int(extra_unmapped_pua)] = unmapped_name

        fb = FontBuilder(upm, isTTF=True)
        fb.setupGlyphOrder(glyph_names)
        fb.setupCharacterMap(cmap)
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics(metrics)
        fb.setupHorizontalHeader(ascent=ascent, descent=descent)
        fb.setupNameTable({"familyName": "TomatoObfuscated", "styleName": "Regular"})
        fb.setupOS2()
        fb.setupPost()

        out_buf = io.BytesIO()
        fb.font.flavor = "woff2"
        fb.font.save(out_buf)
        return out_buf.getvalue()
    finally:
        ref.close()


class TestWoff2FontDecoder(unittest.TestCase):
    def test_decode_real_woff2_pua_and_cache(self) -> None:
        char_to_pua = {
            "0": 0xE3E8,
            "1": 0xE3E9,
            "2": 0xE3EA,
            "3": 0xE3EB,
            "4": 0xE3EC,
            "5": 0xE3ED,
            "6": 0xE3EE,
            "7": 0xE3EF,
            "8": 0xE3F0,
            "9": 0xE3F1,
            "万": 0xE410,
            "在": 0xE411,
            "读": 0xE412,
            "榜": 0xE550,
            "首": 0xE551,
            "第": 0xE55A,
            "名": 0xE55B,
        }
        woff2_bytes = _build_obfuscated_woff2_bytes(
            BUNDLED_FONT_PATH,
            char_to_pua,
            extra_unmapped_pua=0xE4AA,
        )
        self.assertTrue(woff2_bytes.startswith(b"wOF2"))

        with tempfile.TemporaryDirectory(prefix="xsb_font_") as tmpdir:
            cache_dir = Path(tmpdir) / "font_cache"
            decoder = Woff2FontDecoder(cache_dir=cache_dir)
            registered = decoder.register_reference_font(
                BUNDLED_FONT_PATH,
                charset=list(char_to_pua.keys()),
            )
            self.assertEqual(registered, len(char_to_pua))

            obfuscated = "".join(
                chr(char_to_pua[c]) if c in char_to_pua else c for c in "第1名 在读987654万 榜首"
            )
            res1 = decoder.decode_text_detailed(obfuscated, woff2_bytes)
            self.assertFalse(res1.cache_hit)
            self.assertEqual(res1.decoded_text, "第1名 在读987654万 榜首")

            # Second decode hits SHA256 memory cache; fresh decoder hits disk cache
            res2 = decoder.decode_text_detailed(obfuscated, woff2_bytes)
            self.assertTrue(res2.cache_hit)
            self.assertEqual(res2.font_sha256, res1.font_sha256)

            decoder_restarted = Woff2FontDecoder(cache_dir=cache_dir)
            decoded_from_disk = decoder_restarted.decode_text(obfuscated, font_sha256=res1.font_sha256)
            self.assertEqual(decoded_from_disk, "第1名 在读987654万 榜首")

            # Even a single unmapped PUA character or U+FFFD MUST raise FontDecodeResidualError
            dirty_text = obfuscated + "\uE4AA"
            with self.assertRaises(FontDecodeResidualError) as ctx:
                decoder.decode_text(dirty_text, woff2_bytes)
            self.assertIn("U+E4AA", ctx.exception.residual_codepoints)

            with self.assertRaises(FontDecodeResidualError):
                decoder.decode_text("在读12\ufffd万", woff2_bytes)

            # Hotlist helper must also abort on residual PUA
            with self.assertRaises(FontDecodeResidualError):
                decoder.decode_hotlist_entries(
                    [{"rank": "\uE3E9", "title": "正常书名\uE4AA", "heat": "\uE3F1\uE410"}],
                    woff2_bytes,
                )

    def test_decode_shifted_and_rescaled_woff2_via_canonical_shape(self) -> None:
        char_to_pua = {"0": 0xE3E8, "1": 0xE3E9, "9": 0xE3F1, "万": 0xE410}
        shifted_woff2 = _build_obfuscated_woff2_bytes(
            BUNDLED_FONT_PATH,
            char_to_pua,
            coord_shift=(30.0, -20.0),
            coord_scale=2.0,
        )
        decoder = Woff2FontDecoder()
        decoder.register_reference_font(BUNDLED_FONT_PATH, charset=list(char_to_pua.keys()))
        decoded = decoder.decode_text("\uE3E9\uE3F1\uE3E8\uE410", shifted_woff2)
        self.assertEqual(decoded, "190万")


class _CloudStubState:
    def __init__(self, hmac_secret: str) -> None:
        self.hmac_secret = hmac_secret
        self.seen_nonces: Dict[str, float] = {}
        self.fail_writes_with_503 = False
        self.queued_tasks: List[Dict[str, Any]] = []
        self.received_receipts: List[Dict[str, Any]] = []
        self.received_metrics: List[Dict[str, Any]] = []
        self.received_captchas: List[Dict[str, Any]] = []
        self.received_resumes: List[Dict[str, Any]] = []


def _start_cloud_stub(state: _CloudStubState) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == API_HEALTH:
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            try:
                verify_hmac_headers(
                    state.hmac_secret,
                    "POST",
                    self.path,
                    dict(self.headers.items()),
                    body=body,
                    seen_nonces=state.seen_nonces,
                )
            except Exception as exc:
                self._send_json(401, {"error": str(exc)})
                return

            data = json.loads(body.decode("utf-8")) if body else {}
            if self.path == API_COMPANION_POLL:
                self._send_json(200, {"tasks": list(state.queued_tasks)})
                return
            if self.path == API_COMPANION_LEASE:
                task_id = data["task_id"]
                lease = CompanionLeaseResponse(
                    task_id=task_id,
                    task_type=CompanionTaskType.PUBLISH_CHAPTER.value,
                    book_id=data["book_id"],
                    book_title="我在十万大山开盲盒",
                    author_name="墨山行",
                    fanqie_book_id="fq_book_881",
                    chapter=int(data["chapter"]),
                    public_title="第3章 柜台里的铜尺",
                    body="第一段正文测试内容。\n\n第二段正文测试内容。",
                    author_note="求催更！",
                )
                self._send_json(200, lease.to_dict())
                return

            if state.fail_writes_with_503:
                self._send_json(503, {"error": "temporary_unavailable"})
                return

            if self.path == API_COMPANION_RECEIPT:
                state.received_receipts.append(data)
                self._send_json(200, {"ok": True})
                return
            if self.path == API_COMPANION_METRICS:
                state.received_metrics.append(data)
                self._send_json(200, {"ok": True})
                return
            if self.path == API_COMPANION_CAPTCHA:
                state.received_captchas.append(data)
                self._send_json(200, {"ok": True, "status": StockpileStatus.PAUSED_AUTH_CAPTCHA.value})
                return
            if self.path == API_COMPANION_RESUME:
                state.received_resumes.append(data)
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"error": "unknown_path"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, t, f"http://{host}:{port}"


class TestCompanionHttpClientAndOutbox(unittest.TestCase):
    def test_hmac_poll_lease_and_sqlite_wal_outbox_durability(self) -> None:
        secret = "test-hmac-secret-key-987654321"
        state = _CloudStubState(secret)
        state.queued_tasks = [
            {
                "task_id": "t_ch3",
                "task_type": CompanionTaskType.PUBLISH_CHAPTER.value,
                "book_id": "bk_1",
                "chapter": 3,
            }
        ]
        srv, thread, base_url = _start_cloud_stub(state)
        try:
            with tempfile.TemporaryDirectory(prefix="xsb_client_") as tmpdir:
                db_path = Path(tmpdir) / "outbox.db"
                with CompanionHttpClient(
                    base_url=base_url,
                    hmac_secret=secret,
                    client_id="companion-pc-01",
                    outbox_db_path=db_path,
                ) as client:
                    self.assertEqual(client.outbox.journal_mode(), "wal")
                    tasks = client.poll_tasks("acc_01", author_name="墨山行", book_ids=["bk_1"], timeout_sec=5)
                    self.assertEqual(len(tasks), 1)
                    lease = client.lease_task(
                        CompanionLeaseRequest(
                            task_id="t_ch3",
                            book_id="bk_1",
                            chapter=3,
                            account_id="acc_01",
                        )
                    )
                    self.assertEqual(lease.book_title, "我在十万大山开盲盒")

                    # Simulate cloud disconnect / 503 outage
                    state.fail_writes_with_503 = True
                    r1 = client.send_receipt(
                        CompanionReceiptRequest(
                            task_id="t_ch3",
                            book_id="bk_1",
                            chapter=3,
                            account_id="acc_01",
                            status=StockpileStatus.SCHEDULED.value,
                        )
                    )
                    r2 = client.send_metrics(
                        CompanionMetricsRequest(
                            book_id="bk_1",
                            account_id="acc_01",
                            settled=True,
                            settlement_status="SETTLED",
                            ch2_read_through=0.64,
                            bookshelf_rate=0.22,
                        )
                    )
                    r3 = client.send_captcha_report(
                        CompanionCaptchaReport(
                            account_id="acc_01",
                            book_id="bk_1",
                            chapter=4,
                            reason="DOM_SELECTOR:.secsdk-captcha",
                        )
                    )
                    self.assertFalse(r1.delivered)
                    self.assertFalse(r2.delivered)
                    self.assertFalse(r3.delivered)
                    self.assertEqual(client.outbox.count_pending(), 3)

                # Simulate process restart on reconnect: open new client against same outbox.db
                state.fail_writes_with_503 = False
                with CompanionHttpClient(
                    base_url=base_url,
                    hmac_secret=secret,
                    client_id="companion-pc-01",
                    outbox_db_path=db_path,
                ) as reconnected:
                    flush_res = reconnected.flush_outbox()
                    self.assertEqual(flush_res.attempted, 3)
                    self.assertEqual(flush_res.sent, 3)
                    self.assertEqual(flush_res.failed, 0)
                    self.assertEqual(flush_res.remaining_pending, 0)

                self.assertEqual(len(state.received_receipts), 1)
                self.assertEqual(len(state.received_metrics), 1)
                self.assertEqual(len(state.received_captchas), 1)

                # Bad HMAC secret raises CompanionAuthError without leaking secret
                with CompanionHttpClient(
                    base_url=base_url,
                    hmac_secret="wrong-secret",
                    client_id="companion-pc-01",
                    outbox_db_path=db_path,
                ) as bad_client:
                    with self.assertRaises(CompanionAuthError) as auth_ctx:
                        bad_client.poll_tasks("acc_01")
                    self.assertNotIn("wrong-secret", str(auth_ctx.exception))

                with self.assertRaises(CompanionConfigError):
                    CompanionHttpClient(base_url="ftp://invalid", hmac_secret="abc", outbox_db_path=db_path)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)


class _TomatoSimState:
    def __init__(self, woff2_bytes: bytes) -> None:
        self.woff2_bytes = woff2_bytes
        self.captcha_dom_events: List[Dict[str, Any]] = []
        self.published_submissions: List[Dict[str, Any]] = []
        self.star_card_submissions: List[Dict[str, Any]] = []
        self.signing_submissions: List[Dict[str, Any]] = []
        self.rotate_submissions: List[Dict[str, Any]] = []
        self.captcha_resolved = False


def _start_tomato_dom_server(sim: _TomatoSimState) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    def _base_identity(author: str = "墨山行", book_id: str = "fq_book_881", title: str = "我在十万大山开盲盒") -> str:
        return (
            f'<div id="header">'
            f'<span data-testid="author-name">{author}</span>'
            f'<span data-testid="book-id" data-book-id="{book_id}">{book_id}</span>'
            f'<span data-testid="book-title">{title}</span>'
            f'</div>'
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_html(self, code: int, html: str) -> None:
            raw = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if self.path == "/event/captcha-interaction":
                sim.captcha_dom_events.append(payload)
            elif self.path == "/api/publish":
                sim.published_submissions.append(payload)
            elif self.path == "/api/star-card":
                sim.star_card_submissions.append(payload)
            elif self.path == "/api/signing":
                sim.signing_submissions.append(payload)
            elif self.path == "/api/rotate":
                sim.rotate_submissions.append(payload)
            resp = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def do_GET(self) -> None:
            if self.path == "/hotlist.woff2":
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(sim.woff2_bytes)))
                self.end_headers()
                self.wfile.write(sim.woff2_bytes)
                return

            if self.path == "/publish_401":
                self._send_html(401, "<html><body>Unauthorized</body></html>")
                return

            if self.path == "/login_redirect_trigger":
                self.send_response(302)
                self.send_header("Location", "/passport/login")
                self.end_headers()
                return

            if self.path == "/passport/login":
                self._send_html(200, "<html><body><h1>请扫码登录</h1></body></html>")
                return

            if self.path == "/publish_captcha" and not sim.captcha_resolved:
                html = f"""<!doctype html><html><body>
                {_base_identity()}
                <div class="secsdk-captcha" data-testid="secsdk-captcha" style="width:320px;height:180px;background:#eee;border:1px solid #999;">
                  请完成安全验证
                  <button id="captcha-slider-btn" type="button">拖动滑块</button>
                </div>
                <script>
                  const logEvt = (e) => {{
                    fetch('/event/captcha-interaction', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{type: e.type, target: e.target ? e.target.id : ''}})
                    }});
                  }};
                  ['mousedown','click','keydown','input'].forEach(ev => document.addEventListener(ev, logEvt, true));
                </script>
                </body></html>"""
                self._send_html(200, html)
                return

            if self.path == "/publish_mismatch":
                self._send_html(
                    200,
                    f"<!doctype html><html><body>{_base_identity(author='别人笔名')}</body></html>",
                )
                return

            if self.path in ("/publish", "/publish_reconcile", "/publish_word_mismatch", "/publish_captcha"):
                recent_items = []
                for ch_i in range(1, 5):
                    recent_items.append(
                        f'<div class="chapter-list-item" data-chapter="{ch_i}" data-status="PUBLISHED" data-remote-chapter-id="rm-{ch_i}">第{ch_i}章 旧章节</div>'
                    )
                if self.path == "/publish_reconcile":
                    recent_items.append(
                        '<div class="chapter-list-item" data-chapter="5" data-status="SCHEDULED" data-remote-chapter-id="rm-5" data-scheduled-at="2026-11-19 08:00">第5章 已定时章节</div>'
                    )

                drop_ratio = "0.5" if self.path == "/publish_word_mismatch" else "1.0"
                html = f"""<!doctype html><html><body>
                {_base_identity()}
                <div data-testid="recent-chapters">{''.join(recent_items)}</div>
                <input id="chapter-number-input" name="chapter_no" />
                <input id="chapter-title-input" name="chapter_title" />
                <div class="ProseMirror" contenteditable="true" style="min-height:120px;border:1px solid #ccc;"></div>
                <span data-testid="editor-word-count">0</span>
                <textarea id="author-note-input" name="author_note"></textarea>
                <input id="schedule-time-input" name="scheduled_publish_at" />
                <button id="save-draft-btn" data-testid="save-draft-btn" type="button">存草稿</button>
                <button id="publish-submit-btn" data-testid="publish-submit-btn" data-remote-chapter-id="rm-new-6" type="button">定时发布</button>
                <script>
                  const pm = document.querySelector('.ProseMirror');
                  const wc = document.querySelector('[data-testid="editor-word-count"]');
                  pm.addEventListener('input', () => {{
                    const len = pm.innerText.replace(/\\s+/g, '').length;
                    wc.textContent = String(Math.floor(len * {drop_ratio}));
                  }});
                  document.getElementById('publish-submit-btn').addEventListener('click', () => {{
                    fetch('/api/publish', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{
                        chapter_no: document.getElementById('chapter-number-input').value,
                        chapter_title: document.getElementById('chapter-title-input').value,
                        body_text: pm.innerText,
                        word_count: wc.textContent,
                        author_note: document.getElementById('author-note-input').value,
                        scheduled_publish_at: document.getElementById('schedule-time-input').value,
                      }})
                    }});
                  }});
                </script>
                </body></html>"""
                self._send_html(200, html)
                return

            if self.path == "/star_cards":
                html = f"""<!doctype html><html><body>
                {_base_identity()}
                <input id="star-card-name" />
                <input id="star-card-role" />
                <input id="star-card-tagline" />
                <textarea id="star-card-personality"></textarea>
                <button id="star-card-submit" type="button">创建星耀卡</button>
                <script>
                  document.getElementById('star-card-submit').addEventListener('click', () => {{
                    fetch('/api/star-card', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{
                        name: document.getElementById('star-card-name').value,
                        role: document.getElementById('star-card-role').value,
                        tagline: document.getElementById('star-card-tagline').value,
                      }})
                    }});
                  }});
                </script>
                </body></html>"""
                self._send_html(200, html)
                return

            if self.path == "/signing_draft":
                html = f"""<!doctype html><html><body>
                {_base_identity()}
                <textarea id="signing-core-hook"></textarea>
                <textarea id="signing-protagonist"></textarea>
                <textarea id="signing-golden-finger"></textarea>
                <textarea id="signing-outline"></textarea>
                <textarea id="signing-highlights"></textarea>
                <button id="signing-submit" type="button">提交签约申请</button>
                <script>
                  document.getElementById('signing-submit').addEventListener('click', () => {{
                    fetch('/api/signing', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{
                        core_hook: document.getElementById('signing-core-hook').value,
                        protagonist: document.getElementById('signing-protagonist').value,
                      }})
                    }});
                  }});
                </script>
                </body></html>"""
                self._send_html(200, html)
                return

            if self.path == "/rotate_packaging":
                html = f"""<!doctype html><html><body>
                {_base_identity()}
                <input id="rotate-new-title" />
                <textarea id="rotate-new-blurb"></textarea>
                <button id="rotate-submit" type="button">提交换皮</button>
                <script>
                  document.getElementById('rotate-submit').addEventListener('click', () => {{
                    fetch('/api/rotate', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{
                        new_title: document.getElementById('rotate-new-title').value,
                        new_blurb: document.getElementById('rotate-new-blurb').value,
                      }})
                    }});
                  }});
                </script>
                </body></html>"""
                self._send_html(200, html)
                return

            if self.path == "/metrics_settled":
                obf_rank = "\uE55A\uE3E9\uE55B"  # 第1名
                obf_heat = "\uE411\uE412\uE3F1\uE3F0\uE410"  # 在读98万
                html = f"""<!doctype html><html><body>
                {_base_identity()}
                <span data-testid="metrics-settlement-status">SETTLED</span>
                <span data-testid="metrics-date">2026-11-17</span>
                <span data-testid="metric-ch2-read-through">64.5%</span>
                <span data-testid="metric-word100k-completion">26.0%</span>
                <span data-testid="metric-ctr">18.2%</span>
                <span data-testid="metric-bookshelf-rate">23.4%</span>
                <span data-testid="metric-impressions">12500</span>
                <span data-testid="metric-daily-revenue">¥318.50</span>
                <div data-testid="hotlist-item" data-woff2-url="/hotlist.woff2">
                  <span class="rank">{obf_rank}</span>
                  <span class="title">九幽十万界</span>
                  <span class="author">青崖客</span>
                  <span class="heat">{obf_heat}</span>
                </div>
                </body></html>"""
                self._send_html(200, html)
                return

            if self.path == "/metrics_unsettled":
                html = f"""<!doctype html><html><body>
                {_base_identity()}
                <span data-testid="metrics-settlement-status">SETTLED</span>
                <span data-testid="metric-ch2-read-through">--</span>
                <span data-testid="metric-bookshelf-rate">--</span>
                </body></html>"""
                self._send_html(200, html)
                return

            self._send_html(404, "<html><body>Not Found</body></html>")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, t, f"http://{host}:{port}"


class TestTomatoBrowserWorkerE2E(unittest.TestCase):
    def test_production_defaults_and_no_silent_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_cfg_") as tmpdir:
            cfg = BrowserLaunchConfig(profile_root=Path(tmpdir))
            self.assertEqual(cfg.channel, "chrome")
            self.assertFalse(cfg.headless)

            # Invalid explicit channel must fail loudly (never silently fallback)
            bad_cfg = BrowserLaunchConfig(
                profile_root=Path(tmpdir),
                channel="nonexistent-browser-channel-xyz",
                headless=True,
            )
            with self.assertRaises(Exception):
                with TomatoBrowserWorker("acc_bad", bad_cfg):
                    pass

    def test_per_account_root_dir_and_exclusive_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_lock_") as tmpdir:
            root = Path(tmpdir)
            dir_a = resolve_account_user_data_dir(root, "account_alpha")
            dir_b = resolve_account_user_data_dir(root, "account_beta")
            self.assertEqual(dir_a.parent, root.resolve())
            self.assertEqual(dir_b.parent, root.resolve())
            self.assertNotEqual(dir_a, dir_b)

            lock_a = AccountProfileLock(dir_a, "account_alpha")
            lock_b = AccountProfileLock(dir_b, "account_beta")
            with lock_a:
                self.assertTrue((dir_a / ".companion.lock").is_file())
                # Second lock on same account raises ProfileLockError immediately
                with self.assertRaises(ProfileLockError):
                    AccountProfileLock(dir_a, "account_alpha").acquire()
                # Independent account lock succeeds concurrently
                with lock_b:
                    self.assertTrue((dir_b / ".companion.lock").is_file())

    def test_real_playwright_publish_reconcile_mismatch_captcha_and_metrics(self) -> None:
        char_to_pua = {
            "0": 0xE3E8,
            "1": 0xE3E9,
            "8": 0xE3F0,
            "9": 0xE3F1,
            "万": 0xE410,
            "在": 0xE411,
            "读": 0xE412,
            "第": 0xE55A,
            "名": 0xE55B,
        }
        woff2_bytes = _build_obfuscated_woff2_bytes(BUNDLED_FONT_PATH, char_to_pua)
        sim = _TomatoSimState(woff2_bytes)
        srv_dom, t_dom, dom_url = _start_tomato_dom_server(sim)

        secret = "e2e-companion-hmac-secret-123456"
        cloud_state = _CloudStubState(secret)
        srv_cloud, t_cloud, cloud_url = _start_cloud_stub(cloud_state)

        try:
            with tempfile.TemporaryDirectory(prefix="xsb_pw_e2e_") as tmpdir:
                tmp_path = Path(tmpdir)
                decoder = Woff2FontDecoder(cache_dir=tmp_path / "font_cache")
                decoder.register_reference_font(BUNDLED_FONT_PATH, charset=list(char_to_pua.keys()))

                client = CompanionHttpClient(
                    base_url=cloud_url,
                    hmac_secret=secret,
                    client_id="companion-e2e",
                    outbox_db_path=tmp_path / "outbox.db",
                )
                try:
                    launch_cfg = BrowserLaunchConfig(
                        profile_root=tmp_path / "chrome_profiles",
                        channel=None,
                        headless=True,
                        review_pause_sec=0.05,
                        screenshot_dir=tmp_path / "screenshots",
                    )

                    # 1. Normal publish: strips 第6章 prefix, inserts into .ProseMirror, verifies <=5% word count, schedules in SHANGHAI_TZ
                    task_ch6 = CompanionLeaseResponse(
                        task_id="task-ch6",
                        task_type=CompanionTaskType.PUBLISH_CHAPTER.value,
                        book_id="bk_881",
                        book_title="我在十万大山开盲盒",
                        author_name="墨山行",
                        fanqie_book_id="fq_book_881",
                        chapter=6,
                        public_title="第6章：深夜响起的旧铜钟",
                        body="陈青推开木门时，屋檐下的铜铃猛地响了一声。\n\n柜台后面的账本翻到第三页，墨迹还没干透。",
                        author_note="明天继续加更，求催更支持！",
                        sha256="sha-ch6",
                        scheduled_publish_at="2026-11-19T08:30:00+08:00",
                    )

                    with TomatoBrowserWorker(
                        "acc_main",
                        launch_cfg,
                        client=client,
                        font_decoder=decoder,
                        now_provider=lambda: datetime(2026, 11, 18, 13, 5, 0, tzinfo=SHANGHAI_TZ),
                    ) as worker:
                        outcome = worker.execute_task(task_ch6, target_url=f"{dom_url}/publish")
                        self.assertEqual(outcome.status, StockpileStatus.SCHEDULED.value)
                        self.assertFalse(outcome.idempotent_skip)
                        self.assertEqual(len(sim.published_submissions), 1)
                        sub = sim.published_submissions[0]
                        self.assertEqual(sub["chapter_no"], "6")
                        self.assertEqual(sub["chapter_title"], "深夜响起的旧铜钟")
                        self.assertEqual(sub["scheduled_publish_at"], "2026-11-19 08:30")
                        self.assertEqual(sub["author_note"], "明天继续加更，求催更支持！")
                        self.assertNotIn("作者有话说", sub["body_text"])

                        # 2. Remote pre-write reconciliation: chapter 5 already scheduled -> idempotent skip!
                        task_ch5 = CompanionLeaseResponse(
                            task_id="task-ch5",
                            task_type=CompanionTaskType.PUBLISH_CHAPTER.value,
                            book_id="bk_881",
                            book_title="我在十万大山开盲盒",
                            author_name="墨山行",
                            fanqie_book_id="fq_book_881",
                            chapter=5,
                            public_title="已定时章节",
                            body="不会覆盖已有章节的内容。",
                            sha256="sha-ch5",
                        )
                        outcome_ch5 = worker.execute_task(task_ch5, target_url=f"{dom_url}/publish_reconcile")
                        self.assertTrue(outcome_ch5.idempotent_skip)
                        self.assertEqual(outcome_ch5.status, StockpileStatus.SCHEDULED.value)
                        self.assertEqual(len(sim.published_submissions), 1)

                        # 3. Gate 1 mismatch: pen-name differs -> raises AccountBookMismatchError & emits FAILED_HARD_MISMATCH
                        with self.assertRaises(AccountBookMismatchError):
                            worker.execute_task(task_ch6, target_url=f"{dom_url}/publish_mismatch")

                        # 4. Gate 3 word count mismatch (> 5% deviation) -> raises EditorWordCountMismatchError
                        with self.assertRaises(EditorWordCountMismatchError):
                            worker.publish_chapter(task_ch6, target_url=f"{dom_url}/publish_word_mismatch")

                        # 5. Captcha 0-click immediate halt + PNG screenshot + PAUSED_AUTH_CAPTCHA report
                        sim.captcha_dom_events.clear()
                        captcha_outcome = worker.execute_task(task_ch6, target_url=f"{dom_url}/publish_captcha")
                        self.assertEqual(captcha_outcome.status, StockpileStatus.PAUSED_AUTH_CAPTCHA.value)
                        self.assertEqual(sim.captcha_dom_events, [])  # STRICT 0 mouse/keyboard events!
                        self.assertIsNotNone(captcha_outcome.screenshot_path)
                        self.assertTrue(captcha_outcome.screenshot_path.is_file())
                        self.assertTrue(captcha_outcome.screenshot_path.read_bytes().startswith(b"\x89PNG"))
                        self.assertIsNotNone(captcha_outcome.captcha_report)
                        self.assertGreater(len(captcha_outcome.captcha_report.screenshot_base64), 100)
                        self.assertGreaterEqual(len(cloud_state.received_captchas), 1)

                        # 5b. Captcha wait_for_resume -> operator resolves -> notify_resume -> publish succeeds
                        def _operator_resolves(_report: CompanionCaptchaReport) -> bool:
                            sim.captcha_resolved = True
                            return True

                        resumed_outcome = worker.execute_task(
                            task_ch6,
                            target_url=f"{dom_url}/publish_captcha",
                            wait_for_resume=_operator_resolves,
                        )
                        self.assertEqual(resumed_outcome.status, StockpileStatus.SCHEDULED.value)
                        self.assertEqual(len(cloud_state.received_resumes), 1)

                        # 6. Layer 2 HTTP 401 & Layer 1 login redirect sentinels
                        with self.assertRaises(CaptchaAuthPauseError):
                            worker.publish_chapter(task_ch6, target_url=f"{dom_url}/publish_401")
                        with self.assertRaises(CaptchaAuthPauseError):
                            worker.publish_chapter(task_ch6, target_url=f"{dom_url}/login_redirect_trigger")

                        # 7. Star cards, 20k signing draft, and packaging rotation
                        star_outcome = worker.execute_task(
                            CompanionLeaseResponse(
                                task_id="task-star",
                                task_type=CompanionTaskType.CREATE_STAR_CARDS.value,
                                book_id="bk_881",
                                book_title="我在十万大山开盲盒",
                                author_name="墨山行",
                                fanqie_book_id="fq_book_881",
                                chapter=0,
                                public_title="",
                                body="",
                                metadata={
                                    "star_cards": [
                                        {"name": "陈青", "role": "主角", "tagline": "瞎猜必中", "personality": "冷静果断"}
                                    ]
                                },
                            ),
                            target_url=f"{dom_url}/star_cards",
                        )
                        self.assertEqual(star_outcome.status, StockpileStatus.PUBLISHED.value)
                        self.assertEqual(len(sim.star_card_submissions), 1)

                        sign_outcome = worker.execute_task(
                            CompanionLeaseResponse(
                                task_id="task-sign",
                                task_type=CompanionTaskType.SUBMIT_SIGNING_DRAFT.value,
                                book_id="bk_881",
                                book_title="我在十万大山开盲盒",
                                author_name="墨山行",
                                fanqie_book_id="fq_book_881",
                                chapter=0,
                                public_title="",
                                body="",
                                metadata={
                                    "signing_application": {
                                        "core_hook": "十万大山开盲盒反杀",
                                        "protagonist": "陈青",
                                        "golden_finger": "每日盲盒系统",
                                        "outline": "百章升级路线",
                                        "highlights": "第5章大爆发",
                                    }
                                },
                            ),
                            target_url=f"{dom_url}/signing_draft",
                        )
                        self.assertEqual(sign_outcome.status, StockpileStatus.DRAFT_SAVED.value)
                        self.assertEqual(len(sim.signing_submissions), 1)

                        rot_outcome = worker.execute_task(
                            CompanionLeaseResponse(
                                task_id="task-rot",
                                task_type=CompanionTaskType.ROTATE_PACKAGING.value,
                                book_id="bk_881",
                                book_title="我在十万大山开盲盒",
                                author_name="墨山行",
                                fanqie_book_id="fq_book_881",
                                chapter=0,
                                public_title="",
                                body="",
                                metadata={"new_title": "盲盒开出仙帝剑", "new_blurb": "全新简介内容" * 8},
                            ),
                            target_url=f"{dom_url}/rotate_packaging",
                        )
                        self.assertEqual(rot_outcome.status, StockpileStatus.PUBLISHED.value)
                        self.assertEqual(sim.rotate_submissions[0]["new_title"], "盲盒开出仙帝剑")

                        # 8. Metrics collection after 12:30 CST + WOFF2 hotlist decoding
                        metrics_task = CompanionLeaseResponse(
                            task_id="task-metrics",
                            task_type=CompanionTaskType.COLLECT_METRICS.value,
                            book_id="bk_881",
                            book_title="我在十万大山开盲盒",
                            author_name="墨山行",
                            fanqie_book_id="fq_book_881",
                            chapter=0,
                            public_title="",
                            body="",
                        )
                        metrics_outcome = worker.execute_task(
                            metrics_task,
                            target_url=f"{dom_url}/metrics_settled",
                        )
                        self.assertIsNotNone(metrics_outcome.metrics)
                        self.assertAlmostEqual(metrics_outcome.metrics.ch2_read_through or 0.0, 0.645, places=4)
                        self.assertAlmostEqual(metrics_outcome.metrics.bookshelf_rate or 0.0, 0.234, places=4)
                        self.assertEqual(metrics_outcome.metrics.decoded_hotlist[0]["rank"], "第1名")
                        self.assertEqual(metrics_outcome.metrics.decoded_hotlist[0]["heat"], "在读98万")

                        # 9. "--" unsettled metrics raises MetricsNotSettledError (never coerces to 0)
                        with self.assertRaises(MetricsNotSettledError):
                            worker.collect_metrics(metrics_task, target_url=f"{dom_url}/metrics_unsettled")

                    # 10. Before 12:30 CST refuses T-1 metrics collection immediately
                    with TomatoBrowserWorker(
                        "acc_early",
                        launch_cfg,
                        now_provider=lambda: datetime(2026, 11, 18, 12, 29, 0, tzinfo=SHANGHAI_TZ),
                    ) as early_worker:
                        with self.assertRaises(MetricsSettlementWindowError):
                            early_worker.collect_metrics(
                                metrics_task,
                                target_url=f"{dom_url}/metrics_settled",
                            )
                finally:
                    client.close()
        finally:
            srv_dom.shutdown()
            srv_dom.server_close()
            srv_cloud.shutdown()
            srv_cloud.server_close()
            t_dom.join(timeout=2)
            t_cloud.join(timeout=2)


class TestCompanionStaticSecurityScan(unittest.TestCase):
    def test_zero_captcha_bypass_stealth_unlink_or_zoneinfo(self) -> None:
        forbidden_code_patterns = [
            re.compile(r"\.unlink\s*\("),
            re.compile(r"Remove-Item", re.IGNORECASE),
            re.compile(r"\bZoneInfo\b"),
            re.compile(r"^\s*except\s*:", re.MULTILINE),
            re.compile(r"stealth\.min\.js", re.IGNORECASE),
            re.compile(r"navigator\.webdriver", re.IGNORECASE),
            re.compile(r"ddddocr|rapidocr|pytesseract|2captcha|anticaptcha", re.IGNORECASE),
            re.compile(r"solve_captcha|click_captcha|drag_slider|bypass_captcha", re.IGNORECASE),
        ]
        py_files = sorted(COMPANION_PKG_DIR.glob("*.py"))
        self.assertGreaterEqual(len(py_files), 4)
        for py_file in py_files:
            content = py_file.read_text(encoding="utf-8")
            for pat in forbidden_code_patterns:
                self.assertIsNone(
                    pat.search(content),
                    f"Forbidden pattern {pat.pattern!r} found in {py_file}",
                )


if __name__ == "__main__":
    unittest.main()
