"""Unit tests for the 4-pillar Factory V2 upgrades:
1. Long-form serialization safety: planner ledger overdraft pre-check + lv2_plus wizard preference.
2. High-CTR commercial book titles: genre template injection + anti-bland rules + title_override.
3. Prose quality upgrades: anti-prop-fiddling, anti-staccato burstiness exemplar, hook/payoff density.
4. True critic score lift (zero rubric weakening): top-K surgical target selection, per-paragraph
   length/quote budgets, post-polish same-rubric re-evaluation & score-regression guard.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from typing import Any, Dict, List, Optional

from xiaoshuo_bot.checker import AITicChecker
from xiaoshuo_bot.compiler import PromptCompiler
from xiaoshuo_bot.critic_polisher import (
    CriticEvaluation,
    SurgicalPolishResult,
    TomatoCriticPolisher,
    select_polish_targets,
)
from xiaoshuo_bot.engine import NovelGenerationEngine
from xiaoshuo_bot.genres import get_genre
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.models import (
    CharacterAnchor,
    NovelProject,
    ProjectMeta,
    ScanReport,
    SceneOutline,
    WorldStateSnapshot,
)
from xiaoshuo_bot.planner import (
    _collect_scene_batch_errors,
    _format_recipe_title_and_blurb_block,
    _load_master_outline_slice,
    find_ledger_overdrafts,
)
from xiaoshuo_bot.server.stockpile_db import StockpileDB
from xiaoshuo_bot.server.wizard import NewBookWizard, WizardError


class _FnProvider(LLMProvider):
    def __init__(self, fn) -> None:
        self._fn = fn
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        content = self._fn(request)
        return LLMResponse(
            request_id=request.request_id,
            provider="stub",
            content=content,
            model="stub-model",
            latency_sec=0.01,
        )


def _sample_project(contract_track: str = "serial_20k") -> NovelProject:
    meta = ProjectMeta(
        project_id="divination_test",
        title="让你摆摊算命，你全网连麦抓通缉犯",
        genre="都市脑洞",
        genre_id="urban_brainhole",
        synopsis="摆摊算命反杀恶霸",
        default_pov_character="mc",
        contract_track=contract_track,
        author_level="lv2_plus" if contract_track == "serial_20k" else "lv0_1",
        pov_mode="third_limited",
        panel_policy="allow_system_tag",
    )
    mc = CharacterAnchor(
        id="mc",
        name="沈青川",
        role="下山相师",
        personality="冷静毒舌",
        speech_style="一针见血",
    )
    foe = CharacterAnchor(
        id="foe",
        name="周海",
        role="地痞经理",
        personality="嚣张贪婪",
        speech_style="粗鲁傲慢",
    )
    scenes = [
        SceneOutline(
            chapter=1,
            scene_id="ch0001_scene",
            internal_title="摊前开卦",
            public_title="你印堂发黑，今晚进局子",
            pov_character="mc",
            active_characters=["mc", "foe"],
            active_assets=[],
            style_profile_ids=[],
            location_time="城中村夜市摊位 · 傍晚七点",
            events=["周海带人砸摊勒索保护费", "沈青川一眼断出周海赃款藏处并反杀"],
            stop_condition="警笛声在巷口响起",
            min_chars=2050,
            max_chars=2300,
            beat_type="破局激活与首笔立威",
            cliffhanger_hook="警车停在摊位前",
        ),
        SceneOutline(
            chapter=2,
            scene_id="ch0002_scene",
            internal_title="连麦爆火",
            public_title="一卦五百，富豪连夜刷火箭",
            pov_character="mc",
            active_characters=["mc", "foe"],
            active_assets=[],
            style_profile_ids=[],
            location_time="出租屋直播间 · 深夜十点",
            events=["开启第一场算命直播", "连麦揭穿富豪家中假古董骗局"],
            stop_condition="榜一大哥刷出十个嘉年华求见面",
            min_chars=2050,
            max_chars=2300,
            beat_type="升级验证与规则边界试探",
            cliffhanger_hook="陌生私信发来一串带血坐标",
        ),
    ]
    return NovelProject(
        meta=meta,
        characters={"mc": mc, "foe": foe},
        assets={},
        foreshadows={},
        style_profiles={},
        scenes=scenes,
        initial_balances={"cash": 500.0},
    )


def _scene_dict(chapter: int, *, ledger_mutations: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "chapter": chapter,
        "volume": 1,
        "scene_id": f"ch{chapter:04d}_scene",
        "internal_title": f"节点{chapter}",
        "public_title": f"第{chapter}个悬念标题".replace(f"第{chapter}", f"卦象{chapter}"),
        "pov_character": "mc",
        "active_characters": ["mc"],
        "active_assets": [],
        "style_profile_ids": ["urban_brainhole_human"],
        "location_time": f"地点{chapter}",
        "location_id": f"loc_{chapter}",
        "events": [f"事件{chapter}"],
        "stop_condition": f"停在{chapter}",
        "min_chars": 2050,
        "max_chars": 2300,
        "emotion_polarity": "burst",
        "beat_type": "常规推进",
        "cliffhanger_hook": f"钩子{chapter}",
        "post_scene_asset_mutations": [],
        "ledger_mutations": list(ledger_mutations or []),
        "character_mutations": [],
        "expectation_directives": [],
    }


class TestPillar1LedgerOverdraftGuard(unittest.TestCase):
    def test_collect_scene_batch_errors_blocks_ledger_overdraft(self) -> None:
        bible = {
            "project": {"default_pov_character": "mc"},
            "characters": [{"id": "mc", "name": "沈青川", "role": "主角"}],
            "assets": [],
            "style_profiles": [{"id": "urban_brainhole_human", "name": "样本"}],
        }
        batch = [
            _scene_dict(11, ledger_mutations=[{"ledger_key": "cash", "delta": -150, "reason": "买朱砂"}]),
            _scene_dict(12, ledger_mutations=[{"ledger_key": "cash", "delta": -200, "reason": "交房租"}]),
        ]
        errs = _collect_scene_batch_errors(
            batch,
            bible,
            (11, 12),
            prior_balances={"cash": 200.0},
        )
        self.assertTrue(any("STATE_OVERDRAFT" in e and "Chapter 12" in e for e in errs), errs)

        ok_batch = [
            _scene_dict(11, ledger_mutations=[{"ledger_key": "cash", "delta": 500, "reason": "一卦卦金"}]),
            _scene_dict(12, ledger_mutations=[{"ledger_key": "cash", "delta": -600, "reason": "交房租"}]),
        ]
        self.assertEqual(
            _collect_scene_batch_errors(ok_batch, bible, (11, 12), prior_balances={"cash": 200.0}),
            [],
        )

    def test_find_ledger_overdrafts_respects_snapshot_baseline(self) -> None:
        scenes = [
            _scene_dict(1, ledger_mutations=[{"ledger_key": "cash", "delta": -999, "reason": "历史章"}]),
            _scene_dict(2, ledger_mutations=[{"ledger_key": "cash", "delta": -100, "reason": "买符纸"}]),
            _scene_dict(3, ledger_mutations=[{"ledger_key": "cash", "delta": -250, "reason": "透支支出"}]),
        ]
        snap = WorldStateSnapshot(current_chapter=1, numeric_balances={"cash": 300.0})
        overdrafts = find_ledger_overdrafts(scenes, initial_balances={"cash": 0.0}, snapshot=snap)
        self.assertEqual(len(overdrafts), 1)
        self.assertEqual(overdrafts[0]["chapter"], 3)
        self.assertEqual(overdrafts[0]["after"], -50.0)

    def test_load_master_outline_slice_returns_matching_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("无", _load_master_outline_slice(root, 16, 25))
            (root / "master_outline.yaml").write_text(
                "master_outline:\n"
                "  - chapter: 15\n    title: 卷末钩子\n    summary: 十五章\n"
                "  - chapter: 16\n    title: 顺藤摸瓜\n    summary: 十六章新线索\n"
                "  - chapter: 25\n    title: 水落石出\n    summary: 二十五章破局\n"
                "  - chapter: 26\n    title: 下一卷\n    summary: 二十六章\n",
                encoding="utf-8",
            )
            sliced = _load_master_outline_slice(root, 16, 25)
            self.assertIn("第 16 章《顺藤摸瓜》：十六章新线索", sliced)
            self.assertIn("第 25 章《水落石出》：二十五章破局", sliced)
            self.assertNotIn("第 15 章", sliced)
            self.assertNotIn("第 26 章", sliced)

    def test_clean_keywords_splits_comma_packed_evidence_keywords_and_checker_hint(self) -> None:
        from xiaoshuo_bot.models import ExpectationDirective, ExpectationState, ForeshadowItem

        ed = ExpectationDirective.from_dict(
            {
                "id": "exp_zhou",
                "title": "老周下落",
                "due_chapter": 18,
                "action": "fulfill",
                "evidence_keywords": ["冯念慈电话,城郊仓库,老周活着、袖口银扣；潘园管事"],
            }
        )
        self.assertEqual(
            ed.evidence_keywords,
            ["冯念慈电话", "城郊仓库", "老周活着", "袖口银扣", "潘园管事"],
        )
        fs = ForeshadowItem.from_dict(
            {
                "id": "FS-X",
                "title": "铁函",
                "plant_chapter": 1,
                "due_chapter": 28,
                "plant_instruction": "p",
                "payoff_instruction": "q",
                "plant_keywords": ["铜钱，月牙缺口"],
                "payoff_keywords": ["祖祠铁函,同炉三十七枚"],
            }
        )
        self.assertEqual(fs.plant_keywords, ["铜钱", "月牙缺口"])
        self.assertEqual(fs.payoff_keywords, ["祖祠铁函", "同炉三十七枚"])

        scene = SceneOutline(
            chapter=18,
            scene_id="ch0018",
            internal_title="连麦",
            pov_character="mc",
            active_characters=["mc"],
            active_assets=[],
            style_profile_ids=[],
            location_time="直播间",
            events=["接电话"],
            stop_condition="结束",
            min_chars=10,
            max_chars=500,
            expectation_directives=[ed],
        )
        snap = WorldStateSnapshot(
            current_chapter=17,
            active_expectations={
                "exp_zhou": ExpectationState(
                    id="exp_zhou", title="老周下落", planted_chapter=11, due_chapter=18, status="active"
                )
            },
        )
        checker = AITicChecker(contract_track="legacy")
        # Matches when a single split keyword appears naturally in text
        ok_rep = checker.scan("电话响起，城郊仓库里果然找到了人。", scene=scene, world_state=snap, chapter=18)
        self.assertFalse(any(v.rule_id == "STATE_EXPECTATION_OVERDUE" for v in ok_rep.violations))
        # Fails with actionable keyword fix_hint when none appear
        bad_rep = checker.scan("直播间一片热闹，没人提起任何旧人。", scene=scene, world_state=snap, chapter=18)
        exp_viols = [v for v in bad_rep.violations if v.rule_id == "STATE_EXPECTATION_OVERDUE"]
        self.assertEqual(len(exp_viols), 1)
        self.assertIn("城郊仓库", exp_viols[0].fix_hint)


class TestPillar2CommercialTitlesAndWizardLongForm(unittest.TestCase):
    def test_wizard_scale_prompt_injects_genre_templates_long_form_and_supports_title_override(self) -> None:
        captured_prompts: Dict[str, str] = {}

        def _fake_llm(req: LLMRequest) -> str:
            stage = req.metadata["stage"]
            captured_prompts[stage] = req.user_prompt
            if stage == "wizard_direction":
                opts = [
                    {
                        "label": f"都市算命{i}",
                        "logline": "下山小道士直播算命破奇案",
                        "genre_id": "urban_brainhole",
                        "selling_point": "卦卦奇准全网震动",
                        "tone": "爽快悬疑",
                    }
                    for i in range(3)
                ]
            elif stage == "wizard_protagonist":
                opts = [
                    {
                        "label": f"毒舌相师{i}",
                        "persona": "穷困但嘴毒心善的年轻道士",
                        "golden_finger": "紫微天机卦象推演",
                        "cost": "泄露天机须收功德因果",
                        "arc": "从夜市摆摊到执掌玄门",
                    }
                    for i in range(3)
                ]
            elif stage == "wizard_style":
                opts = [
                    {
                        "label": f"快节奏白描{i}",
                        "description": "长短句错落，高密对话交锋",
                        "sample": "铜钱落地炸开三道裂纹，直播间十万人瞬间刷屏。",
                    }
                    for i in range(3)
                ]
            else:
                opts = [
                    {
                        "label": f"长篇连载方案{i}",
                        "title": f"算命太准首富求我别闭麦{i}",
                        "author_level": "lv2_plus",
                        "target_chapters": 0,
                        "blurb": "下山摆摊一卦千金，直播连麦抓通缉犯，步步揭开百年大局。",
                    }
                    for i in range(3)
                ]
            return json.dumps({"options": opts}, ensure_ascii=False)

        provider = _FnProvider(_fake_llm)
        with tempfile.TemporaryDirectory() as tmp:
            db = StockpileDB(Path(tmp) / "stockpile.db")
            created_reqs: List[Any] = []
            wizard = NewBookWizard(
                db=db,
                provider_fn=lambda: provider,
                create_book_fn=lambda req: created_reqs.append(req) or type("S", (), {"book_id": req.project_id})(),
            )
            view = wizard.start("下山道士直播算命抓通缉犯")
            sid = view["session_id"]
            for _ in range(3):
                view = wizard.answer(sid, {"choice": 0})
            scale_prompt = captured_prompts["wizard_scale"]
            self.assertIn("lv2_plus", scale_prompt)
            self.assertIn("三点十七分", scale_prompt)
            self.assertIn("让你送外卖，没让你买下整条街", scale_prompt)

            # Invalid title_override (>15 chars) rejected cleanly
            with self.assertRaises(WizardError):
                wizard.answer(sid, {"choice": 0, "title_override": "这个自定义书名故意超过十五个字限制了"})

            # Valid title_override applied and forwarded into AutoCreateBookRequest
            view = wizard.answer(sid, {"choice": 0, "title_override": "一卦算哭首富，巡捕连夜借人"})
            self.assertEqual(view["status"], "READY_TO_CONFIRM")
            wizard.confirm(sid, pen_name="青崖居士")
            self.assertEqual(len(created_reqs), 1)
            self.assertEqual(created_reqs[0].author_level, "lv2_plus")
            self.assertIn("书名必须为：一卦算哭首富，巡捕连夜借人", created_reqs[0].creative_brief)

    def test_planner_title_block_forbids_bland_literary_titles(self) -> None:
        block = _format_recipe_title_and_blurb_block(get_genre("urban_brainhole"))
        self.assertIn("高点击商业书名硬性法则", block)
        self.assertIn("三点十七分", block)


class TestPillar3CompilerProseUpgrades(unittest.TestCase):
    def test_compiler_prompts_enforce_anti_drag_burstiness_and_hook_rules(self) -> None:
        proj = _sample_project("serial_20k")
        compiler = PromptCompiler(proj)
        from xiaoshuo_bot.state import WorldStateManager

        state = WorldStateManager(proj)
        sys_ch2, _ = compiler.compile_scene_prompt(
            proj.scenes[1], state, recent_chapter_texts=("第1章正文。",)
        )
        self.assertIn("给道具做体检", sys_ch2)
        self.assertIn("每写 250～350 字", sys_ch2)
        self.assertIn("严禁连续 4 句以上全是 6～12 字的碎片短句", sys_ch2)
        self.assertIn("每章首屏前 200 字", sys_ch2)

        bad_report = ScanReport(passed=False, char_count=1700, paragraph_count=10, violations=[])
        from xiaoshuo_bot.models import Violation

        bad_report.violations.append(
            Violation(
                rule_id="LEN_001",
                category="LENGTH",
                severity="CRITICAL",
                matched_text="1700 < 2050",
                line_number=1,
                context_snippet="",
                fix_hint="补足字数",
            )
        )
        _, rewrite_user = compiler.compile_rewrite_prompt(
            proj.scenes[1], state, "草稿", bad_report, recent_chapter_texts=("第1章正文。",)
        )
        self.assertIn("严禁用反复摆弄道具", rewrite_user)


class TestPillar4SurgicalTargetingAndPostPolishReEval(unittest.TestCase):
    def test_select_polish_targets_prioritizes_llm_weak_and_caps_at_max_targets(self) -> None:
        ev = CriticEvaluation(
            first_screen_hook=65,
            cool_point_density=62,
            comment_interactivity=70,
            pacing_drag=55,
            overall_score=63.0,
            weak_paragraphs=list(range(25)),
            llm_weak_paragraphs=[0, 5, 14, 24],
            checker_warning_paragraphs=[i for i in range(25) if i not in (0, 5, 14, 24)],
            paragraph_reasons={5: "道具复读拖沓", 14: "对白重复"},
        )
        selected = select_polish_targets(ev, num_paragraphs=25, max_targets=10)
        self.assertEqual(len(selected), 10)
        for must_idx in (0, 5, 14, 24):
            self.assertIn(must_idx, selected)
        # Edge checker warnings (1, 2, 23) prioritized over middle-only warnings
        self.assertIn(1, selected)
        self.assertIn(23, selected)

    def test_surgical_polish_prompt_injects_per_paragraph_length_budget_and_quote_guard(self) -> None:
        draft = (
            "周海一脚踹翻了木凳。\n\n"
            "“算命的，今天不交五千保护费别想收摊！”周海把铁棍往桌上重重一磕。\n\n"
            "沈青川放下铜钱，淡淡扫了眼他的眉心。"
        )
        captured: List[LLMRequest] = []

        def _polisher(req: LLMRequest) -> str:
            captured.append(req)
            return json.dumps(
                {"replacements": {"1": "“算命的，五千保护费今天少一分都别想收摊！”周海拎起铁棍砸在算命桌角。"}},
                ensure_ascii=False,
            )

        cp = TomatoCriticPolisher(
            polisher_provider=_FnProvider(_polisher),
            checker=AITicChecker(pov_mode="third_limited", panel_policy="allow_system_tag", contract_track="serial_20k"),
        )
        ev = CriticEvaluation(
            first_screen_hook=70,
            cool_point_density=68,
            comment_interactivity=72,
            pacing_drag=60,
            overall_score=67.5,
            weak_paragraphs=[1],
            llm_weak_paragraphs=[1],
            paragraph_reasons={1: "气势平，动作不够利落"},
        )
        res = cp.surgical_polish(draft, ev, chapter=1)
        self.assertTrue(res.scan_report.passed)
        self.assertEqual(len(captured), 1)
        self.assertIn("【字数预算：", captured[0].user_prompt)
        self.assertIn("【原段含引号对白/引文，精修后必须保留双引号台词】", captured[0].user_prompt)

    def test_engine_post_polish_reeval_accepts_lift_and_rejects_regression(self) -> None:
        class _PassChecker(AITicChecker):
            def scan(self, text: str, **_: Any) -> ScanReport:
                return ScanReport(passed=True, char_count=len(text), paragraph_count=3, violations=[])

        class _SeqCriticPolisher:
            def __init__(self, scores: List[float]) -> None:
                self.scores = list(scores)
                self.eval_calls = 0

            def evaluate_chapter(self, text: str, **_: Any) -> CriticEvaluation:
                sc = self.scores[min(self.eval_calls, len(self.scores) - 1)]
                self.eval_calls += 1
                return CriticEvaluation(
                    first_screen_hook=sc,
                    cool_point_density=sc,
                    comment_interactivity=sc,
                    pacing_drag=sc,
                    overall_score=sc,
                    weak_paragraphs=[1] if self.eval_calls == 1 else [],
                    llm_weak_paragraphs=[1] if self.eval_calls == 1 else [],
                    model="critic-sim",
                )

            def surgical_polish(self, text: str, *a: Any, **k: Any) -> SurgicalPolishResult:
                polished = text.replace("原稿中段", "精修后高张力中段")
                return SurgicalPolishResult(
                    polished_text=polished,
                    original_text=text,
                    replaced_indices=[1],
                    paragraphs_before=text.split("\n\n"),
                    paragraphs_after=polished.split("\n\n"),
                    scan_report=ScanReport(passed=True, char_count=len(polished), paragraph_count=3, violations=[]),
                    model="polisher-sim",
                )

            def generate_author_note(self, text: str, **_: Any) -> str:
                return "今晚这卦准不准？评论区扣1看下章！"

        draft_text = "开篇第一段。\n\n原稿中段内容。\n\n章末警笛响起。"
        proj = _sample_project("serial_20k")

        # Case A: score improves 68.0 -> 83.5 -> accept polished text and record real delta
        events_a: List[tuple[str, Dict[str, Any]]] = []
        with tempfile.TemporaryDirectory() as tmp:
            cp_a = _SeqCriticPolisher([68.0, 83.5])
            engine_a = NovelGenerationEngine(
                proj,
                _FnProvider(lambda _: draft_text),
                tmp,
                checker=_PassChecker(),
                critic_polisher=cp_a,
                on_progress=lambda ev, data: events_a.append((ev, data)),
            )
            man_a = engine_a.run(target_chapters=[1])
            self.assertEqual(cp_a.eval_calls, 2)
            ch_txt = (Path(tmp) / "chapters" / "chapter_0001.txt").read_text(encoding="utf-8")
            self.assertIn("精修后高张力中段", ch_txt)
            self.assertTrue((Path(tmp) / "audit" / "ch0001_critic_eval_post.json").is_file())
            meta_a = json.loads((Path(tmp) / "chapters" / "chapter_0001.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta_a["critic_score"], 83.5)
            self.assertEqual(meta_a["pre_polish_critic_score"], 68.0)
            self.assertEqual(meta_a["polish_score_delta"], 15.5)
            self.assertEqual(man_a["chapters"][0]["polish_summary"]["post_polish_score"], 83.5)
            polish_done_events = [
                d for ev, d in events_a if ev == "pipeline_stage" and d.get("stage") == "polish" and d.get("status") == "done"
            ]
            self.assertTrue(any("68.0 → 83.5 (+15.5)" in str(d.get("detail", "")) for d in polish_done_events))

        # Case B: score regresses 78.0 -> 71.0 -> revert to higher-scoring pre-polish draft
        with tempfile.TemporaryDirectory() as tmp:
            cp_b = _SeqCriticPolisher([78.0, 71.0])
            engine_b = NovelGenerationEngine(
                proj,
                _FnProvider(lambda _: draft_text),
                tmp,
                checker=_PassChecker(),
                critic_polisher=cp_b,
            )
            man_b = engine_b.run(target_chapters=[1])
            self.assertEqual(cp_b.eval_calls, 2)
            ch_txt = (Path(tmp) / "chapters" / "chapter_0001.txt").read_text(encoding="utf-8")
            self.assertIn("原稿中段", ch_txt)
            self.assertNotIn("精修后高张力中段", ch_txt)
            self.assertIsNone(man_b["chapters"][0]["polish_summary"])
            meta_b = json.loads((Path(tmp) / "chapters" / "chapter_0001.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta_b["critic_score"], 78.0)
            self.assertTrue(any("POLISH_SCORE_REGRESSION" in i.incident_id for i in engine_b.incidents))


class TestSelfHealingAndAntiPingPongLength(unittest.TestCase):
    def test_compiler_uses_surgical_micro_adjust_for_small_length_gaps(self) -> None:
        from xiaoshuo_bot.models import Violation
        from xiaoshuo_bot.state import WorldStateManager

        proj = _sample_project("serial_20k")
        compiler = PromptCompiler(proj)
        state = WorldStateManager(proj)

        short_rep = ScanReport(
            passed=False,
            char_count=1951,
            paragraph_count=50,
            violations=[
                Violation(
                    rule_id="LEN_001",
                    category="LENGTH_VIOLATION",
                    severity="CRITICAL",
                    matched_text="1951 chars < min 2050",
                    line_number=50,
                    context_snippet="",
                    fix_hint="不足",
                )
            ],
        )
        _, short_user = compiler.compile_rewrite_prompt(
            proj.scenes[0], state, "原稿", short_rep, recent_chapter_texts=()
        )
        self.assertIn("轻量精准补足", short_user)
        self.assertIn("切勿大幅重写或过度扩写", short_user)

        long_rep = ScanReport(
            passed=False,
            char_count=2383,
            paragraph_count=55,
            violations=[
                Violation(
                    rule_id="LEN_002",
                    category="LENGTH_VIOLATION",
                    severity="CRITICAL",
                    matched_text="2383 chars > max 2300",
                    line_number=55,
                    context_snippet="",
                    fix_hint="超限",
                )
            ],
        )
        _, long_user = compiler.compile_rewrite_prompt(
            proj.scenes[0], state, "原稿", long_rep, recent_chapter_texts=()
        )
        self.assertIn("轻量精准微裁", long_user)
        self.assertIn("切勿大段删减", long_user)

    def test_scheduler_self_healing_recovers_stochastic_prose_and_blocks_structural_deadlocks(self) -> None:
        from datetime import datetime, timedelta
        from xiaoshuo_bot.contracts import BookRunStatus
        from xiaoshuo_bot.models import SHANGHAI_TZ
        from xiaoshuo_bot.server.matrix_scheduler import (
            MatrixScheduler,
            classify_self_heal_eligibility,
        )

        # 1. Classification checks
        ok_len, r_len, ch_len, max_len = classify_self_heal_eligibility(
            {
                "status": BookRunStatus.ISOLATED_GATE_FAILURE.value,
                "paused_reason": "Chapter 308 gate failure",
                "isolation_detail": {
                    "chapter": 308,
                    "error_type": "ProductionGateFailure",
                    "violations": [
                        {"rule_id": "LEN_001", "severity": "CRITICAL"},
                        {"rule_id": "RHYTHM_BURSTINESS", "severity": "WARNING"},
                    ],
                },
            }
        )
        self.assertTrue(ok_len)
        self.assertIn("LEN_001", r_len)
        self.assertEqual(ch_len, 308)
        self.assertEqual(max_len, 3)

        bad_state, r_state, _, _ = classify_self_heal_eligibility(
            {
                "status": BookRunStatus.ISOLATED_GATE_FAILURE.value,
                "paused_reason": "Chapter 18 gate failure",
                "isolation_detail": {
                    "chapter": 18,
                    "error_type": "ProductionGateFailure",
                    "violations": [{"rule_id": "STATE_EXPECTATION_OVERDUE", "severity": "CRITICAL"}],
                },
            }
        )
        self.assertFalse(bad_state)
        self.assertIn("structural_STATE_EXPECTATION_OVERDUE", r_state)

        bad_manual, _, _, _ = classify_self_heal_eligibility(
            {
                "status": BookRunStatus.PAUSED_MANUAL.value,
                "paused_reason": "Manually paused",
                "isolation_detail": {},
            }
        )
        self.assertFalse(bad_manual)

        # 2. End-to-end cooldown, retry cap (3/3), and manual-pause protection
        clock = [datetime(2026, 11, 21, 8, 0, 0, tzinfo=SHANGHAI_TZ)]
        with tempfile.TemporaryDirectory() as tmp:
            db = StockpileDB(Path(tmp) / "m.db", now_fn=lambda: clock[0])
            db.register_book(book_id="b_len", project_dir=Path(tmp) / "p1", title="算卦长篇", total_chapters=315)
            db.register_book(book_id="b_state", project_dir=Path(tmp) / "p2", title="状态死锁书", total_chapters=30)
            db.register_book(book_id="b_manual", project_dir=Path(tmp) / "p3", title="手动暂停书", total_chapters=30)

            db.update_book_status(
                "b_len",
                BookRunStatus.ISOLATED_GATE_FAILURE,
                paused_reason="Chapter 308 gate failure",
                isolation_detail={
                    "chapter": 308,
                    "error_type": "ProductionGateFailure",
                    "violations": [{"rule_id": "LEN_001", "severity": "CRITICAL"}],
                    "isolated_at": clock[0].isoformat(),
                },
            )
            db.update_book_status(
                "b_state",
                BookRunStatus.ISOLATED_GATE_FAILURE,
                paused_reason="Chapter 18 gate failure",
                isolation_detail={
                    "chapter": 18,
                    "error_type": "ProductionGateFailure",
                    "violations": [{"rule_id": "STATE_OVERDRAFT", "severity": "CRITICAL"}],
                    "isolated_at": clock[0].isoformat(),
                },
            )
            db.update_book_status("b_manual", BookRunStatus.PAUSED_MANUAL, paused_reason="Manually paused")

            sched = MatrixScheduler(
                db=db,
                projects_root=Path(tmp),
                export_only=True,
                now_fn=lambda: clock[0],
                auto_heal_cooldown_secs=(120, 300, 900),
            )

            # Before 120s cooldown -> nothing healed yet
            clock[0] += timedelta(seconds=60)
            self.assertEqual(sched.run_self_healing_pass(), [])
            self.assertEqual(db.get_book("b_len")["status"], BookRunStatus.ISOLATED_GATE_FAILURE.value)

            # After 125s -> attempt 1/3 heals b_len only; b_state & b_manual stay untouched!
            clock[0] += timedelta(seconds=65)
            res1 = sched.run_self_healing_pass()
            self.assertEqual(len(res1), 1)
            self.assertEqual(res1[0]["book_id"], "b_len")
            self.assertEqual(res1[0]["attempt"], 1)
            self.assertEqual(db.get_book("b_len")["status"], BookRunStatus.ACTIVE.value)
            self.assertEqual(db.get_book("b_state")["status"], BookRunStatus.ISOLATED_GATE_FAILURE.value)
            self.assertEqual(db.get_book("b_manual")["status"], BookRunStatus.PAUSED_MANUAL.value)

            # Simulate failing 2 more times -> heals on 300s and 900s, then caps permanently at 3/3
            for wait_s, expected_attempt in ((305, 2), (905, 3)):
                db.update_book_status(
                    "b_len",
                    BookRunStatus.ISOLATED_GATE_FAILURE,
                    paused_reason="Chapter 308 gate failure",
                    isolation_detail={
                        "chapter": 308,
                        "error_type": "ProductionGateFailure",
                        "violations": [{"rule_id": "LEN_002", "severity": "CRITICAL"}],
                        "isolated_at": clock[0].isoformat(),
                    },
                )
                clock[0] += timedelta(seconds=wait_s)
                r = sched.run_self_healing_pass()
                self.assertEqual(len(r), 1)
                self.assertEqual(r[0]["attempt"], expected_attempt)

            # 4th failure on same ch 308 -> hits hard ceiling 3/3 and stays isolated permanently
            db.update_book_status(
                "b_len",
                BookRunStatus.ISOLATED_GATE_FAILURE,
                paused_reason="Chapter 308 gate failure",
                isolation_detail={
                    "chapter": 308,
                    "error_type": "ProductionGateFailure",
                    "violations": [{"rule_id": "LEN_001", "severity": "CRITICAL"}],
                    "isolated_at": clock[0].isoformat(),
                },
            )
            clock[0] += timedelta(seconds=3600)
            self.assertEqual(sched.run_self_healing_pass(), [])
            self.assertEqual(db.get_book("b_len")["status"], BookRunStatus.ISOLATED_GATE_FAILURE.value)
            self.assertIn("自愈已达上限 3/3", db.get_book("b_len")["paused_reason"])

            # 3. Quota reset auto-resume (when reset_at has passed by >= 60s)
            db.upsert_model_quota(
                model="wb/hy4flash-free",
                percent=100.0,
                reset_at=(clock[0] - timedelta(minutes=5)).isoformat(),
                reset_at_raw="",
                raw_model="hy4-flash",
                shared_models=[],
                role="writer",
            )
            db.register_book(book_id="b_quota", project_dir=Path(tmp) / "p4", title="额度恢复书", total_chapters=30)
            db.update_book_status(
                "b_quota",
                BookRunStatus.PAUSED_QUOTA_EXHAUSTED,
                paused_reason="All whitelist models exhausted (HTTP 429)",
            )
            clock[0] += timedelta(seconds=90)
            rq = sched.run_self_healing_pass()
            self.assertEqual(len(rq), 1)
            self.assertEqual(rq[0]["book_id"], "b_quota")
            self.assertEqual(db.get_book("b_quota")["status"], BookRunStatus.ACTIVE.value)


if __name__ == "__main__":
    unittest.main()
