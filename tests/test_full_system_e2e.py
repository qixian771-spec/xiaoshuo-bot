from __future__ import annotations

import base64
from datetime import datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.cli import build_parser, build_serve_server, main as cli_main
from xiaoshuo_bot.companion import (
    CompanionConfigError,
    CompanionHttpClient,
    TomatoBrowserWorker,
    BrowserLaunchConfig,
    Woff2FontDecoder,
)
from xiaoshuo_bot.contracts import (
    API_BOOKS,
    API_BOOKS_AUTO_CREATE,
    API_BOOK_COVER,
    API_BOOK_EXPORT,
    API_BOOK_PROMOTE,
    API_BOOK_RESUME,
    API_BOOK_STATUS,
    API_COMPANION_METRICS,
    API_COMPANION_RECEIPT,
    API_EVENTS_STREAM,
    API_HEALTH,
    BookRunStatus,
    CompanionLeaseResponse,
    CompanionTaskType,
    SSE_CHAPTER_STOCKPILED,
    SSE_CRITIC_SCORED,
    SSE_GATE_PASSED,
    SSE_METRICS_COLLECTED,
    SSE_RACING_DECISION,
    StockpileStatus,
    compute_hmac_headers,
    format_api_route,
)
from xiaoshuo_bot.cover import CoverGenerationError
from xiaoshuo_bot.llm import LLMConfigurationError
from xiaoshuo_bot.models import SHANGHAI_TZ
from xiaoshuo_bot.server import InsecureBindError
from xiaoshuo_bot.telegram_bot import TelegramConfigError

BUNDLED_FONT_PATH = ROOT / "src" / "xiaoshuo_bot" / "fonts" / "SmileySans-Oblique.ttf"


def _make_clean_png_bytes() -> bytes:
    img = Image.new("RGB", (600, 800), (22, 34, 58))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_obfuscated_woff2_bytes(
    reference_font_path: Path,
    char_to_pua: Dict[str, int],
) -> bytes:
    ref = TTFont(str(reference_font_path))
    try:
        ref_cmap = ref.getBestCmap()
        ref_glyph_set = ref.getGlyphSet()
        ref_hmtx = ref["hmtx"].metrics
        upm = int(ref["head"].unitsPerEm)
        ascent = int(ref["hhea"].ascent)
        descent = int(ref["hhea"].descent)

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

        for idx, (ch, pua_cp) in enumerate(char_to_pua.items()):
            src_gname = ref_cmap[ord(ch)]
            rec = RecordingPen()
            ref_glyph_set[src_gname].draw(rec)
            tt_pen = TTGlyphPen(None)
            for op, pts in rec.value:
                getattr(tt_pen, op)(*pts)
            new_gname = f"pua_{pua_cp:04X}_{idx}"
            glyph_names.append(new_gname)
            glyphs[new_gname] = tt_pen.glyph()
            adv, lsb = ref_hmtx[src_gname]
            metrics[new_gname] = (int(adv), int(lsb))
            cmap[int(pua_cp)] = new_gname

        fb = FontBuilder(upm, isTTF=True)
        fb.setupGlyphOrder(glyph_names)
        fb.setupCharacterMap(cmap)
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics(metrics)
        fb.setupHorizontalHeader(ascent=ascent, descent=descent)
        fb.setupNameTable({"familyName": "E2EObfuscated", "styleName": "Regular"})
        fb.setupOS2()
        fb.setupPost()

        out_buf = io.BytesIO()
        fb.font.flavor = "woff2"
        fb.font.save(out_buf)
        return out_buf.getvalue()
    finally:
        ref.close()


def _clean_chapter_prose(
    chapter_no: int,
    *,
    pov_name: str = "陈牧野",
    ally_name: str = "周成",
    rival_name: str = "赵明",
) -> str:
    if chapter_no == 1:
        paras = [
            f"人事主管{rival_name}把辞退通知拍在玻璃桌上时，{pov_name}兜里只剩三百块。",
            f"“赔款单按最低走，五点前清工位。”{rival_name}抬起腕表敲了敲桌面。",
            "他把那份熬了三个月写出来的核心算法底稿，直接推给旁边的亲戚。",
            "【检测到恶意抢功，十倍反噬账单已绑定。】",
            "面板只有十六个字，账面余额跳到一百万。",
            f"“主管，这是你签的移交单？”{pov_name}把工牌拍在移交单旁边。",
            f"{rival_name}嗤笑一声，伸手就要撕掉那页纸。",
            f"{pov_name}抬手按住他的手腕，指节抵着桌沿，直接把纸抽了回来。",
            f"“你敢动一下，我就把昨晚的后台提交记录发进全公司两千人的大群。”{pov_name}说。",
            "走廊外的茶水间门忽然响了一声，几个实习生探头往里张望。",
            f"“你疯了？全集团都知道你签了保密协议！”{rival_name}脸色骤变，猛地起身想抢手机。",
            f"{pov_name}退后半步，拇指直接点下发送键，大屏幕瞬间弹出原始时间戳铁证。",
            "会议室里的投影仪跟着闪烁起来，所有高管的邮箱同时叮咚作响。",
            f"{rival_name}伸在半空的手僵在原地，额头滚下一滴冷汗。",
            "【第一笔十倍反噬结算完成，可用余额已突破五百万。】",
            f"{pov_name}收起手机，拎起背包转身走出玻璃门，电梯门正好在这一刻打开。",
            "门外站着集团总部的审计专员，手里拿着一份刚打印出来的紧急停职令。",
        ]
    else:
        paras = [
            f"审计专员{ally_name}把红色封皮的停职令摊在走廊长桌上，直接挡住了{rival_name}的去路。",
            f"“请配合核对上季度的外包采购回单。”{ally_name}语气平淡。",
            f"{pov_name}站在电梯口没有走，从背包里抽出昨晚备份的那张加密存储卡。",
            "【新任务激活：当场拆穿虚假外包发票，可解锁千倍股权托管权限。】",
            f"“刚才有人说工位归他表弟，”{pov_name}把卡递给{ally_name}，“顺便查查那笔八十万的设备款。”",
            f"{rival_name}嘴唇发白，伸手扶住墙面才没滑坐在地。",
            f"{ally_name}接过卡插进随身平板，屏幕瞬间跳出三张连号假发票的扫描件。",
            "“这不是设备款，这是空壳公司套现的流水！”旁边围观的财务主管脱口而出。",
            "大厅门口忽然涌进四名市局稽查司人员，为首的队长直接亮出调查函。",
            f"“谁是{rival_name}？”队长目光扫过人群，“跟我们回局里走一趟。”",
            f"{rival_name}双腿发软，差点撞翻走廊上的不锈钢垃圾桶。",
            f"{pov_name}看着他被带进专用电梯，顺手在手机上签收了猎头刚发来的竞业买断协议。",
            "一小时后，对家风投的林总亲自打来电话，开价五千万收购专利。",
            f"{pov_name}站在大夏海城金融大厦三十六层落地窗前，指尖轻轻敲着合同封面。",
            "电话那头突然传来一阵急促的敲门声，林总压低声音说总部刚换了新老板。",
        ]
    extra_blocks = [
        "大厅中央的电子钟跳过五点整，玻璃幕墙外车流如织，霓虹灯一盏接一盏亮起来。",
        f"{pov_name}低头核对手机银行刚发来的托管回执，每一笔流水都带着合规风控编码。",
        "“这是您的贵宾通行卡。”前台经理双手递过一张黑金门禁卡。",
        f"{pov_name}接过卡放进西装内袋，顺手按下三十八楼的专属直达按钮。",
        f"电梯平稳上行，金属轿厢壁映出{pov_name}平静的侧脸，楼层数字一路跳到顶层。",
        "顶层会议室大门敞开着，长桌尽头坐着三位来自风投基金的合伙人。",
        "“条件我们看过了，”坐在中间的李合伙人推过一份股权转让书，“只要你今天签字，明早开盘前资金到位。”",
        f"{pov_name}翻开第三页附加条款，用钢笔划掉其中两行限制研发权限的霸王条目。",
        f"“这两条改掉，我立刻签。”{pov_name}把钢笔往桌上一搁，抬头看向对方。",
        "两位副手交换了一下眼色，李合伙人沉默三秒后直接点头同意修改。",
        "打印机嗡嗡作响，三分钟后重新装订好的协议摆回面前的红木桌面。",
        f"{pov_name}拔开笔帽落笔签下名字，指腹压在印泥上，在甲方落款处按下一枚清晰的指印。",
        "窗外夜风掠过楼顶停机坪，远处的跨江大桥亮起一排橘黄色路灯。",
        "“合作愉快，不过明天上午十点的董事会，”李合伙人收起合同，“旧东家那边恐怕会派人来闹场。”",
        f"{pov_name}拿起桌上的冰美式喝了一口，手机屏幕正好弹出一条陌生号码发来的匿名威胁短信。",
    ]
    idx = 0
    while len("".join(paras)) < 1750:
        paras.append(extra_blocks[idx % len(extra_blocks)])
        idx += 1
    return "\n\n".join(paras)


