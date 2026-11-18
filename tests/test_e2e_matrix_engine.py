from __future__ import annotations

import base64
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.backup import (
    BackupManifestError,
    REDACTED_PLACEHOLDER,
    export_backup,
    import_backup,
    verify_backup,
)
from xiaoshuo_bot.cli import main as cli_main
from xiaoshuo_bot.contracts import (
    ALL_API_ROUTES,
    ALL_SSE_EVENTS,
    API_BOOK_CHAPTER_LOCK,
    AutoCreateBookRequest,
    BookRunStatus,
    BookSummaryResponse,
    CompanionCaptchaReport,
    CompanionLeaseResponse,
    CompanionMetricsRequest,
    CompanionReceiptRequest,
    ErrorPayload,
    HMACReplayError,
    HMACSignatureError,
    HMACTimestampError,
    MatrixControlPort,
    MatrixDailyDigest,
    SSEEventPayload,
    StockpileStatus,
    VALID_STOCKPILE_TRANSITIONS,
    compute_hmac_headers,
    format_api_route,
    format_sse_frame,
    verify_hmac_headers,
)
from xiaoshuo_bot.cover import COVER_HEIGHT, COVER_WIDTH, generate_cover_for_project
from xiaoshuo_bot.engine import (
    LockedChapterError,
    MissingPredecessorSnapshotError,
    NovelGenerationEngine,
)
from xiaoshuo_bot.evolution import MotifWeightStore
from xiaoshuo_bot.exporter import (
    export_qimao_bundle,
    export_tomato_bundle,
    export_zhihu_yanxuan_bundle,
)
from xiaoshuo_bot.llm import RoleEndpointConfig, RoleModelRouter, UsageBudgetLedger
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.models import ChapterArtifact, SHANGHAI_TZ
from xiaoshuo_bot.planner import AutoNovelPlanner
from xiaoshuo_bot.reporter import generate_html_dashboard, generate_sharded_html_dashboards
from xiaoshuo_bot.transcreator import (
    WebnovelTranscreator,
    build_glossary,
    export_english_serialization_bundle,
)


def _make_clean_png_bytes() -> bytes:
    img = Image.new("RGB", (600, 800), (24, 38, 66))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_clean_first_person_chapter(chapter_no: int) -> str:
    """Build clean first-person Tomato urban-brainhole prose passing all AITicChecker 2.0 rules."""
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
            "一小时后，对家风投的林总亲自打来电话，开价五千万收购我的专利。",
            "我站在大夏海城金融大厦三十六层落地窗前，指尖轻轻敲着合同封面。",
            "电话那头突然传来一阵急促的敲门声，林总压低声音说总部刚换了新老板。",
        ]
    # Ensure sufficient length (>= 1600 chars) by adding varied crisp action/dialogue blocks
    extra_blocks = [
        "大厅中央的电子钟跳过五点整，玻璃幕墙外车流如织，霓虹灯一盏接一盏亮起来。",
        "我低头核对手机银行刚发来的托管回执，每一笔流水都带着合规风控编码。",
        "“陈先生，这是您的贵宾通行卡。”前台经理双手递过一张黑金门禁卡。",
        "我接过卡放进西装内袋，顺手按下三十八楼的专属直达按钮。",
        "电梯平稳上行，金属轿厢壁映出我平静的侧脸，楼层数字一路跳到顶层。",
        "顶层会议室大门敞开着，长桌尽头坐着三位来自风投基金的合伙人。",
        "“条件我们看过了，”坐在中间的李合伙人推过一份股权转让书，“只要你今天签字，明早开盘前资金到位。”",
        "我翻开第三页附加条款，用钢笔划掉其中两行限制研发权限的霸王条目。",
        "“这两条改掉，我立刻签。”我把钢笔往桌上一搁，抬头看向对方。",
        "两位副手交换了一下眼色，李合伙人沉默三秒后直接点头同意修改。",
        "打印机嗡嗡作响，三分钟后重新装订好的协议摆回我面前的红木桌面。",
        "我拔开笔帽落笔签下名字，指腹压在印泥上，在甲方落款处按下一枚清晰的指印。",
        "窗外夜风掠过楼顶停机坪，远处的跨江大桥亮起一排橘黄色路灯。",
        "“合作愉快，不过明天上午十点的董事会，”李合伙人收起合同，“旧东家那边恐怕会派人来闹场。”",
        "我拿起桌上的冰美式喝了一口，手机屏幕正好弹出一条陌生号码发来的匿名威胁短信。",
    ]
    idx = 0
    while len("".join(paras)) < 1750:
        paras.append(extra_blocks[idx % len(extra_blocks)])
        idx += 1
    return "\n\n".join(paras)


def _build_faulty_first_chapter() -> str:
    """Deliberately triggers HOOK_001 + SAFE_002 + TIC_004 for gate-rewrite verification."""
    return (
        "窗外下着淅淅沥沥的小雨，灰蒙蒙的天空让人心里有些压抑。\n\n"
        "我叫陈牧野，今年二十五岁，毕业于某大学，平时在公司做算法工作。\n\n"
        "赵明冷笑一声，我眸光微闪，掏出手机准备报警找警*察处理。"
    )


