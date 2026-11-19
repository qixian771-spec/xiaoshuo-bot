"""T2: public studio (游客演播厅), admin new-book wizard, production KPI.

Owner requirements under test (Web UI grill-me): visitors read-only (Q10=C), fully transparent
settings + plan (Q6=B) but ONLY gate-passed chapter text (Q8=A), stage-completion events only
(Q2=C); admin step-by-step wizard (Q7=A); KPI = 写稿耗时 vs 重写率 (Q9=A).

The LLM is replaced by an in-test double so the HTTP/validation logic can be exercised offline;
real-model acceptance is task card T3 (docs/delivery/2026-11-18-需求对齐与抢修.md).
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple
import unittest
import urllib.error
import urllib.request

from xiaoshuo_bot.contracts import (
    SSE_CHAPTER_ATTEMPT,
    SSE_CHAPTER_STOCKPILED,
    SSE_GATE_FAILED,
    AutoCreateBookRequest,
    BookRunStatus,
    SSEEventPayload,
)
from xiaoshuo_bot.genres import list_genres
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.server import create_server
from xiaoshuo_bot.server.public_view import public_event
from xiaoshuo_bot.server.stockpile_db import StockpileDB
from xiaoshuo_bot.server.wizard import NewBookWizard, WizardError

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROJECT = REPO_ROOT / "projects" / "baiwu_dukou"
ADMIN_TOKEN = "t2-admin-token-0123456789abcdef"
SECRET_ACCOUNT = "acct_secret_should_never_leak"
SECRET_FANQIE_ID = "7412589630000000001"
SECRET_NOTE = "作者私密备注不应公开"
GENRE_ID = list_genres()[0].genre_id


def _options_for(stage: str) -> List[Dict[str, Any]]:
    if stage == "wizard_direction":
        return [
            {"label": f"方向{i}", "logline": f"梗概{i}", "genre_id": GENRE_ID, "selling_point": "卖点", "tone": "爽快"}
            for i in range(3)
        ]
    if stage == "wizard_protagonist":
        return [
            {"label": f"主角{i}", "persona": "处境", "golden_finger": "能力", "cost": "代价", "arc": "成长"}
            for i in range(3)
        ]
    if stage == "wizard_style":
        return [{"label": f"笔风{i}", "description": "短句快节奏", "sample": "门开了。风灌进来。"} for i in range(3)]
    if stage == "wizard_scale":
        return [
            {
                "label": f"方案{i}",
                "title": f"雾港夜行{i}",
                "author_level": "lv0_1",
                "target_chapters": 80,
                "blurb": "简介",
            }
            for i in range(3)
        ]
    raise AssertionError(f"unexpected stage {stage}")


class FakeWizardLLM(LLMProvider):
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.prompts: List[str] = []
        self.broken_stage: Optional[str] = None
        self.broken_times: Optional[int] = None  # None = always broken
        self.long_title_once = False

    def generate(self, request: LLMRequest) -> LLMResponse:
        stage = request.metadata["stage"]
        self.calls.append(stage)
        self.prompts.append(request.user_prompt)
        options = _options_for(stage)
        if stage == self.broken_stage and (self.broken_times is None or self.calls.count(stage) <= self.broken_times):
            options = options[:2]
        if stage == "wizard_scale" and self.long_title_once:
            self.long_title_once = False
            options[0] = dict(options[0], title="每日头条：我的外卖配送清单有BUG")
        return LLMResponse(
            request_id=request.request_id,
            content=json.dumps({"options": options}, ensure_ascii=False),
            model="fake",
            provider="fake",
        )


def _http(url: str, *, method: str = "GET", payload: Optional[Dict[str, Any]] = None,
          token: Optional[str] = None) -> Tuple[int, Dict[str, str], bytes]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


class _ServerCase(unittest.TestCase):
    provider: Optional[FakeWizardLLM] = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="xsb_t2_"))
        self.projects_root = self.tmp / "projects"
        self.project_dir = self.projects_root / "baiwu_dukou"
        shutil.copytree(SOURCE_PROJECT, self.project_dir, ignore=shutil.ignore_patterns("output"))
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            db_path=self.tmp / "matrix.db",
            projects_root=self.projects_root,
            admin_token=ADMIN_TOKEN,
            companion_secret="t2-companion-secret-0123456789",
            provider=self.provider,
        )
        db = self.server.db
        db.register_book(
            book_id="baiwu_dukou",
            project_dir=self.project_dir,
            title="白雾渡口",
            account_id=SECRET_ACCOUNT,
            fanqie_book_id=SECRET_FANQIE_ID,
            total_chapters=5,
        )
        db.add_stockpiled_chapter("baiwu_dukou", 1, "红漆印", "第一章正文。" * 50, author_note=SECRET_NOTE)
        db.add_stockpiled_chapter("baiwu_dukou", 2, "断电", "第二章正文。" * 50, author_note=SECRET_NOTE)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown_and_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp)


class TestPublicStudio(_ServerCase):
    def test_public_api_open_admin_api_locked(self) -> None:
        code, _, body = _http(self.base + "/api/public/books")
        self.assertEqual(code, 200)
        books = json.loads(body)["books"]
        self.assertEqual([b["book_id"] for b in books], ["baiwu_dukou"])
        self.assertEqual(books[0]["written_chapters"], 2)
        code_admin, _, _ = _http(self.base + "/api/books")
        self.assertEqual(code_admin, 401)

    def test_public_payloads_never_leak_private_fields(self) -> None:
        texts = []
        for path in ("/api/public/books", "/api/public/books/baiwu_dukou", "/api/public/books/baiwu_dukou/chapters/1"):
            code, _, body = _http(self.base + path)
            self.assertEqual(code, 200, path)
            texts.append(body.decode("utf-8"))
        joined = "\n".join(texts)
        for forbidden in (SECRET_ACCOUNT, SECRET_FANQIE_ID, SECRET_NOTE, str(self.tmp), "project_dir",
                          "account_id", "fanqie_book_id", "author_note"):
            self.assertNotIn(forbidden, joined)

    def test_book_view_is_transparent_settings_and_current_plan(self) -> None:
        _, _, body = _http(self.base + "/api/public/books/baiwu_dukou")
        book = json.loads(body)
        self.assertGreater(len(book["characters"]), 0)
        self.assertIn("personality", book["characters"][0])
        self.assertEqual([c["chapter"] for c in book["chapters"]], [1, 2])
        self.assertEqual(book["current_plan"]["chapter"], 3)
        self.assertGreater(len(book["current_plan"]["events"]), 0)

    def test_only_gate_passed_chapters_are_readable(self) -> None:
        code, _, body = _http(self.base + "/api/public/books/baiwu_dukou/chapters/1")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["body"].startswith("第一章正文"))
        code_missing, _, _ = _http(self.base + "/api/public/books/baiwu_dukou/chapters/3")
        self.assertEqual(code_missing, 404)

    def test_public_api_is_read_only(self) -> None:
        code, _, _ = _http(self.base + "/api/public/books", method="POST", payload={})
        self.assertEqual(code, 405)

    def test_static_page_routing(self) -> None:
        for path, marker in (("/", "创作演播厅"), ("/admin/", "书目总览"), ("/admin/wizard", "新书向导")):
            code, headers, body = _http(self.base + path)
            self.assertEqual(code, 200, path)
            self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
            self.assertIn(marker, body.decode("utf-8"), path)

    def test_public_event_filter_hides_rewrites(self) -> None:
        titles = {"baiwu_dukou": "白雾渡口"}

        def evt(name: str) -> SSEEventPayload:
            return SSEEventPayload(event=name, book_id="baiwu_dukou", chapter=3, message="raw",
                                   data={"secret": SECRET_ACCOUNT}, timestamp="2026-11-18T00:00:00+08:00")

        self.assertIsNone(public_event(evt(SSE_GATE_FAILED), titles))
        self.assertIsNone(public_event(evt(SSE_CHAPTER_ATTEMPT), titles))
        pub = public_event(evt(SSE_CHAPTER_STOCKPILED), titles)
        self.assertEqual(pub["message"], "《白雾渡口》第 3 章成稿")
        self.assertNotIn(SECRET_ACCOUNT, json.dumps(pub, ensure_ascii=False))

    def test_p1_2_public_status_text_distinguishes_manual_pause_from_quota_exhausted(self) -> None:
        self.server.db.update_book_status(
            "baiwu_dukou", BookRunStatus.PAUSED_MANUAL, paused_reason="控制台手动暂停"
        )
        code, _, body = _http(self.base + "/api/public/books")
        self.assertEqual(code, 200)
        b_manual = json.loads(body)["books"][0]
        self.assertEqual(b_manual["status"], "PAUSED_MANUAL")
        self.assertEqual(b_manual["status_text"], "手动暂停")

        self.server.db.update_book_status(
            "baiwu_dukou", BookRunStatus.PAUSED_QUOTA_EXHAUSTED, paused_reason="额度耗尽"
        )
        code2, _, body2 = _http(self.base + "/api/public/books")
        self.assertEqual(code2, 200)
        b_quota = json.loads(body2)["books"][0]
        self.assertEqual(b_quota["status"], "PAUSED_QUOTA_EXHAUSTED")
        self.assertEqual(b_quota["status_text"], "额度用完，暂停中")

    def test_p2_1_public_book_ordering_active_first_newest_updated_first(self) -> None:
        db = self.server.db
        for bid, title, status, ts in (
            ("bk_active_old", "旧在写书", BookRunStatus.ACTIVE, "2026-11-18T08:00:00+08:00"),
            ("bk_active_new", "新在写书", BookRunStatus.ACTIVE, "2026-11-18T12:00:00+08:00"),
            ("bk_paused_new", "新暂停书", BookRunStatus.PAUSED_MANUAL, "2026-11-18T15:00:00+08:00"),
            ("bk_done_old", "旧完本书", BookRunStatus.COMPLETED, "2026-11-18T07:00:00+08:00"),
        ):
            db.register_book(book_id=bid, project_dir=self.project_dir, title=title, status=status)
            with db._lock:
                db._conn.execute("UPDATE books SET updated_at = ? WHERE book_id = ?", (ts, bid))
        with db._lock:
            db._conn.execute(
                "UPDATE books SET updated_at = ? WHERE book_id = 'baiwu_dukou'",
                ("2026-11-18T10:00:00+08:00",),
            )

        code, _, body = _http(self.base + "/api/public/books")
        self.assertEqual(code, 200)
        ordered_ids = [b["book_id"] for b in json.loads(body)["books"]]
        self.assertEqual(
            ordered_ids,
            ["bk_active_new", "baiwu_dukou", "bk_active_old", "bk_paused_new", "bk_done_old"],
        )

    def test_p2_1_public_studio_renders_current_plan_hook_contract(self) -> None:
        import yaml

        outline_path = self.project_dir / "outline.yaml"
        outline_raw = yaml.safe_load(outline_path.read_text(encoding="utf-8"))
        for sc in outline_raw["scenes"]:
            if sc.get("chapter") == 3:
                sc["cliffhanger_hook"] = "配电箱骤然断电，透气窗外响起红漆胶鞋踩水声"
        outline_path.write_text(
            yaml.safe_dump(outline_raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        code, _, body = _http(self.base + "/api/public/books/baiwu_dukou")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIsNotNone(data["current_plan"])
        self.assertEqual(
            data["current_plan"].get("hook"),
            "配电箱骤然断电，透气窗外响起红漆胶鞋踩水声",
        )

        static_dir = REPO_ROOT / "src" / "xiaoshuo_bot" / "server" / "static"
        html_txt = (static_dir / "public.html").read_text(encoding="utf-8")
        js_txt = (static_dir / "public.js").read_text(encoding="utf-8")
        css_txt = (static_dir / "public.css").read_text(encoding="utf-8")
        self.assertIn('id="plan-hook"', html_txt)
        self.assertIn('$("plan-hook")', js_txt)
        self.assertIn("plan.hook", js_txt)
        self.assertIn("章末钩子：", js_txt)
        self.assertIn(".plan-hook", css_txt)

    def test_p2_2_http_head_supported_for_static_health_and_public_routes_with_keepalive(self) -> None:
        import http.client

        host, port = self.server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            for path, expected_ct in (
                ("/", "text/html; charset=utf-8"),
                ("/admin/", "text/html; charset=utf-8"),
                ("/admin/wizard", "text/html; charset=utf-8"),
                ("/public.css", "text/css; charset=utf-8"),
                ("/api/health", "application/json; charset=utf-8"),
                ("/api/public/books", "application/json; charset=utf-8"),
                ("/api/public/books/baiwu_dukou", "application/json; charset=utf-8"),
                ("/api/public/events/stream", "text/event-stream; charset=utf-8"),
            ):
                conn.request("HEAD", path)
                resp = conn.getresponse()
                body = resp.read()
                self.assertEqual(resp.status, 200, f"HEAD {path} returned {resp.status}")
                self.assertEqual(resp.getheader("Content-Type"), expected_ct, path)
                self.assertEqual(body, b"", f"HEAD {path} must return empty body")
                if path != "/api/public/events/stream":
                    cl = int(resp.getheader("Content-Length") or "0")
                    self.assertGreater(cl, 0, f"HEAD {path} should report positive Content-Length")

            # Missing static route returns 404 (not 501), empty body
            conn.request("HEAD", "/nonexistent_asset_404.html")
            resp_404 = conn.getresponse()
            self.assertEqual(resp_404.status, 404)
            self.assertEqual(resp_404.read(), b"")

            # Admin route without token still enforces 401 (never bypasses auth)
            conn.request("HEAD", "/api/books")
            resp_401 = conn.getresponse()
            self.assertEqual(resp_401.status, 401)
            self.assertEqual(resp_401.read(), b"")

            # Follow-up GET on the same HTTP/1.1 keep-alive connection succeeds cleanly
            conn.request("GET", "/api/health")
            resp_get = conn.getresponse()
            self.assertEqual(resp_get.status, 200)
            health = json.loads(resp_get.read().decode("utf-8"))
            self.assertEqual(health["status"], "ok")
        finally:
            conn.close()


class TestWizardWithoutProvider(_ServerCase):
    provider = None

    def test_wizard_reports_unconfigured_llm(self) -> None:
        code, _, body = _http(self.base + "/api/wizard/sessions", method="POST",
                              payload={"idea": "外卖小哥捡到能看见明天新闻的手机"}, token=ADMIN_TOKEN)
        self.assertEqual(code, 503)
        self.assertEqual(json.loads(body)["error_code"], "SERVICE_NOT_CONFIGURED")


class TestWizardHttpFlow(_ServerCase):
    def setUp(self) -> None:
        self.provider = FakeWizardLLM()
        super().setUp()

    def _post(self, path: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        code, _, body = _http(self.base + path, method="POST", payload=payload, token=ADMIN_TOKEN)
        return code, json.loads(body)

    def test_wizard_requires_admin(self) -> None:
        code, _, _ = _http(self.base + "/api/wizard/sessions", method="POST", payload={"idea": "题材想法"})
        self.assertEqual(code, 401)

    def test_four_steps_with_reroll_then_ready(self) -> None:
        code, view = self._post("/api/wizard/sessions", {"idea": "外卖小哥捡到能看见明天新闻的手机"})
        self.assertEqual(code, 201)
        sid = view["session_id"]
        self.assertEqual(view["step"]["key"], "direction")
        self.assertEqual(len(view["options"]), 3)

        code, view = self._post(f"/api/wizard/sessions/{sid}/answer", {"reroll": True})
        self.assertEqual(code, 200)
        self.assertEqual(view["step"]["key"], "direction")

        code, err = self._post(f"/api/wizard/sessions/{sid}/answer", {"custom": "我自己的方向"})
        self.assertEqual(code, 400)
        self.assertEqual(err["error_code"], "WIZARD_ERROR")

        for expected_next in ("protagonist", "style", "scale"):
            code, view = self._post(f"/api/wizard/sessions/{sid}/answer", {"choice": 0})
            self.assertEqual(code, 200)
            self.assertEqual(view["step"]["key"], expected_next)

        code, err = self._post(f"/api/wizard/sessions/{sid}/answer", {"custom": "自己起书名"})
        self.assertEqual(code, 400)

        code, view = self._post(f"/api/wizard/sessions/{sid}/answer", {"choice": 2})
        self.assertEqual(code, 200)
        self.assertEqual(view["status"], "READY_TO_CONFIRM")
        self.assertEqual([c["step"] for c in view["choices"]], ["direction", "protagonist", "style", "scale"])
        self.assertEqual(view["choices"][3]["option"]["title"], "雾港夜行2")
        self.assertEqual(
            self.provider.calls,
            ["wizard_direction", "wizard_direction", "wizard_protagonist", "wizard_style", "wizard_scale"],
        )

        code, view = self._post(f"/api/wizard/sessions/{sid}/answer", {"choice": 0})
        self.assertEqual(code, 400)

    def test_invalid_llm_proposal_is_reported_not_patched(self) -> None:
        self.provider.broken_stage = "wizard_direction"
        code, err = self._post("/api/wizard/sessions", {"idea": "外卖小哥捡到能看见明天新闻的手机"})
        self.assertEqual(code, 400)
        self.assertIn("failed validation 3 times", err["message"])
        self.assertIn("exactly 3 options", err["message"])
        self.assertEqual(self.provider.calls, ["wizard_direction"] * 3)

    def test_invalid_proposal_is_fed_back_and_fixed(self) -> None:
        self.provider.broken_stage = "wizard_direction"
        self.provider.broken_times = 1
        code, view = self._post("/api/wizard/sessions", {"idea": "外卖小哥捡到能看见明天新闻的手机"})
        self.assertEqual(code, 201)
        self.assertEqual(len(view["options"]), 3)
        self.assertEqual(self.provider.calls, ["wizard_direction", "wizard_direction"])
        self.assertNotIn("上一次输出不合格", self.provider.prompts[0])
        self.assertIn("上一次输出不合格", self.provider.prompts[1])
        self.assertIn("exactly 3 options", self.provider.prompts[1])

    def test_overlong_title_is_rejected_then_corrected(self) -> None:
        code, view = self._post("/api/wizard/sessions", {"idea": "外卖小哥捡到能看见明天新闻的手机"})
        sid = view["session_id"]
        for _ in range(2):
            self._post(f"/api/wizard/sessions/{sid}/answer", {"choice": 0})
        self.provider.long_title_once = True
        code, view = self._post(f"/api/wizard/sessions/{sid}/answer", {"choice": 0})
        self.assertEqual(code, 200)
        self.assertEqual(view["step"]["key"], "scale")
        self.assertTrue(all(len(o["title"]) <= 15 for o in view["options"]))
        self.assertIn("must be 1~15 chars", self.provider.prompts[-1])


class TestWizardConfirm(unittest.TestCase):
    def test_confirm_creates_book_with_chosen_brief(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="xsb_t2c_"))
        try:
            db = StockpileDB(tmp / "m.db")
            created: List[AutoCreateBookRequest] = []

            class _Summary:
                book_id = "book_created"

            def create(req: AutoCreateBookRequest) -> _Summary:
                created.append(req)
                return _Summary()

            provider = FakeWizardLLM()
            wiz = NewBookWizard(db=db, provider_fn=lambda: provider, create_book_fn=create)
            view = wiz.start("外卖小哥捡到能看见明天新闻的手机")
            sid = view["session_id"]
            with self.assertRaises(WizardError):
                wiz.confirm(sid)
            wiz.answer(sid, {"choice": 1})
            wiz.answer(sid, {"custom": "主角是个话很少的退伍兵"})
            wiz.answer(sid, {"choice": 0})
            wiz.answer(sid, {"choice": 0})
            view = wiz.confirm(sid, pen_name="雾山")
            self.assertEqual(view["status"], "CREATED")
            self.assertEqual(view["book_id"], "book_created")
            self.assertEqual(len(created), 1)
            req = created[0]
            self.assertEqual(req.genre_id, GENRE_ID)
            self.assertEqual(req.author_level, "lv0_1")
            self.assertEqual(req.target_chapters, 80)
            self.assertEqual(req.pen_name, "雾山")
            self.assertIn("书名必须为：雾港夜行0", req.creative_brief)
            self.assertIn("主角是个话很少的退伍兵", req.creative_brief)
            self.assertIn("方向1", req.creative_brief)
            with self.assertRaises(WizardError):
                wiz.confirm(sid)
            db.close()
        finally:
            shutil.rmtree(tmp)


class TestProductionKpi(unittest.TestCase):
    def test_kpi_duration_and_rewrite_rate(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="xsb_t2k_"))
        try:
            db = StockpileDB(tmp / "m.db")
            self.assertEqual(db.get_production_kpi()["chapters"], 0)
            db.record_production_stat("b1", 1, duration_sec=60, attempts_used=1, rewrites_triggered=0, char_count=3000)
            db.record_production_stat("b1", 2, duration_sec=120, attempts_used=3, rewrites_triggered=2, char_count=3200)
            kpi = db.get_production_kpi()
            self.assertEqual(kpi["chapters"], 2)
            self.assertEqual(kpi["avg_duration_sec"], 90.0)
            self.assertEqual(kpi["rewrite_rate"], 0.5)
            self.assertEqual(kpi["avg_attempts"], 2.0)
            self.assertEqual(kpi["total_chars"], 6200)
            self.assertEqual(db.get_production_kpi("other")["chapters"], 0)
            db.close()
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