class _E2ECpaSimState:
    def __init__(self) -> None:
        self.png_bytes = _make_clean_png_bytes()
        self.chat_requests: List[Dict[str, Any]] = []
        self.image_requests: List[Dict[str, Any]] = []
        self.pov_name = "陈牧野"
        self.ally_name = "周成"
        self.rival_name = "赵明"

    def build_planner_json(self, usr_txt: str) -> str:
        m = re.search(r"推荐安全角色名池[^:：]*[:：]\s*(.+)", usr_txt)
        if m:
            names = [n.strip() for n in m.group(1).split(",") if n.strip()]
            while len(names) < 3:
                names.extend(["陈牧野", "周成", "赵明"])
            self.pov_name, self.ally_name, self.rival_name = names[0], names[1], names[2]
        pov_name, ally_name, rival_name = self.pov_name, self.ally_name, self.rival_name
        scenes = []
        for ch in range(1, 16):
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
                    "emotion_polarity": "burst" if ch % 2 == 1 else "suppress",
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
        range_m = re.search(r"本批章号范围[:：]\s*第\s*(\d+)\s*章\s*~\s*第\s*(\d+)\s*章", usr_txt)
        if "阶段: scenes" in usr_txt:
            start_ch = int(range_m.group(1)) if range_m else 1
            end_ch = int(range_m.group(2)) if range_m else len(scenes)
            return json.dumps(
                {"scenes": [s for s in scenes if start_ch <= s["chapter"] <= end_ch]},
                ensure_ascii=False,
            )
        if "阶段: master_outline" in usr_txt:
            start_ch = int(range_m.group(1)) if range_m else 1
            end_ch = int(range_m.group(2)) if range_m else len(master_outline)
            return json.dumps(
                {
                    "master_outline": [
                        item for item in master_outline if start_ch <= item["chapter"] <= end_ch
                    ]
                },
                ensure_ascii=False,
            )
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
            },
            ensure_ascii=False,
        )

    def handle_chat(self, payload: Dict[str, Any]) -> str:
        self.chat_requests.append(payload)
        sys_txt = ""
        usr_txt = ""
        for msg in payload.get("messages", []):
            if msg.get("role") == "system":
                sys_txt = str(msg.get("content", ""))
            elif msg.get("role") == "user":
                usr_txt += str(msg.get("content", ""))

        if "番茄爆款总策划师" in sys_txt:
            return self.build_planner_json(usr_txt)
        if "毒舌主编" in sys_txt:
            return json.dumps(
                {
                    "first_screen_hook": 89,
                    "cool_point_density": 86,
                    "comment_interactivity": 84,
                    "pacing_drag": 83,
                    "overall_score": 85.5,
                    "weak_paragraphs": [],
                    "paragraph_reasons": {},
                    "critique": "首屏冲突犀利，节奏紧凑。",
                },
                ensure_ascii=False,
            )
        if "互动运营作者" in sys_txt:
            return "审计专员突然堵门，大家猜赵明下一步会怎么求饶？点个催更明天继续加更！"
        ch_no = 2 if ("第 2 章" in usr_txt or "ch02_" in usr_txt) else 1
        return _clean_chapter_prose(
            ch_no,
            pov_name=self.pov_name,
            ally_name=self.ally_name,
            rival_name=self.rival_name,
        )


