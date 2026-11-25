"""U6 Offline Full-Stack E2E Verification for Milestone M2 (Pipeline v2.1).

Verifies the complete "One-Volume-One-Dungeon + Strong Planner / Daily Writer Routing +
End-of-Volume 6-Check Hard Health Gate + Lock -> Repair -> CLI Unlock -> Vol 2 Advance"
lifecycle end-to-end using a real local loopback HTTP server and zero external network calls.
"""

from __future__ import annotations

import http.server
import io
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List, Tuple

import yaml

from xiaoshuo_bot import cli
from xiaoshuo_bot.blueprint import load_blueprint
from xiaoshuo_bot.blueprint_planner import expand_blueprint_chapters
from xiaoshuo_bot.engine import NovelGenerationEngine
from xiaoshuo_bot.judge import PairwiseJudge
from xiaoshuo_bot.llm import (
    RoleModelRouter,
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_HY4,
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
    WorkBuddyModelCatalog,
)
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.models import ScanReport
from xiaoshuo_bot.volume_health import (
    LOCK_STATE_LOCKED,
    LOCK_STATE_PASSED,
    VOLUME_HEALTH_CHECK_IDS,
    VolumeHealthLockError,
    load_volume_locks,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CATALOG_MODELS: Dict[str, str] = {
    WORKBUDDY_SLOT_HY4_FLASH: "wb/e2e-hy4-flash",
    WORKBUDDY_SLOT_HY4: "wb/e2e-hy4-manual",
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: "wb/e2e-deepseek-v41-flash",
    WORKBUDDY_SLOT_MIMO_V2_6_PRO: "wb/e2e-mimo-v26-pro",
}


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _sse_reply(model: str, content: str) -> List[str]:
    c1 = json.dumps(
        {
            "id": "chatcmpl-u6-e2e",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
        },
        ensure_ascii=False,
    )
    c2 = json.dumps(
        {
            "id": "chatcmpl-u6-e2e",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        },
        ensure_ascii=False,
    )
    return [f"data: {c1}\n\n", f"data: {c2}\n\n", "data: [DONE]\n\n"]


def _json_reply(model: str, content: str) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-u6-e2e",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
    }


def _health_payload(*, passed: bool, failed_id: str = "VH_05_THREE_ACT_AND_HOOK") -> str:
    checks = []
    for cid in VOLUME_HEALTH_CHECK_IDS:
        is_fail = (not passed) and (cid == failed_id)
        checks.append(
            {
                "id": cid,
                "status": "FAIL" if is_fail else "PASS",
                "chapters": [2] if is_fail else [1, 2],
                "evidence": "卷末钩子收得太平淡，缺少进入第2卷的突发事件驱动。" if is_fail else "本项核验完整通过。",
                "repair_hint": "重写第2章结尾两段，补入新地图突发委托事件钩子。" if is_fail else "",
            }
        )
    return json.dumps(
        {
            "verdict": "PASS" if passed else "FAIL",
            "checks": checks,
            "summary": "全卷六项体检全部合格，放行进入下一卷。" if passed else "第2章卷末钩子未达标，锁卷待修。",
        },
        ensure_ascii=False,
    )


class _AlwaysPassChecker:
    def scan(self, text: str, **_: object) -> ScanReport:
        return ScanReport(
            passed=True,
            char_count=len(text),
            paragraph_count=max(1, text.count("\n\n") + 1),
        )


