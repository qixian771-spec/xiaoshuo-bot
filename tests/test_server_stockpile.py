from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile

from PIL import Image
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.contracts import (
    ALL_API_ROUTES,
    API_BOOKS,
    API_BOOKS_AUTO_CREATE,
    API_BOOK_CHAPTERS,
    API_BOOK_CHAPTER_DETAIL,
    API_BOOK_CHAPTER_LOCK,
    API_BOOK_COVER,
    API_BOOK_CUT,
    API_BOOK_EXPORT,
    API_BOOK_EXTEND,
    API_BOOK_PROMOTE,
    API_BOOK_RESUME,
    API_BOOK_STATUS,
    API_COMPANION_CAPTCHA,
    API_COMPANION_LEASE,
    API_COMPANION_METRICS,
    API_COMPANION_POLL,
    API_COMPANION_RECEIPT,
    API_COMPANION_RESUME,
    API_CPA_AUTH_FILES,
    API_CPA_AUTH_FILE_DETAIL,
    API_CPA_BINDINGS,
    API_CPA_MODELS,
    API_CPA_OAUTH,
    API_CPA_USAGE,
    API_EVENTS_STREAM,
    API_GENRES,
    API_HEALTH,
    AutoCreateBookRequest,
    BookRunStatus,
    CompanionCaptchaReport,
    CompanionLeaseRequest,
    CompanionMetricsRequest,
    CompanionReceiptRequest,
    MatrixControlPort,
    SSE_BOOK_ISOLATED,
    SSE_CHAPTER_STOCKPILED,
    SSE_COMPANION_CAPTCHA,
    SSE_COMPANION_RESUMED,
    SSE_SAFETY_CUSHION_ALERT,
    StockpileStatus,
    compute_hmac_headers,
    format_api_route,
)
from xiaoshuo_bot.evolution import MotifWeightStore
from xiaoshuo_bot.llm import (
    CPAManagementClient,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    RoleEndpointConfig,
    RoleModelRouter,
)
from xiaoshuo_bot.models import ProjectValidationError, SHANGHAI_TZ
from xiaoshuo_bot.server import (
    CutConfirmationConflictError,
    DailyQuotaExceededError,
    InsecureBindError,
    MatrixScheduler,
    SequentialLeaseLockError,
    StockpileDB,
    create_server,
    validate_bind_security,
)
from xiaoshuo_bot.server.app import MatrixHTTPServer, MatrixRequestHandler
from xiaoshuo_bot.server.auth import AdminAuthManager, CompanionAuthManager
from xiaoshuo_bot.server.matrix_scheduler import _select_latest_snapshot_file


def _clean_chapter_body(chapter_no: int) -> str:
    if chapter_no == 1:
        paras = [
            "人事主管赵明把辞退通知拍在玻璃桌上时，我兜里只剩三百块。",
            "“赔款单按最低走，五点前清工位。”赵明抬起腕表敲了敲桌面。",
            "他把那份我熬了三个月写出来的核心算法底稿，直接推给旁边的亲戚。",
            "【检测到恶意抢功，十倍反噬账单已绑定。】",
            "面板只有十六个字，账面余额跳到一百万。",
            "“赵主管，这是你签的移交单？”我把工牌拍在移交单旁边。",
            "赵明嗤笑一声，伸手就要撕掉那页纸。",
            "我抬手按住他的手腕，指节抵着桌沿，直接把纸抽了回来。",
            "“你敢动一下，我就把昨晚的后台提交记录发进全公司两千人的大群。”我说。",
            "走廊外的茶水间门忽然响了一声，几个实习生探头往里张望。",
            "“你疯了？全集团都知道你签了保密协议！”赵明脸色骤变，猛地起身想抢手机。",
            "我退后半步，拇指直接点下发送键，大屏幕瞬间弹出原始时间戳铁证。",
            "会议室里的投影仪跟着闪烁起来，所有高管的邮箱同时叮咚作响。",
            "赵明伸在半空的手僵在原地，额头滚下一滴冷汗。",
            "【第一笔十倍反噬结算完成，可用余额已突破五百万。】",
            "我收起手机，拎起背包转身走出玻璃门，电梯门正好在这一刻打开。",
            "门外站着集团总部的审计专员，手里拿着一份刚打印出来的紧急停职令。",
        ]
    else:
        paras = [
            "审计专员周成把红色封皮的停职令摊在走廊长桌上，直接挡住了赵明的去路。",
            "“赵主管，请配合核对上季度的外包采购回单。”周成语气平淡。",
            "我站在电梯口没有走，从背包里抽出昨晚备份的那张加密存储卡。",
            "【新任务激活：当场拆穿虚假外包发票，可解锁千倍股权托管权限。】",
            "“赵主管刚才说我的工位归他表弟，”我把卡递给周成，“顺便查查那笔八十万的设备款。”",
            "赵明嘴唇发白，伸手扶住墙面才没滑坐在地。",
            "周成接过卡插进随身平板，屏幕瞬间跳出三张连号假发票的扫描件。",
            "“这不是设备款，这是空壳公司套现的流水！”旁边围观的财务主管脱口而出。",
            "大厅门口忽然涌进四名市局稽查司人员，为首的队长直接亮出调查函。",
            "“谁是赵明？”队长目光扫过人群，“跟我们回局里走一趟。”",
            "赵明双腿发软，差点撞翻走廊上的不锈钢垃圾桶。",
            "我看着他被带进专用电梯，顺手在手机上签收了猎头刚发来的竞业买断协议。",
            "周成递过一张烫金名片，指节敲了敲名片背面的加密内线号码。",
            "“总部技术委员会正在缺一位首席安全顾问，下午两点有空聊聊？”他问。",
            "我把名片放进西装内袋，迎着电梯厅落下的白光按下了下行键。",
        ]
    filler = [
        "大厅落地窗外的电子屏正好切到晚间财经快讯，红绿交错的行情数字一路往上蹿。",
        "“陈先生，这是您要的股权托管确认函。”前台主管小跑着递过烫金文件夹。",
        "我低头看了眼腕表，距离对家风投宣布撤资只剩不到二十分钟。",
        "【提示：十倍返现额度已转入托管专用账户，当前可动用五百万元。】",
        "“把会议室大门打开，让所有股东现在进来签字。”我把文件夹拍在桌面。",
    ]
    idx = 0
    while len("".join(paras)) < 900:
        paras.append(filler[idx % len(filler)])
        idx += 1
    return "\n\n".join(paras)


