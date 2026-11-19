from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.checker import AITicChecker
from xiaoshuo_bot.compiler import PromptCompiler
from xiaoshuo_bot.critic_polisher import (
    AuthorNoteViolationError,
    CriticEvaluation,
    CriticResponseParseError,
    SurgicalPolishGateError,
    TomatoCriticPolisher,
    validate_author_note,
)
from xiaoshuo_bot.genres import UnknownGenreError
from xiaoshuo_bot.llm.base import (
    LLMConfigurationError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from xiaoshuo_bot.llm.router import RoleEndpointConfig, RoleModelRouter
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.memory import MemoryPyramid
from xiaoshuo_bot.models import (
    AssetState,
    CharacterAnchor,
    CharacterMutation,
    ExpectationDirective,
    LedgerMutation,
    NovelProject,
    ProjectMeta,
    SceneOutline,
    StyleProfile,
    VolumeCapLock,
)
from xiaoshuo_bot.state import WorldStateManager


class _StubProvider(LLMProvider):
    """Deterministic in-test LLMProvider that records incoming requests."""

    def __init__(self, content_fn) -> None:
        self._content_fn = content_fn
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        content = self._content_fn(request)
        return LLMResponse(
            request_id=request.request_id,
            content=content,
            model="stub-model",
            provider="stub",
            latency_sec=0.01,
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )


def _build_tomato_project(
    *,
    genre_id: str = "urban_brainhole",
    pov_mode: str = "third_limited",
    panel_policy: str = "allow_system_tag",
    contract_track: str = "complete_150k",
) -> NovelProject:
    meta = ProjectMeta(
        project_id="test_tomato",
        title="工位刷出百亿账单",
        genre="都市脑洞",
        synopsis="测试都市脑洞逆袭大纲",
        default_pov_character="chen_mu",
        author_level="lv0_1",
        contract_track=contract_track,
        pov_mode=pov_mode,
        panel_policy=panel_policy,
        genre_id=genre_id,
    )
    characters = {
        "chen_mu": CharacterAnchor(
            id="chen_mu",
            name="陈牧",
            role="主角",
            personality="冷静果断，睚眦必报",
            speech_style="简短带刺",
            behavioral_anchors={"受压时": "先看手机时间戳再反击"},
            forbidden_ooc=["圣母原谅"],
            relations={"lin_wei": "married"},
        ),
        "lin_wei": CharacterAnchor(
            id="lin_wei",
            name="林薇",
            role="前妻",
            personality="势利虚荣",
            speech_style="阴阳怪气",
            relations={"chen_mu": "married"},
        ),
    }
    assets = {
        "briefcase": AssetState(
            id="briefcase",
            name="加密公文包",
            location="工位抽屉",
            holder="陈牧",
            condition="完好",
            physical_details="黑色密码锁，内藏原始项目底稿",
        )
    }
    styles = {
        "urban_style": StyleProfile(
            id="urban_style",
            name="都市短段样本",
            granularity_notes="多用金额数字与动作动词推进",
            sample_text="他把工牌按在桌沿，抬眼看向主管。",
        )
    }
    scene1 = SceneOutline(
        chapter=1,
        scene_id="ch01_office",
        internal_title="工位被夺反手反击",
        pov_character="chen_mu",
        active_characters=["chen_mu", "lin_wei"],
        active_assets=["briefcase"],
        style_profile_ids=["urban_style"],
        location_time="周一上午九点，海城科技大厦十二层",
        events=["主管逼交算法底稿", "系统账单激活", "当众群发原始提交记录"],
        stop_condition="全集团公告群弹出附件截图时立即截断",
        min_chars=2000,
        max_chars=2600,
        beat_type="打脸反杀",
        emotion_polarity="mixed",
        cliffhanger_hook="董事长内线电话突然打进办公室座机",
        segment_comment_cue="工位门禁卡当场被锁，你猜陈牧兜里还剩几块钱？",
        location_id="tech_tower_12f",
        calendar_days_elapsed=1,
    )
    scene2 = SceneOutline(
        chapter=2,
        scene_id="ch02_auction",
        internal_title="离婚清算与资产破局",
        pov_character="chen_mu",
        active_characters=["chen_mu", "lin_wei"],
        active_assets=["briefcase"],
        style_profile_ids=["urban_style"],
        location_time="周二下午，海城公证处",
        events=["当场办理离婚协议", "动用返现资金收购核心股权"],
        stop_condition="公证员递出股权变更回执时停笔",
        min_chars=2000,
        max_chars=2600,
        beat_type="实力碾压",
        emotion_polarity="burst",
        cliffhanger_hook="神秘风投大佬亲自在门外拦车递名片",
        segment_comment_cue="当场甩出买楼合同，前妻表情值多少分？",
        location_id="notary_hall",
        calendar_days_elapsed=2,
        expectation_directives=[
            ExpectationDirective(
                id="EXP_DIVORCE",
                title="三日内拿回千万违约金",
                due_chapter=4,
                description="让前妻和主管吐出侵占分成",
                action="open",
            )
        ],
    )
    return NovelProject(
        meta=meta,
        characters=characters,
        assets=assets,
        foreshadows={},
        style_profiles=styles,
        scenes=[scene1, scene2],
        initial_balances={"cash": 5000.0},
        volume_cap_locks={
            "1:cash": VolumeCapLock(
                ledger_key="cash", volume=1, max_value=10_000_000.0, unit="元"
            )
        },
    )


class TestPromptCompilerV2(unittest.TestCase):
    def test_first_person_allow_panel_vs_legacy_baiwu_dukou_prompts(self) -> None:
        # 1. First-person + allow_system_tag Tomato project
        rules_project = _build_tomato_project(
            genre_id="suspense_rules",
            pov_mode="first_person",
            panel_policy="allow_system_tag",
            contract_track="serial_20k",
        )
        rules_state = WorldStateManager(rules_project)
        rules_compiler = PromptCompiler(rules_project)
        sys_p, usr_p = rules_compiler.compile_scene_prompt(rules_project.scenes[0], rules_state)

        self.assertIn("第一人称限知视角", sys_p)
        self.assertIn("唯一视点人物：陈牧", sys_p)
        self.assertNotIn("严禁叙述段出现第一人称“我”", sys_p)
        self.assertNotIn("严禁出现我", sys_p)
        self.assertIn("允许在正文内穿插受控系统面板/规则提示【...】", sys_p)
        self.assertIn("受控系统标签【", sys_p)
        self.assertNotIn("第一个字符必须是正文第 1 句的第 1 个汉字", sys_p)
        self.assertIn("竖屏短段呼吸排版", sys_p)
        self.assertIn("规则推理自洽", usr_p)

        fake_report = AITicChecker(
            pov_mode="first_person",
            panel_policy="allow_system_tag",
            contract_track="serial_20k",
        ).scan("今天天气晴朗，风和日丽。", chapter=1)
        _, rewrite_usr = rules_compiler.compile_rewrite_prompt(
            rules_project.scenes[0], rules_state, "今天天气晴朗，风和日丽。", fake_report
        )
        self.assertIn("受控系统标签【", rewrite_usr)
        self.assertNotIn("严禁前置空格、章节标题或【...】面板", rewrite_usr)

        # 2. Legacy third_limited + forbid project (baiwu_dukou)
        legacy_project = load_project(ROOT / "projects" / "baiwu_dukou")
        legacy_state = WorldStateManager(legacy_project)
        legacy_compiler = PromptCompiler(legacy_project)
        leg_sys, leg_usr = legacy_compiler.compile_scene_prompt(legacy_project.scenes[0], legacy_state)

        self.assertIn("唯一视点人物：林晚", leg_sys)
        self.assertIn("严禁叙述段出现第一人称“我”", leg_sys)
        self.assertIn("任何【...】系统面板", leg_sys)
        self.assertNotIn("竖屏短段呼吸排版", leg_sys)

        _, leg_rewrite_usr = legacy_compiler.compile_rewrite_prompt(
            legacy_project.scenes[0], legacy_state, "测试草稿", fake_report
        )
        self.assertIn("严禁前置空格、章节标题或【...】面板", leg_rewrite_usr)

    def test_injects_5d_state_memory_pyramid_genre_card_and_tomato_hooks(self) -> None:
        project = _build_tomato_project(
            genre_id="urban_brainhole",
            pov_mode="third_limited",
            panel_policy="allow_system_tag",
            contract_track="complete_150k",
        )
        state = WorldStateManager(project)
        # Mutate 5D state: divorce + injury + chapter 1 completion (opens expectation & advances calendar)
        state.apply_character_mutation(
            CharacterMutation(
                character_id="lin_wei",
                relation_target_id="chen_mu",
                relation_label="divorced",
                injury="扭伤手腕",
                injury_recovery_chapters=3,
            ),
            chapter=1,
        )
        state.advance_after_chapter(project.scenes[1], "两人签完字离开公证处，随后在海城开启新项目。")

        pyramid = MemoryPyramid()
        for ch in range(1, 12):
            pyramid.record_chapter(
                ch,
                f"第{ch}章，陈牧在海城推进收购计划。\n他收好加密公文包，记下关键时间戳。",
                title=f"第{ch}章测试",
                character_ids=["chen_mu", "lin_wei"] if ch == 1 else ["chen_mu"],
                asset_ids=["briefcase"],
                entity_names={"chen_mu": "陈牧", "lin_wei": "林薇", "briefcase": "加密公文包"},
                entity_facts={"chen_mu": f"Ch.{ch} 掌控局势"} if ch in (1, 11) else None,
            )

        compiler = PromptCompiler(project)
        _, usr_prompt = compiler.compile_scene_prompt(
            project.scenes[1], state, memory_pyramid=pyramid
        )

        # 5D state assertions
        self.assertIn("cash=5000", usr_prompt)
        self.assertIn("硬上限锁死 <= 1e+07 元", usr_prompt)
        self.assertIn("老公", usr_prompt)
        self.assertIn("丈夫", usr_prompt)
        self.assertIn("EXP_DIVORCE", usr_prompt)
        self.assertIn("三日内拿回千万违约金", usr_prompt)
        self.assertIn("扭伤手腕(还需恢复2章)", usr_prompt)
        self.assertIn("虚拟时间与季节", usr_prompt)

        # L0-L3 MemoryPyramid assertions
        self.assertIn("[Memory_Pyramid_L0_L3]", usr_prompt)
        self.assertIn("[L0 上章尾锚点]", usr_prompt)
        self.assertIn("[L1 近五章摘要]", usr_prompt)
        self.assertIn("[L2 十章阶段纪要]", usr_prompt)
        self.assertIn("[L3 登场实体历史溯源]", usr_prompt)

        # T03 genre card + sanitizers + Tomato scene hooks
        self.assertIn("现代都市金融与职场常识", usr_prompt)
        self.assertIn("「清华大学」->「海城大学」", usr_prompt)
        self.assertIn("「公安局」->「市局巡捕」", usr_prompt)
        self.assertIn("实力碾压", usr_prompt)
        self.assertIn("当场甩出买楼合同，前妻表情值多少分？", usr_prompt)
        self.assertIn("神秘风投大佬亲自在门外拦车递名片", usr_prompt)

    def test_unknown_genre_id_raises_without_fallback(self) -> None:
        project = _build_tomato_project(genre_id="nonexistent_genre")
        state = WorldStateManager(project)
        compiler = PromptCompiler(project)
        with self.assertRaises(UnknownGenreError):
            compiler.compile_scene_prompt(project.scenes[0], state)


class TestTomatoCriticPolisher(unittest.TestCase):
    def _clean_eight_paragraph_draft(self) -> str:
        # Sentence lengths intentionally varied (burstiness_sigma >= 10) and 100% compliant with AITicChecker
        paras = [
            "陈牧推开玻璃门，工位上的电脑黑着屏幕，主管已经把他的纸箱踢到了走廊垃圾桶旁边。",
            "“签了字。”主管敲着桌面。",
            "周围几个同事低头假装核对报表，键盘声停了半拍，没有人抬头看他一眼。",
            "第四段原本写的有些罗嗦，陈牧站在原地想了很多关于过去三年的委屈和项目加班的事，甚至还回忆起了刚入职那天。",
            "第五段继续解释他为什么需要这份工资来交房租，并且反复描述大厅里的空调风吹在脸上让他觉得很难受。",
            "他拔下主板背面的备份盘，指尖摩挲着金属外壳的缺口，直接点开了集团全员公告邮箱。",
            "附件上传完成，时间戳定格在昨晚十一点零四分，比主管那份篡改后的提交记录整整早了九个小时。",
            "手机屏幕忽然亮起，董事长秘书的号码出现在来电显示上，走廊尽头主管的脸色瞬间刷白。",
        ]
        return "\n\n".join(paras)

    def test_evaluate_chapter_and_surgical_polish_byte_for_byte_and_post_gate(self) -> None:
        draft = self._clean_eight_paragraph_draft()
        original_paras = [p for p in draft.split("\n\n")]

        # Spin up an in-process simulated CPA SSE HTTP server so RoleModelRouter is tested end-to-end
        recorded_bodies: List[Dict[str, Any]] = []

        class _SSEHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                recorded_bodies.append(body)
                user_msg = body["messages"][-1]["content"]
                if "按 0-based 索引编号的正文段落" in user_msg:
                    content = json.dumps(
                        {
                            "first_screen_hook": 88,
                            "cool_point_density": 74,
                            "comment_interactivity": 81,
                            "pacing_drag": 62,
                            "overall_score": 76.25,
                            "weak_paragraphs": [3, 4],
                            "paragraph_reasons": {
                                "3": "回忆注水拖节奏",
                                "4": "心理解释冗长，缺乏动作",
                            },
                            "critique": "首屏干脆，但第4、5段严重拖沓注水，立刻砍掉内心废话。",
                        },
                        ensure_ascii=False,
                    )
                else:
                    content = json.dumps(
                        {
                            "replacements": {
                                "3": "陈牧没看那只纸箱，弯腰捡起地上的工牌卡套，指节在金属门框上叩了两声。",
                                "4": "“三百块打发我？”他把一张银行流水单拍在主管手背上，油墨印在对方腕表旁。",
                            }
                        },
                        ensure_ascii=False,
                    )
                sse_payload = (
                    ": keepalive\n\n"
                    f"data: {json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
                    f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                    "data: [DONE]\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(sse_payload)))
                self.end_headers()
                self.wfile.write(sse_payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            router = RoleModelRouter(
                {
                    "critic": RoleEndpointConfig(
                        role="critic",
                        base_url=base_url,
                        model="critic-model",
                        api_keys=["sk-test-critic"],
                    ),
                    "polisher": RoleEndpointConfig(
                        role="polisher",
                        base_url=base_url,
                        model="polisher-model",
                        api_keys=["sk-test-polisher"],
                    ),
                }
            )
            checker = AITicChecker(
                pov_mode="third_limited",
                panel_policy="forbid",
                contract_track="complete_150k",
            )
            cp = TomatoCriticPolisher(router, checker=checker)

            evaluation = cp.evaluate_chapter(draft, chapter=1)
            self.assertEqual(evaluation.first_screen_hook, 88.0)
            self.assertEqual(evaluation.cool_point_density, 74.0)
            self.assertEqual(evaluation.comment_interactivity, 81.0)
            self.assertEqual(evaluation.pacing_drag, 62.0)
            self.assertEqual(evaluation.weak_paragraphs, [3, 4])

            result = cp.surgical_polish(draft, evaluation, chapter=1)
            self.assertEqual(result.replaced_indices, [3, 4])
            self.assertTrue(result.scan_report.passed)

            # Verify ONLY flagged paragraphs +1/-1 context (2, 3, 4, 5) were sent to polisher
            polisher_user_msg = recorded_bodies[1]["messages"][-1]["content"]
            self.assertIn(original_paras[2], polisher_user_msg)
            self.assertIn(original_paras[3], polisher_user_msg)
            self.assertIn(original_paras[4], polisher_user_msg)
            self.assertIn(original_paras[5], polisher_user_msg)
            self.assertNotIn(original_paras[0], polisher_user_msg)
            self.assertNotIn(original_paras[1], polisher_user_msg)
            self.assertNotIn(original_paras[6], polisher_user_msg)
            self.assertNotIn(original_paras[7], polisher_user_msg)

            # Verify ALL untouched paragraphs remain 100% byte-for-byte identical
            polished_paras = result.polished_text.split("\n\n")
            self.assertEqual(len(polished_paras), 8)
            for idx in (0, 1, 2, 5, 6, 7):
                self.assertEqual(polished_paras[idx], original_paras[idx])
            self.assertNotEqual(polished_paras[3], original_paras[3])
            self.assertNotEqual(polished_paras[4], original_paras[4])
        finally:
            server.shutdown()
            server.server_close()

    def test_evaluate_merges_diagnostics_and_rejects_malformed_or_missing_role(self) -> None:
        # Draft with 2 bystander-shock clichés -> diagnostics bystander_shock_paragraphs = [1, 2]
        shock_draft = (
            "陈牧推门而入，把合同甩在黑胡桃木桌上。\n\n"
            "四周瞬间鸦雀无声，所有人都不敢喘气。\n\n"
            "围观群众面面相觑，吓得倒抽一口凉气。\n\n"
            "他拿起印章盖了下去。"
        )
        critic_stub = _StubProvider(
            lambda _: json.dumps(
                {
                    "first_screen_hook": 80,
                    "cool_point_density": 75,
                    "comment_interactivity": 70,
                    "pacing_drag": 68,
                    "weak_paragraphs": [0],
                    "critique": "开篇还不够狠。",
                },
                ensure_ascii=False,
            )
        )
        checker = AITicChecker(contract_track="complete_150k")
        cp = TomatoCriticPolisher(critic_provider=critic_stub, checker=checker)
        eval_res = cp.evaluate_chapter(shock_draft, chapter=2)
        self.assertEqual(eval_res.llm_weak_paragraphs, [0])
        self.assertEqual(eval_res.checker_warning_paragraphs, [1, 2])
        self.assertEqual(eval_res.weak_paragraphs, [0, 1, 2])

        # Malformed JSON -> CriticResponseParseError
        bad_json_cp = TomatoCriticPolisher(critic_provider=_StubProvider(lambda _: "not json at all"))
        with self.assertRaises(CriticResponseParseError):
            bad_json_cp.evaluate_chapter(shock_draft, chapter=2)

        # Out-of-range score -> CriticResponseParseError
        bad_score_cp = TomatoCriticPolisher(
            critic_provider=_StubProvider(
                lambda _: json.dumps(
                    {
                        "first_screen_hook": 120,
                        "cool_point_density": 70,
                        "comment_interactivity": 70,
                        "pacing_drag": 70,
                        "weak_paragraphs": [],
                    }
                )
            )
        )
        with self.assertRaises(CriticResponseParseError):
            bad_score_cp.evaluate_chapter(shock_draft, chapter=2)

        # Missing 'critic' role in RoleModelRouter -> LLMConfigurationError (no silent downgrade)
        writer_only_router = RoleModelRouter(
            {
                "writer": RoleEndpointConfig(
                    role="writer",
                    base_url="http://127.0.0.1:8317/v1",
                    model="writer-m",
                    api_keys=["sk-w"],
                )
            }
        )
        no_critic_cp = TomatoCriticPolisher(writer_only_router)
        with self.assertRaises(LLMConfigurationError):
            no_critic_cp.evaluate_chapter(shock_draft, chapter=2)

    def test_surgical_polish_raises_gate_error_if_polisher_introduces_critical_violation(self) -> None:
        draft = self._clean_eight_paragraph_draft()
        # Polisher injects forbidden AI tic + real city/police identifier ("北京市公安局", "不禁一愣")
        bad_polisher = _StubProvider(
            lambda _: json.dumps(
                {
                    "replacements": {
                        "3": "主管不禁一愣，眸光微闪，直接拨通了北京市公安局的报警电话。",
                        "4": "陈牧嘴角勾起一抹弧度，声音淬了冰。",
                    }
                },
                ensure_ascii=False,
            )
        )
        checker = AITicChecker(
            pov_mode="third_limited",
            panel_policy="forbid",
            contract_track="complete_150k",
        )
        cp = TomatoCriticPolisher(polisher_provider=bad_polisher, checker=checker)
        eval_stub = CriticEvaluation(
            first_screen_hook=85,
            cool_point_density=70,
            comment_interactivity=75,
            pacing_drag=60,
            overall_score=72.5,
            weak_paragraphs=[3, 4],
        )
        with self.assertRaises(SurgicalPolishGateError) as ctx:
            cp.surgical_polish(draft, eval_stub, chapter=1)
        self.assertIsNotNone(ctx.exception.report)
        self.assertFalse(ctx.exception.report.passed)

    def test_surgical_polish_prompt_preserves_foreshadow_keywords_and_length_bounds(self) -> None:
        from xiaoshuo_bot.models import ForeshadowItem

        draft = (
            "雨点砸在玻璃柜上。\n\n"
            "潘怀安拨动摆轮，秒针不动，游丝仍紧。\n\n"
            "冯念慈把两百块押金压在柜台上。"
        )
        fs_plant = ForeshadowItem(
            id="FS-02",
            title="被按住的秒针",
            plant_chapter=1,
            due_chapter=20,
            plant_instruction="写清秒针不动且游丝仍紧",
            payoff_instruction="比对撬痕",
            plant_keywords=["秒针不动", "游丝仍紧", "轴心撬痕"],
            payoff_keywords=["折断的表冠轴"],
        )
        captured: List[LLMRequest] = []

        def _good_polish(req: LLMRequest) -> str:
            captured.append(req)
            return json.dumps(
                {"replacements": {"1": "潘怀安捏住镊子轻拨摆轮，秒针不动，游丝仍紧。"}},
                ensure_ascii=False,
            )

        checker = AITicChecker(
            pov_mode="third_limited",
            panel_policy="forbid",
            contract_track="complete_150k",
        )
        cp = TomatoCriticPolisher(polisher_provider=_StubProvider(_good_polish), checker=checker)
        scene = SceneOutline(
            chapter=1,
            scene_id="ch0001_scene",
            internal_title="雨夜开盖",
            pov_character="mc",
            active_characters=["mc"],
            active_assets=[],
            style_profile_ids=[],
            location_time="雨夜",
            events=["开盖验表"],
            stop_condition="停在刻痕",
            min_chars=20,
            max_chars=500,
        )
        res = cp.surgical_polish(
            draft,
            [1],
            scene=scene,
            plant_foreshadows=[fs_plant],
            chapter=1,
        )
        self.assertTrue(res.scan_report.passed)
        self.assertEqual(len(captured), 1)
        self.assertGreaterEqual(captured[0].max_tokens, 6000)
        self.assertIn("必保伏笔原词：秒针不动、游丝仍紧", captured[0].user_prompt)
        self.assertIn("20～500 字", captured[0].user_prompt)

        # If polisher still paraphrases away mandatory foreshadow cues, gate must STILL reject it
        bad_cp = TomatoCriticPolisher(
            polisher_provider=_StubProvider(
                lambda _: json.dumps(
                    {"replacements": {"1": "潘怀安轻拨摆轮，秒针仍未走动，游丝绷着。"}},
                    ensure_ascii=False,
                )
            ),
            checker=checker,
        )
        with self.assertRaises(SurgicalPolishGateError):
            bad_cp.surgical_polish(draft, [1], scene=scene, plant_foreshadows=[fs_plant], chapter=1)

    def test_generate_author_note_length_and_anti_diversion_guards(self) -> None:
        sample_chapter = "陈牧挂断内线电话，把加密底稿投上了集团大屏，全楼骤然响起急促的电梯警报。"
        good_note = "董事长连夜来电到底要保谁？下一章直接当面掀桌！求一波催更和五星好评，评论区猜中明天的反转加更一章！"
        ok_cp = TomatoCriticPolisher(polisher_provider=_StubProvider(lambda _: good_note))
        note = ok_cp.generate_author_note(
            sample_chapter, cliffhanger_hook="董事长秘书打来加急内线"
        )
        self.assertEqual(note, good_note)
        self.assertLessEqual(len(note), 180)

        # > 180 chars must fail
        long_note = "催" * 181
        long_cp = TomatoCriticPolisher(polisher_provider=_StubProvider(lambda _: long_note))
        with self.assertRaises(AuthorNoteViolationError):
            long_cp.generate_author_note(sample_chapter)

        # Diversion words must fail
        for bad_text in (
            "后续大纲先发在微信群里了，大家速看！",
            "加我VX：abc123领番外福利。",
            "欢迎来QQ群123456一起催更讨论剧情！",
            "更多彩蛋请关注同名公众号更新。",
            "想看后续剧透的去小红书搜我笔名。",
        ):
            with self.subTest(bad_text=bad_text):
                with self.assertRaises(AuthorNoteViolationError):
                    validate_author_note(bad_text)


if __name__ == "__main__":
    unittest.main()