def _write_mini_two_volume_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    blueprint = {
        "premise": {
            "logline": "江城夜市摊主许青川借旧卦簿一卷一副本连破在逃通缉重案。",
            "selling_points": ["夜市算卦一卦抓通缉犯", "一卷一副本零套路重复"],
            "selling_point_keywords": ["算卦", "相面", "拆字", "通缉", "夜市"],
            "benchmark_refs": [{"title": "对标热书", "why": "强节奏反差与悬疑闭环"}],
            "target_readers": "番茄都市脑洞读者",
        },
        "target_total_chars": 100000,
        "chapter_chars_range": [2050, 2650],
        "protagonist": {
            "character_id": "mc",
            "desire": "清还阴债查清沉案真相",
            "flaw": "嘴硬独扛",
            "arc": "孤身摊主成长为破局核心",
            "voice_card": {"speech_patterns": "短句冷幽默", "catchphrases": ["看事五十，每晚三卦。"]},
        },
        "golden_finger": {
            "rules": "蓝皮旧卦簿逐卷解锁传统占法，绝无系统面板",
            "cost": "每晚三卦交租，越界反噬增债",
            "limits": "不测彩票股价",
            "upgrade_path": [
                {"volume": 1, "unlock": "第一页相面篇"},
                {"volume": 2, "unlock": "第二页拆字篇"},
            ],
        },
        "scale_rules": [
            {"ledger_key": "cash", "policy": "bounded_realistic", "hard_max": 500000, "unit": "元"},
        ],
        "volumes": [
            {
                "index": 1,
                "title": "南门夜摊初试相面",
                "chapter_start": 1,
                "chapter_end": 2,
                "protagonist_goal": "立住夜市算卦摊口碑并拿下摊霸暗账",
                "antagonist_id": "vol01_boss",
                "cool_point_modes": ["夜市相面三步断死穴", "当众送通缉犯进巡捕房"],
                "climax": "砸开古玩仓夹墙人赃并获",
                "payoff": "拿下临江老街青瓦铺租约",
                "enters": ["mc", "detective_ally", "vol01_boss"],
                "exits": [{"id": "vol01_boss", "how": "被宋海潮带队刑拘"}],
                "map": None,
                "key_scenes": ["南门夜市头一卦拿下抢劫通缉犯", "古玩仓夹墙照出矿运黑账"],
                "foreshadows_plant": ["fs_old_tag"],
                "foreshadows_payoff": [],
                "antagonist_motive": "垄断夜市地盘掩护销赃",
                "new_gameplay": "相面篇三步断死穴配夜市守摊规反差",
                "mainline_step": "起获二十年前矿运铜牌与黑账",
                "daily_case_quota": 3,
                "fugitive_case_quota": 1,
                "forbidden_from_prev_volume": [],
            },
            {
                "index": 2,
                "title": "老街青瓦铺拆字打假",
                "chapter_start": 3,
                "chapter_end": 4,
                "protagonist_goal": "稳住老街铺并以拆字篇撕破伪国学大师骗局",
                "antagonist_id": "vol02_boss",
                "cool_point_modes": ["蘸墨拆字拆偏旁揭空壳账", "公证台白板当众拆暗扣"],
                "climax": "文化馆公证台当众拍下左袖磁控暗扣",
                "payoff": "何小满入铺成为首位反诈搭档",
                "enters": ["he_xiaoman", "vol02_boss"],
                "exits": [{"id": "vol02_boss", "how": "涉嫌集资诈骗当众带离"}],
                "map": {"location_id": "loc_linjiang_shop", "name": "临江老街青瓦算卦铺"},
                "key_scenes": ["文化馆蘸墨拆善字点破空壳公司", "何小满白板直播拍下磁控暗扣"],
                "foreshadows_plant": [],
                "foreshadows_payoff": ["fs_old_tag"],
                "antagonist_motive": "披伪国学外衣收割老人养老金",
                "new_gameplay": "拆字篇蘸墨拆偏旁配白板公证镜头打假",
                "mainline_step": "缴获船员登记卡指向北郊冷库",
                "daily_case_quota": 4,
                "fugitive_case_quota": 1,
                "forbidden_from_prev_volume": ["禁止重复夜市摊霸踢摊起手式"],
            },
        ],
        "cast": [
            {
                "id": "mc",
                "name": "许青川",
                "role": "算卦摊主",
                "first_volume": 1,
                "exit_volume": None,
                "appearance_card": "藏青帆布外套右腕暗墨印",
                "voice_card": {"speech_patterns": "简练冷幽默", "catchphrases": []},
                "arc": "从摊主成长为破局核心",
            },
            {
                "id": "detective_ally",
                "name": "宋海潮",
                "role": "重案巡捕副队长",
                "first_volume": 1,
                "exit_volume": None,
                "appearance_card": "寸头眉骨旧疤斜挎证件包",
                "voice_card": {"speech_patterns": "重物证链", "catchphrases": []},
                "arc": "查清亲叔冤案",
                "independent_goal": "查清亲叔宋卫国二十年前弃船污名真相",
                "flaw_or_setback": "死抠程序前期常与主角冲突",
                "stage_arcs": ["第1阶段：磨合成搭档暗查档案", "第2阶段：顶住停职压力翻盘"],
            },
            {
                "id": "he_xiaoman",
                "name": "何小满",
                "role": "反诈搭档兼学徒",
                "first_volume": 2,
                "exit_volume": None,
                "appearance_card": "黑框眼镜反诈帆布包",
                "voice_card": {"speech_patterns": "语速快条理清", "catchphrases": []},
                "arc": "成长为独立反诈品牌主理人",
                "independent_goal": "还清母亲手术债并做出自己的独立反诈铺面",
                "flaw_or_setback": "急功近利爱追爆款险闯祸",
                "stage_arcs": ["第1阶段：带欠条入铺建反诈库", "第2阶段：独立盘铺平起平坐"],
            },
            {
                "id": "vol01_boss",
                "name": "钱万顺",
                "role": "夜市摊霸",
                "first_volume": 1,
                "exit_volume": 1,
                "appearance_card": "矮胖金戒棕色手包",
                "voice_card": {"speech_patterns": "假江湖腔", "catchphrases": []},
                "arc": "栽赃不成反入狱",
            },
            {
                "id": "vol02_boss",
                "name": "齐景明",
                "role": "伪国学大师",
                "first_volume": 2,
                "exit_volume": 2,
                "appearance_card": "山羊胡左袖磁控开关",
                "voice_card": {"speech_patterns": "半吊子文言", "catchphrases": []},
                "arc": "当众拆穿落网",
            },
        ],
        "foreshadow_plan": [
            {"id": "fs_old_tag", "title": "二十年前矿运铜牌", "plant_volume": 1, "payoff_volume": 2}
        ],
        "ending": "沉案告破归于人间烟火。",
        "style": {
            "genre_id": "urban_brainhole",
            "allow_pov_inner_monologue": True,
            "humor": True,
            "narration_rules": "短段白描，禁系统面板",
            "sample_text": "青布未动，许青川抬眼报了句价。",
        },
        "adaptation": {"visual_hook": "夜市暖灯青布卦簿", "protagonist_look": "藏青外套青年"},
    }
    (root / "blueprint.yaml").write_text(
        yaml.safe_dump(blueprint, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (root / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "project_id": "u6_e2e_book",
                "title": "夜市算卦测试卷",
                "genre": "都市脑洞",
                "synopsis": "端到端测试用书",
                "default_pov_character": "mc",
                "contract_track": "serial_20k",
                "max_rewrite_retries": 0,
                "initial_balances": {"cash": 10000},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "characters.yaml").write_text(
        yaml.safe_dump(
            {
                "characters": [
                    {
                        "id": m["id"],
                        "name": m["name"],
                        "role": m["role"],
                        "personality": "稳重",
                        "speech_style": m["voice_card"]["speech_patterns"],
                        "initial_status": "alive",
                    }
                    for m in blueprint["cast"]
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "assets.yaml").write_text(
        yaml.safe_dump(
            {
                "assets": [
                    {
                        "id": "old_book",
                        "name": "旧卦簿",
                        "location": "摊位",
                        "holder": "许青川",
                        "condition": "完好",
                        "physical_details": "蓝皮线装",
                    }
                ],
                "initial_balances": {"cash": 10000},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "foreshadows.yaml").write_text(
        yaml.safe_dump(
            {
                "foreshadows": [
                    {
                        "id": "fs_old_tag",
                        "title": "二十年前矿运铜牌",
                        "plant_chapter": 1,
                        "due_chapter": 4,
                        "plant_instruction": "埋下铜牌",
                        "payoff_instruction": "兑现铜牌",
                        "plant_keywords": ["矿运铜牌"],
                        "payoff_keywords": ["矿运黑账"],
                        "status": "pending",
                        "cross_volume": True,
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "style_profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "style_profiles": [
                    {
                        "id": "book_style",
                        "name": "白描",
                        "granularity_notes": "短句",
                        "sample_text": "青布未动。",
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "outline.yaml").write_text(
        yaml.safe_dump(
            {
                "volume": 1,
                "scenes": [
                    {
                        "chapter": 1,
                        "scene_id": "ch0001_v1_open",
                        "internal_title": "夜市开卦",
                        "public_title": "夜市头一卦抓通缉犯",
                        "pov_character": "mc",
                        "active_characters": ["mc", "detective_ally", "vol01_boss"],
                        "active_assets": ["old_book"],
                        "style_profile_ids": ["book_style"],
                        "location_time": "秋夜，南门夜市",
                        "location_id": "loc_nanmen_stall",
                        "min_chars": 2050,
                        "max_chars": 2650,
                        "events": ["踢摊", "算卦抓逃犯", "拍上矿运铜牌"],
                        "stop_condition": "止于铜牌现身",
                        "cliffhanger_hook": "黑车灯打上青布",
                        "volume": 1,
                        "calendar_days_elapsed": 1,
                        "ledger_mutations": [{"ledger_key": "cash", "delta": 150, "reason": "三卦"}],
                    },
                    {
                        "chapter": 2,
                        "scene_id": "ch0002_v1_end",
                        "internal_title": "古玩仓收网",
                        "public_title": "砸开夹墙拿下摊霸",
                        "pov_character": "mc",
                        "active_characters": ["mc", "detective_ally", "vol01_boss"],
                        "active_assets": ["old_book"],
                        "style_profile_ids": ["book_style"],
                        "location_time": "次夜，旧古玩仓",
                        "location_id": "loc_antique_warehouse",
                        "min_chars": 2050,
                        "max_chars": 2650,
                        "events": ["围堵", "破墙", "刑拘钱万顺"],
                        "stop_condition": "止于老街租约",
                        "cliffhanger_hook": "旧卦簿翻开第二页拆字篇",
                        "volume": 1,
                        "calendar_days_elapsed": 1,
                        "ledger_mutations": [{"ledger_key": "cash", "delta": 3000, "reason": "奖励"}],
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class TestU6M2FullStackVolumeLifecycle(unittest.TestCase):
    def test_30vol_blueprint_fixture_loads_with_all_m2_fields(self) -> None:
        bp = load_blueprint(REPO_ROOT / "tests" / "fixtures" / "blueprint_30vols_valid.yaml")
        self.assertEqual(len(bp.volumes), 30)
        self.assertEqual(bp.volumes[0].chapter_start, 1)
        self.assertEqual(bp.volumes[-1].chapter_end, 1000)
        self.assertTrue(all(v.new_gameplay and v.mainline_step and v.antagonist_motive for v in bp.volumes))
        self.assertTrue(any(m.independent_goal and m.flaw_or_setback and m.stage_arcs for m in bp.cast))

    def test_full_m2_lifecycle_lock_block_repair_cli_unlock_and_vol2_advance(self) -> None:
        catalog = WorkBuddyModelCatalog.from_mapping(CATALOG_MODELS)
        calls: List[Dict[str, Any]] = []
        state = {"repaired_marker": "【已补强卷末新地图钩子】"}

        def _handler(payload: Dict[str, Any]) -> Tuple[int, Any, bool]:
            model = str(payload.get("model") or "")
            is_stream = bool(payload.get("stream", False))
            messages = payload.get("messages") or []
            user_msg = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
            calls.append({"model": model, "user_msg": user_msg})

            # Strict invariant: Hy4 manual-only MUST NEVER be called automatically
            self.assertNotEqual(model, CATALOG_MODELS[WORKBUDDY_SLOT_HY4])

            # 1. End-of-volume 6-point health judge (routed to strong MiMo)
            if "VH_01_NO_ROUTINE_REPEAT" in user_msg and "VH_06_LEDGER_AND_DEBT_MATH" in user_msg:
                repaired = state["repaired_marker"] in user_msg
                body = _health_payload(passed=repaired)
                return 200, (_sse_reply(model, body) if is_stream else _json_reply(model, body)), is_stream

            # 2. Volume 2 outline expansion (routed to strong MiMo planner)
            if "blueprint_expand" in user_msg:
                scenes_payload = {
                    "scenes": [
                        {
                            "chapter": 3,
                            "scene_id": "ch0003_v2_shop_open",
                            "internal_title": "老街青瓦铺开张拆字",
                            "public_title": "蘸墨拆善字，当众掀翻伪大师",
                            "beat_type": "反手打脸",
                            "emotion_polarity": "burst",
                            "pov_character": "mc",
                            "active_characters": ["mc", "he_xiaoman", "vol02_boss"],
                            "active_assets": ["old_book"],
                            "style_profile_ids": ["book_style"],
                            "location_time": "秋日午后，临江老街青瓦算卦铺",
                            "location_id": "loc_linjiang_shop",
                            "min_chars": 2100,
                            "max_chars": 2500,
                            "scene_goal": "稳住青瓦铺并揭穿齐景明养老骗局",
                            "scene_obstacle": "齐景明带水军砸场挑衅",
                            "scene_outcome": "蘸墨拆字点破空壳公司，何小满留证入铺",
                            "events": [
                                "齐景明登门挑衅写下善字",
                                "许青川蘸墨拆偏旁点破空壳养老账",
                                "何小满推黑框眼镜用白板直播拍下磁控暗扣线索",
                            ],
                            "stop_condition": "止于公证台约战",
                            "cliffhanger_hook": "齐景明当场拍下文化馆公证台赌约宣称明午三点直播对决",
                            "segment_comment_cue": "拆字直戳伪大师死穴",
                            "volume": 2,
                            "calendar_days_elapsed": 1,
                            "post_scene_asset_mutations": [],
                            "ledger_mutations": [{"ledger_key": "cash", "delta": 500, "reason": "开铺首日卦金"}],
                            "character_mutations": [],
                            "expectation_directives": [],
                        },
                        {
                            "chapter": 4,
                            "scene_id": "ch0004_v2_notary_stage",
                            "internal_title": "文化馆公证台收网",
                            "public_title": "公证台白板实锤，伪大师左袖露馅",
                            "beat_type": "高潮收网",
                            "emotion_polarity": "burst",
                            "pov_character": "mc",
                            "active_characters": ["mc", "detective_ally", "he_xiaoman", "vol02_boss"],
                            "active_assets": ["old_book"],
                            "style_profile_ids": ["book_style"],
                            "location_time": "次日午后，江城文化馆公证台",
                            "location_id": "loc_culture_hall",
                            "min_chars": 2100,
                            "max_chars": 2500,
                            "scene_goal": "彻底坐实齐景明诈骗并兑现矿运黑账线索",
                            "scene_obstacle": "齐景明企图抵赖并指使托儿起哄",
                            "scene_outcome": "何小满高清抓拍暗扣，宋海潮带队人赃并获",
                            "events": [
                                "文化馆公证台当众展开第二轮拆字对质",
                                "何小满高清特写拍下齐景明左袖磁控暗扣并展示资金流白板",
                                "宋海潮带巡捕抓捕齐景明并从其公文包起出矿运黑账关联船员卡",
                            ],
                            "stop_condition": "止于北郊冷库密条现身",
                            "cliffhanger_hook": "船员登记卡背面赫然印着北郊冷库老曹的名字与午夜交货地址",
                            "segment_comment_cue": "左袖暗扣铁证拍进镜头",
                            "volume": 2,
                            "calendar_days_elapsed": 1,
                            "post_scene_asset_mutations": [],
                            "ledger_mutations": [{"ledger_key": "cash", "delta": 2000, "reason": "协作奖励"}],
                            "character_mutations": [],
                            "expectation_directives": [],
                        },
                    ]
                }
                body = json.dumps(scenes_payload, ensure_ascii=False)
                return 200, (_sse_reply(model, body) if is_stream else _json_reply(model, body)), is_stream

            # 3. Daily chapter writing (routed to DeepSeek-V4.1-Flash writer; distinct per chapter to satisfy L1)
            writer_seq = sum(
                1
                for c in calls
                if c["model"] == CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH]
            )
            chapter_proses = {
                1: (
                    "秋夜南门夜市灯火通明，许青川稳坐褪色青布后以相面三步点破在逃抢劫通缉犯，"
                    "夜班跑腿小哥慌忙拍下二十年前的矿运铜牌求救，宋海潮闻讯赶到封锁巷口。"
                ),
                2: (
                    "次夜旧古玩仓雨雾沉沉，许青川与宋海潮联手砸开西墙夹层起获伪造账册与亡师旧照，"
                    "摊霸钱万顺当场人赃并获被刑拘，蓝皮旧卦簿在桌角悄然显出第二页拆字篇字迹。"
                ),
                3: (
                    "临江老街青瓦算卦铺首日挂牌开张，伪国学大师齐景明带托儿登门写下善字挑衅，"
                    "许青川蘸墨拆偏旁直戳空壳养老账，何小满举起手机白板镜头当众录下袖口磁控暗扣。"
                ),
            }
            prose = chapter_proses.get(
                writer_seq,
                f"第{writer_seq}幕江城算卦断案现场，许青川以相面拆字配合宋海潮与何小满锁死在逃通缉犯物证。",
            )
            return 200, (_sse_reply(model, prose) if is_stream else _json_reply(model, prose)), is_stream

        class _ReqHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                status, body_obj, is_stream = _handler(payload)
                if is_stream:
                    self.send_response(status)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.end_headers()
                    for line in body_obj:
                        self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                else:
                    raw_out = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw_out)))
                    self.end_headers()
                    self.wfile.write(raw_out)

        server = _ThreadedHTTPServer(("127.0.0.1", 0), _ReqHandler)
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_root = Path(tmp)
                proj_dir = tmp_root / "project"
                out_dir = tmp_root / "out"
                audit_dir = out_dir / "audit"
                _write_mini_two_volume_project(proj_dir)

                router = RoleModelRouter.from_workbuddy_catalog(
                    catalog,
                    base_url=base_url,
                    api_keys=["sk-e2e-loopback"],
                    role_model_overrides={
                        "writer": CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                    },
                    sleep_fn=lambda _s: None,
                )
                # Confirm U3 strong-planner vs daily-writer split
                self.assertEqual(router.get_active_model("planner"), CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
                self.assertEqual(router.get_active_model("writer"), CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])

                project = load_project(proj_dir)
                blueprint = load_blueprint(proj_dir / "blueprint.yaml")
                strong_judge = PairwiseJudge(
                    router.as_provider("planner"),
                    judge_model=catalog.strong_planner_model(),
                    writer_model=router.get_active_model("writer"),
                )

                # Step 1: Engine writes Vol 1 (Ch 1-2) with DeepSeek -> auto 6-check via MiMo -> FAIL & LOCK!
                engine = NovelGenerationEngine(
                    project=project,
                    provider=router,
                    output_dir=out_dir,
                    checker=_AlwaysPassChecker(),
                    blueprint=blueprint,
                    volume_health_judge=strong_judge,
                    enforce_volume_health_gate=True,
                    enable_critic_polish=False,
                )
                with self.assertRaises(VolumeHealthLockError) as lock_ctx:
                    engine.run(target_chapters=[1, 2])
                self.assertEqual(lock_ctx.exception.volume_index, 1)
                self.assertIn("VH_05_THREE_ACT_AND_HOOK", lock_ctx.exception.failed_checks)

                locks_after_fail = load_volume_locks(audit_dir)
                self.assertEqual(locks_after_fail["volumes"]["1"]["lock_state"], LOCK_STATE_LOCKED)
                self.assertEqual(
                    [c["model"] for c in calls],
                    [
                        CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                        CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                        CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    ],
                )

                # Step 2: While Vol 1 is locked, Vol 2 outline expansion MUST raise with 0 LLM calls!
                calls_before_block = len(calls)
                with self.assertRaises(VolumeHealthLockError):
                    expand_blueprint_chapters(
                        project_dir=proj_dir,
                        start_chapter=3,
                        end_chapter=4,
                        provider=router,
                        audit_dir=audit_dir,
                    )
                self.assertEqual(len(calls), calls_before_block)

                # Step 3: Repair Vol 1 Ch 2 on disk and re-run CLI `volume-check` -> PASS (exit 0) & UNLOCK!
                ch2_path = out_dir / "chapters" / "chapter_0002.txt"
                ch2_path.write_text(
                    ch2_path.read_text(encoding="utf-8") + f"\n\n{state['repaired_marker']}",
                    encoding="utf-8",
                )
                cli_stdout = io.StringIO()
                with redirect_stdout(cli_stdout):
                    rc = cli.main(
                        [
                            "volume-check",
                            "--project",
                            str(proj_dir),
                            "--output",
                            str(out_dir),
                            "--volume",
                            "1",
                            "--base-url",
                            base_url,
                            "--api-key",
                            "sk-e2e-loopback",
                            "--workbuddy-hy4-flash",
                            CATALOG_MODELS[WORKBUDDY_SLOT_HY4_FLASH],
                            "--workbuddy-hy4",
                            CATALOG_MODELS[WORKBUDDY_SLOT_HY4],
                            "--workbuddy-deepseek-v4-1-flash",
                            CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                            "--workbuddy-mimo-v2-6-pro",
                            CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                        ]
                    )
                self.assertEqual(rc, 0, cli_stdout.getvalue())
                self.assertEqual(load_volume_locks(audit_dir)["volumes"]["1"]["lock_state"], LOCK_STATE_PASSED)
                self.assertEqual(calls[-1]["model"], CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])

                # Step 4: Now expand Vol 2 (Ch 3-4) via strong planner MiMo -> injects Vol 1 blacklist & cast arcs!
                exp_res = expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=3,
                    end_chapter=4,
                    provider=router,
                    audit_dir=audit_dir,
                )
                self.assertEqual(exp_res.added_chapters, [3, 4])
                self.assertEqual(exp_res.planner_model, CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
                expand_prompt = calls[-1]["user_msg"]
                self.assertIn("防套路重复黑名单", expand_prompt)
                self.assertIn("相面篇三步断死穴配夜市守摊规反差", expand_prompt)
                self.assertIn("禁止重复夜市摊霸踢摊起手式", expand_prompt)
                self.assertIn("还清母亲手术债并做出自己的独立反诈铺面", expand_prompt)
                self.assertIn("查清亲叔宋卫国二十年前弃船污名真相", expand_prompt)

                # Step 5: Engine reloads updated project and writes Vol 2 Ch 3 cleanly with DeepSeek!
                project_v2 = load_project(proj_dir)
                engine_v2 = NovelGenerationEngine(
                    project=project_v2,
                    provider=router,
                    output_dir=out_dir,
                    checker=_AlwaysPassChecker(),
                    blueprint=blueprint,
                    volume_health_judge=strong_judge,
                    enforce_volume_health_gate=True,
                    enable_critic_polish=False,
                )
                manifest = engine_v2.run(target_chapters=[3], resume=True)
                self.assertTrue(manifest["all_gates_passed"])
                self.assertEqual(manifest["chapters"][0]["chapter"], 3)
                self.assertEqual(calls[-1]["model"], CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])

                # Final invariant: zero calls ever touched manual-only Hy4
                self.assertTrue(all(c["model"] != CATALOG_MODELS[WORKBUDDY_SLOT_HY4] for c in calls))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
