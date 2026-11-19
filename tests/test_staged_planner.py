"""T5 offline tests: staged project planning, full error collectors, async wizard confirm & DB migration."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
import unittest
import urllib.error
import urllib.request

from xiaoshuo_bot.contracts import AutoCreateBookRequest
from xiaoshuo_bot.genres import get_genre
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.namer import CharacterNamer
from xiaoshuo_bot.planner import (
    AutoNovelPlanner,
    PlanningValidationError,
    _collect_bible_errors,
    _collect_master_outline_batch_errors,
    _collect_scene_batch_errors,
)
from xiaoshuo_bot.server import create_server
from xiaoshuo_bot.server.stockpile_db import StockpileDB
from xiaoshuo_bot.server.wizard import NewBookWizard, WizardError


ADMIN_TOKEN = "t5-staged-admin-token-0123456789abcdef"


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data: Optional[bytes] = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, json.loads(raw) if raw.strip() else {}


def _make_valid_bible(
    safe_names: List[str],
    *,
    author_level: str = "lv0_1",
    target_chapters: int = 80,
) -> Dict[str, Any]:
    if author_level == "lv0_1":
        foreshadows = [
            {
                "id": "FS-01",
                "title": "加密U盘哈希残片",
                "plant_chapter": 1,
                "due_chapter": 12,
                "plant_instruction": "第1章主角在旧手机里发现加密日志哈希",
                "payoff_instruction": "第12章当众比对原始哈希揭露造假",
                "plant_keywords": ["哈希", "加密"],
                "payoff_keywords": ["比对", "揭露"],
                "cross_volume": False,
            },
            {
                "id": "FS-02",
                "title": "顶层董事会对赌底单",
                "plant_chapter": 25,
                "due_chapter": min(target_chapters, 78),
                "plant_instruction": "第25章查到对赌协议编号",
                "payoff_instruction": "终局前当众宣读底单完成收网",
                "plant_keywords": ["对赌", "编号"],
                "payoff_keywords": ["底单", "收网"],
                "cross_volume": False,
            },
        ]
        target_words = target_chapters * 2200
    else:
        foreshadows = [
            {
                "id": "FS-01",
                "title": "旧手机未来新闻推送",
                "plant_chapter": 1,
                "due_chapter": 10,
                "plant_instruction": "第1章雨夜旧手机跳出次日头版新闻",
                "payoff_instruction": "第10章新闻精确应验完成首轮反杀",
                "plant_keywords": ["推送", "旧手机"],
                "payoff_keywords": ["应验", "反杀"],
                "cross_volume": False,
            },
            {
                "id": "FS-02",
                "title": "幕后资本总账",
                "plant_chapter": 12,
                "due_chapter": 80,
                "plant_instruction": "第12章发现境外空壳账户代号",
                "payoff_instruction": "后续换地图大高潮清算总账",
                "plant_keywords": ["空壳", "代号"],
                "payoff_keywords": ["总账", "清算"],
                "cross_volume": True,
            },
        ]
        target_words = 1_000_000

    return {
        "project": {
            "title": "雨夜明日新闻",
            "alternate_titles": [
                "旧手机收到明天新闻",
                "雨夜外卖员预知头条",
                "明天头版我先看",
                "提前一天拿捏全城",
                "外卖箱里的明日新闻",
            ],
            "synopsis": "暴雨夜，外卖骑手捡到一部能提前收到明天新闻的旧手机，从此步步抢占先机，拆穿阴谋并逆转人生。",
            "alternate_blurbs": [
                "第一版三段式简介：雨夜拾机，提前看见明日新闻，从外卖站一路逆袭。",
                "第二版三段式简介：别人追着新闻跑，他提前一天布好局，连本带利讨回公道。",
                "第三版三段式简介：一部裂屏旧手机每天零点推送明天头条，揭开全城暗流。",
            ],
            "tweet_aliases": ["明日头条先看", "雨夜拾机逆袭", "预知明天新闻"],
            "default_pov_character": "hero",
            "pov_mode": "third_limited",
            "panel_policy": "allow_system_tag",
            "opening_motif": "底层逆袭信息差",
            "golden_finger_type": "提前二十四小时新闻推送",
            "golden_finger_description": "裂屏旧手机每晚零点准时推送次日全城新闻头条，但每次干预都会消耗电量与信任代价。",
            "target_word_count": target_words,
            "initial_balances": {"cash": 500.0},
        },
        "characters": [
            {
                "id": "hero",
                "name": safe_names[0],
                "role": "雨夜外卖骑手（主角）",
                "personality": "沉稳克制、重实证、行动力极强",
                "speech_style": "短句平实、直击要害",
                "behavioral_anchors": {"冷静判断": "指节轻敲旧手机裂屏边缘"},
                "forbidden_ooc": ["绝不无底线原谅算计自己的对手"],
                "initial_status": "alive",
                "faction": "自由骑手",
                "relations": {"rival": "死敌"},
                "forbidden_titles": [],
            },
            {
                "id": "rival",
                "name": safe_names[1],
                "role": "站点黑心主管（前期反派）",
                "personality": "贪婪精明、欺软怕硬",
                "speech_style": "官腔敷衍、夹枪带棒",
                "behavioral_anchors": {"虚张声势": "反复拍打考勤表"},
                "forbidden_ooc": ["绝不无缘无故悔改"],
                "initial_status": "alive",
                "faction": "站点管理层",
                "relations": {"hero": "打压对象"},
                "forbidden_titles": [],
            },
        ],
        "assets": [
            {
                "id": "old_phone",
                "name": "裂屏旧手机",
                "location": "主角防水外套内袋",
                "holder": safe_names[0],
                "condition": "右上角有斜裂纹，电量仅剩百分之十八",
                "physical_details": "黑色金属旧直板机，后盖刻着模糊出厂编号",
            }
        ],
        "foreshadows": foreshadows,
        "style_profiles": [
            {
                "id": "urban_brainhole_human",
                "name": "都市冷峻快节奏",
                "granularity_notes": "短句高频动作推进，少形容词堆砌，多具体器物与数字细节。",
                "sample_text": "雨点砸在头盔面罩上，发出密密麻麻的闷响。他把电动车停在巷口灯柱下，抹了一把屏上的水珠，新闻标题里的时间写着明天早上八点半。",
            }
        ],
        "volume_cap_locks": [
            {"ledger_key": "cash", "volume": 1, "max_value": 5_000_000.0, "unit": "元"}
        ],
    }


def _make_valid_scene(chapter: int) -> Dict[str, Any]:
    polarity = "burst" if chapter % 3 == 0 else ("suppress" if chapter % 3 == 1 else "mixed")
    return {
        "chapter": chapter,
        "scene_id": f"ch{chapter:04d}_scene",
        "internal_title": f"雨夜先机推进{chapter}",
        "public_title": f"雨夜头条应验{chapter}",
        "pov_character": "hero",
        "active_characters": ["hero", "rival"],
        "active_assets": ["old_phone"],
        "style_profile_ids": ["urban_brainhole_human"],
        "location_time": f"第{chapter}日夜，海城配送站与旧街口",
        "location_id": f"loc_{(chapter - 1) % 3 + 1}",
        "events": [
            f"第{chapter}章主角核对旧手机推送的次日新闻细节",
            f"主角提前落子截断反派算计并拿到物理证据",
        ],
        "stop_condition": f"第{chapter}章反派当场失算且新推送弹出时停笔",
        "min_chars": 2050,
        "max_chars": 2300,
        "beat_type": "反转打脸" if chapter % 2 == 0 else "危机铺垫",
        "emotion_polarity": polarity,
        "cliffhanger_hook": f"旧手机屏幕亮起第{chapter + 1}条明日快讯",
        "segment_comment_cue": "这波提前预判太解气",
        "volume": 1,
        "calendar_days_elapsed": 1,
    }


def _make_master_outline_batch(start_ch: int, end_ch: int) -> List[Dict[str, Any]]:
    return [
        {
            "chapter": ch,
            "title": f"明日头条节点{ch}",
            "summary": f"第{ch}章借助次日新闻提前破局并升级地图冲突",
        }
        for ch in range(start_ch, end_ch + 1)
    ]


class ScriptedStagedPlannerLLM(LLMProvider):
    """Deterministic staged-planning test double recording every LLMRequest."""

    def __init__(
        self,
        *,
        safe_names: List[str],
        author_level: str = "lv0_1",
        target_chapters: int = 80,
        fault_injector: Optional[Any] = None,
    ) -> None:
        self.safe_names = safe_names
        self.author_level = author_level
        self.target_chapters = target_chapters
        self.fault_injector = fault_injector
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        meta = dict(request.metadata or {})
        stage = str(meta.get("stage", ""))

        if self.fault_injector is not None:
            override = self.fault_injector(request)
            if override is not None:
                return LLMResponse(
                    request_id=request.request_id,
                    provider="fake",
                    content=override if isinstance(override, str) else json.dumps(override, ensure_ascii=False),
                    model="fake-staged-planner",
                    latency_sec=0.01,
                )

        if stage == "bible":
            payload = _make_valid_bible(
                self.safe_names,
                author_level=self.author_level,
                target_chapters=self.target_chapters,
            )
        elif stage == "scenes":
            start_ch = int(meta["chapter_start"])
            end_ch = int(meta["chapter_end"])
            payload = {"scenes": [_make_valid_scene(ch) for ch in range(start_ch, end_ch + 1)]}
        elif stage == "master_outline":
            start_ch = int(meta["chapter_start"])
            end_ch = int(meta["chapter_end"])
            payload = {"master_outline": _make_master_outline_batch(start_ch, end_ch)}
        else:
            raise AssertionError(f"Unexpected planner stage metadata: {meta!r}")

        return LLMResponse(
            request_id=request.request_id,
            provider="fake",
            content=json.dumps(payload, ensure_ascii=False),
            model="fake-staged-planner",
            latency_sec=0.01,
        )


class FakeWizardOptionsLLM(LLMProvider):
    """Returns valid 3-option wizard proposals for all 4 wizard steps."""

    def __init__(self, *, author_level: str = "lv0_1") -> None:
        self.author_level = author_level

    def generate(self, request: LLMRequest) -> LLMResponse:
        stage = str((request.metadata or {}).get("stage", ""))
        if stage == "wizard_direction":
            opts = [
                {
                    "label": f"方向{i}",
                    "logline": f"雨夜旧手机收到明天新闻的逆袭线{i}",
                    "genre_id": "urban_brainhole",
                    "selling_point": "信息差降维打击",
                    "tone": "冷峻爽快",
                }
                for i in range(3)
            ]
        elif stage == "wizard_protagonist":
            opts = [
                {
                    "label": f"沉稳骑手{i}",
                    "persona": "陈峰是个话不多的夜班骑手",
                    "golden_finger": "零点收到明天头版新闻",
                    "cost": "每次干预消耗旧手机寿命",
                    "arc": "从底层骑手到撬动全城资本",
                }
                for i in range(3)
            ]
        elif stage == "wizard_style":
            opts = [
                {
                    "label": f"冷峻白描{i}",
                    "description": "第三人称限知、短句快节奏、强器物细节",
                    "sample": "雨刷左右摆动，旧手机屏幕在仪表盘旁亮了一下，头版日期写着明天。",
                }
                for i in range(3)
            ]
        elif stage == "wizard_scale":
            opts = [
                {
                    "label": f"方案{i}",
                    "title": f"雨夜明日新闻{i}",
                    "author_level": self.author_level,
                    "target_chapters": 80 if self.author_level == "lv0_1" else 0,
                    "blurb": "暴雨夜捡到一部能收明天新闻的旧手机，步步先机反杀幕后黑手。",
                }
                for i in range(3)
            ]
        else:
            raise AssertionError(f"Unexpected wizard stage: {stage!r}")
        return LLMResponse(
            request_id=request.request_id,
            provider="fake",
            content=json.dumps({"options": opts}, ensure_ascii=False),
            model="fake-wizard-llm",
            latency_sec=0.01,
        )


class TestStagedPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.safe_names = CharacterNamer().generate_cast(
            count=5, era="urban_brainhole", channel="male", seed=42
        )

    def test_lv0_1_staged_planning_order_batch_ranges_and_load_project(self) -> None:
        """T5 §6.1: lv0_1 (80ch) runs 1 bible + 8 scene batches (1..10 .. 71..80) and passes load_project()."""
        llm = ScriptedStagedPlannerLLM(
            safe_names=self.safe_names, author_level="lv0_1", target_chapters=80
        )
        planner = AutoNovelPlanner(llm, max_validation_retries=3)
        progress_events: List[Dict[str, Any]] = []

        with tempfile.TemporaryDirectory(prefix="xsb_t5_lv01_") as tmpdir:
            out_dir = Path(tmpdir) / "lv01_book"
            res = planner.plan_new_project(
                project_id="lv01_book",
                genre_id="urban_brainhole",
                output_dir=out_dir,
                author_level="lv0_1",
                target_chapters=80,
                pen_name="试跑",
                creative_brief="主角原示意名陈峰，保留冷静性格与旧手机金手指",
                on_progress=progress_events.append,
            )

            # 1 bible + 8 scene batches = 9 requests
            self.assertEqual(len(llm.requests), 9)
            stages = [r.metadata["stage"] for r in llm.requests]
            self.assertEqual(stages, ["bible"] + ["scenes"] * 8)

            # Verify exact chapter ranges and max_tokens >= 8000
            ranges = [
                (r.metadata["chapter_start"], r.metadata["chapter_end"])
                for r in llm.requests[1:]
            ]
            expected_ranges = [(i * 10 + 1, (i + 1) * 10) for i in range(8)]
            self.assertEqual(ranges, expected_ranges)
            for req in llm.requests:
                self.assertGreaterEqual(req.max_tokens, 8000)

            # Verify T5 §4.4 name-only-illustrative guidance in bible prompt
            bible_req = llm.requests[0]
            self.assertIn("人物姓名只是示意", bible_req.user_prompt)
            self.assertIn("推荐安全角色名池", bible_req.user_prompt)

            # Verify scene batch prompt includes bible IDs, foreshadow cues, prior summary, three-act position
            batch1_req = llm.requests[1]
            self.assertIn("hero", batch1_req.user_prompt)
            self.assertIn("old_phone", batch1_req.user_prompt)
            self.assertIn("FS-01", batch1_req.user_prompt)
            self.assertIn("哈希", batch1_req.user_prompt)
            batch2_req = llm.requests[2]
            self.assertIn("雨夜头条应验1", batch2_req.user_prompt)
            self.assertIn("FS-01", batch2_req.user_prompt)
            self.assertIn("比对", batch2_req.user_prompt)

            # Verify progress events emitted for bible + 8 scene batches
            self.assertEqual(progress_events[0]["stage"], "bible")
            scene_prog = [p for p in progress_events if p["stage"] == "scenes"]
            self.assertEqual(
                [(p["batch"], p["batches"]) for p in scene_prog],
                [(i, 8) for i in range(1, 9)],
            )

            # Verify assembled project passes load_project() with 80 scenes
            loaded = load_project(out_dir)
            self.assertEqual(len(loaded.scenes), 80)
            self.assertEqual([s.chapter for s in loaded.scenes], list(range(1, 81)))
            self.assertEqual(res.author_level, "lv0_1")
            self.assertIsNone(res.master_outline_path)

    def test_lv2_plus_staged_planning_scenes_2_batches_and_master_outline_4_batches(self) -> None:
        """T5 §6.2: lv2_plus runs 1 bible + 2 scene batches (1..10, 11..15) + 4 master_outline batches (100 items)."""
        llm = ScriptedStagedPlannerLLM(
            safe_names=self.safe_names, author_level="lv2_plus", target_chapters=15
        )
        planner = AutoNovelPlanner(llm, max_validation_retries=3)
        progress_events: List[Dict[str, Any]] = []

        with tempfile.TemporaryDirectory(prefix="xsb_t5_lv2_") as tmpdir:
            out_dir = Path(tmpdir) / "lv2_book"
            res = planner.plan_new_project(
                project_id="lv2_book",
                genre_id="urban_brainhole",
                output_dir=out_dir,
                author_level="lv2_plus",
                pen_name="试跑",
                on_progress=progress_events.append,
            )

            # 1 bible + 2 scene batches + 4 master_outline batches = 7 requests
            self.assertEqual(len(llm.requests), 7)
            stages = [r.metadata["stage"] for r in llm.requests]
            self.assertEqual(
                stages,
                ["bible", "scenes", "scenes", "master_outline", "master_outline", "master_outline", "master_outline"],
            )

            scene_ranges = [
                (r.metadata["chapter_start"], r.metadata["chapter_end"])
                for r in llm.requests[1:3]
            ]
            self.assertEqual(scene_ranges, [(1, 10), (11, 15)])

            mo_ranges = [
                (r.metadata["chapter_start"], r.metadata["chapter_end"])
                for r in llm.requests[3:7]
            ]
            self.assertEqual(mo_ranges, [(1, 25), (26, 50), (51, 75), (76, 100)])

            loaded = load_project(out_dir)
            self.assertEqual(len(loaded.scenes), 15)
            self.assertIsNotNone(res.master_outline_path)
            self.assertIsNotNone(res.signing_application_path)
            assert res.master_outline_path is not None
            assert res.signing_application_path is not None
            self.assertTrue(res.master_outline_path.is_file())
            self.assertTrue(res.signing_application_path.is_file())

            import yaml

            mo_data = yaml.safe_load(res.master_outline_path.read_text(encoding="utf-8"))
            self.assertEqual(len(mo_data["master_outline"]), 100)
            self.assertEqual(
                [item["chapter"] for item in mo_data["master_outline"]],
                list(range(1, 101)),
            )

            mo_prog = [p for p in progress_events if p["stage"] == "master_outline"]
            self.assertEqual(
                [(p["batch"], p["batches"]) for p in mo_prog],
                [(1, 4), (2, 4), (3, 4), (4, 4)],
            )

    def test_batch_errors_all_collected_and_fed_back_then_fixed(self) -> None:
        """T5 §6.3: Multiple errors in attempt 1 are ALL collected & fed back in attempt 2 prompt, then pass."""
        seen_scene_batch1 = {"count": 0}

        def fault_injector(req: LLMRequest) -> Optional[Dict[str, Any]]:
            meta = req.metadata or {}
            if meta.get("stage") == "scenes" and meta.get("batch") == 1:
                seen_scene_batch1["count"] += 1
                if seen_scene_batch1["count"] == 1:
                    # Inject 3 distinct errors simultaneously in batch 1:
                    # (1) chapter 1 missing stop_condition
                    # (2) chapter 2 public_title has '第2章' prefix
                    # (3) chapter 3 references unknown character 'ghost_char'
                    scenes = [_make_valid_scene(ch) for ch in range(1, 11)]
                    del scenes[0]["stop_condition"]
                    scenes[1]["public_title"] = "第2章 违规前缀标题"
                    scenes[2]["active_characters"] = ["hero", "ghost_char"]
                    return {"scenes": scenes}
            return None

        llm = ScriptedStagedPlannerLLM(
            safe_names=self.safe_names,
            author_level="lv0_1",
            target_chapters=80,
            fault_injector=fault_injector,
        )
        planner = AutoNovelPlanner(llm, max_validation_retries=3)

        with tempfile.TemporaryDirectory(prefix="xsb_t5_fb_") as tmpdir:
            out_dir = Path(tmpdir) / "fb_book"
            planner.plan_new_project(
                project_id="fb_book",
                genre_id="urban_brainhole",
                output_dir=out_dir,
                author_level="lv0_1",
                target_chapters=80,
            )

            # 1 bible + (2 tries for batch 1) + 7 remaining batches = 10 requests
            self.assertEqual(len(llm.requests), 10)
            retry_req = llm.requests[2]
            self.assertEqual(retry_req.metadata["stage"], "scenes")
            self.assertEqual(retry_req.metadata["batch"], 1)
            self.assertEqual(retry_req.metadata["attempt"], 2)
            # ALL 3 errors must appear in the retry prompt (not just the first!)
            self.assertIn("stop_condition", retry_req.user_prompt)
            self.assertIn("第2章", retry_req.user_prompt)
            self.assertIn("ghost_char", retry_req.user_prompt)

            loaded = load_project(out_dir)
            self.assertEqual(len(loaded.scenes), 80)

    def test_batch_three_failures_raises_planning_validation_error_with_stage_batch_and_all_errors(self) -> None:
        """T5 §6.4: 3 consecutive failures on a batch raise PlanningValidationError with stage/batch/all errors; no 4th call."""
        batch2_calls = {"count": 0}

        def always_broken_batch2(req: LLMRequest) -> Optional[Dict[str, Any]]:
            meta = req.metadata or {}
            if meta.get("stage") == "scenes" and meta.get("batch") == 2:
                batch2_calls["count"] += 1
                bad_scenes = [_make_valid_scene(ch) for ch in range(11, 21)]
                del bad_scenes[0]["location_time"]
                bad_scenes[1]["active_assets"] = ["nonexistent_sword"]
                return {"scenes": bad_scenes}
            return None

        llm = ScriptedStagedPlannerLLM(
            safe_names=self.safe_names,
            author_level="lv0_1",
            target_chapters=80,
            fault_injector=always_broken_batch2,
        )
        planner = AutoNovelPlanner(llm, max_validation_retries=3)

        with tempfile.TemporaryDirectory(prefix="xsb_t5_fail3_") as tmpdir:
            out_dir = Path(tmpdir) / "fail3_book"
            with self.assertRaises(PlanningValidationError) as ctx:
                planner.plan_new_project(
                    project_id="fail3_book",
                    genre_id="urban_brainhole",
                    output_dir=out_dir,
                    author_level="lv0_1",
                    target_chapters=80,
                )

            # Strictly 3 attempts for batch 2 (never a 4th request!)
            self.assertEqual(batch2_calls["count"], 3)
            # 1 bible + 1 batch1 + 3 batch2 = 5 total calls
            self.assertEqual(len(llm.requests), 5)
            msg = str(ctx.exception)
            self.assertIn("scenes", msg)
            self.assertIn("2/8", msg)
            self.assertIn("location_time", msg)
            self.assertIn("nonexistent_sword", msg)
            self.assertFalse(out_dir.exists())

    def test_scene_batch_rejects_unknown_character_and_asset_ids_in_collector(self) -> None:
        """T5 §6.5: Collector catches unknown character/asset/style ids, missing fields, prefix & suppress runs at once."""
        recipe = get_genre("urban_brainhole")
        bible = _make_valid_bible(self.safe_names, author_level="lv0_1", target_chapters=80)

        # Direct bible collector check with multiple simultaneous defects
        bad_bible = _make_valid_bible(self.safe_names, author_level="lv0_1", target_chapters=80)
        bad_bible["project"]["title"] = "《超长违规书名超过十五个字符书名号》"
        bad_bible["project"]["alternate_titles"] = ["短名1", "短名2"]
        bad_bible["characters"][0]["name"] = "顾寒洲"  # AI blacklist name
        bible_errors = _collect_bible_errors(
            bad_bible, author_level="lv0_1", target_chapters=80, recipe=recipe
        )
        joined_bible = "\n".join(bible_errors)
        self.assertGreaterEqual(len(bible_errors), 3)
        self.assertIn("title", joined_bible)
        self.assertIn("alternate_titles", joined_bible)
        self.assertIn("顾寒洲", joined_bible)

        # Direct scene batch collector check: unknown character + unknown asset + unknown style + suppress>2
        batch = [_make_valid_scene(ch) for ch in range(1, 11)]
        batch[0]["active_characters"] = ["hero", "unknown_char_id"]
        batch[1]["active_assets"] = ["unknown_asset_id"]
        batch[2]["style_profile_ids"] = ["unknown_style_id"]
        batch[3]["emotion_polarity"] = "suppress"
        batch[4]["emotion_polarity"] = "suppress"
        batch[5]["emotion_polarity"] = "suppress"
        scene_errors = _collect_scene_batch_errors(batch, bible, (1, 10), recipe=recipe)
        joined_scene = "\n".join(scene_errors)
        self.assertIn("unknown_char_id", joined_scene)
        self.assertIn("unknown_asset_id", joined_scene)
        self.assertIn("unknown_style_id", joined_scene)
        self.assertIn("suppress", joined_scene)

        # Cross-batch consecutive suppress check (2 trailing + 1st of next batch = 3 -> rejected)
        next_batch = [_make_valid_scene(ch) for ch in range(11, 21)]
        next_batch[0]["emotion_polarity"] = "suppress"
        cross_errors = _collect_scene_batch_errors(
            next_batch, bible, (11, 20), recipe=recipe, prior_trailing_suppress=2
        )
        self.assertTrue(any("suppress" in e for e in cross_errors))

        # Direct master_outline batch collector check
        bad_mo = [{"chapter": 1, "title": "", "summary": "ok"}]
        mo_errors = _collect_master_outline_batch_errors(bad_mo, (1, 25))
        self.assertGreaterEqual(len(mo_errors), 2)


class TestAsyncWizardAndDBMigration(unittest.TestCase):
    def _advance_to_ready(self, base: str, token: str) -> str:
        code, view = _http_json(
            base + "/api/wizard/sessions",
            method="POST",
            token=token,
            payload={"idea": "外卖骑手雨夜捡到能收明天新闻的旧手机"},
        )
        self.assertEqual(code, 201)
        sid = view["session_id"]
        for _ in range(4):
            code, view = _http_json(
                base + f"/api/wizard/sessions/{sid}/answer",
                method="POST",
                token=token,
                payload={"choice": 0},
            )
            self.assertEqual(code, 200)
        self.assertEqual(view["status"], "READY_TO_CONFIRM")
        return sid

    def test_wizard_async_confirm_202_progress_created_and_failed_retry(self) -> None:
        """T5 §6.6: POST confirm returns 202 -> polls PLANNING + progress -> CREATED; failure -> FAILED + retry."""
        tmp = Path(tempfile.mkdtemp(prefix="xsb_t5_wiz_"))
        try:
            gate_allow_finish = threading.Event()
            reached_scenes = threading.Event()
            should_fail = {"flag": False}
            seen_briefs: List[str] = []

            class _FakeSummary:
                def __init__(self, bid: str) -> None:
                    self.book_id = bid

            def fake_create_book(req: AutoCreateBookRequest, *, on_progress: Any = None) -> _FakeSummary:
                seen_briefs.append(req.creative_brief)
                if on_progress is not None:
                    on_progress({"stage": "bible", "batch": 1, "batches": 1})
                    on_progress({"stage": "scenes", "batch": 3, "batches": 8})
                reached_scenes.set()
                gate_allow_finish.wait(timeout=5.0)
                if should_fail["flag"]:
                    raise PlanningValidationError("scenes batch 3/8: missing required field 'stop_condition'")
                return _FakeSummary(req.project_id)

            server = create_server(
                host="127.0.0.1",
                port=0,
                db_path=tmp / "matrix.db",
                projects_root=tmp / "projects",
                admin_token=ADMIN_TOKEN,
                companion_secret="t5-companion-secret-0123456789abcdef",
                provider=FakeWizardOptionsLLM(author_level="lv0_1"),
            )
            # Wire controlled create_book_fn directly onto wizard for deterministic synchronization
            server.wizard._create_book_fn = fake_create_book
            port = server.server_address[1]
            base = f"http://127.0.0.1:{port}"
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                # --- Flow 1: 202 -> PLANNING + progress -> concurrent confirm rejected -> CREATED ---
                sid1 = self._advance_to_ready(base, ADMIN_TOKEN)
                sid_other = self._advance_to_ready(base, ADMIN_TOKEN)

                code, body = _http_json(
                    base + f"/api/wizard/sessions/{sid1}/confirm",
                    method="POST",
                    token=ADMIN_TOKEN,
                    payload={"pen_name": "试跑"},
                )
                self.assertEqual(code, 202)
                self.assertEqual(body["status"], "PLANNING")
                self.assertIn("progress", body)

                self.assertTrue(reached_scenes.wait(timeout=3.0))
                code_p, prog_view = _http_json(
                    base + f"/api/wizard/sessions/{sid1}", token=ADMIN_TOKEN
                )
                self.assertEqual(code_p, 200)
                self.assertEqual(prog_view["status"], "PLANNING")
                self.assertEqual(prog_view["progress"]["stage"], "scenes")
                self.assertEqual(prog_view["progress"]["batch"], 3)
                self.assertEqual(prog_view["progress"]["batches"], 8)

                # Concurrent confirm on same OR different session must be rejected
                code_dup, _ = _http_json(
                    base + f"/api/wizard/sessions/{sid1}/confirm",
                    method="POST",
                    token=ADMIN_TOKEN,
                    payload={"pen_name": "试跑"},
                )
                self.assertIn(code_dup, (400, 409))
                code_conc, _ = _http_json(
                    base + f"/api/wizard/sessions/{sid_other}/confirm",
                    method="POST",
                    token=ADMIN_TOKEN,
                    payload={"pen_name": "试跑"},
                )
                self.assertIn(code_conc, (400, 409))

                gate_allow_finish.set()
                deadline = time.monotonic() + 5.0
                final_view: Dict[str, Any] = {}
                while time.monotonic() < deadline:
                    _, final_view = _http_json(
                        base + f"/api/wizard/sessions/{sid1}", token=ADMIN_TOKEN
                    )
                    if final_view["status"] == "CREATED":
                        break
                    time.sleep(0.05)
                self.assertEqual(final_view["status"], "CREATED")
                self.assertTrue(final_view["book_id"].startswith("book_"))
                self.assertEqual(final_view["error_message"], "")
                # Verify T5 §4.4 brief wording included
                self.assertIn("人物姓名只是示意", seen_briefs[0])

                # --- Flow 2: failure -> FAILED + full error_message -> retry -> CREATED ---
                should_fail["flag"] = True
                reached_scenes.clear()
                gate_allow_finish.set()
                code_f, body_f = _http_json(
                    base + f"/api/wizard/sessions/{sid_other}/confirm",
                    method="POST",
                    token=ADMIN_TOKEN,
                    payload={"pen_name": "试跑"},
                )
                self.assertEqual(code_f, 202)
                self.assertEqual(body_f["status"], "PLANNING")

                deadline = time.monotonic() + 5.0
                failed_view: Dict[str, Any] = {}
                while time.monotonic() < deadline:
                    _, failed_view = _http_json(
                        base + f"/api/wizard/sessions/{sid_other}", token=ADMIN_TOKEN
                    )
                    if failed_view["status"] == "FAILED":
                        break
                    time.sleep(0.05)
                self.assertEqual(failed_view["status"], "FAILED")
                self.assertIn("scenes batch 3/8", failed_view["error_message"])
                self.assertIn("stop_condition", failed_view["error_message"])

                # Retry after FAILED -> PLANNING -> CREATED
                should_fail["flag"] = False
                code_r, body_r = _http_json(
                    base + f"/api/wizard/sessions/{sid_other}/confirm",
                    method="POST",
                    token=ADMIN_TOKEN,
                    payload={"pen_name": "试跑"},
                )
                self.assertEqual(code_r, 202)
                self.assertEqual(body_r["status"], "PLANNING")
                self.assertEqual(body_r["error_message"], "")

                deadline = time.monotonic() + 5.0
                retried_view: Dict[str, Any] = {}
                while time.monotonic() < deadline:
                    _, retried_view = _http_json(
                        base + f"/api/wizard/sessions/{sid_other}", token=ADMIN_TOKEN
                    )
                    if retried_view["status"] == "CREATED":
                        break
                    time.sleep(0.05)
                self.assertEqual(retried_view["status"], "CREATED")
                self.assertTrue(retried_view["book_id"].startswith("book_"))
            finally:
                server.shutdown_and_close()
                server.db.close()
        finally:
            shutil.rmtree(tmp)

    def test_legacy_wizard_sessions_table_auto_migrates_progress_json(self) -> None:
        """T5 §6.7: Existing SQLite DB with old wizard_sessions schema (no progress_json) upgrades cleanly."""
        tmp = Path(tempfile.mkdtemp(prefix="xsb_t5_mig_"))
        try:
            db_path = tmp / "legacy.db"
            # Create old table schema without progress_json
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE wizard_sessions (
                    session_id TEXT PRIMARY KEY,
                    idea TEXT NOT NULL,
                    status TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    choices_json TEXT NOT NULL DEFAULT '[]',
                    options_json TEXT NOT NULL DEFAULT '[]',
                    book_id TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO wizard_sessions
                    (session_id, idea, status, step_index, choices_json, options_json, book_id, error_message, created_at, updated_at)
                VALUES
                    ('0123456789abcdef0123456789abcdef', '旧库测试题材', 'READY_TO_CONFIRM', 4, '[]', '[]', '', '', '2026-11-18T10:00:00+08:00', '2026-11-18T10:00:00+08:00');
                """
            )
            conn.commit()
            conn.close()

            db = StockpileDB(db_path)
            try:
                cols = {
                    row[1]
                    for row in db._conn.execute("PRAGMA table_info(wizard_sessions)").fetchall()
                }
                self.assertIn("progress_json", cols)
                sess = db.get_wizard_session("0123456789abcdef0123456789abcdef")
                self.assertEqual(sess["progress"], {})
                db.update_wizard_progress(
                    "0123456789abcdef0123456789abcdef",
                    {"stage": "scenes", "batch": 2, "batches": 8},
                )
                sess2 = db.get_wizard_session("0123456789abcdef0123456789abcdef")
                self.assertEqual(sess2["progress"], {"stage": "scenes", "batch": 2, "batches": 8})
            finally:
                db.close()
        finally:
            shutil.rmtree(tmp)

    def test_startup_marks_orphaned_planning_sessions_as_failed(self) -> None:
        """T5 §6.8: Leftover PLANNING session from a prior crash/restart becomes FAILED on startup with clear reason."""
        tmp = Path(tempfile.mkdtemp(prefix="xsb_t5_rst_"))
        try:
            db_path = tmp / "restart.db"
            sid = "abcdef0123456789abcdef0123456789"
            db = StockpileDB(db_path)
            db.create_wizard_session(sid, "重启中断测试", [])
            db.update_wizard_session(
                sid,
                status="PLANNING",
                step_index=4,
                choices=[],
                options=[],
                progress={"stage": "scenes", "batch": 4, "batches": 8},
            )
            db.close()

            # Simulate service restart creating a fresh NewBookWizard
            db2 = StockpileDB(db_path)
            try:
                wiz = NewBookWizard(
                    db=db2,
                    provider_fn=lambda: FakeWizardOptionsLLM(),
                    create_book_fn=lambda req: req,
                )
                view = wiz.get(sid)
                self.assertEqual(view["status"], "FAILED")
                self.assertEqual(view["error_message"], "服务重启中断了立项，请重新点确认")
            finally:
                db2.close()
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