def _create_minimal_project(
    project_dir: Path,
    *,
    project_id: str,
    title: str,
    num_chapters: int = 12,
    bad_project: bool = False,
) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    outline_dir = project_dir / "outline"
    outline_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "project_id": project_id,
        "title": title,
        "genre": "都市脑洞",
        "genre_id": "urban_brainhole",
        "synopsis": (
            "实习转正前夜被主管抢走核心算法底稿，兜里只剩三百块的陈牧野反手绑定十倍反噬账单。"
            "他当众群发原始时间戳铁证，从写字楼底层的实习生一路逆袭买下整栋大厦，让所有轻视者付出代价。"
        ),
        "default_pov_character": "mc",
        "max_rewrite_retries": 1,
        "author_level": "lv0_1",
        "contract_track": "complete_150k",
        "pov_mode": "first_person",
        "panel_policy": "allow_system_tag",
        "pen_name": "砚山行",
        "author_name": "砚山行",
        "primary_category": "都市脑洞",
        "tags": ["系统", "神豪", "职场逆袭"],
        "alternate_titles": ["备选爆款书名甲", "备选爆款书名乙"],
        "alternate_blurbs": ["第一版备选简介，聚焦开篇打脸与反噬账单。"],
        "tweet_aliases": ["工位反噬账单", "十倍逆袭神豪", "海城买楼传奇"],
    }
    (project_dir / "project.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    chars = {
        "characters": [
            {
                "id": "mc",
                "name": "陈牧野",
                "role": "protagonist",
                "personality": "果断犀利",
                "speech_style": "短句干脆",
            },
            {
                "id": "antag",
                "name": "赵明",
                "role": "antagonist",
                "personality": "贪婪跋扈",
                "speech_style": "傲慢推诿",
            },
        ]
    }
    (project_dir / "characters.yaml").write_text(
        yaml.safe_dump(chars, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (project_dir / "assets.yaml").write_text(
        yaml.safe_dump(
            {
                "assets": [
                    {
                        "id": "work_badge",
                        "name": "加密工牌",
                        "location": "陈牧野手边",
                        "holder": "mc",
                        "condition": "完好",
                        "physical_details": "银色金属边工牌",
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project_dir / "foreshadows.yaml").write_text(
        yaml.safe_dump({"foreshadows": []}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (project_dir / "style_profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "style_profiles": [
                    {
                        "id": "urban_fast",
                        "name": "都市快节奏",
                        "granularity_notes": "短段干脆",
                        "sample_text": "赵明把辞退通知拍在玻璃桌上。",
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    scenes = []
    for ch in range(1, num_chapters + 1):
        scenes.append(
            {
                "chapter": ch,
                "scene_id": f"s_{ch:04d}",
                "internal_title": f"第{ch}幕冲突",
                "public_title": f"天价反噬第{ch}波",
                "pov_character": "mc",
                "active_characters": ["mc", "antag"],
                "active_assets": ["work_badge"],
                "style_profile_ids": ["urban_fast"],
                "location_time": "故障隔离测试地点" if bad_project else "海城写字楼",
                "events": ["反击抢功者"],
                "stop_condition": "冲突反转收尾",
                "min_chars": 600,
                "max_chars": 4000,
                "emotion_polarity": "burst" if ch % 2 == 1 else "neutral",
            }
        )
    (outline_dir / "vol_01.yaml").write_text(
        yaml.safe_dump(
            {
                "volume": 1,
                "is_final_volume": False,
                "volume_cap_locks": [{"ledger_key": "cash", "volume": 1, "max_value": 10000000.0}],
                "scenes": scenes,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if bad_project:
        # Mark marker file so test provider intentionally yields gate-violating text for this project
        (project_dir / ".force_gate_failure").write_text("1", encoding="utf-8")
    return project_dir


class _PerBookDeterministicProvider(LLMProvider):
    """Returns clean prose normally, or deliberate gate-failing prose for failing book."""

    def __init__(self, failing_project_id: str) -> None:
        self.failing_project_id = failing_project_id
        self.calls: List[str] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request.request_id)
        combined = f"{request.system_prompt}\n{request.user_prompt}"
        if self.failing_project_id != "__none__" and (
            self.failing_project_id in combined or "故障隔离测试地点" in combined
        ):
            # Deliberately violate SAFE_004 + HOOK_001 + HOOK_002 so ProductionGateFailure fires
            bad_text = (
                "雨夜的寒风吹过北京市朝阳区公安局门口，他名叫张三，今年22岁。\n\n"
                "他的嘴角微不可察地勾起三分薄凉，周围的空气仿佛凝固了。\n\n"
                "既然对方道歉了，主角决定大度原谅杀父仇人。\n\n"
                "一夜无话，大家沉沉睡去，期待明天。"
            )
            return LLMResponse(
                request_id=request.request_id,
                content=bad_text,
                model="test-writer",
                provider="test",
            )
        return LLMResponse(
            request_id=request.request_id,
            content=_clean_chapter_body(request.chapter),
            model="test-writer",
            provider="test",
        )


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 10.0,
) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        err_body = exc.read() if exc.fp is not None else b""
        return exc.code, dict(exc.headers.items()), err_body


class _MockCPAHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    auth_files: List[Dict[str, Any]] = [
        {"name": "claude-sub-1.json", "provider": "claude", "status": "healthy"}
    ]
    last_uploaded: Optional[bytes] = None
    last_deleted: Optional[str] = None

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.startswith("/v0/management/auth-files"):
            self._send_json(200, {"files": list(self.auth_files)})
            return
        if self.path.startswith("/v0/management/usage"):
            self._send_json(200, {"requests_today": 42, "accounts_active": len(self.auth_files)})
            return
        if self.path.startswith("/v0/management/anthropic-auth-url"):
            self._send_json(200, {"url": "https://example.invalid/oauth/mock", "state": "st-123"})
            return
        if self.path.startswith("/v1/models"):
            self._send_json(200, {"data": [{"id": "claude-sonnet-4-5"}, {"id": "gpt-5"}]})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        if self.path.startswith("/v0/management/auth-files"):
            type(self).last_uploaded = body
            self.auth_files.append({"name": "gemini-sub-2.json", "provider": "gemini", "status": "healthy"})
            self._send_json(200, {"uploaded": True, "count": len(self.auth_files)})
            return
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if self.path.startswith("/v0/management/auth-files"):
            type(self).last_deleted = self.path
            self._send_json(200, {"deleted": True})
            return
        self._send_json(404, {"error": "not found"})


class TestServerStockpileAndScheduler(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["XIAOSHUO_API_KEY"] = ""
        os.environ["OPENAI_API_KEY"] = ""
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["XIAOSHUO_ADMIN_TOKEN"] = ""
        os.environ["XIAOSHUO_COMPANION_SECRET"] = ""

    def test_insecure_bind_error_on_non_loopback_without_strong_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(InsecureBindError):
                create_server(
                    host="0.0.0.0",
                    port=0,
                    db_path=root / "stockpile.db",
                    projects_root=root / "projects",
                    admin_token="",
                )
            with self.assertRaises(InsecureBindError):
                create_server(
                    host="0.0.0.0",
                    port=0,
                    db_path=root / "stockpile.db",
                    projects_root=root / "projects",
                    admin_token="short-23-chars-token-xx",  # 23 chars < 24
                )
            # 24+ chars passes bind validation
            strong = "a" * 24
            self.assertEqual(validate_bind_security("0.0.0.0", strong), strong)
            # Loopback hosts NO LONGER allow an empty token (prod runs on 127.0.0.1 behind Caddy)
            for loop_host in ("127.0.0.1", "localhost", "::1"):
                with self.assertRaises(InsecureBindError):
                    validate_bind_security(loop_host, "")
                # ...unless explicitly opted out for local dev/tests
                self.assertEqual(
                    validate_bind_security(loop_host, "", allow_insecure_loopback_no_auth=True), ""
                )

    def test_admin_bearer_auth_and_5_failure_429_ban(self) -> None:
        mono_clock = [1000.0]
        cst_now = datetime(2026, 11, 18, 14, 0, 0, tzinfo=SHANGHAI_TZ)
        admin_token = "super-secret-admin-token-24plus-chars"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=root / "stockpile.db",
                projects_root=root / "projects",
                admin_token=admin_token,
                companion_secret="comp-secret-12345678",
                now_fn=lambda: cst_now,
                monotonic_fn=lambda: mono_clock[0],
            )
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{srv.server_port}"
            try:
                # Health check is public and reports +08:00
                code, _, body = _http_request(f"{base_url}{API_HEALTH}")
                self.assertEqual(code, 200)
                health = json.loads(body.decode("utf-8"))
                self.assertIn("+08:00", health["shanghai_time"])

                # 4 consecutive bad attempts -> 401
                for i in range(4):
                    code, _, _ = _http_request(
                        f"{base_url}{API_BOOKS}",
                        headers={"Authorization": f"Bearer wrong-{i}"},
                    )
                    self.assertEqual(code, 401)

                # 5th consecutive bad attempt -> 429 lockout
                code, hdrs, err_raw = _http_request(
                    f"{base_url}{API_BOOKS}",
                    headers={"Authorization": "Bearer wrong-5"},
                )
                self.assertEqual(code, 429)
                self.assertIn("Retry-After", hdrs)
                err_obj = json.loads(err_raw.decode("utf-8"))
                self.assertEqual(err_obj["error_code"], "TOO_MANY_AUTH_FAILURES")

                # Even valid token is blocked during 30-min ban
                code, _, _ = _http_request(
                    f"{base_url}{API_BOOKS}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(code, 429)

                # After 30 minutes (1801s), valid token works again
                mono_clock[0] += 1801.0
                code, _, _ = _http_request(
                    f"{base_url}{API_BOOKS}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(code, 200)
            finally:
                srv.shutdown_and_close()
                srv.db.close()

    def test_companion_hmac_auth_timestamp_and_nonce_replay_rejected_401(self) -> None:
        cst_now = datetime(2026, 11, 18, 15, 0, 0, tzinfo=SHANGHAI_TZ)
        secret = "edge-hmac-secret-key-2026"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=root / "stockpile.db",
                projects_root=root / "projects",
                admin_token="admin-token-for-hmac-test-24plus",
                companion_secret=secret,
                now_fn=lambda: cst_now,
            )
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{srv.server_port}"
            try:
                payload = json.dumps({"account_id": "acc_1", "timeout_sec": 0}).encode("utf-8")
                ts_now = int(cst_now.timestamp())
                good_headers = compute_hmac_headers(
                    secret,
                    "POST",
                    API_COMPANION_POLL,
                    payload,
                    client_id="comp-node-1",
                    timestamp=ts_now,
                    nonce="nonce-unique-0001",
                )
                good_headers["Content-Type"] = "application/json"

                code, _, body = _http_request(
                    f"{base_url}{API_COMPANION_POLL}",
                    method="POST",
                    headers=good_headers,
                    body=payload,
                )
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body.decode("utf-8"))["tasks"], [])

                # Replay same nonce -> 401 HMAC_REPLAY_ERROR
                code, _, err_raw = _http_request(
                    f"{base_url}{API_COMPANION_POLL}",
                    method="POST",
                    headers=good_headers,
                    body=payload,
                )
                self.assertEqual(code, 401)
                self.assertEqual(json.loads(err_raw.decode("utf-8"))["error_code"], "HMAC_REPLAY_ERROR")

                # Expired timestamp (>60s) -> 401 HMAC_TIMESTAMP_ERROR
                stale_headers = compute_hmac_headers(
                    secret,
                    "POST",
                    API_COMPANION_POLL,
                    payload,
                    client_id="comp-node-1",
                    timestamp=ts_now - 120,
                    nonce="nonce-unique-0002",
                )
                stale_headers["Content-Type"] = "application/json"
                code, _, err_raw = _http_request(
                    f"{base_url}{API_COMPANION_POLL}",
                    method="POST",
                    headers=stale_headers,
                    body=payload,
                )
                self.assertEqual(code, 401)
                self.assertEqual(json.loads(err_raw.decode("utf-8"))["error_code"], "HMAC_TIMESTAMP_ERROR")
            finally:
                srv.shutdown_and_close()
                srv.db.close()

    def test_sequential_lease_lock_and_5min_lease_expiry(self) -> None:
        clock = [datetime(2026, 11, 18, 10, 0, 0, tzinfo=SHANGHAI_TZ)]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = StockpileDB(root / "stockpile.db", now_fn=lambda: clock[0])
            try:
                self.assertEqual(db.get_journal_mode(), "wal")
                db.register_book(
                    book_id="b1",
                    project_dir=root / "b1",
                    title="序列锁测试",
                    account_id="acc_seq",
                )
                db.add_stockpiled_chapter("b1", 1, "第一章", "这是第一章正文内容" * 50)
                db.add_stockpiled_chapter("b1", 2, "第二章", "这是第二章正文内容" * 50)

                # Chapter 2 blocked while Chapter 1 is STOCKPILED
                with self.assertRaises(SequentialLeaseLockError):
                    db.lease_chapter(
                        CompanionLeaseRequest(
                            task_id="t2", book_id="b1", chapter=2, account_id="acc_seq"
                        )
                    )

                # Lease Chapter 1 (5-min lease)
                lease1 = db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="t1", book_id="b1", chapter=1, account_id="acc_seq", lease_seconds=300
                    )
                )
                self.assertEqual(lease1.status, StockpileStatus.LEASED.value)

                # Chapter 2 still blocked while Chapter 1 is LEASED
                with self.assertRaises(SequentialLeaseLockError):
                    db.lease_chapter(
                        CompanionLeaseRequest(
                            task_id="t2", book_id="b1", chapter=2, account_id="acc_seq"
                        )
                    )

                # Advance 301 seconds -> Chapter 1 lease expires back to STOCKPILED
                clock[0] = clock[0] + timedelta(seconds=301)
                db.expire_stale_leases()
                ch1_state = db.get_chapter("b1", 1)
                self.assertEqual(ch1_state["status"], StockpileStatus.STOCKPILED.value)

                # Re-lease Chapter 1 & mark DRAFT_SAVED -> Chapter 2 still blocked
                db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="t1b", book_id="b1", chapter=1, account_id="acc_seq"
                    )
                )
                db.record_receipt(
                    CompanionReceiptRequest(
                        task_id="t1b",
                        book_id="b1",
                        chapter=1,
                        account_id="acc_seq",
                        status=StockpileStatus.DRAFT_SAVED.value,
                    )
                )
                with self.assertRaises(SequentialLeaseLockError):
                    db.lease_chapter(
                        CompanionLeaseRequest(
                            task_id="t2", book_id="b1", chapter=2, account_id="acc_seq"
                        )
                    )

                # Mark Chapter 1 SCHEDULED -> now Chapter 2 can be leased!
                db.record_receipt(
                    CompanionReceiptRequest(
                        task_id="t1b",
                        book_id="b1",
                        chapter=1,
                        account_id="acc_seq",
                        status=StockpileStatus.SCHEDULED.value,
                        scheduled_at=clock[0].isoformat(),
                    )
                )
                lease2 = db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="t2", book_id="b1", chapter=2, account_id="acc_seq"
                    )
                )
                self.assertEqual(lease2.chapter, 2)
                self.assertEqual(lease2.status, StockpileStatus.LEASED.value)
            finally:
                db.close()

    def test_cst_daily_word_quota_lock_and_2330_kst_equals_2230_cst_same_day(self) -> None:
        kst_tz = timezone(timedelta(hours=9), name="Asia/Seoul")
        # 23:30 KST on 2026-11-18 == 22:30 CST on 2026-11-18 (same natural day!)
        clock = [datetime(2026, 11, 18, 23, 30, 0, tzinfo=kst_tz)]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = StockpileDB(root / "stockpile.db", now_fn=lambda: clock[0])
            try:
                db.register_book(
                    book_id="lv0_book",
                    project_dir=root / "lv0_book",
                    title="新号配额测试",
                    author_level="lv0_1",
                    account_id="acc_lv0",
                )
                db.add_stockpiled_chapter("lv0_book", 1, "一", "字" * 4500)
                db.add_stockpiled_chapter("lv0_book", 2, "二", "字" * 4000)
                db.add_stockpiled_chapter("lv0_book", 3, "三", "字" * 2000)

                # Lease & schedule Ch.1 (4500 chars) at 23:30 KST
                db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="q1", book_id="lv0_book", chapter=1, account_id="acc_lv0"
                    )
                )
                db.record_receipt(
                    CompanionReceiptRequest(
                        task_id="q1",
                        book_id="lv0_book",
                        chapter=1,
                        account_id="acc_lv0",
                        status=StockpileStatus.SCHEDULED.value,
                        scheduled_at="2026-11-18T23:30:00+09:00",
                    )
                )

                # Lease & schedule Ch.2 (4000 chars) -> total 8500 on 2026-11-18 CST
                db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="q2", book_id="lv0_book", chapter=2, account_id="acc_lv0"
                    )
                )
                db.record_receipt(
                    CompanionReceiptRequest(
                        task_id="q2",
                        book_id="lv0_book",
                        chapter=2,
                        account_id="acc_lv0",
                        status=StockpileStatus.SCHEDULED.value,
                        scheduled_at="2026-11-18T23:32:00+09:00",
                    )
                )

                # Verify 23:30 KST attributed strictly to 2026-11-18 CST, 0 on 2026-11-19
                self.assertEqual(db.get_daily_used_chars("acc_lv0", "2026-11-18"), 8500)
                self.assertEqual(db.get_daily_used_chars("acc_lv0", "2026-11-19"), 0)

                # Ch.3 (2000 chars) at 23:35 KST (22:35 CST) exceeds 8500 -> deferred to 2026-11-19 CST
                clock[0] = datetime(2026, 11, 18, 23, 35, 0, tzinfo=kst_tz)
                self.assertEqual(db.poll_available_tasks("acc_lv0"), [])
                with self.assertRaises(DailyQuotaExceededError) as ctx:
                    db.lease_chapter(
                        CompanionLeaseRequest(
                            task_id="q3", book_id="lv0_book", chapter=3, account_id="acc_lv0"
                        )
                    )
                self.assertEqual(ctx.exception.cst_date, "2026-11-18")
                self.assertEqual(ctx.exception.deferred_to_date, "2026-11-19")
                self.assertEqual(db.get_chapter("lv0_book", 3)["quota_deferred_to_date"], "2026-11-19")

                # Advance to 2026-11-19 01:05 KST == 00:05 CST -> quota resets, Ch.3 leases cleanly!
                clock[0] = datetime(2026, 11, 19, 1, 5, 0, tzinfo=kst_tz)
                tasks = db.poll_available_tasks("acc_lv0")
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["chapter"], 3)
                lease3 = db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="q3", book_id="lv0_book", chapter=3, account_id="acc_lv0"
                    )
                )
                self.assertEqual(lease3.status, StockpileStatus.LEASED.value)

                # Verify lv2_plus 17,000 cap
                db.register_book(
                    book_id="lv2_book",
                    project_dir=root / "lv2_book",
                    title="老号配额测试",
                    author_level="lv2_plus",
                    account_id="acc_lv2",
                )
                db.add_stockpiled_chapter("lv2_book", 1, "一", "字" * 10000)
                db.add_stockpiled_chapter("lv2_book", 2, "二", "字" * 8000)
                db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="l2_1", book_id="lv2_book", chapter=1, account_id="acc_lv2"
                    )
                )
                db.record_receipt(
                    CompanionReceiptRequest(
                        task_id="l2_1",
                        book_id="lv2_book",
                        chapter=1,
                        account_id="acc_lv2",
                        status=StockpileStatus.SCHEDULED.value,
                        scheduled_at=clock[0].isoformat(),
                    )
                )
                with self.assertRaises(DailyQuotaExceededError) as ctx2:
                    db.lease_chapter(
                        CompanionLeaseRequest(
                            task_id="l2_2", book_id="lv2_book", chapter=2, account_id="acc_lv2"
                        )
                    )
                self.assertEqual(ctx2.exception.daily_cap, 17000)
            finally:
                db.close()

    def test_metrics_idempotency_unsettled_skip_and_t09_evolution_linkage(self) -> None:
        cst_now = datetime(2026, 11, 18, 13, 0, 0, tzinfo=SHANGHAI_TZ)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = StockpileDB(root / "stockpile.db", now_fn=lambda: cst_now)
            motif_store = MotifWeightStore(root / "motif_weights.json")
            try:
                db.register_book(
                    book_id="evo_book",
                    project_dir=root / "evo_book",
                    title="原书名",
                    genre_id="urban_brainhole",
                    author_level="lv0_1",
                    account_id="acc_evo",
                    max_published_chapter=3,
                    signed=False,
                    alternate_titles=["高点击换皮名一", "高点击换皮名二"],
                    alternate_blurbs=["新版三段式简介"],
                    opening_motif="被裁抢功",
                    golden_finger_type="十倍反噬账单",
                )

                # 1. Unsettled metrics -> skipped loudly without falsifying 0s or cutting book
                unsettled_req = CompanionMetricsRequest(
                    book_id="evo_book",
                    account_id="acc_evo",
                    settled=False,
                    settlement_status="NOT_SETTLED",
                    metrics_date="2026-11-17",
                    ch2_read_through=None,
                    bookshelf_rate=None,
                )
                res_unsettled = db.record_metrics(unsettled_req, motif_store=motif_store)
                self.assertEqual(res_unsettled["racing_decision"]["status"], "METRICS_NOT_SETTLED")
                self.assertEqual(db.get_book("evo_book")["status"], BookRunStatus.ACTIVE.value)

                # 2. Settled metrics triggering ROTATE_PACKAGING + ReviewFeedbackOptimizer (> ch 3)
                settled_req = CompanionMetricsRequest(
                    book_id="evo_book",
                    account_id="acc_evo",
                    settled=True,
                    settlement_status="SETTLED",
                    metrics_date="2026-11-18",
                    ch2_read_through=0.64,
                    bookshelf_rate=0.11,
                    click_through_rate=0.09,
                    impressions=2500,
                    comments=[
                        {"chapter": 2, "content": "前面节奏慢了点，反派蹦跶太久", "likes": 12},
                        {"chapter": 3, "content": "配角周成太讨喜了，多写点", "likes": 8},
                    ],
                    decoded_hotlist=[
                        {"rank": 1, "title": "十倍返现买下写字楼", "genre": "都市脑洞"}
                    ],
                )
                res_settled = db.record_metrics(settled_req, motif_store=motif_store)
                self.assertFalse(res_settled["idempotent_replay"])
                self.assertEqual(res_settled["racing_decision"]["action"], "ROTATE_PACKAGING")
                self.assertEqual(
                    res_settled["racing_decision"]["packaging_task"]["new_title"],
                    "高点击换皮名一",
                )
                # Directives strictly target chapter > max_published_chapter (3)
                directives = db.get_outline_directives("evo_book")
                self.assertTrue(len(directives) >= 1)
                for d in directives:
                    self.assertGreater(d["target_chapter"], 3)

                # Decoded hotlist persisted & ROTATE_PACKAGING task queued for companion
                self.assertEqual(len(db.get_latest_hotlist()), 1)
                tasks = db.poll_available_tasks("acc_evo")
                self.assertTrue(any(t["task_type"] == "ROTATE_PACKAGING" for t in tasks))

                # MotifWeightStore recorded exactly 1 trial
                entry = motif_store.get_entry("urban_brainhole", "被裁抢功", "十倍反噬账单")
                self.assertEqual(entry.trials, 1)

                # 3. Idempotent replay does NOT double-count trials or duplicate records
                res_replay = db.record_metrics(settled_req, motif_store=motif_store)
                self.assertTrue(res_replay["idempotent_replay"])
                self.assertEqual(
                    motif_store.get_entry("urban_brainhole", "被裁抢功", "十倍反噬账单").trials, 1
                )
            finally:
                db.close()

    def test_safety_cushion_priority_under_6_and_per_book_fault_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            projects_root = root / "projects"
            db = StockpileDB(root / "stockpile.db")
            provider = _PerBookDeterministicProvider(failing_project_id="book_bad")
            scheduler = MatrixScheduler(db=db, projects_root=projects_root, provider=provider)
            try:
                _create_minimal_project(
                    projects_root / "book_norm", project_id="book_norm", title="普通水位测试书", num_chapters=12
                )
                _create_minimal_project(
                    projects_root / "book_bad",
                    project_id="book_bad",
                    title="故障隔离测试书",
                    num_chapters=12,
                    bad_project=True,
                )
                _create_minimal_project(
                    projects_root / "book_urgent",
                    project_id="book_urgent",
                    title="紧急抢产测试书",
                    num_chapters=12,
                )

                scheduler.register_project(projects_root / "book_norm", account_id="acc_1")
                scheduler.register_project(projects_root / "book_bad", account_id="acc_1")
                scheduler.register_project(projects_root / "book_urgent", account_id="acc_1")

                # Seed stockpile counts: book_norm=7 (<10), book_urgent=2 (<6), book_bad=3 (<6)
                for ch in range(1, 8):
                    db.add_stockpiled_chapter("book_norm", ch, f"标题{ch}", _clean_chapter_body(ch))
                for ch in range(1, 3):
                    db.add_stockpiled_chapter("book_urgent", ch, f"标题{ch}", _clean_chapter_body(ch))
                for ch in range(1, 4):
                    db.add_stockpiled_chapter("book_bad", ch, f"标题{ch}", _clean_chapter_body(ch))

                # Seed predecessor snapshots on disk for ch 2 and ch 3 so engine partial run works
                for bid, prev_ch in (("book_norm", 7), ("book_urgent", 2), ("book_bad", 3)):
                    p_dir = projects_root / bid
                    out_dir = p_dir / "output"
                    from xiaoshuo_bot.engine import NovelGenerationEngine
                    from xiaoshuo_bot.loader import load_project

                    proj = load_project(p_dir)
                    eng = NovelGenerationEngine(
                        proj, _PerBookDeterministicProvider("__none__"), out_dir
                    )
                    eng.run(target_chapters=list(range(1, prev_ch + 1)))

                queue = scheduler.inspect_safety_cushions(emit_alerts=True)
                # <6 books MUST come before 7-chapter book_norm, ordered by lowest stockpile first
                self.assertEqual([q.book_id for q in queue], ["book_urgent", "book_bad", "book_norm"])
                self.assertTrue(queue[0].urgent)
                self.assertTrue(queue[1].urgent)
                self.assertFalse(queue[2].urgent)

                events_before = scheduler.event_broker.get_recent_events()
                self.assertTrue(any(e.event == SSE_SAFETY_CUSHION_ALERT for e in events_before))

                # Run full production cycle: book_bad isolates on gate failure, while book_urgent and book_norm succeed!
                cycle = scheduler.run_production_cycle()
                self.assertEqual(cycle["queued_books"], ["book_urgent", "book_bad", "book_norm"])
                self.assertEqual(cycle["produced_count"], 2)

                self.assertEqual(db.get_book("book_bad")["status"], BookRunStatus.ISOLATED_GATE_FAILURE.value)
                iso_detail = db.get_book("book_bad")["isolation_detail"]
                self.assertEqual(iso_detail["chapter"], 4)
                self.assertTrue(len(iso_detail["violations"]) >= 1)
                self.assertTrue(len(iso_detail["diff"]) > 0)

                self.assertEqual(db.get_book("book_urgent")["status"], BookRunStatus.ACTIVE.value)
                self.assertEqual(db.count_stockpiled("book_urgent"), 3)
                self.assertEqual(db.get_book("book_norm")["status"], BookRunStatus.ACTIVE.value)
                self.assertEqual(db.count_stockpiled("book_norm"), 8)

                events_after = scheduler.event_broker.get_recent_events()
                self.assertTrue(any(e.event == SSE_BOOK_ISOLATED and e.book_id == "book_bad" for e in events_after))
                self.assertTrue(any(e.event == SSE_CHAPTER_STOCKPILED and e.book_id == "book_urgent" for e in events_after))
                self.assertTrue(any(e.event == SSE_CHAPTER_STOCKPILED and e.book_id == "book_norm" for e in events_after))
            finally:
                db.close()

    def test_matrix_control_port_cut_confirm_and_captcha_pause_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db = StockpileDB(root / "stockpile.db")
            scheduler = MatrixScheduler(db=db, projects_root=root / "projects")
            try:
                self.assertIsInstance(scheduler, MatrixControlPort)
                db.register_book(
                    book_id="ctrl_1",
                    project_dir=root / "ctrl_1",
                    title="控制协议书",
                    account_id="acc_ctrl",
                )
                db.add_stockpiled_chapter("ctrl_1", 1, "一", _clean_chapter_body(1))
                db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="tc1", book_id="ctrl_1", chapter=1, account_id="acc_ctrl"
                    )
                )

                # Captcha pause -> book and chapter paused
                cap_res = scheduler.report_captcha_pause(
                    CompanionCaptchaReport(
                        account_id="acc_ctrl",
                        book_id="ctrl_1",
                        chapter=1,
                        reason="SLIDER_CAPTCHA",
                        screenshot_base64="aGVsbG8=",
                    )
                )
                self.assertEqual(cap_res["status"], BookRunStatus.PAUSED_AUTH_CAPTCHA.value)
                self.assertEqual(db.get_book("ctrl_1")["status"], BookRunStatus.PAUSED_AUTH_CAPTCHA.value)
                self.assertEqual(db.get_chapter("ctrl_1", 1)["status"], StockpileStatus.PAUSED_AUTH_CAPTCHA.value)

                # Resume companion -> restores ACTIVE & STOCKPILED
                resumed = scheduler.resume_companion("acc_ctrl", "ctrl_1")
                self.assertEqual(resumed["status"], "RESUMED")
                self.assertEqual(db.get_book("ctrl_1")["status"], BookRunStatus.ACTIVE.value)
                self.assertEqual(db.get_chapter("ctrl_1", 1)["status"], StockpileStatus.STOCKPILED.value)

                # 2-step cut-loss confirmation with CAS version check
                req_cut = scheduler.request_cut_book("ctrl_1", reason="Low read-through")
                ver = req_cut["confirmation_version"]
                with self.assertRaises(CutConfirmationConflictError):
                    scheduler.confirm_cut_book("ctrl_1", confirmation_version=ver + 99)
                self.assertEqual(db.get_book("ctrl_1")["status"], BookRunStatus.ACTIVE.value)

                confirmed = scheduler.confirm_cut_book("ctrl_1", confirmation_version=ver)
                self.assertEqual(confirmed["status"], BookRunStatus.CUT_LOSS.value)
                self.assertEqual(db.get_book("ctrl_1")["status"], BookRunStatus.CUT_LOSS.value)

                events = [e.event for e in scheduler.event_broker.get_recent_events()]
                self.assertIn(SSE_COMPANION_CAPTCHA, events)
                self.assertIn(SSE_COMPANION_RESUMED, events)
            finally:
                db.close()

    def test_long_poll_sse_stream_cpa_proxy_and_all_rest_endpoints(self) -> None:
        mock_cpa = ThreadingHTTPServer(("127.0.0.1", 0), _MockCPAHandler)
        mock_cpa_thread = threading.Thread(target=mock_cpa.serve_forever, daemon=True)
        mock_cpa_thread.start()
        cpa_url = f"http://127.0.0.1:{mock_cpa.server_port}"

        admin_token = "integration-admin-token-24plus-chars"
        comp_secret = "integration-hmac-secret-2026"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            projects_root = root / "projects"
            _create_minimal_project(
                projects_root / "srv_book", project_id="srv_book", title="服务端全链路书", num_chapters=4
            )
            cpa_client = CPAManagementClient(base_url=cpa_url, management_key="cpa-mgmt-key-123")
            router = RoleModelRouter(
                {
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=f"{cpa_url}/v1",
                        model="claude-sonnet-4-5",
                        api_keys=["cpa-api-key-1"],
                    )
                }
            )
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=root / "stockpile.db",
                projects_root=projects_root,
                admin_token=admin_token,
                companion_secret=comp_secret,
                provider=router,
                cpa_client=cpa_client,
                sse_heartbeat_sec=1.0,
            )
            srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
            srv_thread.start()
            base_url = f"http://127.0.0.1:{srv.server_port}"
            auth_headers = {"Authorization": f"Bearer {admin_token}"}

            try:
                srv.scheduler.register_project(projects_root / "srv_book", account_id="acc_srv")

                # 1. Genres & Books
                code, _, body = _http_request(f"{base_url}{API_GENRES}", headers=auth_headers)
                self.assertEqual(code, 200)
                self.assertEqual(len(json.loads(body.decode("utf-8"))["genres"]), 11)

                # 2. SSE live stream receives heartbeat + published chapter event
                sse_lines: List[str] = []
                sse_done = threading.Event()

                def _read_sse() -> None:
                    req = urllib.request.Request(f"{base_url}{API_EVENTS_STREAM}", headers=auth_headers)
                    with urllib.request.urlopen(req, timeout=8.0) as resp:
                        while not sse_done.is_set():
                            line = resp.readline().decode("utf-8", errors="replace")
                            if not line:
                                break
                            sse_lines.append(line.rstrip("\r\n"))
                            if "event: chapter_stockpiled" in sse_lines and len(sse_lines) >= 6:
                                sse_done.set()
                                break

                sse_thread = threading.Thread(target=_read_sse, daemon=True)
                sse_thread.start()
                time.sleep(0.2)

                # 3. 25s Companion Long-Poll wakes immediately when a chapter is stockpiled!
                def _delayed_stockpile() -> None:
                    time.sleep(0.25)
                    srv.db.add_stockpiled_chapter("srv_book", 1, "天价反噬开局", _clean_chapter_body(1))
                    from xiaoshuo_bot.contracts import SSEEventPayload

                    srv.scheduler.event_broker.publish(
                        SSEEventPayload(
                            event=SSE_CHAPTER_STOCKPILED,
                            book_id="srv_book",
                            chapter=1,
                            message="Chapter 1 stockpiled",
                        )
                    )

                producer_thread = threading.Thread(target=_delayed_stockpile, daemon=True)
                producer_thread.start()

                poll_body = json.dumps({"account_id": "acc_srv", "timeout_sec": 5}).encode("utf-8")
                poll_hdrs = compute_hmac_headers(
                    comp_secret, "POST", API_COMPANION_POLL, poll_body, client_id="comp-e2e"
                )
                poll_hdrs["Content-Type"] = "application/json"
                t0 = time.monotonic()
                code, _, raw_poll = _http_request(
                    f"{base_url}{API_COMPANION_POLL}",
                    method="POST",
                    headers=poll_hdrs,
                    body=poll_body,
                    timeout=6.0,
                )
                elapsed = time.monotonic() - t0
                self.assertEqual(code, 200)
                self.assertLess(elapsed, 2.5)
                poll_data = json.loads(raw_poll.decode("utf-8"))
                self.assertEqual(len(poll_data["tasks"]), 1)
                self.assertEqual(poll_data["tasks"][0]["chapter"], 1)

                sse_thread.join(timeout=4.0)
                sse_done.set()
                joined_sse = "\n".join(sse_lines)
                self.assertIn("event: heartbeat", joined_sse)
                self.assertIn("event: chapter_stockpiled", joined_sse)

                # 4. Chapter lock, export ZIP & procedural cover
                lock_url = base_url + format_api_route(
                    API_BOOK_CHAPTER_LOCK, book_id="srv_book", chapter=1
                )
                code, _, raw_lock = _http_request(
                    lock_url,
                    method="POST",
                    headers={**auth_headers, "Content-Type": "application/json"},
                    body=json.dumps({"locked": True, "reason": "Published check"}).encode("utf-8"),
                )
                self.assertEqual(code, 200)
                self.assertTrue(json.loads(raw_lock.decode("utf-8"))["locked"])

                export_url = base_url + format_api_route(API_BOOK_EXPORT, book_id="srv_book") + "?platform=tomato"
                code, _, zip_bytes = _http_request(export_url, headers=auth_headers)
                self.assertEqual(code, 200)
                with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                    names = zf.namelist()
                    self.assertIn("project_card.json", names)

                cover_url = base_url + format_api_route(API_BOOK_COVER, book_id="srv_book") + "?generate=1&base_mode=procedural"
                code, _, png_bytes = _http_request(cover_url, headers=auth_headers)
                self.assertEqual(code, 200)
                with Image.open(io.BytesIO(png_bytes)) as img:
                    self.assertEqual(img.size, (600, 800))

                # 5. /api/cpa/* proxy routes
                code, _, raw_files = _http_request(f"{base_url}{API_CPA_AUTH_FILES}", headers=auth_headers)
                self.assertEqual(code, 200)
                self.assertGreaterEqual(len(json.loads(raw_files.decode("utf-8"))["files"]), 1)

                # Multipart upload proxy
                boundary = f"----testboundary{uuid.uuid4().hex}"
                mp_body = (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="file"; filename="gemini-sub-2.json"\r\n'
                    f"Content-Type: application/json\r\n\r\n"
                    f'{{"token":"ok"}}\r\n'
                    f"--{boundary}--\r\n"
                ).encode("utf-8")
                code, _, _ = _http_request(
                    f"{base_url}{API_CPA_AUTH_FILES}",
                    method="POST",
                    headers={
                        **auth_headers,
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                    body=mp_body,
                )
                self.assertEqual(code, 200)
                self.assertIsNotNone(_MockCPAHandler.last_uploaded)

                del_url = base_url + format_api_route(API_CPA_AUTH_FILE_DETAIL, name="gemini-sub-2.json")
                code, _, _ = _http_request(del_url, method="DELETE", headers=auth_headers)
                self.assertEqual(code, 200)

                code, _, raw_models = _http_request(f"{base_url}{API_CPA_MODELS}?role=writer", headers=auth_headers)
                self.assertEqual(code, 200)
                self.assertIn("claude-sonnet-4-5", json.loads(raw_models.decode("utf-8"))["models"])

                code, _, raw_usage = _http_request(f"{base_url}{API_CPA_USAGE}", headers=auth_headers)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(raw_usage.decode("utf-8"))["cpa_usage"]["requests_today"], 42)

                code, _, raw_oauth = _http_request(f"{base_url}{API_CPA_OAUTH}?provider=claude", headers=auth_headers)
                self.assertEqual(code, 200)
                self.assertIn("url", json.loads(raw_oauth.decode("utf-8")))

                code, _, raw_bind = _http_request(f"{base_url}{API_CPA_BINDINGS}", headers=auth_headers)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(raw_bind.decode("utf-8"))["bindings"][0]["model"], "claude-sonnet-4-5")

                # Update binding via PUT
                code, _, raw_bind2 = _http_request(
                    f"{base_url}{API_CPA_BINDINGS}",
                    method="PUT",
                    headers={**auth_headers, "Content-Type": "application/json"},
                    body=json.dumps(
                        {"role": "writer", "model": "gpt-5", "base_url": f"{cpa_url}/v1"}
                    ).encode("utf-8"),
                )
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(raw_bind2.decode("utf-8"))["bindings"][0]["model"], "gpt-5")
            finally:
                srv.shutdown_and_close()
                srv.db.close()
                mock_cpa.shutdown()
                mock_cpa.server_close()

    def test_http_book_extend_normal_and_finale_with_stale_directives(self) -> None:
        class _PlannerExtensionProvider(LLMProvider):
            def __init__(self) -> None:
                self.prompts: List[str] = []

            def generate(self, request: LLMRequest) -> LLMResponse:
                self.prompts.append(request.user_prompt)
                meta = dict(request.metadata or {})
                stage = meta.get("stage")
                start_ch = int(meta.get("start_chapter", 1))
                end_ch = int(meta.get("end_chapter", start_ch))
                if stage == "extend_volume":
                    target_vol = int(meta.get("target_volume", 1))
                    scenes = [
                        {
                            "chapter": ch,
                            "scene_id": f"s_{ch:04d}",
                            "internal_title": f"第{ch}幕新局",
                            "public_title": f"反噬风暴第{ch}波",
                            "pov_character": "mc",
                            "active_characters": ["mc", "antag"],
                            "active_assets": ["work_badge"],
                            "style_profile_ids": ["urban_fast"],
                            "location_time": "海城新大厦",
                            "location_id": "loc_new_plaza",
                            "events": ["继续推进反击"],
                            "stop_condition": "悬念收尾",
                            "min_chars": 600,
                            "max_chars": 4000,
                            "emotion_polarity": "burst" if ch % 2 == 1 else "neutral",
                            "volume": target_vol,
                        }
                        for ch in range(start_ch, end_ch + 1)
                    ]
                    payload = {
                        "map_name": "海城新CBD",
                        "location_id": "loc_new_plaza",
                        "soft_identity_reboot": "陈牧野以托管基金合伙人身份重返总部",
                        "volume_cap_locks": [
                            {"ledger_key": "cash", "volume": target_vol, "max_value": 50000000.0, "unit": "元"}
                        ],
                        "scenes": scenes,
                    }
                    return LLMResponse(
                        request_id=request.request_id,
                        content=json.dumps(payload, ensure_ascii=False),
                        model="test-planner",
                        provider="test",
                    )
                if stage == "plan_finale_volume":
                    finale_vol = int(meta.get("finale_volume", 2))
                    unresolved_fs = list(meta.get("unresolved_foreshadow_ids") or [])
                    active_exps = list(meta.get("active_expectation_ids") or [])
                    scenes = [
                        {
                            "chapter": ch,
                            "scene_id": f"s_{ch:04d}",
                            "internal_title": f"终卷第{ch}幕",
                            "public_title": f"终局清算第{ch}波",
                            "pov_character": "mc",
                            "active_characters": ["mc", "antag"],
                            "active_assets": ["work_badge"],
                            "style_profile_ids": ["urban_fast"],
                            "location_time": "海城总控室",
                            "events": ["终局清账"],
                            "stop_condition": "圆满收官",
                            "min_chars": 600,
                            "max_chars": 4000,
                            "emotion_polarity": "burst" if ch % 2 == 1 else "neutral",
                            "volume": finale_vol,
                        }
                        for ch in range(start_ch, end_ch + 1)
                    ]
                    payload = {
                        "foreshadow_resolutions": {fid: end_ch for fid in unresolved_fs},
                        "expectation_fulfillments": {eid: end_ch for eid in active_exps},
                        "scenes": scenes,
                    }
                    return LLMResponse(
                        request_id=request.request_id,
                        content=json.dumps(payload, ensure_ascii=False),
                        model="test-planner",
                        provider="test",
                    )
                raise AssertionError(f"Unexpected planner stage: {stage!r}")

        admin_token = "extend-admin-token-24plus-characters"
        planner_provider = _PlannerExtensionProvider()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            projects_root = root / "projects"
            _create_minimal_project(
                projects_root / "ext_book", project_id="ext_book", title="续纲接口测试书", num_chapters=4
            )
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=root / "stockpile.db",
                projects_root=projects_root,
                admin_token=admin_token,
                companion_secret="extend-hmac-secret-2026",
                provider=planner_provider,
            )
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{srv.server_port}"
            auth_headers = {
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
            }
            try:
                srv.scheduler.register_project(projects_root / "ext_book", account_id="acc_ext")
                for ch in range(1, 5):
                    srv.db.add_stockpiled_chapter("ext_book", ch, f"标题{ch}", _clean_chapter_body(ch))
                for ch in range(1, 4):
                    srv.db.lease_chapter(
                        CompanionLeaseRequest(
                            task_id=f"lease-{ch}", book_id="ext_book", chapter=ch, account_id="acc_ext"
                        )
                    )
                    srv.db.record_receipt(
                        CompanionReceiptRequest(
                            task_id=f"lease-{ch}",
                            book_id="ext_book",
                            chapter=ch,
                            account_id="acc_ext",
                            status=StockpileStatus.PUBLISHED.value,
                        )
                    )
                # At max_published_chapter == 3, metrics creates directive targeting chapter 4
                srv.db.record_metrics(
                    CompanionMetricsRequest(
                        book_id="ext_book",
                        account_id="acc_ext",
                        settled=True,
                        settlement_status="SETTLED",
                        metrics_date="2026-11-17",
                        ch2_read_through=0.65,
                        bookshelf_rate=0.25,
                        impressions=2000,
                        comments=[{"chapter": 3, "content": "前面节奏慢了点，反派蹦跶太久", "likes": 10}],
                    )
                )
                # Now publish chapter 4 so stored directive (target_chapter=4) becomes stale (<= max_published_chapter)
                srv.db.lease_chapter(
                    CompanionLeaseRequest(
                        task_id="lease-4", book_id="ext_book", chapter=4, account_id="acc_ext"
                    )
                )
                srv.db.record_receipt(
                    CompanionReceiptRequest(
                        task_id="lease-4",
                        book_id="ext_book",
                        chapter=4,
                        account_id="acc_ext",
                        status=StockpileStatus.PUBLISHED.value,
                    )
                )
                # Also record newer metrics at max_published_chapter == 4 -> directive targeting chapter 5
                srv.db.record_metrics(
                    CompanionMetricsRequest(
                        book_id="ext_book",
                        account_id="acc_ext",
                        settled=True,
                        settlement_status="SETTLED",
                        metrics_date="2026-11-18",
                        ch2_read_through=0.68,
                        bookshelf_rate=0.26,
                        impressions=2200,
                        comments=[{"chapter": 4, "content": "配角周成太讨喜了，多写点", "likes": 12}],
                    )
                )

                extend_url = base_url + format_api_route(API_BOOK_EXTEND, book_id="ext_book")
                code, _, raw_ext = _http_request(
                    extend_url,
                    method="POST",
                    headers=auth_headers,
                    body=json.dumps({"batch_size": 3, "finale": False}).encode("utf-8"),
                )
                self.assertEqual(code, 200, raw_ext.decode("utf-8", errors="replace"))
                ext_data = json.loads(raw_ext.decode("utf-8"))
                self.assertFalse(ext_data["finale"])
                self.assertEqual(ext_data["book_id"], "ext_book")
                self.assertEqual(ext_data["project_id"], "ext_book")
                self.assertEqual(ext_data["start_chapter"], 5)
                self.assertEqual(ext_data["end_chapter"], 7)
                self.assertEqual(ext_data["total_chapters"], 7)
                self.assertIsInstance(ext_data["volume_file_path"], str)
                # Stale directive (target_chapter=4) skipped; valid directive (target_chapter=5) passed to planner
                self.assertNotIn("目标章>=4", planner_provider.prompts[0])
                self.assertIn("目标章>=5", planner_provider.prompts[0])

                # Finale volume extension over HTTP
                code, _, raw_fin = _http_request(
                    extend_url,
                    method="POST",
                    headers=auth_headers,
                    body=json.dumps({"finale": True, "num_chapters": 3}).encode("utf-8"),
                )
                self.assertEqual(code, 200, raw_fin.decode("utf-8", errors="replace"))
                fin_data = json.loads(raw_fin.decode("utf-8"))
                self.assertTrue(fin_data["finale"])
                self.assertTrue(fin_data["is_final_volume"])
                self.assertEqual(fin_data["start_chapter"], 8)
                self.assertEqual(fin_data["end_chapter"], 10)
                self.assertEqual(fin_data["total_chapters"], 10)
                self.assertIsInstance(fin_data["volume_file_path"], str)
            finally:
                srv.shutdown_and_close()
                srv.db.close()

    def test_x_real_ip_spoofing_from_non_loopback_cannot_bypass_429_lockout(self) -> None:
        mono_clock = [2000.0]
        cst_now = datetime(2026, 11, 18, 16, 0, 0, tzinfo=SHANGHAI_TZ)
        admin_token = "spoof-test-admin-token-24plus-chars"

        class _RemotePeerHandler(MatrixRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.client_address = ("203.0.113.5", 45678)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # 1. Non-loopback remote peer rotating X-Real-IP MUST still be locked out on 5th attempt
            db = StockpileDB(root / "remote.db", now_fn=lambda: cst_now)
            scheduler = MatrixScheduler(db=db, projects_root=root / "projects")
            remote_srv = MatrixHTTPServer(
                ("127.0.0.1", 0),
                scheduler=scheduler,
                admin_auth=AdminAuthManager(
                    admin_token=admin_token,
                    now_fn=lambda: cst_now,
                    monotonic_fn=lambda: mono_clock[0],
                ),
                companion_auth=CompanionAuthManager(
                    "comp-secret-spoof-test",
                    now_fn=lambda: cst_now,
                ),
                now_fn=lambda: cst_now,
            )
            remote_srv.RequestHandlerClass = _RemotePeerHandler
            thread = threading.Thread(target=remote_srv.serve_forever, daemon=True)
            thread.start()
            remote_url = f"http://127.0.0.1:{remote_srv.server_port}"
            try:
                statuses: List[int] = []
                last_err: Dict[str, Any] = {}
                for i in range(5):
                    code, _, raw = _http_request(
                        f"{remote_url}{API_BOOKS}",
                        headers={
                            "Authorization": f"Bearer bad-{i}",
                            "X-Real-IP": f"10.0.0.{i}",
                        },
                    )
                    statuses.append(code)
                    if code == 429:
                        last_err = json.loads(raw.decode("utf-8"))
                self.assertEqual(statuses, [401, 401, 401, 401, 429])
                self.assertEqual(last_err["error_code"], "TOO_MANY_AUTH_FAILURES")
                self.assertEqual(last_err["detail"]["client_ip"], "203.0.113.5")
            finally:
                remote_srv.shutdown_and_close()
                db.close()

            # 2. Trusted loopback proxy (127.0.0.1) DOES honor X-Real-IP per client
            proxy_srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=root / "proxy.db",
                projects_root=root / "projects2",
                admin_token=admin_token,
                companion_secret="comp-secret-proxy-test",
                now_fn=lambda: cst_now,
                monotonic_fn=lambda: mono_clock[0],
            )
            proxy_thread = threading.Thread(target=proxy_srv.serve_forever, daemon=True)
            proxy_thread.start()
            proxy_url = f"http://127.0.0.1:{proxy_srv.server_port}"
            try:
                proxy_statuses: List[int] = []
                for i in range(5):
                    code, _, _ = _http_request(
                        f"{proxy_url}{API_BOOKS}",
                        headers={
                            "Authorization": f"Bearer bad-{i}",
                            "X-Real-IP": "198.51.100.10",
                        },
                    )
                    proxy_statuses.append(code)
                self.assertEqual(proxy_statuses, [401, 401, 401, 401, 429])
                # Different proxied client IP is NOT locked out
                code, _, _ = _http_request(
                    f"{proxy_url}{API_BOOKS}",
                    headers={
                        "Authorization": f"Bearer {admin_token}",
                        "X-Real-IP": "198.51.100.11",
                    },
                )
                self.assertEqual(code, 200)
            finally:
                proxy_srv.shutdown_and_close()
                proxy_srv.db.close()

    def test_auto_create_book_rejects_path_traversal_and_numeric_snapshot_sort(self) -> None:
        class _DummyPlannerProvider(LLMProvider):
            def __init__(self) -> None:
                self.last_metadata: Dict[str, Any] = {}

            def generate(self, request: LLMRequest) -> LLMResponse:
                self.last_metadata = dict(request.metadata or {})
                meta = self.last_metadata
                start_ch = int(meta.get("start_chapter", 1))
                end_ch = int(meta.get("end_chapter", start_ch))
                target_vol = int(meta.get("target_volume", 1))
                scenes = [
                    {
                        "chapter": ch,
                        "scene_id": f"s_{ch:04d}",
                        "internal_title": f"第{ch}幕冲突",
                        "public_title": f"天价反噬第{ch}波",
                        "pov_character": "mc",
                        "active_characters": ["mc", "antag"],
                        "active_assets": ["work_badge"],
                        "style_profile_ids": ["urban_fast"],
                        "location_time": "海城新大厦",
                        "location_id": "loc_cbd_2",
                        "events": ["反击抢功者"],
                        "stop_condition": "悬念收尾",
                        "min_chars": 600,
                        "max_chars": 4000,
                        "emotion_polarity": "burst" if ch % 2 == 1 else "neutral",
                        "volume": target_vol,
                    }
                    for ch in range(start_ch, end_ch + 1)
                ]
                return LLMResponse(
                    request_id=request.request_id,
                    content=json.dumps(
                        {
                            "map_name": "海城金融中心",
                            "location_id": "loc_cbd_2",
                            "soft_identity_reboot": "资本操盘手新身份登场",
                            "volume_cap_locks": [
                                {"ledger_key": "cash", "volume": target_vol, "max_value": 50000000.0, "unit": "元"}
                            ],
                            "scenes": scenes,
                        },
                        ensure_ascii=False,
                    ),
                    model="test-planner",
                    provider="test",
                )

        admin_token = "traversal-test-token-24plus-chars"
        provider = _DummyPlannerProvider()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            projects_root = root / "projects"
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=root / "stockpile.db",
                projects_root=projects_root,
                admin_token=admin_token,
                companion_secret="comp-secret-traversal",
                provider=provider,
            )
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{srv.server_port}"
            headers = {
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
            }
            try:
                # 1. HTTP POST /api/books/auto-create rejects "../outside"
                code, _, raw_err = _http_request(
                    f"{base_url}{API_BOOKS_AUTO_CREATE}",
                    method="POST",
                    headers=headers,
                    body=json.dumps(
                        {"project_id": "../outside", "genre_id": "urban_brainhole", "author_level": "lv0_1"}
                    ).encode("utf-8"),
                )
                self.assertEqual(code, 400)
                self.assertEqual(json.loads(raw_err.decode("utf-8"))["error_code"], "BAD_REQUEST")
                self.assertFalse((root / "outside").exists())

                for bad_id in ("sub/dir", "sub\\dir", "..", "."):
                    with self.assertRaises(ProjectValidationError):
                        srv.scheduler.auto_create_book(
                            AutoCreateBookRequest(project_id=bad_id, genre_id="urban_brainhole")
                        )
                with self.assertRaises(ValueError):
                    srv.scheduler.auto_create_book(
                        AutoCreateBookRequest(project_id="", genre_id="urban_brainhole")
                    )

                # 2. Numeric snapshot sorting (chapter_99_after.json vs chapter_0100_after.json)
                snap_book_dir = _create_minimal_project(
                    projects_root / "snap_book", project_id="snap_book", title="快照排序测试", num_chapters=4
                )
                srv.scheduler.register_project(snap_book_dir, account_id="acc_snap")
                snaps_dir = snap_book_dir / "output" / "snapshots"
                snaps_dir.mkdir(parents=True, exist_ok=True)
                (snaps_dir / "chapter_99_after.json").write_text(
                    json.dumps({"current_chapter": 99, "current_volume": 1}),
                    encoding="utf-8",
                )
                (snaps_dir / "chapter_0100_after.json").write_text(
                    json.dumps({"current_chapter": 100, "current_volume": 1}),
                    encoding="utf-8",
                )
                latest_snap = _select_latest_snapshot_file(snaps_dir)
                self.assertIsNotNone(latest_snap)
                self.assertEqual(latest_snap.name, "chapter_0100_after.json")

                ext_res = srv.scheduler.extend_book_volume("snap_book", batch_size=2)
                self.assertEqual(ext_res["start_chapter"], 101)
                self.assertEqual(ext_res["end_chapter"], 102)
            finally:
                srv.shutdown_and_close()
                srv.db.close()


if __name__ == "__main__":
    unittest.main()