class _MatrixSimState:
    def __init__(self, *, first_ch_faulty: bool = True) -> None:
        self.lock = threading.Lock()
        self.first_ch_faulty = first_ch_faulty
        self.writer_calls: dict[int, int] = {}
        self.png_bytes = _make_clean_png_bytes()

    def handle_chat(self, payload: dict) -> str:
        messages = payload.get("messages", [])
        sys_txt = ""
        usr_txt = ""
        for m in messages:
            if m.get("role") == "system":
                sys_txt = str(m.get("content", ""))
            elif m.get("role") == "user":
                usr_txt += str(m.get("content", ""))

        # 1. AutoNovelPlanner: plan_new_project
        if "番茄爆款总策划师" in sys_txt:
            return self._build_planner_json(usr_txt)

        # 2. AutoNovelPlanner: extend_volume
        if "滚动续纲策划师" in sys_txt:
            return self._build_extend_json(usr_txt)

        # 3. TomatoCriticPolisher: evaluate_chapter
        if "毒舌主编" in sys_txt:
            return json.dumps(
                {
                    "first_screen_hook": 88,
                    "cool_point_density": 84,
                    "comment_interactivity": 82,
                    "pacing_drag": 71,
                    "overall_score": 81.25,
                    "weak_paragraphs": [2],
                    "paragraph_reasons": {"2": "动作不够干脆，需要压缩"},
                    "critique": "首屏冲突够狠，第3段可再利落一点。",
                },
                ensure_ascii=False,
            )

        # 4. TomatoCriticPolisher: surgical_polish
        if "手术刀精修编辑" in sys_txt:
            return json.dumps(
                {
                    "replacements": {
                        "2": "他抬手就把我熬夜写的算法底稿，当众推给了旁边的关系户。"
                    }
                },
                ensure_ascii=False,
            )

        # 5. TomatoCriticPolisher: generate_author_note
        if "互动运营作者" in sys_txt:
            return "审计专员突然堵门，大家猜赵明下一步会怎么求饶？点个催更明天继续打脸！"

        # 6. WebnovelTranscreator
        if "Webnovel cultural transcreator" in sys_txt:
            if "Chapter 1" in usr_txt:
                return (
                    "Chapter 1: Tenfold Rebate at Resignation\n\n"
                    "When Mr. Zhao, the HR manager, slammed the termination notice onto the glass desk, "
                    "I only had forty-five dollars left in my pocket.\n\n"
                    "A translucent prompt flickered before my eyes: malicious credit theft detected, "
                    "tenfold corporate rebate unlocked."
                )
            return (
                "Chapter 2: Audit at the Elevator\n\n"
                "Auditor Zhou Cheng spread the red suspension order across the corridor table, "
                "blocking Zhao Ming's path.\n\n"
                "Within an hour, the rival venture fund offered eight million dollars for my patent."
            )

        # 7. Writer LLM (chapters)
        ch_no = 1
        if "第 2 章" in usr_txt or "ch02_" in usr_txt or "Chapter 2" in usr_txt:
            ch_no = 2
        elif "第 3 章" in usr_txt or "ch03_" in usr_txt:
            ch_no = 3

        with self.lock:
            cnt = self.writer_calls.get(ch_no, 0) + 1
            self.writer_calls[ch_no] = cnt

        if ch_no == 1 and self.first_ch_faulty and cnt == 1:
            return _build_faulty_first_chapter()
        return _build_clean_first_person_chapter(ch_no)

    @staticmethod
    def _build_planner_json(usr_txt: str) -> str:
        # Extract suggested_cast names passed by AutoNovelPlanner
        import re

        m = re.search(r"推荐安全角色名池[^:：]*[:：]\s*(.+)", usr_txt)
        names = [n.strip() for n in m.group(1).split(",") if n.strip()] if m else []
        while len(names) < 3:
            names.extend(["陈牧野", "周成", "赵明"])
        pov_name = names[0]
        ally_name = names[1]
        rival_name = names[2]

        scenes = []
        for ch in range(1, 16):
            pol = "burst" if ch % 2 == 1 else "suppress"
            scenes.append(
                {
                    "chapter": ch,
                    "scene_id": f"ch{ch:02d}_scene",
                    "internal_title": f"第{ch}章职场逆袭节点",
                    "public_title": f"被裁当天系统到账{ch}百万",
                    "pov_character": "mc",
                    "active_characters": ["mc", "ally", "rival"],
                    "active_assets": ["badge"],
                    "style_profile_ids": ["urban_brainhole_human"],
                    "location_time": "海城金融大厦写字楼",
                    "location_id": f"loc_{(ch - 1) % 3 + 1}",
                    "events": [f"第{ch}章主角用系统账单反击{rival_name}"],
                    "stop_condition": "反派吃瘪退场并引出下一环",
                    "min_chars": 1500,
                    "max_chars": 4500,
                    "beat_type": "打脸反转" if ch % 2 == 1 else "危机加码",
                    "emotion_polarity": pol,
                    "cliffhanger_hook": "更高层审计电话打进办公室",
                    "segment_comment_cue": "系统余额暴涨瞬间",
                    "locked": (ch == 1),
                    "volume": 1,
                }
            )
        master_outline = [
            {"chapter": i, "title": f"总纲节点{i}", "summary": f"第{i}章核心爽点推进"}
            for i in range(1, 101)
        ]
        return json.dumps(
            {
                "project": {
                    "title": "被裁当天我刷出百亿",
                    "default_pov_character": "mc",
                    "alternate_titles": [
                        "十倍返现从被裁开始",
                        "我的工位能刷出百亿",
                        "辞退当天我买下大楼",
                        "职场反噬账单到账了",
                        "被抢功劳后我暴富了",
                    ],
                    "synopsis": (
                        "实习转正前夜，陈牧野被主管当众抢走核心算法，兜里只剩三百块。"
                        "危急关头他绑定十倍反噬账单系统，每次拆穿职场黑幕就能十倍返现。"
                        "从买下整层写字楼到控股风投基金，昔日欺辱他的人只能跪在电梯口颤抖。"
                    ),
                    "alternate_blurbs": [
                        "被裁当天，系统绑定十倍返现，拆穿一次职场黑幕到账百万，一路横扫商界。",
                        "方案被抢、工位被清，他反手晒出时间戳铁证，十倍账单到账直接买下总部大楼。",
                        "兜里只剩三百块却激活天降反噬账单，欺负过他的人越嚣张，他账户里的零就越多。",
                    ],
                    "tweet_aliases": ["被裁暴富录", "工位刷百亿", "反噬账单局"],
                    "primary_category": "都市脑洞",
                    "tags": ["系统", "神豪", "职场逆袭"],
                    "opening_motif": "被抢功劳触发十倍返现",
                    "golden_finger_type": "职场反噬账单系统",
                    "target_word_count": 220000,
                },
                "golden_finger_description": "每次拆穿职场或商业黑幕按涉案价值十倍返现",
                "characters": [
                    {
                        "id": "mc",
                        "name": pov_name,
                        "role": "protagonist",
                        "personality": "冷静果断，有仇必报",
                        "speech_style": "短句带刺",
                        "initial_status": "alive",
                    },
                    {
                        "id": "ally",
                        "name": ally_name,
                        "role": "ally",
                        "personality": "公私分明",
                        "speech_style": "干练克制",
                        "initial_status": "alive",
                    },
                    {
                        "id": "rival",
                        "name": rival_name,
                        "role": "antagonist",
                        "personality": "贪婪跋扈",
                        "speech_style": "阴阳怪气",
                        "initial_status": "alive",
                    },
                ],
                "assets": [
                    {
                        "id": "badge",
                        "name": "员工工牌",
                        "location": "主角手中",
                        "holder": pov_name,
                        "condition": "完好",
                        "physical_details": "蓝带磁卡工牌",
                    }
                ],
                "foreshadows": [
                    {
                        "id": "FS-01",
                        "title": "十倍反噬账单首次结算",
                        "plant_chapter": 1,
                        "due_chapter": 2,
                        "plant_instruction": "第1章主角绑定十倍反噬账单系统",
                        "payoff_instruction": "第2章审计专员拿停职令堵门回收",
                        "plant_keywords": ["十倍反噬"],
                        "payoff_keywords": ["停职令"],
                        "cross_volume": False,
                    }
                ],
                "style_profiles": [
                    {
                        "id": "urban_brainhole_human",
                        "name": "都市脑洞白描",
                        "granularity_notes": "竖屏短段",
                        "sample_text": "人事主管把辞退通知推过玻璃桌时，我兜里只剩三百块。",
                    }
                ],
                "volume_cap_locks": [
                    {"ledger_key": "cash_wan", "volume": 1, "max_value": 10000, "unit": "万元"}
                ],
                "scenes": scenes,
                "master_outline": master_outline,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _build_extend_json(usr_txt: str) -> str:
        import re

        m = re.search(r"第\s*(\d+)\s*章\s*～\s*第\s*(\d+)\s*章", usr_txt)
        start_ch = int(m.group(1)) if m else 16
        end_ch = int(m.group(2)) if m else 17
        vol_m = re.search(r"target_volume=(\d+)", usr_txt)
        vol = int(vol_m.group(1)) if vol_m else 1
        scenes = []
        for ch in range(start_ch, end_ch + 1):
            scenes.append(
                {
                    "chapter": ch,
                    "scene_id": f"ch{ch:02d}_ext",
                    "internal_title": f"续纲第{ch}章",
                    "public_title": f"控股总部第{ch}局",
                    "pov_character": "mc",
                    "active_characters": ["mc", "ally"],
                    "active_assets": ["badge"],
                    "style_profile_ids": ["urban_brainhole_human"],
                    "location_time": "海城国际资本中心",
                    "location_id": f"loc_ext_{vol}",
                    "events": ["新地图资本并购战"],
                    "stop_condition": "签署控股协议",
                    "min_chars": 1500,
                    "max_chars": 4500,
                    "beat_type": "商业反杀",
                    "emotion_polarity": "burst" if ch % 2 == 1 else "neutral",
                    "cliffhanger_hook": "跨洋对手发出挑战书",
                    "volume": vol,
                }
            )
        return json.dumps(
            {
                "map_name": "海城资本中心",
                "location_id": f"loc_ext_{vol}",
                "soft_identity_reboot": "以独立风投合伙人身份入局新商圈",
                "volume_cap_locks": [
                    {"ledger_key": "cash_wan", "volume": vol, "max_value": 50000 * vol, "unit": "万元"}
                ],
                "scenes": scenes,
            },
            ensure_ascii=False,
        )


def _start_sim_cpa_server(state: _MatrixSimState) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))

            # Gemini native :generateContent image generation endpoint
            if ":generateContent" in self.path:
                b64 = base64.b64encode(state.png_bytes).decode("ascii")
                resp_body = json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"text": "clean background"},
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": b64,
                                            }
                                        },
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                return

            content = state.handle_chat(payload)
            is_stream = bool(payload.get("stream", False))
            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                self.wfile.write(b": keepalive\n\n")
                chunk1 = json.dumps(
                    {
                        "id": "chatcmpl-sim",
                        "model": payload.get("model", "sim-model"),
                        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                    },
                    ensure_ascii=False,
                )
                chunk2 = json.dumps(
                    {
                        "id": "chatcmpl-sim",
                        "model": payload.get("model", "sim-model"),
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 360},
                    },
                    ensure_ascii=False,
                )
                self.wfile.write(f"data: {chunk1}\n\n".encode("utf-8"))
                self.wfile.write(f"data: {chunk2}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            resp_body = json.dumps(
                {
                    "id": "chatcmpl-sim",
                    "model": payload.get("model", "sim-model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 360},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


class TestContractsAndBackupAndReporter(unittest.TestCase):
    def test_frozen_contracts_hmac_sse_routes_and_dataclasses(self) -> None:
        self.assertIn("/api/health", ALL_API_ROUTES)
        self.assertIn("/api/companion/poll", ALL_API_ROUTES)
        self.assertIn("/api/events/stream", ALL_API_ROUTES)
        self.assertEqual(
            format_api_route(API_BOOK_CHAPTER_LOCK, book_id="book_1", chapter=3),
            "/api/books/book_1/chapters/3/lock",
        )
        self.assertEqual(len(StockpileStatus), 7)
        self.assertIn(StockpileStatus.LEASED, VALID_STOCKPILE_TRANSITIONS[StockpileStatus.STOCKPILED])
        self.assertIn("chapter_stockpiled", ALL_SSE_EVENTS)

        # HMAC sign + verify + ±60s skew + replay nonce + tampered body
        secret = "super-secret-matrix-hmac-key"
        body = json.dumps({"book_id": "b1", "chapter": 1}, ensure_ascii=False)
        fixed_now = int(datetime.now(SHANGHAI_TZ).timestamp())
        headers = compute_hmac_headers(
            secret,
            "POST",
            "/api/companion/lease",
            body,
            client_id="win-pc-01",
            timestamp=fixed_now,
            nonce="nonce_abcdef123456",
        )
        seen_nonces: dict[str, float] = {}
        lower_headers = {k.lower(): v for k, v in headers.items()}
        cid, ts, nonce = verify_hmac_headers(
            secret,
            "POST",
            "/api/companion/lease",
            lower_headers,
            body,
            now=fixed_now + 10,
            seen_nonces=seen_nonces,
        )
        self.assertEqual((cid, ts, nonce), ("win-pc-01", fixed_now, "nonce_abcdef123456"))

        # Replay rejected
        with self.assertRaises(HMACReplayError):
            verify_hmac_headers(
                secret,
                "POST",
                "/api/companion/lease",
                headers,
                body,
                now=fixed_now + 12,
                seen_nonces=seen_nonces,
            )

        # Skew > 60s rejected
        with self.assertRaises(HMACTimestampError):
            verify_hmac_headers(
                secret,
                "POST",
                "/api/companion/lease",
                headers,
                body,
                now=fixed_now + 120,
            )

        # Tampered body rejected
        with self.assertRaises(HMACSignatureError):
            verify_hmac_headers(
                secret,
                "POST",
                "/api/companion/lease",
                headers,
                '{"book_id":"b1","chapter":99}',
                now=fixed_now,
            )

        # SSE frame formatting & dataclasses roundtrip
        sse_payload = SSEEventPayload(
            event="chapter_stockpiled",
            book_id="b1",
            chapter=1,
            message="ok",
            event_id="evt-1",
        )
        frame = sse_payload.to_sse_frame()
        self.assertIn("event: chapter_stockpiled", frame)
        self.assertIn("id: evt-1", frame)
        self.assertTrue(frame.endswith("\n\n"))
        self.assertIn("event: heartbeat", format_sse_frame("heartbeat", {"ok": True}))

        # Dataclass roundtrips
        req = AutoCreateBookRequest(project_id="b1", genre_id="urban_brainhole", author_level="lv2_plus")
        self.assertEqual(AutoCreateBookRequest.from_dict(req.to_dict()).genre_id, "urban_brainhole")
        summary = BookSummaryResponse(book_id="b1", title="测试书名", status=BookRunStatus.ACTIVE.value)
        self.assertEqual(BookSummaryResponse.from_dict(summary.to_dict()).title, "测试书名")
        lease = CompanionLeaseResponse(
            task_id="t1",
            task_type="PUBLISH_CHAPTER",
            book_id="b1",
            book_title="测试书名",
            author_name="笔名",
            fanqie_book_id="fq1",
            chapter=1,
            public_title="开局",
            body="正文",
        )
        self.assertEqual(CompanionLeaseResponse.from_dict(lease.to_dict()).chapter, 1)
        receipt = CompanionReceiptRequest(
            task_id="t1",
            book_id="b1",
            chapter=1,
            account_id="acc1",
            status=StockpileStatus.SCHEDULED.value,
        )
        self.assertEqual(CompanionReceiptRequest.from_dict(receipt.to_dict()).status, "SCHEDULED")
        metrics = CompanionMetricsRequest(
            book_id="b1",
            account_id="acc1",
            settled=True,
            settlement_status="SETTLED",
            ch2_read_through=0.65,
        )
        self.assertEqual(CompanionMetricsRequest.from_dict(metrics.to_dict()).ch2_read_through, 0.65)
        cap = CompanionCaptchaReport(account_id="acc1", book_id="b1", chapter=1)
        self.assertEqual(CompanionCaptchaReport.from_dict(cap.to_dict()).reason, "CAPTCHA_DETECTED")
        digest = MatrixDailyDigest(total_books=1, active_books=1, book_summaries=[summary])
        self.assertEqual(MatrixDailyDigest.from_dict(digest.to_dict()).total_books, 1)
        err = ErrorPayload(error_code="ERR", message="boom")
        self.assertEqual(ErrorPayload.from_dict(err.to_dict()).error_code, "ERR")
        self.assertTrue(hasattr(MatrixControlPort, "confirm_cut_book"))

    def test_sharded_html_dashboard_over_30_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            chapters_dir = root / "chapters"
            chapters_dir.mkdir(parents=True)
            chapters_meta = []
            marker_body_token = "UNIQUE_RAW_BODY_SENTINEL_CH_35_DO_NOT_EMBED_IN_INDEX"
            for ch in range(1, 36):
                vol = 1 if ch <= 20 else 2
                body = f"第{ch}章正文内容。" + (marker_body_token if ch == 35 else "普通片段。") * 20
                (chapters_dir / f"chapter_{ch:04d}.txt").write_text(body, encoding="utf-8")
                chapters_meta.append(
                    {
                        "chapter": ch,
                        "volume": vol,
                        "scene_id": f"s_{ch}",
                        "internal_title": f"内部{ch}",
                        "public_title": f"公开标题{ch}",
                        "pov_character": "主角",
                        "char_count": len(body),
                        "paragraph_count": 2,
                        "attempts_used": 1,
                        "rewrites_triggered": 0,
                        "opening_preview": body[:24],
                        "ending_preview": body[-24:],
                        "state_transitions": {"asset_updates": []},
                    }
                )

            manifest = {
                "project": {"project_id": "long_book", "title": "百万长篇分片测试"},
                "total_chars": sum(c["char_count"] for c in chapters_meta),
                "chapter_count": 35,
                "incidents": [],
                "final_world_state": {"assets": {}, "foreshadows": {}},
                "chapters": chapters_meta,
            }
            res = generate_sharded_html_dashboards(manifest, chapters_dir, output_dir=root)
            self.assertTrue(res["sharded"])
            self.assertEqual(len(res["shard_paths"]), 2)
            self.assertNotIn(marker_body_token, res["index_html"])
            self.assertIn("volume_0001.html", res["index_html"])
            self.assertIn("volume_0002.html", res["index_html"])
            self.assertIn("dashboard_shards/", res["index_html"])
            self.assertIn(marker_body_token, (root / "dashboard_shards" / "volume_0002.html").read_text(encoding="utf-8"))

            # Direct generate_html_dashboard also returns lightweight index
            idx_html = generate_html_dashboard(manifest, chapters_dir, output_dir=root)
            self.assertNotIn(marker_body_token, idx_html)


class TestE2EMatrixEngine(unittest.TestCase):
    def test_full_m1_matrix_pipeline_end_to_end_and_guards(self) -> None:
        sim_state = _MatrixSimState(first_ch_faulty=True)
        server, base_url = _start_sim_cpa_server(sim_state)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                ws = Path(tmpdir)
                projects_root = ws / "projects"
                projects_root.mkdir()
                data_dir = ws / "data"
                data_dir.mkdir()

                ledger = UsageBudgetLedger(
                    daily_budget_limit=50.0,
                    daily_chapter_limit=50,
                    state_path=data_dir / "usage_ledger.json",
                )
                v1_url = f"{base_url}/v1"
                router = RoleModelRouter(
                    {
                        "planner": RoleEndpointConfig(
                            role="planner",
                            base_url=v1_url,
                            model="sim-planner",
                            api_keys=["sk-sim-planner-secret-1111"],
                        ),
                        "writer": RoleEndpointConfig(
                            role="writer",
                            base_url=v1_url,
                            model="sim-writer",
                            api_keys=["sk-sim-writer-secret-2222"],
                        ),
                        "critic": RoleEndpointConfig(
                            role="critic",
                            base_url=v1_url,
                            model="sim-critic",
                            api_keys=["sk-sim-critic-secret-3333"],
                        ),
                        "polisher": RoleEndpointConfig(
                            role="polisher",
                            base_url=v1_url,
                            model="sim-polisher",
                            api_keys=["sk-sim-polisher-secret-4444"],
                        ),
                        "image": RoleEndpointConfig(
                            role="image",
                            base_url=base_url,
                            model="gemini-2.5-flash-image",
                            api_keys=["AIzaSimImageSecret5555"],
                            provider_type="gemini_native",
                        ),
                    },
                    ledger=ledger,
                )

                # 1. Motif weights + 3-source AutoNovelPlanner (lv2_plus male urban_brainhole)
                motif_store = MotifWeightStore(data_dir / "motif_weights.json")
                motif_store.record_outcome(
                    "urban_brainhole",
                    "被抢功劳触发十倍返现",
                    "职场反噬账单系统",
                    success=True,
                    ch2_read_through=0.68,
                )
                motif_store.save()

                book_dir = projects_root / "urban_rebate"
                planner = AutoNovelPlanner(router)
                planned = planner.plan_new_project(
                    project_id="urban_rebate",
                    genre_id="urban_brainhole",
                    output_dir=book_dir,
                    author_level="lv2_plus",
                    projects_root=projects_root,
                    pen_name="江风行",
                    hot_keywords=["十倍返现", "反噬账单"],
                    motif_store=motif_store,
                    seed=42,
                )
                self.assertTrue((book_dir / "signing_application.md").is_file())
                self.assertTrue((book_dir / "master_outline.yaml").is_file())

                # Update project.yaml to first_person + allow_system_tag so engine tests male 1st-person system flow
                import yaml

                proj_raw = yaml.safe_load((book_dir / "project.yaml").read_text(encoding="utf-8"))
                proj_raw["pov_mode"] = "first_person"
                proj_raw["panel_policy"] = "allow_system_tag"
                (book_dir / "project.yaml").write_text(
                    yaml.safe_dump(proj_raw, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                project = load_project(book_dir)
                self.assertEqual(project.meta.pov_mode, "first_person")
                self.assertEqual(project.meta.panel_policy, "allow_system_tag")

                # 2. Missing predecessor snapshot guard: --chapters 3 without Ch.2 snapshot must raise!
                out_dir = book_dir / "output"
                engine_guard = NovelGenerationEngine(
                    project=project,
                    provider=router,
                    output_dir=out_dir,
                )
                with self.assertRaises(MissingPredecessorSnapshotError):
                    engine_guard.run(target_chapters=[3])

                # 3. Generate Chapter 1 with fault-injection rewrite + critic + surgical polish + author_note + on_progress
                progress_events: list[tuple[str, dict]] = []
                engine1 = NovelGenerationEngine(
                    project=project,
                    provider=router,
                    output_dir=out_dir,
                    on_progress=lambda ev, data: progress_events.append((ev, data)),
                )
                man1 = engine1.run(target_chapters=[1])
                self.assertEqual(man1["chapter_count"], 1)
                self.assertEqual(man1["total_incidents_detected"], 1)
                self.assertEqual(man1["chapters"][0]["attempts_used"], 2)
                self.assertEqual(man1["chapters"][0]["polish_summary"]["replaced_indices"], [2])
                self.assertTrue(man1["chapters"][0]["author_note"])
                self.assertTrue(man1["chapters"][0]["locked"])

                ch1_file = out_dir / "chapters" / "chapter_0001.txt"
                ch1_meta_file = out_dir / "chapters" / "chapter_0001.meta.json"
                self.assertTrue(ch1_file.is_file())
                self.assertTrue(ch1_meta_file.is_file())
                self.assertTrue((out_dir / "snapshots" / "chapter_0000_initial.json").is_file())
                self.assertTrue((out_dir / "snapshots" / "chapter_0001_after.json").is_file())
                self.assertTrue((out_dir / "snapshots" / "memory_0001_after.json").is_file())
                self.assertIn("当众推给了旁边的关系户", ch1_file.read_text(encoding="utf-8"))

                emitted_names = [ev for ev, _ in progress_events]
                for expected_ev in (
                    "chapter_started",
                    "gate_failed",
                    "gate_passed",
                    "critic_scored",
                    "surgical_polished",
                    "chapter_stockpiled",
                ):
                    self.assertIn(expected_ev, emitted_names)

                # 4. Locked chapter guard: re-running locked Chapter 1 without unlock=True must raise!
                engine_locked = NovelGenerationEngine(
                    project=project,
                    provider=router,
                    output_dir=out_dir,
                )
                with self.assertRaises(LockedChapterError):
                    engine_locked.run(target_chapters=[1])

                # 5. Partial --chapters 2 run: loads Ch.1 snapshot + memory, preserves initial snapshot & incidents.jsonl,
                #    and merges Ch.1 + Ch.2 into manuscript_pure.txt & manuscript_reader.md!
                incidents_before = (out_dir / "audit" / "incidents.jsonl").read_text(encoding="utf-8")
                init_snap_before = (out_dir / "snapshots" / "chapter_0000_initial.json").read_text(encoding="utf-8")

                engine2 = NovelGenerationEngine(
                    project=project,
                    provider=router,
                    output_dir=out_dir,
                )
                man2 = engine2.run(target_chapters=[2])
                self.assertEqual(man2["chapter_count"], 1)
                self.assertEqual(man2["all_completed_chapters_on_disk"], [1, 2])
                self.assertEqual(
                    (out_dir / "audit" / "incidents.jsonl").read_text(encoding="utf-8"),
                    incidents_before,
                )
                self.assertEqual(
                    (out_dir / "snapshots" / "chapter_0000_initial.json").read_text(encoding="utf-8"),
                    init_snap_before,
                )
                pure_full = (out_dir / "manuscript_pure.txt").read_text(encoding="utf-8")
                reader_full = (out_dir / "manuscript_reader.md").read_text(encoding="utf-8")
                self.assertIn("十倍反噬账单已绑定", pure_full)
                self.assertIn("审计专员周成", pure_full)
                self.assertIn("## 第 1 章", reader_full)
                self.assertIn("## 第 2 章", reader_full)

                # 6. Unlock overwrite archives displaced Ch.1 files into _archive/ (zero .unlink())
                engine_unlock = NovelGenerationEngine(
                    project=project,
                    provider=router,
                    output_dir=out_dir,
                )
                engine_unlock.run(target_chapters=[1], unlock=True)
                archived_files = list((out_dir / "_archive").rglob("chapter_0001.txt"))
                self.assertTrue(len(archived_files) >= 1)

                # 7. Extend volume via AutoNovelPlanner
                ext_res = planner.extend_volume(
                    book_dir,
                    snapshot_path=out_dir / "snapshots" / "chapter_0001_after.json",
                    batch_size=2,
                )
                self.assertEqual((ext_res.start_chapter, ext_res.end_chapter), (16, 17))
                self.assertEqual(len(ext_res.project.scenes), 17)

                # 8. 600x800 cover generation (AI image via CPA Gemini + procedural via CLI)
                ai_cover_path = ws / "cover_ai.png"
                ai_cov = generate_cover_for_project(
                    project,
                    ai_cover_path,
                    base_mode="ai_image",
                    router=router,
                )
                self.assertEqual((ai_cov.width, ai_cov.height), (COVER_WIDTH, COVER_HEIGHT))

                proc_cover_path = ws / "cover_proc.png"
                self.assertEqual(
                    cli_main(
                        [
                            "cover",
                            "--project",
                            str(book_dir),
                            "--output",
                            str(proc_cover_path),
                            "--base-mode",
                            "procedural",
                        ]
                    ),
                    0,
                )
                self.assertTrue(proc_cover_path.is_file())

                # 9. Multi-platform exports (Tomato, Qimao, Zhihu Yanxuan, English Webnovel)
                artifacts = []
                for ch_no in (1, 2):
                    txt = (out_dir / "chapters" / f"chapter_{ch_no:04d}.txt").read_text(encoding="utf-8")
                    meta = json.loads(
                        (out_dir / "chapters" / f"chapter_{ch_no:04d}.meta.json").read_text(encoding="utf-8")
                    )
                    artifacts.append(
                        ChapterArtifact(
                            chapter=ch_no,
                            public_title=meta["public_title"],
                            body=txt,
                            author_note=meta.get("author_note", ""),
                            sha256=meta["sha256"],
                            locked=meta["locked"],
                        )
                    )

                tomato_dir = ws / "exports" / "tomato"
                tomato_res = export_tomato_bundle(
                    meta=project.meta,
                    chapters=artifacts,
                    output_dir=tomato_dir,
                    characters=project.characters,
                    scenes=project.scenes,
                    cover_path=ai_cover_path,
                )
                self.assertTrue(tomato_res.zip_path.is_file())

                zhihu_dir = ws / "exports" / "zhihu"
                zhihu_res = export_zhihu_yanxuan_bundle(
                    meta=project.meta,
                    chapters=artifacts,
                    output_dir=zhihu_dir,
                    scenes=project.scenes,
                )
                self.assertIn("「", zhihu_res.manuscript_path.read_text(encoding="utf-8"))

                qimao_dir = ws / "exports" / "qimao"
                qimao_res = export_qimao_bundle(
                    meta=project.meta,
                    chapters=artifacts,
                    output_dir=qimao_dir,
                    scenes=project.scenes,
                )
                self.assertTrue(qimao_res.manuscript_path.is_file())

                glossary = build_glossary(project, title_en="Tenfold Rebate Ledger")
                transcreator = WebnovelTranscreator(glossary, router=router)
                en_chapters = [transcreator.transcreate_chapter(art) for art in artifacts]
                en_res = export_english_serialization_bundle(en_chapters, glossary, ws / "exports" / "webnovel")
                self.assertEqual(en_res.chapter_count, 2)

                # 10. Portable Backup export -> secret stripping -> verify -> tamper detect -> import restore
                # Place a config file with plaintext secret + an SQLite DB in data_dir
                (data_dir / "router_config.json").write_text(
                    json.dumps(
                        {
                            "api_key": "sk-plaintext-leak-999999",
                            "roles": {"writer": {"api_keys": ["rk-secret-token-888888"]}},
                        }
                    ),
                    encoding="utf-8",
                )
                db_path = data_dir / "stockpile.db"
                conn = sqlite3.connect(str(db_path))
                conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
                conn.execute("INSERT INTO t (val) VALUES ('safe_row')")
                conn.commit()
                conn.close()

                backup_zip = ws / "site_backup.zip"
                self.assertEqual(
                    cli_main(
                        [
                            "backup",
                            "export",
                            "--projects-dir",
                            str(projects_root),
                            "--data-dir",
                            str(data_dir),
                            "--output",
                            str(backup_zip),
                        ]
                    ),
                    0,
                )
                self.assertTrue(backup_zip.is_file())

                manifest_verified = verify_backup(backup_zip)
                self.assertGreater(manifest_verified.file_count, 5)

                # Confirm plaintext secrets were stripped inside the ZIP
                with zipfile.ZipFile(backup_zip, "r") as zf:
                    router_bak = zf.read("data/router_config.json").decode("utf-8")
                    self.assertNotIn("sk-plaintext-leak-999999", router_bak)
                    self.assertNotIn("rk-secret-token-888888", router_bak)
                    self.assertIn(REDACTED_PLACEHOLDER, router_bak)

                # Tamper detection test
                tampered_zip = ws / "tampered_backup.zip"
                with zipfile.ZipFile(backup_zip, "r") as zin, zipfile.ZipFile(tampered_zip, "w") as zout:
                    for item in zin.infolist():
                        data_bytes = zin.read(item.filename)
                        if item.filename == "data/motif_weights.json":
                            data_bytes = data_bytes + b"\n// tampered"
                        zout.writestr(item, data_bytes)
                with self.assertRaises(BackupManifestError):
                    verify_backup(tampered_zip)

                # Import restore onto target directory with pre-existing file (archives displaced file)
                restore_dir = ws / "restored_site"
                existing_file = restore_dir / "data" / "motif_weights.json"
                existing_file.parent.mkdir(parents=True, exist_ok=True)
                existing_file.write_text('{"old": true}', encoding="utf-8")

                imp_res = import_backup(backup_zip, restore_dir)
                self.assertEqual(imp_res.archived_count, 1)
                self.assertIsNotNone(imp_res.archive_dir)
                self.assertTrue((imp_res.archive_dir / "data" / "motif_weights.json").is_file())

                restored_project = load_project(restore_dir / "projects" / "urban_rebate")
                self.assertEqual(restored_project.meta.title, "被裁当天我刷出百亿")

                conn2 = sqlite3.connect(str(restore_dir / "data" / "stockpile.db"))
                rows = conn2.execute("SELECT val FROM t").fetchall()
                conn2.close()
                self.assertEqual(rows, [("safe_row",)])

                # Verify CLI `genres`, `validate`, `check`, `export`, `backup verify/import` subcommands
                self.assertEqual(cli_main(["genres", "--channel", "male", "--json"]), 0)
                self.assertEqual(
                    cli_main(["validate", "--project", str(ROOT / "projects" / "baiwu_dukou")]),
                    0,
                )
                self.assertEqual(
                    cli_main(
                        [
                            "check",
                            "--file",
                            str(ROOT / "projects" / "baiwu_dukou" / "output" / "chapters" / "chapter_01.txt"),
                            "--project",
                            str(ROOT / "projects" / "baiwu_dukou"),
                            "--chapter",
                            "1",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    cli_main(
                        [
                            "export",
                            "--project",
                            str(book_dir),
                            "--platform",
                            "tomato",
                            "--chapters-dir",
                            str(out_dir / "chapters"),
                            "--output-dir",
                            str(ws / "exports" / "tomato_cli"),
                        ]
                    ),
                    0,
                )
                self.assertEqual(cli_main(["backup", "verify", "--input", str(backup_zip)]), 0)
                self.assertEqual(
                    cli_main(
                        [
                            "backup",
                            "import",
                            "--input",
                            str(backup_zip),
                            "--target-dir",
                            str(ws / "restored_cli"),
                        ]
                    ),
                    0,
                )
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
