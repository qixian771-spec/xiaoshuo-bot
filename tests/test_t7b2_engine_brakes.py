from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from typing import Callable, List, Optional
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.blueprint import BookBlueprint, load_blueprint
from xiaoshuo_bot.critic_polisher import CriticEvaluation, SurgicalPolishResult
from xiaoshuo_bot.engine import (
    ArcReviewBrakeError,
    ConsecutiveGateFailureBrakeError,
    NovelGenerationEngine,
    ProductionGateFailure,
)
from xiaoshuo_bot.judge import PairwiseJudge
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.models import (
    AssetState,
    CharacterAnchor,
    LedgerMutation,
    NovelProject,
    ProjectMeta,
    ScanReport,
    SceneOutline,
    StyleProfile,
    Violation,
)
from xiaoshuo_bot.quality_rules import (
    SELLING_POINT_MONOPOLY_MIN_KEYWORDS,
    SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO,
    SELLING_POINT_MONOPOLY_SOLE_SHARE,
    run_l1_checks,
)


def _load_valid_blueprint() -> BookBlueprint:
    return load_blueprint(ROOT / "tests" / "fixtures" / "blueprint_valid.yaml")


def _scene(
    chapter: int,
    *,
    volume: int = 1,
    ledger_mutations: Optional[List[LedgerMutation]] = None,
) -> SceneOutline:
    return SceneOutline(
        chapter=chapter,
        scene_id=f"ch{chapter:04d}_scene",
        internal_title=f"第{chapter}章场景",
        pov_character="mc",
        active_characters=["mc"],
        active_assets=[],
        style_profile_ids=["default_style"],
        location_time="傍晚，南桥夜市",
        events=[f"第{chapter}章推进事件"],
        stop_condition="止于悬念",
        location_id=f"loc_{chapter}",
        calendar_days_elapsed=1,
        volume=volume,
        ledger_mutations=ledger_mutations or [],
    )


def _project(
    *,
    num_chapters: int = 4,
    scenes: Optional[List[SceneOutline]] = None,
    max_rewrite_retries: int = 1,
) -> NovelProject:
    meta = ProjectMeta(
        project_id="t7b2_test_book",
        title="夜市因果录",
        genre="都市脑洞",
        synopsis="测试用书",
        default_pov_character="mc",
        contract_track="serial_20k",
        max_rewrite_retries=max_rewrite_retries,
    )
    characters = {
        "mc": CharacterAnchor(
            id="mc",
            name="许青川",
            role="主角",
            personality="冷静",
            speech_style="短句",
        ),
    }
    return NovelProject(
        meta=meta,
        characters=characters,
        assets={
            "scale": AssetState(
                id="scale",
                name="因果秤",
                location="摊位木盒",
                holder="许青川",
                condition="完好",
                physical_details="旧铜秤",
            )
        },
        foreshadows={},
        style_profiles={
            "default_style": StyleProfile(
                id="default_style",
                name="测试文风",
                granularity_notes="短句白描",
                sample_text="夜市白炽灯刚亮。",
            )
        },
        scenes=scenes or [_scene(i) for i in range(1, num_chapters + 1)],
    )


class _PassChecker:
    """Checker stub that passes unless text contains the sentinel marker '[FAIL_GATE]'."""

    def scan(self, text: str, **_: object) -> ScanReport:
        if "[FAIL_GATE]" in text:
            return ScanReport(
                passed=False,
                char_count=len(text),
                paragraph_count=text.count("\n\n") + 1,
                violations=[
                    Violation(
                        rule_id="TEST_FAIL",
                        category="TEST_GATE",
                        severity="CRITICAL",
                        matched_text="[FAIL_GATE]",
                        line_number=1,
                        context_snippet=text[:120],
                        fix_hint="移除 [FAIL_GATE] 标记",
                    )
                ],
            )
        return ScanReport(
            passed=True,
            char_count=len(text),
            paragraph_count=text.count("\n\n") + 1,
        )


class _ScriptedProvider(LLMProvider):
    def __init__(self, handler: Callable[[LLMRequest], str]) -> None:
        self._handler = handler
        self.requests: List[LLMRequest] = []
        self.model = "hy4_flash"

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            request_id=request.request_id,
            content=self._handler(request),
            model=self.model,
            provider="fake",
        )