def _start_cpa_sim_server(state: _E2ECpaSimState) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/v0/management/auth-files":
                self._send_json(200, {"files": [{"name": "codex-e2e.json", "status": "active"}]})
                return
            if self.path == "/v0/management/usage":
                self._send_json(200, {"requests_today": len(state.chat_requests), "status": "ok"})
                return
            if self.path == "/v1/models":
                self._send_json(200, {"data": [{"id": "gpt-5.2"}, {"id": "claude-sonnet-4.6"}]})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            if ":generateContent" in self.path:
                state.image_requests.append(body)
                b64_png = base64.b64encode(state.png_bytes).decode("ascii")
                self._send_json(
                    200,
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": b64_png,
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                )
                return

            if self.path == "/v1/chat/completions":
                answer = state.handle_chat(body)
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.end_headers()
                    mid = max(1, len(answer) // 2)
                    chunks = [
                        b": keepalive\n\n",
                        f"data: {json.dumps({'choices': [{'delta': {'content': answer[:mid]}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n".encode("utf-8"),
                        f"data: {json.dumps({'choices': [{'delta': {'content': answer[mid:]}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 120, 'completion_tokens': 240}}, ensure_ascii=False)}\n\n".encode("utf-8"),
                        b"data: [DONE]\n\n",
                    ]
                    for ch in chunks:
                        self.wfile.write(ch)
                        self.wfile.flush()
                    return
                self._send_json(
                    200,
                    {
                        "model": body.get("model", "gpt-5.2"),
                        "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 240},
                    },
                )
                return

            self._send_json(404, {"error": "unknown endpoint"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, t, f"http://{host}:{port}"


class _TelegramSimState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.updates_queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self.sent_messages: List[Dict[str, Any]] = []
        self.answered_callbacks: List[Dict[str, Any]] = []
        self.edited_markups: List[Dict[str, Any]] = []
        self._msg_seq = 100

    def enqueue_update(self, update: Dict[str, Any]) -> None:
        self.updates_queue.put(update)


def _start_telegram_sim_server(state: _TelegramSimState) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_ok(self, result: Any) -> None:
            raw = json.dumps({"ok": True, "result": result}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            method = self.path.rsplit("/", 1)[-1]
            payload = json.loads(raw_body.decode("utf-8")) if "json" in (self.headers.get("Content-Type") or "") else {}

            if method == "getUpdates":
                items: List[Dict[str, Any]] = []
                try:
                    items.append(state.updates_queue.get(timeout=0.15))
                    while not state.updates_queue.empty():
                        items.append(state.updates_queue.get_nowait())
                except queue.Empty:
                    pass
                self._send_ok(items)
                return

            with state.lock:
                if method == "sendMessage":
                    state._msg_seq += 1
                    rec = {"message_id": state._msg_seq, **payload}
                    state.sent_messages.append(rec)
                    self._send_ok(rec)
                    return
                if method == "answerCallbackQuery":
                    state.answered_callbacks.append(payload)
                    self._send_ok(True)
                    return
                if method == "editMessageReplyMarkup":
                    state.edited_markups.append(payload)
                    self._send_ok(True)
                    return

            self._send_ok(True)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, t, f"http://{host}:{port}"


class _TomatoSimState:
    def __init__(self, woff2_bytes: bytes, *, book_id: str, book_title: str, author_name: str) -> None:
        self.woff2_bytes = woff2_bytes
        self.book_id = book_id
        self.book_title = book_title
        self.author_name = author_name
        self.published_submissions: List[Dict[str, Any]] = []
        self.draft_submissions: List[Dict[str, Any]] = []
        self.rotate_submissions: List[Dict[str, Any]] = []


def _start_tomato_sim_server(sim: _TomatoSimState) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    def _identity_html() -> str:
        return (
            f'<div id="writer-header">'
            f'<span data-testid="author-name">{sim.author_name}</span>'
            f'<span data-testid="book-id" data-book-id="{sim.book_id}">{sim.book_id}</span>'
            f'<span data-testid="book-title">{sim.book_title}</span>'
            f"</div>"
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
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            if self.path == "/api/publish":
                sim.published_submissions.append(payload)
            elif self.path == "/api/save-draft":
                sim.draft_submissions.append(payload)
            elif self.path == "/api/rotate":
                sim.rotate_submissions.append(payload)
                if payload.get("new_title"):
                    sim.book_title = str(payload["new_title"])
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

            if self.path == "/publish":
                html = f"""<!doctype html><html><body>
                {_identity_html()}
                <div data-testid="recent-chapters"></div>
                <input id="chapter-number-input" name="chapter_no" />
                <input id="chapter-title-input" name="chapter_title" />
                <div class="ProseMirror" contenteditable="true" style="min-height:120px;border:1px solid #ccc;"></div>
                <span data-testid="editor-word-count">0</span>
                <textarea id="author-note-input" name="author_note"></textarea>
                <input id="schedule-time-input" name="scheduled_publish_at" />
                <button id="save-draft-btn" data-testid="save-draft-btn" type="button">存草稿</button>
                <button id="publish-submit-btn" data-testid="publish-submit-btn" data-remote-chapter-id="rm-ch-1" type="button">定时发布</button>
                <script>
                  const pm = document.querySelector('.ProseMirror');
                  const wc = document.querySelector('[data-testid="editor-word-count"]');
                  pm.addEventListener('input', () => {{
                    wc.textContent = String(pm.innerText.replace(/\\s+/g, '').length);
                  }});
                  document.getElementById('save-draft-btn').addEventListener('click', () => {{
                    fetch('/api/save-draft', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{
                        chapter_no: document.getElementById('chapter-number-input').value,
                        chapter_title: document.getElementById('chapter-title-input').value,
                        body_text: pm.innerText,
                        word_count: wc.textContent
                      }})
                    }});
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
                        scheduled_publish_at: document.getElementById('schedule-time-input').value
                      }})
                    }});
                  }});
                </script>
                </body></html>"""
                self._send_html(200, html)
                return

            if self.path == "/rotate_packaging":
                html = f"""<!doctype html><html><body>
                {_identity_html()}
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
                        new_blurb: document.getElementById('rotate-new-blurb').value
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
                {_identity_html()}
                <span data-testid="metrics-settlement-status">SETTLED</span>
                <span data-testid="metrics-date">2026-11-17</span>
                <span data-testid="metric-ch2-read-through">62.5%</span>
                <span data-testid="metric-word100k-completion">21.0%</span>
                <span data-testid="metric-ctr">4.2%</span>
                <span data-testid="metric-bookshelf-rate">11.5%</span>
                <span data-testid="metric-impressions">8500</span>
                <span data-testid="metric-daily-revenue">¥142.00</span>
                <div data-testid="hotlist-item" data-woff2-url="/hotlist.woff2">
                  <span class="rank">{obf_rank}</span>
                  <span class="title">九幽十万界</span>
                  <span class="author">青崖客</span>
                  <span class="heat">{obf_heat}</span>
                </div>
                </body></html>"""
                self._send_html(200, html)
                return

            self._send_html(404, "<html><body>Not Found</body></html>")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, t, f"http://{host}:{port}"


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json"}
    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def _http_bytes(url: str, *, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return int(resp.status), {k.lower(): v for k, v in resp.headers.items()}, resp.read()


class TestCliServeCompanionConfig(unittest.TestCase):
    def test_cli_serve_and_companion_parser_and_zero_fallback_validation(self) -> None:
        parser = build_parser()

        serve_args = parser.parse_args(
            [
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8080",
                "--db",
                "./tmp_matrix.db",
                "--projects-root",
                "./tmp_projects",
            ]
        )
        self.assertEqual(serve_args.command, "serve")
        self.assertEqual(serve_args.db_path, "./tmp_matrix.db")
        self.assertEqual(serve_args.port, 8080)

        comp_args = parser.parse_args(
            [
                "companion",
                "--server-url",
                "http://127.0.0.1:8765",
                "--hmac-secret",
                "secret-xyz",
                "--account-id",
                "acc_1",
                "--target-url",
                "http://127.0.0.1:9000/publish",
            ]
        )
        self.assertEqual(comp_args.chrome_channel, "chrome")
        self.assertFalse(comp_args.headless)

        # Non-loopback bind without >=24-char token MUST raise InsecureBindError
        with tempfile.TemporaryDirectory(prefix="xsb_cli_val_") as tmpdir:
            bad_bind = parser.parse_args(
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "0",
                    "--db-path",
                    str(Path(tmpdir) / "m.db"),
                    "--projects-root",
                    str(Path(tmpdir) / "projects"),
                    "--admin-token",
                    "too-short",
                    "--no-tg-bot",
                ]
            )
            with self.assertRaises(InsecureBindError):
                build_serve_server(bad_bind)

            # --with-tg-bot without token/owner MUST raise TelegramConfigError
            bad_tg = parser.parse_args(
                [
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--db-path",
                    str(Path(tmpdir) / "m.db"),
                    "--projects-root",
                    str(Path(tmpdir) / "projects"),
                    "--with-tg-bot",
                ]
            )
            old_tg_tok = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            old_tg_uid = os.environ.pop("TELEGRAM_OWNER_USER_ID", None)
            try:
                with self.assertRaises(TelegramConfigError):
                    build_serve_server(bad_tg)
            finally:
                if old_tg_tok is not None:
                    os.environ["TELEGRAM_BOT_TOKEN"] = old_tg_tok
                if old_tg_uid is not None:
                    os.environ["TELEGRAM_OWNER_USER_ID"] = old_tg_uid

            # Partial CPA config MUST raise LLMConfigurationError
            bad_cpa = parser.parse_args(
                [
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--db-path",
                    str(Path(tmpdir) / "m.db"),
                    "--projects-root",
                    str(Path(tmpdir) / "projects"),
                    "--cpa-url",
                    "http://127.0.0.1:8317",
                    "--no-tg-bot",
                ]
            )
            with self.assertRaises(LLMConfigurationError):
                build_serve_server(bad_cpa)

            # Companion missing required flags MUST raise CompanionConfigError
            missing_comp = parser.parse_args(["companion", "--once"])
            old_srv = os.environ.pop("XIAOSHUO_SERVER_URL", None)
            old_sec = os.environ.pop("XIAOSHUO_COMPANION_HMAC_SECRET", None)
            old_sec2 = os.environ.pop("XIAOSHUO_COMPANION_SECRET", None)
            try:
                with self.assertRaises(CompanionConfigError):
                    cli_main(["companion", "--once"])
                self.assertEqual(missing_comp.command, "companion")
            finally:
                if old_srv is not None:
                    os.environ["XIAOSHUO_SERVER_URL"] = old_srv
                if old_sec is not None:
                    os.environ["XIAOSHUO_COMPANION_HMAC_SECRET"] = old_sec
                if old_sec2 is not None:
                    os.environ["XIAOSHUO_COMPANION_SECRET"] = old_sec2

    def test_cli_cover_pen_name_override_and_zero_fallback(self) -> None:
        src_project = ROOT / "projects" / "baiwu_dukou"
        with tempfile.TemporaryDirectory(prefix="xsb_cli_cov_") as tmpdir:
            tmp = Path(tmpdir)
            proj_dir = tmp / "baiwu_copy"
            shutil.copytree(src_project, proj_dir)

            out_missing = tmp / "cov_missing.png"
            with self.assertRaises(CoverGenerationError):
                cli_main(
                    [
                        "cover",
                        "--project",
                        str(proj_dir),
                        "--output",
                        str(out_missing),
                        "--base-mode",
                        "procedural",
                    ]
                )

            out_blank = tmp / "cov_blank.png"
            with self.assertRaises(CoverGenerationError):
                cli_main(
                    [
                        "cover",
                        "--project",
                        str(proj_dir),
                        "--output",
                        str(out_blank),
                        "--pen-name",
                        "   ",
                        "--base-mode",
                        "procedural",
                    ]
                )

            out_ok = tmp / "cov_ok.png"
            rc = cli_main(
                [
                    "cover",
                    "--project",
                    str(proj_dir),
                    "--output",
                    str(out_ok),
                    "--pen-name",
                    "砚山行",
                    "--base-mode",
                    "procedural",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out_ok.is_file())
            with Image.open(out_ok) as img:
                self.assertEqual(img.size, (600, 800))
                self.assertEqual(img.mode, "RGB")


class TestServerRoutesAndStaticAssets(unittest.TestCase):
    def test_static_assets_book_pause_and_non_chapter_receipts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_srv_routes_") as tmpdir:
            tmp = Path(tmpdir)
            admin_token = "t15-admin-token-0123456789abcdef"
            hmac_secret = "t15-companion-hmac-secret-123456"
            args = build_parser().parse_args(
                [
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--db-path",
                    str(tmp / "matrix.db"),
                    "--projects-root",
                    str(tmp / "projects"),
                    "--admin-token",
                    admin_token,
                    "--companion-hmac-secret",
                    hmac_secret,
                    "--no-tg-bot",
                ]
            )
            server = build_serve_server(args)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            auth_h = {"Authorization": f"Bearer {admin_token}"}

            try:
                # 1. Static console assets return 200 OK with explicit UTF-8 MIME & Cache-Control
                for route, expected_ct in (
                    ("/", "text/html; charset=utf-8"),
                    ("/index.html", "text/html; charset=utf-8"),
                    ("/app.js", "application/javascript; charset=utf-8"),
                    ("/styles.css", "text/css; charset=utf-8"),
                ):
                    code, hdrs, body = _http_bytes(f"{base_url}{route}")
                    self.assertEqual(code, 200)
                    self.assertEqual(hdrs.get("content-type"), expected_ct)
                    self.assertEqual(hdrs.get("cache-control"), "no-cache")
                    self.assertGreater(len(body), 100)

                # 2. Register a book and test POST /api/books/{book_id}/status pause & resume
                server.db.register_book(
                    book_id="bk_pause",
                    project_dir=tmp / "projects" / "bk_pause",
                    title="测试暂停书目",
                    genre_id="urban_brainhole",
                    account_id="acc_p",
                )
                status_url = f"{base_url}{format_api_route(API_BOOK_STATUS, book_id='bk_pause')}"
                code_p, res_p = _http_json(
                    status_url,
                    method="POST",
                    payload={"action": "pause", "paused": True, "reason": "控制台手动暂停"},
                    headers=auth_h,
                )
                self.assertEqual(code_p, 200)
                self.assertEqual(res_p["status"], BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value)
                self.assertEqual(res_p["reason"], "控制台手动暂停")

                code_r, res_r = _http_json(
                    status_url,
                    method="POST",
                    payload={"action": "resume", "paused": False},
                    headers=auth_h,
                )
                self.assertEqual(code_r, 200)
                self.assertEqual(res_r["status"], BookRunStatus.ACTIVE.value)

                # 3. Non-chapter task receipts (chapter=0) update companion_tasks cleanly
                now_iso = datetime.now(SHANGHAI_TZ).isoformat()
                for tid, ttype, st in (
                    ("star_bk_pause", CompanionTaskType.CREATE_STAR_CARDS.value, StockpileStatus.PUBLISHED.value),
                    ("sign_bk_pause", CompanionTaskType.SUBMIT_SIGNING_DRAFT.value, StockpileStatus.DRAFT_SAVED.value),
                    ("rot_bk_pause", CompanionTaskType.ROTATE_PACKAGING.value, StockpileStatus.PUBLISHED.value),
                ):
                    with server.db._lock:
                        server.db._conn.execute(
                            """
                            INSERT INTO companion_tasks (
                                task_id, task_type, book_id, account_id, status, payload_json, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'PENDING', '{}', ?, ?)
                            """,
                            (tid, ttype, "bk_pause", "acc_p", now_iso, now_iso),
                        )
                    rec_payload = {
                        "task_id": tid,
                        "book_id": "bk_pause",
                        "chapter": 0,
                        "account_id": "acc_p",
                        "status": st,
                        "remote_chapter_id": f"rm-{tid}",
                    }
                    raw_rec = json.dumps(rec_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
                    h_hmac = compute_hmac_headers(hmac_secret, "POST", API_COMPANION_RECEIPT, raw_rec, client_id="cmp-test")
                    req = urllib.request.Request(
                        f"{base_url}{API_COMPANION_RECEIPT}",
                        data=raw_rec,
                        headers={"Content-Type": "application/json; charset=utf-8", **h_hmac},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        self.assertEqual(resp.status, 200)
                        body_json = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(body_json["task_id"], tid)
                    self.assertEqual(body_json["task_type"], ttype)
                    self.assertEqual(body_json["status"], st)
                    self.assertFalse(body_json["idempotent_replay"])

                    # Verify DB row updated to target status
                    with server.db._lock:
                        row = server.db._conn.execute(
                            "SELECT status FROM companion_tasks WHERE task_id = ?", (tid,)
                        ).fetchone()
                        self.assertEqual(row["status"], st)

                # Unknown chapter=0 task_id MUST still return 404 CHAPTER_NOT_FOUND
                bad_rec = {
                    "task_id": "nonexistent_task",
                    "book_id": "bk_pause",
                    "chapter": 0,
                    "account_id": "acc_p",
                    "status": StockpileStatus.PUBLISHED.value,
                }
                raw_bad = json.dumps(bad_rec, ensure_ascii=False).encode("utf-8")
                h_bad = compute_hmac_headers(hmac_secret, "POST", API_COMPANION_RECEIPT, raw_bad, client_id="cmp-test")
                req_bad = urllib.request.Request(
                    f"{base_url}{API_COMPANION_RECEIPT}",
                    data=raw_bad,
                    headers={"Content-Type": "application/json; charset=utf-8", **h_bad},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req_bad, timeout=10)
                self.assertEqual(ctx.exception.code, 404)
            finally:
                server.shutdown_and_close()
                t.join(timeout=3)
                server.db.close()

    def test_http11_keepalive_promote_resume_export_and_health_auth_flag(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_srv_ka_") as tmpdir:
            tmp = Path(tmpdir)
            # 1. Verify /api/health reports admin_auth_required=False when token is empty
            no_auth_args = build_parser().parse_args(
                [
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--db-path",
                    str(tmp / "noauth.db"),
                    "--projects-root",
                    str(tmp / "noauth_projects"),
                    "--no-tg-bot",
                ]
            )
            old_adm = os.environ.pop("XIAOSHUO_ADMIN_TOKEN", None)
            old_optout = os.environ.get("XIAOSHUO_ALLOW_INSECURE_NO_AUTH")
            try:
                # Without the explicit opt-out, an empty token is refused even on loopback.
                from xiaoshuo_bot.server.auth import InsecureBindError

                os.environ.pop("XIAOSHUO_ALLOW_INSECURE_NO_AUTH", None)
                with self.assertRaises(InsecureBindError):
                    build_serve_server(no_auth_args)
                os.environ["XIAOSHUO_ALLOW_INSECURE_NO_AUTH"] = "1"
                no_auth_srv = build_serve_server(no_auth_args)
            finally:
                if old_optout is None:
                    os.environ.pop("XIAOSHUO_ALLOW_INSECURE_NO_AUTH", None)
                else:
                    os.environ["XIAOSHUO_ALLOW_INSECURE_NO_AUTH"] = old_optout
                if old_adm is not None:
                    os.environ["XIAOSHUO_ADMIN_TOKEN"] = old_adm
            t_na = threading.Thread(target=no_auth_srv.serve_forever, daemon=True)
            t_na.start()
            try:
                h_na, p_na = no_auth_srv.server_address
                code_na, health_na = _http_json(f"http://{h_na}:{p_na}{API_HEALTH}")
                self.assertEqual(code_na, 200)
                self.assertFalse(health_na["admin_auth_required"])
            finally:
                no_auth_srv.shutdown_and_close()
                t_na.join(timeout=3)
                no_auth_srv.db.close()

            # 2. Verify HTTP/1.1 Keep-Alive connection reuse across POST JSON + GET
            admin_token = "t15-ka-admin-token-0123456789abc"
            hmac_secret = "t15-ka-hmac-secret-123456"
            args = build_parser().parse_args(
                [
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--db-path",
                    str(tmp / "matrix.db"),
                    "--projects-root",
                    str(tmp / "projects"),
                    "--admin-token",
                    admin_token,
                    "--companion-hmac-secret",
                    hmac_secret,
                    "--no-tg-bot",
                ]
            )
            server = build_serve_server(args)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            host, port = server.server_address
            auth_headers = {
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
                "Connection": "keep-alive",
            }
            try:
                server.db.register_book(
                    book_id="bk_ka",
                    project_dir=tmp / "projects" / "bk_ka",
                    title="测试保活书目",
                    genre_id="urban_brainhole",
                    account_id="acc_ka",
                )

                conn = http.client.HTTPConnection(host, port, timeout=10)
                try:
                    def _ka_request(
                        method: str,
                        path: str,
                        body_obj: Optional[Dict[str, Any]] = None,
                        headers: Optional[Dict[str, str]] = None,
                    ) -> Tuple[int, bytes]:
                        raw = (
                            json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
                            if body_obj is not None
                            else None
                        )
                        req_hdrs = dict(headers if headers is not None else auth_headers)
                        if raw is not None:
                            req_hdrs["Content-Length"] = str(len(raw))
                        conn.request(method, path, body=raw, headers=req_hdrs)
                        resp = conn.getresponse()
                        data = resp.read()
                        return int(resp.status), data

                    # Health reports admin_auth_required=True with zero token leakage
                    st_h, raw_h = _ka_request("GET", API_HEALTH, headers={"Connection": "keep-alive"})
                    self.assertEqual(st_h, 200)
                    health_json = json.loads(raw_h.decode("utf-8"))
                    self.assertTrue(health_json["admin_auth_required"])
                    self.assertNotIn(admin_token, raw_h.decode("utf-8"))
                    sock_id = id(conn.sock)
                    self.assertIsNotNone(conn.sock)

                    # POST promote + immediate GET /api/books on same TCP socket
                    st_pr, raw_pr = _ka_request(
                        "POST",
                        format_api_route(API_BOOK_PROMOTE, book_id="bk_ka"),
                        {"book_id": "bk_ka"},
                    )
                    self.assertEqual(st_pr, 200, msg=raw_pr.decode("utf-8", errors="replace"))
                    self.assertEqual(id(conn.sock), sock_id)

                    st_g1, raw_g1 = _ka_request("GET", API_BOOKS)
                    self.assertEqual(st_g1, 200, msg=raw_g1.decode("utf-8", errors="replace"))
                    self.assertEqual(id(conn.sock), sock_id)

                    # Pause then POST resume + immediate GET /api/books on same TCP socket
                    st_ps, _ = _ka_request(
                        "POST",
                        format_api_route(API_BOOK_STATUS, book_id="bk_ka"),
                        {"action": "pause", "paused": True, "reason": "ka test"},
                    )
                    self.assertEqual(st_ps, 200)

                    st_rs, raw_rs = _ka_request(
                        "POST",
                        format_api_route(API_BOOK_RESUME, book_id="bk_ka"),
                        {"book_id": "bk_ka"},
                    )
                    self.assertEqual(st_rs, 200, msg=raw_rs.decode("utf-8", errors="replace"))
                    self.assertEqual(id(conn.sock), sock_id)

                    st_g2, raw_g2 = _ka_request("GET", API_BOOKS)
                    self.assertEqual(st_g2, 200, msg=raw_g2.decode("utf-8", errors="replace"))
                    self.assertEqual(id(conn.sock), sock_id)

                    # POST export with JSON body (even when failing validation) + GET /api/books on same socket
                    st_ex, _ = _ka_request(
                        "POST",
                        format_api_route(API_BOOK_EXPORT, book_id="bk_ka"),
                        {"platform": "tomato"},
                    )
                    self.assertEqual(st_ex, 400)
                    st_g3, _ = _ka_request("GET", API_BOOKS)
                    self.assertEqual(st_g3, 200)
                    self.assertEqual(id(conn.sock), sock_id)

                    # Central drain guard on early-return 404 & 401 POST requests with JSON bodies
                    st_404, _ = _ka_request("POST", "/api/nonexistent_ka", {"junk": "payload"})
                    self.assertEqual(st_404, 404)
                    st_g4, _ = _ka_request("GET", API_BOOKS)
                    self.assertEqual(st_g4, 200)
                    self.assertEqual(id(conn.sock), sock_id)

                    st_401, _ = _ka_request(
                        "POST",
                        format_api_route(API_BOOK_PROMOTE, book_id="bk_ka"),
                        {"book_id": "bk_ka"},
                        headers={"Content-Type": "application/json", "Connection": "keep-alive"},
                    )
                    self.assertEqual(st_401, 401)
                    st_h2, _ = _ka_request("GET", API_HEALTH, headers={"Connection": "keep-alive"})
                    self.assertEqual(st_h2, 200)
                    self.assertEqual(id(conn.sock), sock_id)
                finally:
                    conn.close()
            finally:
                server.shutdown_and_close()
                t.join(timeout=3)
                server.db.close()


class TestFullSystemCloudEdgeE2E(unittest.TestCase):
    def test_end_to_end_cloud_edge_sse_playwright_woff2_evolution_and_tg(self) -> None:
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
        cpa_state = _E2ECpaSimState()
        tg_state = _TelegramSimState()
        tomato_state = _TomatoSimState(
            woff2_bytes,
            book_id="e2e_matrix_book",
            book_title="被裁当天我刷出百亿",
            author_name="墨山行",
        )

        srv_cpa, t_cpa, cpa_url = _start_cpa_sim_server(cpa_state)
        srv_tg, t_tg, tg_url = _start_telegram_sim_server(tg_state)
        srv_dom, t_dom, dom_url = _start_tomato_sim_server(tomato_state)

        admin_token = "e2e-strong-admin-token-0123456789"
        hmac_secret = "e2e-strong-companion-hmac-secret-987"
        tg_bot_token = "99887766:E2E_TELEGRAM_BOT_TOKEN_SECRET"
        owner_uid = "88886666"

        sse_events: List[Tuple[str, Dict[str, Any]]] = []
        sse_stop = threading.Event()
        sse_ready = threading.Event()

        try:
            with tempfile.TemporaryDirectory(prefix="xsb_full_e2e_") as tmpdir:
                tmp = Path(tmpdir)
                router_cfg = {
                    "roles": {
                        "planner": {"base_url": cpa_url, "model": "gpt-5.2", "api_key": "k-plan"},
                        "writer": {"base_url": cpa_url, "model": "claude-sonnet-4.6", "api_key": "k-write"},
                        "critic": {"base_url": cpa_url, "model": "gpt-5.2", "api_key": "k-critic"},
                        "polisher": {"base_url": cpa_url, "model": "claude-sonnet-4.6", "api_key": "k-polish"},
                        "image": {
                            "base_url": cpa_url,
                            "model": "gemini-2.5-flash-image",
                            "api_key": "k-img",
                            "provider_type": "gemini_native",
                        },
                    }
                }
                router_cfg_path = tmp / "router.json"
                router_cfg_path.write_text(json.dumps(router_cfg), encoding="utf-8")

                args = build_parser().parse_args(
                    [
                        "serve",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "0",
                        "--projects-root",
                        str(tmp / "projects"),
                        "--db-path",
                        str(tmp / "data" / "matrix.db"),
                        "--admin-token",
                        admin_token,
                        "--companion-hmac-secret",
                        hmac_secret,
                        "--cpa-url",
                        cpa_url,
                        "--cpa-management-key",
                        "cpa-mgmt-key-e2e",
                        "--router-config",
                        str(router_cfg_path),
                        "--with-tg-bot",
                        "--tg-bot-token",
                        tg_bot_token,
                        "--tg-owner-user-id",
                        owner_uid,
                        "--tg-api-base-url",
                        tg_url,
                        "--no-scheduler-loop",
                    ]
                )
                server = build_serve_server(args)
                # Fast poll_timeout for in-process E2E test responsiveness
                server.telegram_bot.client.read_timeout = 10
                from xiaoshuo_bot.telegram_bot import TelegramBotConfig

                server.telegram_bot.config = TelegramBotConfig(
                    bot_token=tg_bot_token,
                    owner_user_id=int(owner_uid),
                    api_base_url=tg_url,
                    poll_timeout=1,
                    read_timeout=10,
                    state_path=tmp / "data" / "tg_state.json",
                )

                t_http = threading.Thread(target=server.serve_forever, daemon=True)
                t_http.start()
                srv_host, srv_port = server.server_address
                cloud_url = f"http://{srv_host}:{srv_port}"
                auth_h = {"Authorization": f"Bearer {admin_token}"}

                def _sse_reader() -> None:
                    req = urllib.request.Request(f"{cloud_url}{API_EVENTS_STREAM}", headers=auth_h)
                    try:
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            sse_ready.set()
                            cur_evt = ""
                            cur_data = ""
                            while not sse_stop.is_set():
                                raw_line = resp.readline()
                                if not raw_line:
                                    break
                                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                                if line.startswith("event: "):
                                    cur_evt = line[7:].strip()
                                elif line.startswith("data: "):
                                    cur_data = line[6:].strip()
                                elif line == "" and cur_evt:
                                    try:
                                        sse_events.append((cur_evt, json.loads(cur_data)))
                                    except json.JSONDecodeError:
                                        pass
                                    cur_evt = ""
                                    cur_data = ""
                    except OSError:
                        sse_ready.set()

                t_sse = threading.Thread(target=_sse_reader, daemon=True)
                t_sse.start()
                self.assertTrue(sse_ready.wait(timeout=5.0))

                try:
                    # 1. Health & Web Console "/" 200 OK
                    code_h, health_json = _http_json(f"{cloud_url}{API_HEALTH}")
                    self.assertEqual(code_h, 200)
                    self.assertTrue(health_json["cpa_reachable"])
                    code_idx, _, idx_bytes = _http_bytes(f"{cloud_url}/")
                    self.assertEqual(code_idx, 200)
                    self.assertIn(b"xiaoshuo-bot", idx_bytes)

                    # 2. Auto-create book via REST API
                    code_ac, book_json = _http_json(
                        f"{cloud_url}{API_BOOKS_AUTO_CREATE}",
                        method="POST",
                        payload={
                            "project_id": "e2e_matrix_book",
                            "genre_id": "urban_brainhole",
                            "author_level": "lv2_plus",
                            "account_id": "acc_e2e",
                            "pen_name": "墨山行",
                        },
                        headers=auth_h,
                    )
                    self.assertEqual(code_ac, 201, msg=str(book_json))
                    self.assertEqual(book_json["title"], "被裁当天我刷出百亿")

                    # 3. Generate AI-image cover via CPA Gemini endpoint
                    cover_route = format_api_route(API_BOOK_COVER, book_id="e2e_matrix_book")
                    code_cov, cov_json = _http_json(
                        f"{cloud_url}{cover_route}",
                        method="POST",
                        payload={"base_mode": "ai_image", "output_format": "PNG"},
                        headers=auth_h,
                    )
                    self.assertEqual(code_cov, 200)
                    self.assertEqual(cov_json["width"], 600)
                    self.assertEqual(cov_json["height"], 800)
                    self.assertEqual(len(cpa_state.image_requests), 1)

                    # 4. Produce Chapter 1 via scheduler (CPA SSE -> AITicChecker -> CriticPolisher -> StockpileDB)
                    prod_res = server.scheduler.produce_next_chapter("e2e_matrix_book")
                    self.assertEqual(prod_res["status"], "PRODUCED")
                    self.assertEqual(prod_res["chapter"], 1)

                    # Wait briefly for SSE delivery
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        names = {ev for ev, _ in sse_events}
                        if {SSE_GATE_PASSED, SSE_CRITIC_SCORED, SSE_CHAPTER_STOCKPILED}.issubset(names):
                            break
                        time.sleep(0.05)
                    sse_names = {ev for ev, _ in sse_events}
                    self.assertIn(SSE_GATE_PASSED, sse_names)
                    self.assertIn(SSE_CRITIC_SCORED, sse_names)
                    self.assertIn(SSE_CHAPTER_STOCKPILED, sse_names)

                    # 5. Run `companion --once` CLI to publish Chapter 1 in real Playwright Chromium
                    outbox_db = tmp / "companion" / "outbox.db"
                    profile_root = tmp / "companion" / "profiles"
                    rc = cli_main(
                        [
                            "companion",
                            "--server-url",
                            cloud_url,
                            "--hmac-secret",
                            hmac_secret,
                            "--account-id",
                            "acc_e2e",
                            "--author-name",
                            "墨山行",
                            "--book-ids",
                            "e2e_matrix_book",
                            "--profile-root",
                            str(profile_root),
                            "--outbox-db",
                            str(outbox_db),
                            "--chrome-channel",
                            "none",
                            "--headless",
                            "--target-url",
                            f"{dom_url}/publish",
                            "--rotate-url",
                            f"{dom_url}/rotate_packaging",
                            "--review-pause-sec",
                            "0.02",
                            "--once",
                        ]
                    )
                    self.assertEqual(rc, 0)
                    # Lease carries no scheduled_publish_at -> companion must only save a draft,
                    # never click the publish button.
                    deadline = time.monotonic() + 3.0
                    while not tomato_state.draft_submissions and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertEqual(len(tomato_state.published_submissions), 0)
                    self.assertEqual(len(tomato_state.draft_submissions), 1)
                    pub_sub = tomato_state.draft_submissions[0]
                    self.assertEqual(pub_sub["chapter_no"], "1")
                    self.assertEqual(pub_sub["chapter_title"], "被裁当天系统到账1百万")
                    ch1_row = server.db.get_chapter("e2e_matrix_book", 1)
                    self.assertEqual(ch1_row["status"], StockpileStatus.DRAFT_SAVED.value)

                    # 6. Collect 12:30 CST metrics & decode WOFF2 PUA hotlist -> triggers ROTATE_PACKAGING
                    decoder = Woff2FontDecoder(cache_dir=tmp / "woff_cache")
                    decoder.register_reference_font(BUNDLED_FONT_PATH, charset=list(char_to_pua.keys()))
                    cmp_client = CompanionHttpClient(
                        base_url=cloud_url,
                        hmac_secret=hmac_secret,
                        client_id="cmp-metrics",
                        outbox_db_path=outbox_db,
                    )
                    try:
                        with TomatoBrowserWorker(
                            "acc_e2e",
                            BrowserLaunchConfig(
                                profile_root=profile_root,
                                channel=None,
                                headless=True,
                                review_pause_sec=0.02,
                            ),
                            client=cmp_client,
                            font_decoder=decoder,
                            now_provider=lambda: datetime(2026, 11, 18, 13, 0, 0, tzinfo=SHANGHAI_TZ),
                        ) as worker:
                            metrics_task = CompanionLeaseResponse(
                                task_id="metrics-e2e-1",
                                task_type=CompanionTaskType.COLLECT_METRICS.value,
                                book_id="e2e_matrix_book",
                                book_title="被裁当天我刷出百亿",
                                author_name="墨山行",
                                fanqie_book_id="e2e_matrix_book",
                                chapter=0,
                                public_title="",
                                body="",
                            )
                            m_out = worker.collect_metrics(
                                metrics_task, target_url=f"{dom_url}/metrics_settled"
                            )
                            self.assertIsNotNone(m_out.metrics)
                            self.assertEqual(m_out.metrics.decoded_hotlist[0]["rank"], "第1名")
                            self.assertEqual(m_out.metrics.decoded_hotlist[0]["heat"], "在读98万")
                    finally:
                        cmp_client.close()

                    latest_metrics = server.db.get_latest_metrics("e2e_matrix_book")
                    self.assertIsNotNone(latest_metrics)
                    self.assertEqual(latest_metrics["racing_decision"]["action"], "ROTATE_PACKAGING")

                    # Verify SSE emitted metrics_collected & racing_decision
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        names = {ev for ev, _ in sse_events}
                        if {SSE_METRICS_COLLECTED, SSE_RACING_DECISION}.issubset(names):
                            break
                        time.sleep(0.05)
                    sse_names = {ev for ev, _ in sse_events}
                    self.assertIn(SSE_METRICS_COLLECTED, sse_names)
                    self.assertIn(SSE_RACING_DECISION, sse_names)

                    # 7. Run `companion --once` again -> picks up ROTATE_PACKAGING, updates DOM & sends chapter=0 receipt
                    rc2 = cli_main(
                        [
                            "companion",
                            "--server-url",
                            cloud_url,
                            "--hmac-secret",
                            hmac_secret,
                            "--account-id",
                            "acc_e2e",
                            "--author-name",
                            "墨山行",
                            "--profile-root",
                            str(profile_root),
                            "--outbox-db",
                            str(outbox_db),
                            "--chrome-channel",
                            "none",
                            "--headless",
                            "--target-url",
                            f"{dom_url}/publish",
                            "--rotate-url",
                            f"{dom_url}/rotate_packaging",
                            "--review-pause-sec",
                            "0.02",
                            "--once",
                        ]
                    )
                    self.assertEqual(rc2, 0)
                    self.assertEqual(len(tomato_state.rotate_submissions), 1)
                    rotated_title = tomato_state.rotate_submissions[0]["new_title"]
                    self.assertTrue(1 <= len(rotated_title) <= 15)
                    # Verify companion_tasks row is now PUBLISHED (not left PENDING)
                    with server.db._lock:
                        rot_row = server.db._conn.execute(
                            "SELECT status FROM companion_tasks WHERE book_id = ? AND task_type = ?",
                            ("e2e_matrix_book", CompanionTaskType.ROTATE_PACKAGING.value),
                        ).fetchone()
                        self.assertEqual(rot_row["status"], StockpileStatus.PUBLISHED.value)

                    # 8. Test PROMOTE_DAILY_UPDATE evolution via next-day settled high-retention metrics
                    promo_payload = {
                        "book_id": "e2e_matrix_book",
                        "account_id": "acc_e2e",
                        "settled": True,
                        "settlement_status": "SETTLED",
                        "metrics_date": "2026-11-18",
                        "ch2_read_through": 0.68,
                        "bookshelf_rate": 0.25,
                        "impressions": 15000,
                    }
                    raw_promo = json.dumps(promo_payload, ensure_ascii=False).encode("utf-8")
                    h_promo = compute_hmac_headers(hmac_secret, "POST", API_COMPANION_METRICS, raw_promo, client_id="cmp-promo")
                    req_promo = urllib.request.Request(
                        f"{cloud_url}{API_COMPANION_METRICS}",
                        data=raw_promo,
                        headers={"Content-Type": "application/json; charset=utf-8", **h_promo},
                        method="POST",
                    )
                    with urllib.request.urlopen(req_promo, timeout=10) as resp:
                        promo_res = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(promo_res["racing_decision"]["action"], "PROMOTE_DAILY_UPDATE")
                    self.assertGreater(server.db.get_book("e2e_matrix_book")["daily_target_chapters"], 2)

                    # 9. Telegram daily digest + owner /status polling round-trip (zero secret leak)
                    server.telegram_bot.send_daily_digest()
                    tg_state.enqueue_update(
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 1,
                                "from": {"id": int(owner_uid), "is_bot": False},
                                "chat": {"id": int(owner_uid), "type": "private"},
                                "text": "/status",
                            },
                        }
                    )
                    deadline = time.monotonic() + 4.0
                    while time.monotonic() < deadline:
                        with tg_state.lock:
                            if len(tg_state.sent_messages) >= 2:
                                break
                        time.sleep(0.05)

                    with tg_state.lock:
                        self.assertGreaterEqual(len(tg_state.sent_messages), 2)
                        combined_tg = "\n".join(m.get("text", "") for m in tg_state.sent_messages)
                    self.assertIn("被裁当天我刷出百亿", combined_tg)
                    self.assertNotIn(tg_bot_token, combined_tg)
                    self.assertNotIn(admin_token, combined_tg)
                finally:
                    sse_stop.set()
                    server.shutdown_and_close()
                    t_http.join(timeout=3)
                    t_sse.join(timeout=2)
                    server.db.close()
        finally:
            srv_cpa.shutdown()
            srv_cpa.server_close()
            srv_tg.shutdown()
            srv_tg.server_close()
            srv_dom.shutdown()
            srv_dom.server_close()
            t_cpa.join(timeout=2)
            t_tg.join(timeout=2)
            t_dom.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
