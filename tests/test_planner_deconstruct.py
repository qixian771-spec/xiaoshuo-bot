from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Callable, Dict, List
import unittest

from xiaoshuo_bot.deconstructor import BestsellerDeconstructor, DeconstructedBlueprint
from xiaoshuo_bot.evolution import (
    MotifWeightStore,
    PublishedChapterImmutableError,
    ReaderComment,
    ReviewFeedbackOptimizer,
)
from xiaoshuo_bot.llm.base import LLMRequest
from xiaoshuo_bot.llm.router import RoleModelRouter
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.models import (
    ExpectationState,
    ForeshadowItem,
    VolumeCapLock,
    WorldStateSnapshot,
)
from xiaoshuo_bot.namer import CharacterNamer
from xiaoshuo_bot.plagiarism_guard import (
    CrossBookCollisionError,
    PlagiarismAndCollisionGuard,
    PlagiarismGuardError,
    PlagiarismViolationError,
)
from xiaoshuo_bot.planner import AutoNovelPlanner, PlanningValidationError


def _build_scene_dict(
    chapter: int,
    *,
    volume: int = 1,
    pov_id: str = "hero",
    active_chars: List[str] | None = None,
    active_assets: List[str] | None = None,
    style_id: str = "urban_brainhole_human",
    location_id: str = "haicheng_office",
    public_title: str | None = None,
    events: List[str] | None = None,
    emotion_polarity: str | None = None,
    expectation_directives: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    polarity = emotion_polarity or ("burst" if chapter % 3 == 0 else ("suppress" if chapter % 3 == 1 else "mixed"))
    return {
        "chapter": chapter,
        "scene_id": f"ch{chapter:04d}_scene",
        "internal_title": f"第{chapter}幕推进",
        "public_title": public_title or f"风暴前夜{chapter}",
        "pov_character": pov_id,
        "active_characters": active_chars if active_chars is not None else [pov_id, "rival"],
        "active_assets": active_assets if active_assets is not None else ["ledger_app"],
        "style_profile_ids": [style_id],
        "location_time": f"第{chapter}日清晨，海城CBD写字楼",
        "location_id": location_id,
        "events": events or [f"第{chapter}章核心冲突交锋与利益结算"],
        "stop_condition": f"完成第{chapter}章悬念断章停笔",
        "min_chars": 2050,
        "max_chars": 2300,
        "beat_type": "escalate",
        "emotion_polarity": polarity,
        "cliffhanger_hook": f"第{chapter}章末神秘指令弹出",
        "segment_comment_cue": "这波反杀太解气",
        "volume": volume,
        "expectation_directives": expectation_directives or [],
    }


def _start_mock_sse_server(
    handler_fn: Callable[[Dict[str, Any]], str],
) -> tuple[ThreadingHTTPServer, str]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw)
            content_str = handler_fn(body)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()

            chunk_1 = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "model": body.get("model", "planner-test-model"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content_str},
                        "finish_reason": None,
                    }
                ],
            }
            chunk_2 = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "model": body.get("model", "planner-test-model"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 350},
            }
            sse_payload = (
                ": keepalive\n\n"
                f"data: {json.dumps(chunk_1, ensure_ascii=False)}\n\n"
                f"data: {json.dumps(chunk_2, ensure_ascii=False)}\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
            self.wfile.write(sse_payload)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1"


def _make_router(base_url: str) -> RoleModelRouter:
    return RoleModelRouter.from_dict(
        {
            "roles": {
                "planner": {
                    "base_url": base_url,
                    "model": "planner-test-v1",
                    "api_keys": ["test-key-planner-001"],
                    "provider_type": "openai",
                    "stream": True,
                }
            }
        }
    )


class TestPlagiarismAndCollisionGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = PlagiarismAndCollisionGuard()
        self.repo_root = Path(__file__).resolve().parents[1]

    def test_catches_eight_char_contiguous_hanzi_and_strips_idioms(self) -> None:
        ref = "暴雨夜里，他推开老旧铁门走进配电室，发现墙上挂着泛黄的值班记录本。"
        # Clean distinct text passes
        clean = "清晨阳光照进海城写字楼大厅，周振拿着竞标文件夹乘电梯直达十八层。"
        res = self.guard.check_against_benchmark(clean, [ref])
        self.assertTrue(res.passed)
        self.assertLessEqual(res.max_lcs_hanzi, 7)

        # Idiom + punctuation stripping: 4-char idiom + 4 chars should NOT inflate to 8
        idiom_only = "他简直不可思议地摇头，转身离开现场。"
        ref_idiom = "对方也不可思议地摇头，随即收起文件。"
        res_idiom = self.guard.check_against_benchmark(idiom_only, [ref_idiom])
        self.assertTrue(res_idiom.passed)

        # Exactly 8 contiguous Hanzi (even separated by commas/spaces) MUST fail
        copied_8 = "某天夜里，他推开老旧，铁门走进去办别的事。"
        # "他推开老旧铁门走进" is 9 chars; let's test exact 8: "推开老旧铁门走进" (8 chars)
        copied_exact_8 = "天黑之后推开老旧，铁门走进旁边的杂物间。"
        with self.assertRaises(PlagiarismViolationError) as ctx:
            self.guard.check_against_benchmark(copied_exact_8, [ref])
        self.assertGreaterEqual(ctx.exception.max_lcs_hanzi, 8)
        self.assertIn("推开老旧铁门走进", ctx.exception.matched_lcs_text)

    def test_catches_sliding_window_4gram_jaccard_over_5_percent(self) -> None:
        # Build a 220-char benchmark passage of unique Hanzi phrases
        base_units = [
            "青铜钟表齿轮缓慢转动", "海港潮汐拍打花岗石堤", "旧式煤油灯芯微微摇晃",
            "牛皮信封封口沾着灰尘", "木制柜台抽屉夹层暗槽", "北站行李寄存处铜钥匙",
            "铁皮档案柜底层的日志", "显微镜下的竹浆纤维纹", "渡口晨雾散开汽笛拉响",
            "法务总监手里的黑公文", "红铁丹防锈漆刺鼻腥气", "钢笔帽旋转半圈的冷光",
            "老街拆迁警戒线外行人", "缺口骨瓷杯倒扣木托盘", "修复师拇指摩挲旧伤疤",
            "黄杨木镊盒铜锁绿锈斑", "调度室凌晨通话旧磁带", "夜班保安值守配电箱旁",
            "防潮密封袋里的复写单", "驳船吃水线超载原始账",
        ]
        ref_text = "，".join(base_units) + "。"
        # Rearrange 8 chunks of 6 Hanzi each into a 200-char candidate (LCS <= 6, yet 4-gram Jaccard >= 5%)
        filler_units = [
            "高楼电梯液晶屏幕跳跃", "早高峰地铁车厢人潮涌", "咖啡厅玻璃窗映出街景",
            "外卖骑手穿梭城市辅路", "证券交易所大屏红绿线", "跨海大桥晚风吹动路灯",
            "实验室离心机高速旋转", "图书馆古籍阅览室安静", "机场跑道夜航引导灯亮",
            "老式收音机传出电流声", "港口集装箱吊机缓缓移", "写字楼地下车库回声响",
        ]
        mixed_chunks: List[str] = []
        for idx in range(8):
            mixed_chunks.append(base_units[idx][:6])
            mixed_chunks.append(filler_units[idx])
        mixed_chunks.extend(filler_units[8:])
        candidate = "，".join(mixed_chunks) + "。"

        lcs_len, _ = self.guard.longest_common_contiguous_hanzi(candidate, ref_text)
        self.assertLessEqual(lcs_len, 7)

        with self.assertRaises(PlagiarismViolationError) as ctx:
            self.guard.check_against_benchmark(candidate, [ref_text])
        self.assertGreaterEqual(ctx.exception.max_4gram_jaccard, 0.05)

    def test_cross_book_collision_against_existing_projects(self) -> None:
        projects_dir = self.repo_root / "projects"
        # 1. Collide by duplicate title against baiwu_dukou
        with self.assertRaises(CrossBookCollisionError):
            self.guard.check_cross_book_collision(
                title="白雾渡口",
                projects_dir=projects_dir,
                opening_summary="一个完全无关的都市外卖故事。",
            )

        # 2. Collide by opening 3-chapter conflict/persona against baiwu_dukou
        with self.assertRaises(CrossBookCollisionError):
            self.guard.check_cross_book_collision(
                title="江雾钟表铺之谜",
                projects_dir=projects_dir,
                opening_summary=(
                    "市博物馆器物修复师收到停在三点十七分的铜壳怀表，来到老街敬山钟表铺发现红铁丹防锈漆鞋印，"
                    "随后潜入旧港务局地下档案室找出被撕走的第19页调度日志，最终打开北站零四九号寄存柜。"
                ),
                protagonist_persona="市博物馆金属与钟表器物修复师，冷静重实证",
            )

        # 3. Golden-finger mechanism collision in same genre against temp matrix dir
        with tempfile.TemporaryDirectory() as tmpdir:
            matrix_dir = Path(tmpdir) / "projects"
            existing_dir = matrix_dir / "existing_urban_01"
            (existing_dir / "outline").mkdir(parents=True, exist_ok=True)
            import yaml

            (existing_dir / "project.yaml").write_text(
                yaml.safe_dump(
                    {
                        "project_id": "existing_urban_01",
                        "title": "工位返现大亨",
                        "genre": "都市脑洞",
                        "genre_id": "urban_brainhole",
                        "synopsis": "职场被辞退后激活十倍消费返现系统买下写字楼。",
                        "default_pov_character": "hero",
                        "author_level": "lv0_1",
                        "contract_track": "complete_150k",
                        "golden_finger_type": "十倍返现账单",
                        "golden_finger_description": "职场维权十倍赔付返现系统",
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (existing_dir / "characters.yaml").write_text(
                yaml.safe_dump(
                    {
                        "characters": [
                            {
                                "id": "hero",
                                "name": "周振",
                                "role": "算法工程师",
                                "personality": "冷静",
                                "speech_style": "干脆",
                            }
                        ]
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (existing_dir / "assets.yaml").write_text(
                yaml.safe_dump({"assets": []}, allow_unicode=True), encoding="utf-8"
            )
            (existing_dir / "foreshadows.yaml").write_text(
                yaml.safe_dump({"foreshadows": []}, allow_unicode=True), encoding="utf-8"
            )
            (existing_dir / "style_profiles.yaml").write_text(
                yaml.safe_dump({"style_profiles": []}, allow_unicode=True), encoding="utf-8"
            )
            scene_item = _build_scene_dict(1, active_chars=["hero"], active_assets=[])
            scene_item["style_profile_ids"] = []
            (existing_dir / "outline" / "vol_01.yaml").write_text(
                yaml.safe_dump(
                    {"volume": 1, "scenes": [scene_item]},
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(CrossBookCollisionError):
                self.guard.check_cross_book_collision(
                    title="完全不同的新书名",
                    projects_dir=matrix_dir,
                    genre_id="urban_brainhole",
                    golden_finger="十倍返现账单",
                    opening_summary="另一个海城创业故事，主角做量化基金投资。",
                )

        # 4. Clean distinct book passes
        res = self.guard.check_cross_book_collision(
            title="每天十倍返现",
            projects_dir=projects_dir,
            genre_id="urban_brainhole",
            golden_finger="外卖箱百倍跑腿奖励系统",
            opening_summary="外卖员激活外卖箱里的百倍跑腿奖励系统，在科技园逆袭收购物流公司。",
            protagonist_persona="爽朗果断的青年创业者",
        )
        self.assertTrue(res.passed)

    def test_check_against_benchmark_rejects_all_blank_chapters(self) -> None:
        candidate = "清晨阳光照进海城写字楼大厅，周振拿着竞标文件夹乘电梯直达十八层。"
        for blank_list in ([""], ["", "   "], ["\n\t", "   "]):
            with self.assertRaises(PlagiarismGuardError):
                self.guard.check_against_benchmark(candidate, blank_list)
            with self.assertRaises(PlagiarismGuardError):
                self.guard.check_against_benchmark(
                    candidate, blank_list, raise_on_violation=False
                )

        mixed_res = self.guard.check_against_benchmark(
            candidate, ["", "   ", "完全不相关的古代驿站与风雪夜归人故事内容。"]
        )
        self.assertTrue(mixed_res.passed)
        self.assertEqual(mixed_res.checked_against_count, 1)


class TestBestsellerDeconstructor(unittest.TestCase):
    def test_deconstruct_extracts_5d_blueprint_and_retries_on_leakage(self) -> None:
        benchmark_chapters = [
            "第3章：沈墨站在江北拍卖行二楼包厢，盯着台下那块号称能测出灵脉纯度的残缺玉盘，周围世家子弟纷纷嘲笑他出身寒门。",
            "第4章：当拍卖师落槌瞬间，沈墨指尖一弹，玉盘内藏的千年玄铁阵纹骤然亮起青光，全场世家家主脸色惨白。",
        ]
        swap_calls = {"count": 0}

        def _handler(body: Dict[str, Any]) -> str:
            user_prompt = body["messages"][-1]["content"]
            if "五维骨架提取" in user_prompt:
                return json.dumps(
                    {
                        "source_domain": "古武鉴宝流",
                        "power_topology": {
                            "protagonist_leverage": "隐藏鉴宝眼力",
                            "opponent_advantage": "世家资金垄断",
                            "reversal_mechanism": "公开拍卖现场打脸反收割",
                        },
                        "info_asymmetry": {
                            "hidden_card": "主角知晓残器真正激活方式",
                            "dramatic_irony_anchors": ["反派误以为废品", "现场众目嘲讽"],
                            "reveal_cadence": "先抑后扬两章一爆",
                        },
                        "emotion_curve": [
                            {"chapter_offset": 1, "polarity": "suppress", "intensity": 6, "beat_purpose": "受辱"},
                            {"chapter_offset": 2, "polarity": "burst", "intensity": 9, "beat_purpose": "反转"},
                        ],
                        "conflict_ladder": [
                            {"tier": 1, "antagonist_archetype": "世家旁系", "stake": "入场资格", "resolution_mode": "当众验货"}
                        ],
                        "micro_hooks_500w": ["第500字反派加价挑衅", "第1000字暗纹初亮", "第2000字高管求见"],
                    },
                    ensure_ascii=False,
                )

            swap_calls["count"] += 1
            if swap_calls["count"] == 1:
                # Deliberately leak >7 Hanzi from benchmark chapter 3 so guard catches it!
                leaked_setting = {
                    "world_backdrop": "海城科技金融区，站在江北拍卖行二楼包厢里看残缺玉盘",
                    "protagonist_identity": "被辞退的量化算法工程师",
                    "golden_finger_rule": "职场漏洞百倍赔付",
                    "core_conflict": "夺回被抢走的核心模型权属",
                }
            else:
                leaked_setting = {
                    "world_backdrop": "大夏海城金融产业园，深夜交易所大厅与创投路演厅",
                    "protagonist_identity": "被夺走核心代码的基层算法工程师",
                    "golden_finger_rule": "每日职场侵占十倍反向赔付账单",
                    "core_conflict": "以股权收购与竞业反制击穿主管利益链",
                }
            return json.dumps(
                {
                    "source_domain": "古武鉴宝",
                    "target_domain": "现代都市职场脑洞",
                    "opening_motif": "职场被夺功反手收购",
                    "golden_finger_type": "十倍赔付账单",
                    "power_topology": {"protagonist_leverage": "原始代码时间戳与赔付系统", "opponent_advantage": "人事权"},
                    "info_asymmetry": {"hidden_card": "幕后控股协议", "reveal_cadence": "每两章亮底牌"},
                    "emotion_curve": [
                        {"chapter_offset": 1, "polarity": "suppress", "intensity": 6, "beat_purpose": "夺功"},
                        {"chapter_offset": 2, "polarity": "burst", "intensity": 9, "beat_purpose": "反向收购"},
                    ],
                    "conflict_ladder": [{"tier": 1, "antagonist_archetype": "抢功主管", "stake": "年终分红"}],
                    "micro_hooks_500w": ["前500字裁员通知拍脸", "1200字系统到账提示", "2100字董事长来电"],
                    "isomorphic_setting": leaked_setting,
                },
                ensure_ascii=False,
            )

        server, base_url = _start_mock_sse_server(_handler)
        try:
            router = _make_router(base_url)
            deconstructor = BestsellerDeconstructor(router)
            bp = deconstructor.deconstruct(
                benchmark_chapters,
                target_genre_id="urban_brainhole",
                hot_keywords=["职场逆袭", "十倍返现"],
            )
            self.assertIsInstance(bp, DeconstructedBlueprint)
            self.assertTrue(bp.sanitized)
            self.assertEqual(swap_calls["count"], 2)
            self.assertIsNotNone(bp.plagiarism_report)
            assert bp.plagiarism_report is not None
            self.assertTrue(bp.plagiarism_report.passed)
            self.assertLessEqual(bp.plagiarism_report.max_lcs_hanzi, 7)
            self.assertLess(bp.plagiarism_report.max_4gram_jaccard, 0.05)
        finally:
            server.shutdown()
            server.server_close()

    def test_deconstruct_raises_when_leakage_persists_all_attempts(self) -> None:
        benchmark = [
            "第3章：陈启站在江北拍卖行二楼包厢，盯着台下那块号称能测出灵脉纯度的残缺玉盘。"
        ]

        def _handler_always_leak(body: Dict[str, Any]) -> str:
            user_prompt = body["messages"][-1]["content"]
            if "五维骨架提取" in user_prompt:
                return json.dumps(
                    {
                        "source_domain": "玄幻鉴宝",
                        "power_topology": {"leverage": "鉴宝眼"},
                        "info_asymmetry": {"card": "真品暗纹"},
                        "emotion_curve": [{"chapter_offset": 1, "polarity": "burst", "intensity": 8}],
                        "conflict_ladder": [{"tier": 1, "stake": "入场资格"}],
                        "micro_hooks_500w": ["500字竞价挑衅"],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "source_domain": "玄幻鉴宝",
                    "target_domain": "都市脑洞",
                    "power_topology": {"leverage": "鉴定器"},
                    "info_asymmetry": {"card": "暗纹"},
                    "emotion_curve": [{"chapter_offset": 1, "polarity": "burst", "intensity": 8}],
                    "conflict_ladder": [{"tier": 1, "stake": "竞拍"}],
                    "micro_hooks_500w": ["站在江北拍卖行二楼包厢里看台下竞价"],
                    "isomorphic_setting": {"world_backdrop": "现代拍卖行"},
                },
                ensure_ascii=False,
            )

        server, base_url = _start_mock_sse_server(_handler_always_leak)
        try:
            router = _make_router(base_url)
            deconstructor = BestsellerDeconstructor(router, max_attempts=2)
            with self.assertRaises(PlagiarismViolationError):
                deconstructor.deconstruct(benchmark, target_genre_id="urban_brainhole")
        finally:
            server.shutdown()
            server.server_close()


class TestAutoNovelPlanner(unittest.TestCase):
    def _build_valid_plan_payload(
        self,
        *,
        project_id: str,
        num_chapters: int,
        author_level: str,
        safe_names: List[str],
        invalid_title: bool = False,
        blacklisted_name: bool = False,
    ) -> Dict[str, Any]:
        pov_name = "顾寒洲" if blacklisted_name else safe_names[0]
        rival_name = safe_names[1]
        ally_name = safe_names[2]
        title = "这是一本绝对超过十五个汉字限制的非法长标题用来测试拦截" if invalid_title else "被裁当天到账百亿"
        scenes = [
            _build_scene_dict(
                ch,
                volume=1,
                pov_id="hero",
                active_chars=["hero", "rival"],
                active_assets=["ledger_phone"],
                style_id="urban_brainhole_human",
                location_id="haicheng_park",
            )
            for ch in range(1, num_chapters + 1)
        ]
        foreshadows = [
            {
                "id": "FS-01",
                "title": "加密算法原始时间戳",
                "plant_chapter": 1,
                "due_chapter": 10 if author_level == "lv0_1" else 65,
                "plant_instruction": "主角保留昨晚离线提交哈希值",
                "payoff_instruction": "在集团审计会上当众比对原始提交记录",
                "plant_keywords": ["哈希值", "提交记录"],
                "payoff_keywords": ["审计会", "原始记录"],
                "cross_volume": author_level == "lv2_plus",
            }
        ]
        payload: Dict[str, Any] = {
            "project": {
                "title": title,
                "synopsis": (
                    "实习算法工程师周振被主管抢走核心底稿并限期搬离工位，激活每日破局返现账单后，"
                    "反手群发原始加密时间戳并逆袭控股整条金融科技产业链。"
                ),
                "default_pov_character": "hero",
                "opening_motif": "职场被夺功反手收购",
                "golden_finger_type": "十倍返现账单",
                "golden_finger_description": "每次揭穿职场黑幕或竞业侵权按十倍公关损失结算现金与股权",
                "alternate_titles": [
                    "工位刷出百亿资金",
                    "抢我方案反手收购",
                    "被裁后主管跪求我",
                    "十倍返现职场破局",
                    "工牌一扔买下大厦",
                ],
                "alternate_blurbs": [
                    "第一版简介：被裁当天激活十倍返现账单，反手买下整栋科技大厦。",
                    "第二版简介：抢走我的核心算法？明天起我是你新任大股东。",
                    "第三版简介：兜里只剩三百块，却在职场拆穿黑幕日入千万。",
                ],
                "tweet_aliases": ["工位刷出百亿", "被裁当天暴富", "抢方案买大厦"],
                "target_word_count": num_chapters * 2200 if author_level == "lv0_1" else 1_000_000,
                "initial_balances": {"cash": 320.0},
            },
            "characters": [
                {
                    "id": "hero",
                    "name": pov_name,
                    "role": "被夺功的青年算法工程师",
                    "personality": "冷静果断，不吃哑巴亏",
                    "speech_style": "短促干脆，习惯拿数据砸脸",
                },
                {
                    "id": "rival",
                    "name": rival_name,
                    "role": "抢功部门主管",
                    "personality": "势利傲慢，欺下媚上",
                    "speech_style": "官僚做派，喜欢拿流程压人",
                },
                {
                    "id": "ally",
                    "name": ally_name,
                    "role": "风投女合伙人",
                    "personality": "敏锐果决，识人极准",
                    "speech_style": "直来直去，只看回报率",
                },
            ],
            "assets": [
                {
                    "id": "ledger_phone",
                    "name": "加密工作手机",
                    "location": "主角风衣口袋",
                    "holder": pov_name,
                    "condition": "内置十倍返现破局账单应用",
                    "physical_details": "黑色直板机，屏幕贴防窥膜",
                }
            ],
            "foreshadows": foreshadows,
            "style_profiles": [],
            "volume_cap_locks": [
                {"ledger_key": "cash", "volume": 1, "max_value": 50_000_000.0, "unit": "元"}
            ],
            "scenes": scenes,
        }
        if author_level == "lv2_plus":
            payload["master_outline"] = [
                {"chapter": ch, "title": f"阶段节点{ch}", "summary": f"第{ch}章职场/资本线推进"}
                for ch in range(1, 101)
            ]
        return payload

    def test_dual_track_lv0_1_and_lv2_plus_with_3_sources_and_extension_and_finale(self) -> None:
        safe_names = CharacterNamer().generate_cast(count=5, era="urban_brainhole", channel="male", seed=77)
        planner_state = {"mode": "normal"}

        def _handler(body: Dict[str, Any]) -> str:
            user_prompt = body["messages"][-1]["content"]
            if "续写范围" in user_prompt:
                # extend_volume at chapter 65 -> Vol 2 map change + soft reboot + higher VolumeCapLock
                scenes_ext = [
                    _build_scene_dict(
                        ch,
                        volume=2,
                        pov_id="hero",
                        active_chars=["hero", "rival"],
                        active_assets=["ledger_phone"],
                        style_id="urban_brainhole_human",
                        location_id="jingzhou_exchange",
                        public_title=f"跨城并购风暴{ch}",
                        events=[f"第{ch}章主角隐匿大股东身份进入京州交易所新地图并完成资金破局"],
                    )
                    for ch in range(65, 75)
                ]
                return json.dumps(
                    {
                        "map_name": "京州国际金融中心",
                        "location_id": "jingzhou_exchange",
                        "soft_identity_reboot": "以海外独立审计顾问新身份空降京州总行，隐藏海城千亿控股幕后人身份",
                        "volume_cap_locks": [
                            {"ledger_key": "cash", "volume": 2, "max_value": 5_000_000_000.0, "unit": "元"}
                        ],
                        "scenes": scenes_ext,
                    },
                    ensure_ascii=False,
                )

            if "终卷编号" in user_prompt:
                # plan_finale_volume resolving FS-01 and EXP-01
                finale_scenes = [
                    _build_scene_dict(
                        ch,
                        volume=3,
                        pov_id="hero",
                        active_chars=["hero", "rival"],
                        active_assets=["ledger_phone"],
                        style_id="urban_brainhole_human",
                        location_id="haicheng_summit",
                        public_title=f"终局清算{ch}",
                        events=[
                            f"第{ch}章在审计会公布原始记录完成FS-01全案收网"
                            if ch == 76
                            else f"第{ch}章完成最终并购协议与新秩序收官"
                        ],
                        expectation_directives=(
                            [
                                {
                                    "id": "EXP-01",
                                    "title": "夺回百亿基金控股权",
                                    "due_chapter": 77,
                                    "description": "当众完成控股权交割",
                                    "action": "fulfill",
                                    "evidence_keywords": ["控股权"],
                                }
                            ]
                            if ch == 77
                            else []
                        ),
                    )
                    for ch in range(75, 80)
                ]
                return json.dumps(
                    {
                        "foreshadow_resolutions": {"FS-01": 76},
                        "expectation_fulfillments": {"EXP-01": 77},
                        "scenes": finale_scenes,
                    },
                    ensure_ascii=False,
                )

            # plan_new_project
            if "lv0_1" in user_prompt:
                return json.dumps(
                    self._build_valid_plan_payload(
                        project_id="auto_lv01_book",
                        num_chapters=80,
                        author_level="lv0_1",
                        safe_names=safe_names,
                    ),
                    ensure_ascii=False,
                )
            return json.dumps(
                self._build_valid_plan_payload(
                    project_id="auto_lv2_book",
                    num_chapters=15,
                    author_level="lv2_plus",
                    safe_names=safe_names,
                ),
                ensure_ascii=False,
            )

        server, base_url = _start_mock_sse_server(_handler)
        try:
            router = _make_router(base_url)
            planner = AutoNovelPlanner(router)
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                # Source 1: T09 MotifWeightStore
                store = MotifWeightStore(tmp_path / "motif_weights.json")
                store.record_outcome(
                    "urban_brainhole",
                    "职场被夺功反手收购",
                    "十倍返现账单",
                    success=True,
                    ch2_read_through=0.66,
                    bookshelf_rate=0.24,
                )

                # A. Test lv0_1 complete 150k-200k track
                lv01_dir = tmp_path / "projects" / "auto_lv01_book"
                res_lv01 = planner.plan_new_project(
                    project_id="auto_lv01_book",
                    genre_id="urban_brainhole",
                    author_level="lv0_1",
                    output_dir=lv01_dir,
                    projects_root=Path(__file__).resolve().parents[1] / "projects",
                    hot_keywords=["职场逆袭", "神豪返现"],
                    motif_store=store,
                    seed=11,
                )
                self.assertEqual(res_lv01.contract_track, "complete_150k")
                self.assertEqual(len(res_lv01.project.scenes), 80)
                self.assertLessEqual(len(res_lv01.project.meta.title), 15)
                self.assertEqual(len(res_lv01.project.meta.alternate_titles), 5)
                self.assertEqual(len(res_lv01.project.meta.alternate_blurbs), 3)
                self.assertIn("1:cash", res_lv01.project.volume_cap_locks)
                self.assertIsNone(res_lv01.signing_application_path)
                # Ensure zero blacklisted names and zero homophone collisions via CharacterNamer
                namer_check = CharacterNamer()
                for c in res_lv01.project.characters.values():
                    namer_check.register_name(c.name)

                # B. Test lv2_plus serial_20k debut track + signing_application.md + 100ch master_outline
                lv2_dir = tmp_path / "projects" / "auto_lv2_book"
                res_lv2 = planner.plan_new_project(
                    project_id="auto_lv2_book",
                    genre_id="urban_brainhole",
                    author_level="lv2_plus",
                    output_dir=lv2_dir,
                    projects_root=Path(__file__).resolve().parents[1] / "projects",
                    hot_keywords=["百倍返现", "收购公司"],
                    motif_store=store,
                    seed=22,
                )
                self.assertEqual(res_lv2.contract_track, "serial_20k")
                self.assertEqual(len(res_lv2.project.scenes), 15)
                self.assertIsNotNone(res_lv2.signing_application_path)
                assert res_lv2.signing_application_path is not None
                self.assertTrue(res_lv2.signing_application_path.is_file())
                signing_txt = res_lv2.signing_application_path.read_text(encoding="utf-8")
                self.assertIn("责编直签申请表", signing_txt)
                self.assertIn("黄金三章", signing_txt)
                self.assertIn("百章总纲", signing_txt)
                self.assertIsNotNone(res_lv2.master_outline_path)
                assert res_lv2.master_outline_path is not None
                self.assertTrue(res_lv2.master_outline_path.is_file())

                # C. Test extend_volume at chapter 65 triggers map change + soft reboot + higher VolumeCapLock
                # Simulate project currently at chapter 64 with max_published_chapter=60
                import yaml

                proj_yaml = yaml.safe_load((lv2_dir / "project.yaml").read_text(encoding="utf-8"))
                proj_yaml["max_published_chapter"] = 60
                (lv2_dir / "project.yaml").write_text(
                    yaml.safe_dump(proj_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8"
                )
                vol1_yaml = yaml.safe_load((lv2_dir / "outline" / "vol_01.yaml").read_text(encoding="utf-8"))
                vol1_yaml["scenes"] = [
                    _build_scene_dict(
                        ch,
                        volume=1,
                        pov_id="hero",
                        active_chars=["hero", "rival"],
                        active_assets=["ledger_phone"],
                        style_id="urban_brainhole_human",
                        location_id="haicheng_park",
                    )
                    for ch in range(1, 65)
                ]
                (lv2_dir / "outline" / "vol_01.yaml").write_text(
                    yaml.safe_dump(vol1_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8"
                )

                snapshot_ch64 = WorldStateSnapshot(
                    current_chapter=64,
                    current_volume=1,
                    numeric_balances={"cash": 35_000_000.0},
                    volume_cap_locks={
                        "1:cash": VolumeCapLock(ledger_key="cash", volume=1, max_value=50_000_000.0, unit="元")
                    },
                    recent_locations=["haicheng_park"],
                    active_expectations={
                        "EXP-01": ExpectationState(
                            id="EXP-01",
                            title="夺回百亿基金控股权",
                            planted_chapter=20,
                            due_chapter=77,
                            description="当众完成控股权交割",
                            status="active",
                        )
                    },
                )

                # Verify immutability guard blocks directives targeting <= max_published_chapter (60)
                optimizer = ReviewFeedbackOptimizer()
                with self.assertRaises(PublishedChapterImmutableError):
                    optimizer.optimize(
                        "auto_lv2_book",
                        [ReaderComment(content="节奏慢了", chapter=10)],
                        max_published_chapter=60,
                        target_chapter=60,
                    )

                opt_report = optimizer.optimize(
                    "auto_lv2_book",
                    [ReaderComment(content="前面很好看，后面反派蹦跶太久了节奏慢", chapter=60)],
                    max_published_chapter=60,
                    target_chapter=65,
                )

                ext_res = planner.extend_volume(
                    lv2_dir,
                    snapshot=snapshot_ch64,
                    optimization_report=opt_report,
                    batch_size=10,
                    map_change_interval=60,
                )
                self.assertTrue(ext_res.triggered_map_change)
                self.assertEqual(ext_res.volume_index, 2)
                self.assertEqual(ext_res.start_chapter, 65)
                self.assertEqual(ext_res.end_chapter, 74)
                self.assertEqual(ext_res.new_map_location_id, "jingzhou_exchange")
                self.assertTrue(ext_res.soft_identity_reboot)
                self.assertIn("2:cash", ext_res.project.volume_cap_locks)
                self.assertGreater(
                    ext_res.project.volume_cap_locks["2:cash"].max_value,
                    ext_res.project.volume_cap_locks["1:cash"].max_value,
                )
                self.assertTrue((lv2_dir / "outline" / "vol_02.yaml").is_file())

                # D. Test plan_finale_volume resolves all cross-volume foreshadows & expectations
                fin_res = planner.plan_finale_volume(
                    lv2_dir,
                    snapshot=snapshot_ch64,
                    num_chapters=5,
                )
                self.assertTrue(fin_res.project.meta.is_final_volume)
                self.assertEqual(fin_res.volume_index, 3)
                self.assertEqual(fin_res.start_chapter, 75)
                self.assertEqual(fin_res.end_chapter, 79)
                self.assertIn("FS-01", fin_res.resolved_foreshadow_ids)
                self.assertIn("EXP-01", fin_res.fulfilled_expectation_ids)
                reloaded_fin = load_project(lv2_dir, is_final_volume=True)
                self.assertTrue(reloaded_fin.meta.is_final_volume)
                self.assertEqual(reloaded_fin.foreshadows["FS-01"].due_chapter, 76)
        finally:
            server.shutdown()
            server.server_close()

    def test_invalid_llm_output_retries_up_to_3_times_then_raises_and_never_persists(self) -> None:
        safe_names = CharacterNamer().generate_cast(count=4, era="urban_brainhole", channel="male", seed=99)
        calls = {"count": 0}

        def _handler_recover_on_3(body: Dict[str, Any]) -> str:
            calls["count"] += 1
            if calls["count"] == 1:
                # Attempt 1: >15 char title (fails load_project)
                return json.dumps(
                    self._build_valid_plan_payload(
                        project_id="retry_book",
                        num_chapters=75,
                        author_level="lv0_1",
                        safe_names=safe_names,
                        invalid_title=True,
                    ),
                    ensure_ascii=False,
                )
            if calls["count"] == 2:
                # Attempt 2: blacklisted AI name "顾寒洲" (fails CharacterNamer)
                return json.dumps(
                    self._build_valid_plan_payload(
                        project_id="retry_book",
                        num_chapters=75,
                        author_level="lv0_1",
                        safe_names=safe_names,
                        blacklisted_name=True,
                    ),
                    ensure_ascii=False,
                )
            # Attempt 3: valid payload
            return json.dumps(
                self._build_valid_plan_payload(
                    project_id="retry_book",
                    num_chapters=75,
                    author_level="lv0_1",
                    safe_names=safe_names,
                ),
                ensure_ascii=False,
            )

        server, base_url = _start_mock_sse_server(_handler_recover_on_3)
        try:
            router = _make_router(base_url)
            planner = AutoNovelPlanner(router, max_validation_retries=3)
            with tempfile.TemporaryDirectory() as tmpdir:
                out_dir = Path(tmpdir) / "retry_book"
                res = planner.plan_new_project(
                    project_id="retry_book",
                    genre_id="urban_brainhole",
                    author_level="lv0_1",
                    output_dir=out_dir,
                    target_chapters=75,
                )
                self.assertEqual(res.attempts_used, 3)
                self.assertTrue((out_dir / "project.yaml").is_file())
        finally:
            server.shutdown()
            server.server_close()

        # Now test permanent failure after 3 attempts -> raises PlanningValidationError and NEVER persists
        def _handler_always_bad(body: Dict[str, Any]) -> str:
            return json.dumps(
                self._build_valid_plan_payload(
                    project_id="broken_book",
                    num_chapters=75,
                    author_level="lv0_1",
                    safe_names=safe_names,
                    blacklisted_name=True,
                ),
                ensure_ascii=False,
            )

        server2, base_url2 = _start_mock_sse_server(_handler_always_bad)
        try:
            router2 = _make_router(base_url2)
            planner2 = AutoNovelPlanner(router2, max_validation_retries=3)
            with tempfile.TemporaryDirectory() as tmpdir:
                broken_dir = Path(tmpdir) / "broken_book"
                with self.assertRaises(PlanningValidationError):
                    planner2.plan_new_project(
                        project_id="broken_book",
                        genre_id="urban_brainhole",
                        author_level="lv0_1",
                        output_dir=broken_dir,
                        target_chapters=75,
                    )
                self.assertFalse(broken_dir.exists())
        finally:
            server2.shutdown()
            server2.server_close()

    def test_plan_finale_volume_syncs_already_resolved_cross_volume_foreshadow_from_snapshot(self) -> None:
        safe_names = CharacterNamer().generate_cast(count=4, era="urban_brainhole", channel="male", seed=55)

        def _handler_finale(body: Dict[str, Any]) -> str:
            user_prompt = body["messages"][-1]["content"]
            self.assertNotIn("FS-01", user_prompt)
            self.assertIn("FS-02", user_prompt)
            finale_scenes = [
                _build_scene_dict(
                    ch,
                    volume=2,
                    pov_id="hero",
                    active_chars=["hero", "rival"],
                    active_assets=["ledger_phone"],
                    style_id="urban_brainhole_human",
                    location_id="haicheng_summit",
                    public_title=f"终局收官{ch}",
                    events=[f"第{ch}章当众公布董事会密约并完成FS-02全案收网"],
                )
                for ch in range(65, 69)
            ]
            return json.dumps(
                {
                    "foreshadow_resolutions": {"FS-02": 67},
                    "expectation_fulfillments": {},
                    "scenes": finale_scenes,
                },
                ensure_ascii=False,
            )

        server, base_url = _start_mock_sse_server(_handler_finale)
        try:
            router = _make_router(base_url)
            planner = AutoNovelPlanner(router)
            with tempfile.TemporaryDirectory() as tmpdir:
                import yaml

                proj_dir = Path(tmpdir) / "cross_vol_finale_book"
                (proj_dir / "outline").mkdir(parents=True, exist_ok=True)
                (proj_dir / "project.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "project_id": "cross_vol_finale_book",
                            "title": "工位反杀大富翁",
                            "genre": "都市脑洞",
                            "genre_id": "urban_brainhole",
                            "synopsis": "被裁当天激活返现账单，最终收购集团完成反杀。",
                            "default_pov_character": "hero",
                            "author_level": "lv2_plus",
                            "contract_track": "serial_20k",
                            "volume_index": 1,
                            "is_final_volume": False,
                            "alternate_titles": [
                                "工位刷出百亿资金",
                                "抢我方案反手收购",
                                "被裁后主管跪求我",
                                "十倍返现职场破局",
                                "工牌一扔买下大厦",
                            ],
                            "alternate_blurbs": [
                                "第一版简介：测试终卷跨卷伏笔同步。",
                                "第二版简介：测试终卷跨卷伏笔同步。",
                                "第三版简介：测试终卷跨卷伏笔同步。",
                            ],
                            "tweet_aliases": ["工位刷出百亿"],
                            "volume_cap_locks": [
                                {"ledger_key": "cash", "volume": 1, "max_value": 50_000_000.0, "unit": "元"}
                            ],
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                (proj_dir / "characters.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "characters": [
                                {
                                    "id": "hero",
                                    "name": safe_names[0],
                                    "role": "算法工程师",
                                    "personality": "冷静",
                                    "speech_style": "干脆",
                                },
                                {
                                    "id": "rival",
                                    "name": safe_names[1],
                                    "role": "主管",
                                    "personality": "势利",
                                    "speech_style": "傲慢",
                                },
                            ]
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                (proj_dir / "assets.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "assets": [
                                {
                                    "id": "ledger_phone",
                                    "name": "加密工作手机",
                                    "location": "口袋",
                                    "holder": safe_names[0],
                                    "condition": "正常",
                                    "physical_details": "黑色直板机",
                                }
                            ]
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                (proj_dir / "foreshadows.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "foreshadows": [
                                {
                                    "id": "FS-01",
                                    "title": "早期哈希证据",
                                    "plant_chapter": 1,
                                    "due_chapter": 120,
                                    "plant_instruction": "第1章保留哈希",
                                    "payoff_instruction": "比对哈希",
                                    "plant_keywords": ["哈希"],
                                    "payoff_keywords": ["比对"],
                                    "status": "planted",
                                    "cross_volume": True,
                                },
                                {
                                    "id": "FS-02",
                                    "title": "董事会密约",
                                    "plant_chapter": 1,
                                    "due_chapter": 130,
                                    "plant_instruction": "埋下密约",
                                    "payoff_instruction": "公布密约",
                                    "plant_keywords": ["密约"],
                                    "payoff_keywords": ["公布"],
                                    "status": "planted",
                                    "cross_volume": True,
                                },
                            ]
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                (proj_dir / "style_profiles.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "style_profiles": [
                                {
                                    "id": "urban_brainhole_human",
                                    "name": "都市脑洞",
                                    "granularity_notes": "节奏紧凑",
                                    "sample_text": "示范正文。",
                                }
                            ]
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                vol1_scenes = [
                    _build_scene_dict(
                        ch,
                        volume=1,
                        pov_id="hero",
                        active_chars=["hero", "rival"],
                        active_assets=["ledger_phone"],
                        style_id="urban_brainhole_human",
                        location_id="haicheng_park",
                    )
                    for ch in range(1, 65)
                ]
                (proj_dir / "outline" / "vol_01.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "volume": 1,
                            "is_final_volume": False,
                            "volume_cap_locks": [
                                {"ledger_key": "cash", "volume": 1, "max_value": 50_000_000.0, "unit": "元"}
                            ],
                            "scenes": vol1_scenes,
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )

                snapshot = WorldStateSnapshot(
                    current_chapter=64,
                    current_volume=1,
                    foreshadows={
                        "FS-01": ForeshadowItem(
                            id="FS-01",
                            title="早期哈希证据",
                            plant_chapter=1,
                            due_chapter=120,
                            plant_instruction="第1章保留哈希",
                            payoff_instruction="比对哈希",
                            plant_keywords=["哈希"],
                            payoff_keywords=["比对"],
                            status="resolved",
                            planted_evidence="Ch.1 留存哈希",
                            payoff_evidence="Ch.40 提前比对曝光",
                            cross_volume=True,
                        ),
                        "FS-02": ForeshadowItem(
                            id="FS-02",
                            title="董事会密约",
                            plant_chapter=1,
                            due_chapter=130,
                            plant_instruction="埋下密约",
                            payoff_instruction="公布密约",
                            plant_keywords=["密约"],
                            payoff_keywords=["公布"],
                            status="planted",
                            planted_evidence="Ch.1 埋下密约",
                            cross_volume=True,
                        ),
                    },
                )

                fin_res = planner.plan_finale_volume(
                    proj_dir,
                    snapshot=snapshot,
                    num_chapters=4,
                )
                self.assertEqual(fin_res.resolved_foreshadow_ids, ["FS-02"])
                self.assertTrue(fin_res.project.meta.is_final_volume)

                reloaded = load_project(proj_dir, is_final_volume=True)
                self.assertEqual(reloaded.foreshadows["FS-01"].status, "resolved")
                self.assertEqual(reloaded.foreshadows["FS-01"].planted_evidence, "Ch.1 留存哈希")
                self.assertEqual(reloaded.foreshadows["FS-01"].payoff_evidence, "Ch.40 提前比对曝光")
                self.assertEqual(reloaded.foreshadows["FS-01"].due_chapter, 120)
                self.assertEqual(reloaded.foreshadows["FS-02"].due_chapter, 67)
        finally:
            server.shutdown()
            server.server_close()

    def test_plan_new_project_rejects_unsanitized_blueprint_without_benchmark_and_validates_with_benchmark(self) -> None:
        safe_names = CharacterNamer().generate_cast(count=4, era="urban_brainhole", channel="male", seed=33)
        llm_calls = {"count": 0}

        def _handler_plan(body: Dict[str, Any]) -> str:
            llm_calls["count"] += 1
            return json.dumps(
                self._build_valid_plan_payload(
                    project_id="bp_validated_book",
                    num_chapters=75,
                    author_level="lv0_1",
                    safe_names=safe_names,
                ),
                ensure_ascii=False,
            )

        benchmark_chapters = [
            "暴雨夜里，沈墨站在江北拍卖行二楼包厢，盯着台下那块号称能测出灵脉纯度的残缺玉盘。"
        ]
        leaking_bp = DeconstructedBlueprint(
            target_genre_id="urban_brainhole",
            source_domain="玄幻鉴宝",
            target_domain="都市脑洞",
            power_topology={"protagonist_leverage": "隐藏估值眼力"},
            info_asymmetry={"hidden_card": "真实股权数据"},
            emotion_curve=[{"chapter_offset": 1, "polarity": "burst", "intensity": 8}],
            conflict_ladder=[{"tier": 1, "stake": "核心算法"}],
            micro_hooks_500w=["主角站在江北拍卖行二楼包厢观察对手动作"],
            isomorphic_setting={"world_backdrop": "海城金融园区"},
            opening_motif="职场被夺功反手收购",
            golden_finger_type="十倍返现账单",
            sanitized=False,
        )
        clean_bp = DeconstructedBlueprint(
            target_genre_id="urban_brainhole",
            source_domain="玄幻鉴宝",
            target_domain="现代科技创投",
            power_topology={"protagonist_leverage": "原始代码时间戳与赔付系统"},
            info_asymmetry={"hidden_card": "幕后控股协议"},
            emotion_curve=[{"chapter_offset": 1, "polarity": "burst", "intensity": 8}],
            conflict_ladder=[{"tier": 1, "stake": "公司控股权"}],
            micro_hooks_500w=["早高峰地铁通知栏突然跳出百亿并购资金到账提示"],
            isomorphic_setting={"world_backdrop": "大夏海城高新创投园区"},
            opening_motif="职场被夺功反手收购",
            golden_finger_type="十倍返现账单",
            sanitized=False,
        )

        server, base_url = _start_mock_sse_server(_handler_plan)
        try:
            router = _make_router(base_url)
            planner = AutoNovelPlanner(router)
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)

                # 1. Unsanitized blueprint WITHOUT benchmark_chapters -> PlanningValidationError (0 LLM calls)
                with self.assertRaises(PlanningValidationError):
                    planner.plan_new_project(
                        project_id="reject_unsanitized",
                        genre_id="urban_brainhole",
                        author_level="lv0_1",
                        output_dir=tmp_path / "reject_unsanitized",
                        blueprint=clean_bp,
                        target_chapters=75,
                    )
                self.assertEqual(llm_calls["count"], 0)
                self.assertFalse((tmp_path / "reject_unsanitized").exists())

                # 2. Unsanitized blueprint WITH leaking text and benchmark_chapters -> PlagiarismViolationError (0 LLM calls)
                with self.assertRaises(PlagiarismViolationError):
                    planner.plan_new_project(
                        project_id="reject_leaking_bp",
                        genre_id="urban_brainhole",
                        author_level="lv0_1",
                        output_dir=tmp_path / "reject_leaking_bp",
                        blueprint=leaking_bp,
                        benchmark_chapters=benchmark_chapters,
                        target_chapters=75,
                    )
                self.assertEqual(llm_calls["count"], 0)
                self.assertFalse((tmp_path / "reject_leaking_bp").exists())

                # 3. Unsanitized clean blueprint WITH benchmark_chapters -> verified and planned cleanly
                res = planner.plan_new_project(
                    project_id="bp_validated_book",
                    genre_id="urban_brainhole",
                    author_level="lv0_1",
                    output_dir=tmp_path / "bp_validated_book",
                    blueprint=clean_bp,
                    benchmark_chapters=benchmark_chapters,
                    target_chapters=75,
                )
                self.assertEqual(llm_calls["count"], 1)
                self.assertTrue((res.project_dir / "project.yaml").is_file())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