class _QueuedJudgeProvider(LLMProvider):
    def __init__(self, payloads: List[dict]) -> None:
        self._payloads = list(payloads)
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        idx = min(len(self.requests) - 1, len(self._payloads) - 1)
        return LLMResponse(
            request_id=request.request_id,
            content=json.dumps(self._payloads[idx], ensure_ascii=False),
            model="deepseek_v4_1_flash",
            provider="fake_judge",
        )


class TestSellingPointDeepeningRules(unittest.TestCase):
    def test_monopoly_constants_exported(self) -> None:
        self.assertEqual(SELLING_POINT_MONOPOLY_MIN_KEYWORDS, 3)
        self.assertAlmostEqual(SELLING_POINT_MONOPOLY_SOLE_SHARE, 0.85)
        self.assertAlmostEqual(SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO, 0.15)

    def test_core_selling_point_drift_catches_blindspot_and_respects_boundaries(self) -> None:
        # 18/20 chapters mention generic "直播", so overall QR_SELLING_POINT_DRIFT is 0;
        # but core keywords only appear in 4/20 (20% < 40%) -> QR_CORE_SELLING_POINT_DRIFT fires!
        chapters = [
            (
                i,
                f"第{i}章直播间热闹非凡，他当场算卦断事。"
                if i <= 4
                else f"第{i}章直播间弹幕刷屏，大家讨论八卦。"
                if i <= 18
                else f"第{i}章逛街买菜喝茶。",
            )
            for i in range(1, 21)
        ]
        hits = run_l1_checks(
            chapters,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦", "观气", "开瞳", "直播", "通缉"],
            core_selling_point_keywords=["算卦", "观气", "开瞳", "通缉"],
            ledger_balances={},
        )
        self.assertEqual([h for h in hits if h.rule_id == "QR_SELLING_POINT_DRIFT"], [])
        core_hits = [h for h in hits if h.rule_id == "QR_CORE_SELLING_POINT_DRIFT"]
        self.assertEqual(len(core_hits), 1)
        self.assertEqual(core_hits[0].severity, "CRITICAL")
        self.assertAlmostEqual(core_hits[0].value["ratio"], 0.20)
        self.assertEqual(core_hits[0].value["matched_chapters"], 4)
        self.assertEqual(core_hits[0].value["per_keyword_counts"]["算卦"], 4)

        # Boundary: exact 8/20 (40%) core coverage -> 0 QR_CORE_SELLING_POINT_DRIFT
        chapters_pass = [
            (
                i,
                f"第{i}章直播算卦观气。" if i <= 8 else f"第{i}章直播聊日常。",
            )
            for i in range(1, 21)
        ]
        hits_pass = run_l1_checks(
            chapters_pass,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦", "观气", "开瞳", "直播", "通缉"],
            core_selling_point_keywords=["算卦", "观气", "开瞳", "通缉"],
            ledger_balances={},
        )
        self.assertEqual([h for h in hits_pass if h.rule_id == "QR_CORE_SELLING_POINT_DRIFT"], [])

        # Short draft (< 20 chapters) -> skipped
        short_hits = run_l1_checks(
            [(i, f"第{i}章买菜散步。") for i in range(1, 16)],
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦", "观气", "直播"],
            core_selling_point_keywords=["算卦", "观气"],
            ledger_balances={},
        )
        self.assertEqual([h for h in short_hits if h.rule_id == "QR_CORE_SELLING_POINT_DRIFT"], [])

    def test_core_selling_point_keywords_bad_args_raise_value_error(self) -> None:
        base = {
            "characters": {},
            "blueprint_scale_rules": [],
            "selling_point_keywords": ["算卦", "观气", "直播"],
            "ledger_balances": {},
        }
        chs = [(1, "算卦直播。")]
        for bad in ([], ["  "], ["算卦", 123], "算卦", ["算卦", None]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    run_l1_checks(chs, core_selling_point_keywords=bad, **base)  # type: ignore[arg-type]

    def test_selling_point_monopoly_positive_and_negative_boundaries(self) -> None:
        kws = ["算卦", "观气", "开瞳", "直播", "通缉"]
        # Positive: 15 matched chapters; 13 are ONLY "直播" (13/15 = 86.7% > 85%),
        # remaining keywords cover 2/20 = 10% < 15% -> WARNING QR_SELLING_POINT_MONOPOLY
        chapters_mono = []
        for i in range(1, 21):
            if i == 1:
                chapters_mono.append((i, "第1章直播顺便算卦。"))
            elif i == 2:
                chapters_mono.append((i, "第2章直播间里开瞳一次。"))
            elif i <= 15:
                chapters_mono.append((i, f"第{i}章直播间大家闲聊吃瓜。"))
            else:
                chapters_mono.append((i, f"第{i}章走在夜市巷口散步。"))

        hits = run_l1_checks(
            chapters_mono,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=kws,
            ledger_balances={},
        )
        mono_hits = [h for h in hits if h.rule_id == "QR_SELLING_POINT_MONOPOLY"]
        self.assertEqual(len(mono_hits), 1)
        hit = mono_hits[0]
        self.assertEqual(hit.severity, "WARNING")
        self.assertEqual(hit.value["dominant_keyword"], "直播")
        self.assertAlmostEqual(hit.value["sole_share"], 13 / 15)
        self.assertAlmostEqual(hit.value["other_keywords_ratio"], 2 / 20)
        self.assertEqual(hit.value["per_keyword_counts"]["直播"], 15)
        self.assertEqual(hit.value["per_keyword_counts"]["算卦"], 1)
        self.assertEqual(hit.value["per_keyword_counts"]["开瞳"], 1)
        self.assertIn("直播", hit.evidence)
        self.assertIn("算卦=1/20", hit.evidence)

        # Negative exact boundary: 17/20 = 85.0% sole share (not > 85%) and 3/20 = 15.0% other (not < 15%)
        chapters_boundary = [
            (
                i,
                f"第{i}章直播并且算卦。" if i <= 3 else f"第{i}章纯直播闲聊。",
            )
            for i in range(1, 21)
        ]
        boundary_hits = run_l1_checks(
            chapters_boundary,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=kws,
            ledger_balances={},
        )
        self.assertEqual(
            [h for h in boundary_hits if h.rule_id == "QR_SELLING_POINT_MONOPOLY"], []
        )

        # Fewer than 3 keywords -> monopoly rule does not activate
        two_kw_hits = run_l1_checks(
            [(i, f"第{i}章纯直播闲聊。") for i in range(1, 21)],
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["直播", "算卦"],
            ledger_balances={},
        )
        self.assertEqual(
            [h for h in two_kw_hits if h.rule_id == "QR_SELLING_POINT_MONOPOLY"], []
        )


class TestEngineL1GateAndBrakes(unittest.TestCase):
    def test_engine_init_strict_parameter_validation(self) -> None:
        proj = _project(num_chapters=1)
        provider = _ScriptedProvider(lambda req: "南桥夜市算卦。")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                NovelGenerationEngine(proj, provider, tmp, blueprint="bad")  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                NovelGenerationEngine(proj, provider, tmp, core_selling_point_keywords=[])
            with self.assertRaises(ValueError):
                NovelGenerationEngine(proj, provider, tmp, arc_judge="not-judge")  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                NovelGenerationEngine(proj, provider, tmp, arc_review_interval=0)
            with self.assertRaises(ValueError):
                NovelGenerationEngine(proj, provider, tmp, trial_checkpoint_interval=False)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                NovelGenerationEngine(proj, provider, tmp, max_consecutive_failures=0)

    def test_l1_verbatim_repeat_blocks_then_recovers_on_rewrite(self) -> None:
        bp = _load_valid_blueprint()
        proj = _project(num_chapters=2, max_rewrite_retries=1)
        dup_para = "许青川抬手按住折叠桌角，浅茶色镜片后的目光落在了对方发白的指节上。"

        def _gen(req: LLMRequest) -> str:
            if req.chapter == 1:
                return f"南桥夜市刚亮起白炽灯，许青川支起算卦摊。\n\n{dup_para}"
            if req.attempt == 1:
                # Copies chapter 1 >=15-char paragraph verbatim -> QR_VERBATIM_REPEAT
                return f"次日夜里雨势渐密，直播间开了。\n\n{dup_para}"
            # Rewrite replaces the repeated paragraph cleanly
            return "次日夜里雨势渐密，直播间准时开播。\n\n许青川把旧木牌擦干净，示意对面扫码后说清来意。"

        provider = _ScriptedProvider(_gen)
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj, provider, tmp, checker=_PassChecker(), blueprint=bp
            )
            manifest = engine.run()
            out = Path(tmp)
            self.assertEqual(manifest["chapter_count"], 2)
            self.assertEqual(manifest["chapters"][1]["attempts_used"], 2)
            self.assertEqual(manifest["chapters"][1]["rewrites_triggered"], 1)
            self.assertTrue((out / "chapters" / "chapter_0002.txt").is_file())

            scan1 = json.loads(
                (out / "audit" / "ch0002_attempt1_scan.json").read_text(encoding="utf-8")
            )
            self.assertFalse(scan1["passed"])
            self.assertTrue(
                any(
                    v["rule_id"] == "QR_VERBATIM_REPEAT" and v["category"] == "L1_QUALITY_RULE"
                    for v in scan1["violations"]
                )
            )
            l1_audit = json.loads(
                (out / "audit" / "ch0002_attempt1_l1.json").read_text(encoding="utf-8")
            )
            self.assertFalse(l1_audit["passed"])

    def test_l1_verbatim_repeat_exhausted_never_writes_to_disk(self) -> None:
        bp = _load_valid_blueprint()
        proj = _project(num_chapters=2, max_rewrite_retries=1)
        dup_para = "许青川抬手按住折叠桌角，浅茶色镜片后的目光落在了对方发白的指节上。"

        provider = _ScriptedProvider(
            lambda req: f"第{req.chapter}章夜市算卦。\n\n{dup_para}"
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj, provider, tmp, checker=_PassChecker(), blueprint=bp
            )
            with self.assertRaises(ProductionGateFailure):
                engine.run()
            out = Path(tmp)
            self.assertTrue((out / "chapters" / "chapter_0001.txt").is_file())
            self.assertFalse((out / "chapters" / "chapter_0002.txt").exists())
            self.assertFalse((out / "chapters" / "chapter_0002.meta.json").exists())
            self.assertFalse((out / "snapshots" / "chapter_0002_after.json").exists())

    def test_l1_bounded_realistic_ledger_cap_blocks_without_mutating_state(self) -> None:
        bp = _load_valid_blueprint()
        # blueprint_valid.yaml sets followers bounded_realistic hard_max = 12,000,000
        scenes = [
            _scene(
                1,
                ledger_mutations=[
                    LedgerMutation(ledger_key="followers", delta=20_000_000, reason="异常暴涨")
                ],
            )
        ]
        proj = _project(scenes=scenes, max_rewrite_retries=1)
        provider = _ScriptedProvider(lambda req: "南桥夜市算卦开张，直播连麦顺利进行。")
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj, provider, tmp, checker=_PassChecker(), blueprint=bp
            )
            with self.assertRaises(ProductionGateFailure) as ctx:
                engine.run()
            self.assertIn("QR_SCALE", str(ctx.exception))
            out = Path(tmp)
            self.assertFalse((out / "chapters" / "chapter_0001.txt").exists())
            self.assertEqual(engine.state.get_balance("followers"), 0.0)

    def test_l1_warning_recorded_in_meta_and_audit_without_blocking(self) -> None:
        bp = _load_valid_blueprint()
        proj = _project(num_chapters=20, max_rewrite_retries=0)

        def _gen(req: LLMRequest) -> str:
            if req.chapter <= 18:
                return f"第{req.chapter}章晚间直播间开麦聊家常，弹幕一片欢快。"
            return f"第{req.chapter}章晚间直播顺手帮路人算卦解惑。"

        provider = _ScriptedProvider(_gen)
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj, provider, tmp, checker=_PassChecker(), blueprint=bp
            )
            manifest = engine.run()
            out = Path(tmp)
            self.assertEqual(manifest["chapter_count"], 20)
            self.assertTrue(manifest["all_gates_passed"])
            ch20_meta = json.loads(
                (out / "chapters" / "chapter_0020.meta.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(w["rule_id"] == "QR_SELLING_POINT_MONOPOLY" for w in ch20_meta["l1_warnings"])
            )
            self.assertTrue((out / "audit" / "ch0020_l1_warnings.json").is_file())

    def test_surgical_polish_breaking_l1_gate_is_rejected_and_draft_kept(self) -> None:
        bp = _load_valid_blueprint()
        proj = _project(num_chapters=2, max_rewrite_retries=0)
        dup_para = "许青川抬手按住折叠桌角，浅茶色镜片后的目光落在了对方发白的指节上。"

        class _L1BreakingPolisher:
            def evaluate_chapter(self, text: str, **_: object) -> CriticEvaluation:
                return CriticEvaluation(
                    first_screen_hook=75,
                    cool_point_density=75,
                    comment_interactivity=75,
                    pacing_drag=75,
                    overall_score=75.0,
                    weak_paragraphs=[0],
                )

            def surgical_polish(self, text: str, *args: object, **kwargs: object) -> SurgicalPolishResult:
                ch = kwargs.get("chapter", 0)
                if ch == 2:
                    broken = f"第二章精修正文。\n\n{dup_para}"
                    return SurgicalPolishResult(
                        polished_text=broken,
                        original_text=text,
                        replaced_indices=[0],
                        paragraphs_before=text.split("\n\n"),
                        paragraphs_after=broken.split("\n\n"),
                        scan_report=ScanReport(passed=True, char_count=len(broken), paragraph_count=2),
                        model="fake_polisher",
                    )
                return SurgicalPolishResult(
                    polished_text=text,
                    original_text=text,
                    replaced_indices=[],
                    paragraphs_before=text.split("\n\n"),
                    paragraphs_after=text.split("\n\n"),
                    scan_report=ScanReport(passed=True, char_count=len(text), paragraph_count=2),
                    model="fake_polisher",
                )

            def generate_author_note(self, text: str, **_: object) -> str:
                return ""

        def _gen(req: LLMRequest) -> str:
            if req.chapter == 1:
                return f"第一章夜市算卦。\n\n{dup_para}"
            return "第二章夜市算卦。\n\n全新不重复的段落内容在这里展开。"

        provider = _ScriptedProvider(_gen)
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj,
                provider,
                tmp,
                checker=_PassChecker(),
                critic_polisher=_L1BreakingPolisher(),  # type: ignore[arg-type]
                re_evaluate_after_polish=False,
                blueprint=bp,
            )
            manifest = engine.run()
            self.assertEqual(manifest["chapter_count"], 2)
            ch2_text = (Path(tmp) / "chapters" / "chapter_0002.txt").read_text(encoding="utf-8")
            self.assertNotIn(dup_para, ch2_text)
            self.assertTrue(any("POLISH_L1_GATE" in inc.incident_id for inc in engine.incidents))

    def test_trial_checkpoint_pauses_cleanly_and_resumes_to_next_checkpoint(self) -> None:
        proj = _project(num_chapters=4, max_rewrite_retries=0)
        provider = _ScriptedProvider(
            lambda req: f"第{req.chapter}章南桥夜市算卦观气，剧情稳步推进。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj,
                provider,
                tmp,
                checker=_PassChecker(),
                trial_checkpoint_interval=2,
            )
            m1 = engine.run()
            out = Path(tmp)
            self.assertTrue(m1["paused_for_review"])
            self.assertEqual(m1["checkpoint_chapter"], 2)
            self.assertEqual(m1["chapter_count"], 2)
            self.assertEqual(m1["all_completed_chapters_on_disk"], [1, 2])
            self.assertFalse((out / "chapters" / "chapter_0003.txt").exists())

            # Resume from checkpoint: chapters 1..2 resumed without LLM calls, writes 3..4 and pauses at 4
            calls_before_resume = len(provider.requests)
            engine2 = NovelGenerationEngine(
                proj,
                provider,
                tmp,
                checker=_PassChecker(),
                trial_checkpoint_interval=10,
            )
            m2 = engine2.run(resume=True, trial_checkpoint_interval=2)
            self.assertTrue(m2["paused_for_review"])
            self.assertEqual(m2["checkpoint_chapter"], 4)
            self.assertEqual(m2["all_completed_chapters_on_disk"], [1, 2, 3, 4])
            self.assertEqual(len(provider.requests) - calls_before_resume, 2)

    def test_consecutive_failure_brake_triggers_at_2_and_resets_after_success(self) -> None:
        # Case 1: 2 consecutive failures -> raises ConsecutiveGateFailureBrakeError, ch 3 not run
        proj1 = _project(num_chapters=3, max_rewrite_retries=0)
        provider1 = _ScriptedProvider(
            lambda req: "[FAIL_GATE]" if req.chapter in (1, 2) else f"第{req.chapter}章正常正文。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj1, provider1, tmp, checker=_PassChecker(), max_consecutive_failures=2
            )
            with self.assertRaises(ConsecutiveGateFailureBrakeError) as ctx:
                engine.run(brake_on_consecutive_failures=True)
            self.assertEqual(ctx.exception.failed_chapters, [1, 2])
            self.assertEqual(ctx.exception.consecutive_failures, 2)
            self.assertEqual([r.chapter for r in provider1.requests], [1, 2])
            self.assertFalse((Path(tmp) / "chapters" / "chapter_0001.txt").exists())
            self.assertFalse((Path(tmp) / "chapters" / "chapter_0002.txt").exists())

        # Case 2: fail ch1 (cnt=1) -> pass ch2 (reset to 0) -> fail ch3 (cnt=1) -> pass ch4 (reset to 0)
        proj2 = _project(num_chapters=4, max_rewrite_retries=0)
        provider2 = _ScriptedProvider(
            lambda req: "[FAIL_GATE]" if req.chapter in (1, 3) else f"第{req.chapter}章正常通过正文。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj2, provider2, tmp, checker=_PassChecker(), max_consecutive_failures=2
            )
            manifest = engine.run(brake_on_consecutive_failures=True)
            self.assertEqual(manifest["failed_chapters"], [1, 3])
            self.assertEqual(manifest["all_completed_chapters_on_disk"], [2, 4])
            self.assertFalse(manifest["all_gates_passed"])
            self.assertFalse((Path(tmp) / "chapters" / "chapter_0001.txt").exists())
            self.assertTrue((Path(tmp) / "chapters" / "chapter_0002.txt").is_file())
            self.assertFalse((Path(tmp) / "chapters" / "chapter_0003.txt").exists())
            self.assertTrue((Path(tmp) / "chapters" / "chapter_0004.txt").is_file())

        # Case 3: fail ch1 (cnt=1) -> pass ch2 (cnt=0) -> fail ch3 (cnt=1) -> fail ch4 (cnt=2 -> brake!)
        proj3 = _project(num_chapters=5, max_rewrite_retries=0)
        provider3 = _ScriptedProvider(
            lambda req: "[FAIL_GATE]" if req.chapter in (1, 3, 4) else f"第{req.chapter}章正常通过正文。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj3, provider3, tmp, checker=_PassChecker(), max_consecutive_failures=2
            )
            with self.assertRaises(ConsecutiveGateFailureBrakeError) as ctx3:
                engine.run(brake_on_consecutive_failures=True)
            self.assertEqual(ctx3.exception.failed_chapters, [1, 3, 4])
            self.assertEqual([r.chapter for r in provider3.requests], [1, 2, 3, 4])

    def test_arc_review_pass_continues_and_writes_audit_json(self) -> None:
        bp = _load_valid_blueprint()
        proj = _project(num_chapters=4, max_rewrite_retries=0)
        writer_provider = _ScriptedProvider(
            lambda req: f"第{req.chapter}章南桥夜市算卦观气，剧情推进不重复{req.chapter * 17}。"
        )
        judge_provider = _QueuedJudgeProvider(
            [
                {"verdict": "PASS", "problems": [], "summary": "前两章弧线节奏良好"},
                {"verdict": "PASS", "problems": [], "summary": "三四章推进清晰"},
            ]
        )
        judge = PairwiseJudge(
            judge_provider,
            judge_model="deepseek_v4_1_flash",
            writer_model="hy4_flash",
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj,
                writer_provider,
                tmp,
                checker=_PassChecker(),
                blueprint=bp,
                arc_judge=judge,
                arc_review_interval=2,
            )
            manifest = engine.run()
            out = Path(tmp)
            self.assertEqual(manifest["chapter_count"], 4)
            self.assertTrue(manifest["all_gates_passed"])
            self.assertEqual(len(manifest["arc_reviews"]), 2)
            self.assertEqual([r["chapter"] for r in manifest["arc_reviews"]], [2, 4])
            self.assertEqual(manifest["arc_reviews"][0]["reviewed_chapters"], [1, 2])
            self.assertEqual(manifest["arc_reviews"][1]["reviewed_chapters"], [3, 4])

            audit2 = json.loads(
                (out / "audit" / "arc_review_ch0002.json").read_text(encoding="utf-8")
            )
            audit4 = json.loads(
                (out / "audit" / "arc_review_ch0004.json").read_text(encoding="utf-8")
            )
            self.assertEqual(audit2["verdict"], "PASS")
            self.assertEqual(audit2["volume_index"], 1)
            self.assertEqual(audit4["verdict"], "PASS")

    def test_arc_review_fail_triggers_brake_and_stops_subsequent_chapters(self) -> None:
        bp = _load_valid_blueprint()
        proj = _project(num_chapters=4, max_rewrite_retries=0)
        writer_provider = _ScriptedProvider(
            lambda req: f"第{req.chapter}章南桥夜市算卦观气，独立新场景编号{req.chapter * 31}。"
        )
        judge_provider = _QueuedJudgeProvider(
            [
                {
                    "verdict": "FAIL",
                    "problems": [
                        {
                            "type": "同质化",
                            "chapters": [1, 2],
                            "evidence": "两章冲突结构高度重复",
                        }
                    ],
                    "summary": "前两章出现严重同质化，需立刻刹车调整",
                }
            ]
        )
        judge = PairwiseJudge(
            judge_provider,
            judge_model="deepseek_v4_1_flash",
            writer_model="hy4_flash",
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj,
                writer_provider,
                tmp,
                checker=_PassChecker(),
                blueprint=bp,
                arc_judge=judge,
                arc_review_interval=2,
            )
            with self.assertRaises(ArcReviewBrakeError) as ctx:
                engine.run(strict_arc_review=True)
            out = Path(tmp)
            self.assertEqual(ctx.exception.chapter, 2)
            # Audit file must already be written atomically before brake error
            audit2_path = out / "audit" / "arc_review_ch0002.json"
            self.assertTrue(audit2_path.is_file())
            audit2 = json.loads(audit2_path.read_text(encoding="utf-8"))
            self.assertEqual(audit2["verdict"], "FAIL")
            self.assertEqual(audit2["problems"][0]["type"], "同质化")
            # Chapters 3 and 4 MUST NOT have been generated
            self.assertEqual([r.chapter for r in writer_provider.requests], [1, 2])
            self.assertFalse((out / "chapters" / "chapter_0003.txt").exists())
            self.assertFalse((out / "chapters" / "chapter_0004.txt").exists())

    def test_arc_review_non_strict_and_combined_with_trial_checkpoint(self) -> None:
        bp = _load_valid_blueprint()
        proj = _project(num_chapters=4, max_rewrite_retries=0)
        writer_provider = _ScriptedProvider(
            lambda req: f"第{req.chapter}章南桥夜市算卦观气，组合场景号{req.chapter * 43}。"
        )
        judge_provider = _QueuedJudgeProvider(
            [
                {
                    "verdict": "FAIL",
                    "problems": [
                        {
                            "type": "跑偏",
                            "chapters": [1, 2],
                            "evidence": "支线篇幅偏重",
                        }
                    ],
                    "summary": "轻微跑偏记录",
                }
            ]
        )
        judge = PairwiseJudge(
            judge_provider,
            judge_model="deepseek_v4_1_flash",
            writer_model="hy4_flash",
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj,
                writer_provider,
                tmp,
                checker=_PassChecker(),
                blueprint=bp,
                arc_judge=judge,
                arc_review_interval=2,
                trial_checkpoint_interval=2,
            )
            manifest = engine.run(strict_arc_review=False)
            self.assertTrue(manifest["paused_for_review"])
            self.assertEqual(manifest["checkpoint_chapter"], 2)
            self.assertEqual(manifest["chapter_count"], 2)
            self.assertEqual(len(manifest["arc_reviews"]), 1)
            self.assertEqual(manifest["arc_reviews"][0]["verdict"], "FAIL")
            self.assertFalse(manifest["all_gates_passed"])
            self.assertFalse((Path(tmp) / "chapters" / "chapter_0003.txt").exists())

    def test_default_brake_false_preserves_immediate_production_gate_failure(self) -> None:
        proj = _project(num_chapters=2, max_rewrite_retries=0)
        provider = _ScriptedProvider(lambda req: "[FAIL_GATE]")
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(proj, provider, tmp, checker=_PassChecker())
            with self.assertRaises(ProductionGateFailure) as ctx:
                engine.run()
            self.assertNotIsInstance(ctx.exception, ConsecutiveGateFailureBrakeError)
            self.assertEqual([r.chapter for r in provider.requests], [1])


if __name__ == "__main__":
    unittest.main()
