from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any, List

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.checker import AITicChecker
from xiaoshuo_bot.cli import main as cli_main
from xiaoshuo_bot.compiler import PromptCompiler
from xiaoshuo_bot.engine import NovelGenerationEngine, ProductionGateFailure
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.memory import MemoryPyramid
from xiaoshuo_bot.models import (
    CharacterAnchor,
    NovelProject,
    ProjectMeta,
    ScanReport,
    SceneOutline,
    StyleProfile,
    Violation,
)
from xiaoshuo_bot.opening_rules import writer_directives
from xiaoshuo_bot.state import WorldStateManager

COOLDOWN_TAG = "【冷却中：近3章已用，本章禁止使用】"
GOLDEN_THREE_HEADER = "### 黄金三章硬指标（对标头部书实测）"


class _PassChecker(AITicChecker):
    def scan(self, text: str, **_: Any) -> ScanReport:
        return ScanReport(passed=True, char_count=len(text), paragraph_count=3, violations=[])


class _RecordingProvider(LLMProvider):
    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        idx = min(len(self.requests) - 1, len(self._responses) - 1)
        return LLMResponse(
            request_id=request.request_id,
            content=self._responses[idx],
            model="stub-writer",
            provider="stub",
            latency_sec=0.01,
        )


def _build_project(
    *,
    contract_track: str = "serial_20k",
    genre_id: str = "urban_brainhole",
    num_chapters: int = 5,
) -> NovelProject:
    meta = ProjectMeta(
        project_id=f"t5b_{contract_track}",
        title="档案室夜审录",
        genre="都市脑洞",
        genre_id=genre_id,
        synopsis="测试 T5b 编译器锚点冷却与黄金三章硬指标注入",
        default_pov_character="mc",
        author_level="lv2_plus" if contract_track == "serial_20k" else "lv0_1",
        contract_track=contract_track,
        pov_mode="third_limited",
        panel_policy="forbid",
    )
    characters = {
        "mc": CharacterAnchor(
            id="mc",
            name="唐子墨",
            role="夜间稽核员",
            personality="冷静克制，盯紧账本漏洞",
            speech_style="简短带刺",
            behavioral_anchors={
                "核对假账前": "食指中指并拢在左眼尾轻轻一按，呼吸停半拍。",
                "准备亮底牌时": "把钢笔帽拧下来扣在台历右下角。",
            },
            forbidden_ooc=["圣母原谅"],
        ),
        "police": CharacterAnchor(
            id="police",
            name="冯念慈",
            role="刑侦副队长",
            personality="务实果断",
            speech_style="短陈述句",
            behavioral_anchors={
                "听到玄乎说法时": "把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。",
                "确认证据成立时": "食指关节在封皮上顶一下，直接报出行动时间。",
            },
        ),
    }
    styles = {
        "custom_sample": StyleProfile(
            id="custom_sample",
            name="档案室冷峻样本",
            granularity_notes="以动作动词与具体数字推进",
            sample_text="他把报废的红章按在回单空白处，抬眼盯住对面发抖的财务经理。",
        )
    }
    scenes = [
        SceneOutline(
            chapter=ch,
            scene_id=f"ch{ch:04d}_scene",
            internal_title=f"第{ch}章交锋",
            public_title=f"第{ch}章公开名",
            pov_character="mc",
            active_characters=["mc", "police"],
            active_assets=[],
            style_profile_ids=["custom_sample"],
            location_time=f"第{ch}日夜晚，海城老档案室",
            events=[f"第{ch}章正面交锋", f"第{ch}章亮出实锤单据"],
            stop_condition=f"第{ch}章新线索落桌时立即截断",
            min_chars=2050,
            max_chars=2300,
            location_id=f"archive_room_{ch}",
            calendar_days_elapsed=2,
        )
        for ch in range(1, num_chapters + 1)
    ]
    return NovelProject(
        meta=meta,
        characters=characters,
        assets={},
        foreshadows={},
        style_profiles=styles,
        scenes=scenes,
    )


def _write_project_yaml_dir(project_dir: Path, *, contract_track: str = "serial_20k") -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    proj_yaml = {
        "project_id": "t5b_cli_proj",
        "title": "档案室夜审录",
        "genre": "都市脑洞",
        "genre_id": "urban_brainhole",
        "synopsis": "CLI compile-prompt 测试项目",
        "default_pov_character": "mc",
        "max_rewrite_retries": 3,
        "author_level": "lv2_plus",
        "contract_track": contract_track,
        "pov_mode": "third_limited",
        "panel_policy": "forbid",
    }
    chars_yaml = {
        "characters": [
            {
                "id": "mc",
                "name": "唐子墨",
                "role": "夜间稽核员",
                "personality": "冷静克制",
                "speech_style": "简短带刺",
                "behavioral_anchors": {
                    "核对假账前": "食指中指并拢在左眼尾轻轻一按，呼吸停半拍。",
                    "准备亮底牌时": "把钢笔帽拧下来扣在台历右下角。",
                },
            },
            {
                "id": "police",
                "name": "冯念慈",
                "role": "刑侦副队长",
                "personality": "务实果断",
                "speech_style": "短陈述句",
                "behavioral_anchors": {
                    "听到玄乎说法时": "把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。",
                },
            },
        ]
    }
    assets_yaml = {"assets": []}
    foreshadows_yaml = {"foreshadows": []}
    styles_yaml = {
        "style_profiles": [
            {
                "id": "custom_sample",
                "name": "档案室冷峻样本",
                "granularity_notes": "动作驱动",
                "sample_text": "他把报废的红章按在回单空白处，抬眼盯住对面发抖的财务经理。",
            }
        ]
    }
    scenes_yaml = {
        "scenes": [
            {
                "chapter": ch,
                "scene_id": f"ch{ch:04d}_scene",
                "internal_title": f"第{ch}章交锋",
                "pov_character": "mc",
                "active_characters": ["mc", "police"],
                "active_assets": [],
                "style_profile_ids": ["custom_sample"],
                "location_time": f"第{ch}日夜晚，海城老档案室",
                "events": [f"第{ch}章冲突交锋"],
                "stop_condition": f"第{ch}章止于警笛声",
                "min_chars": 2050,
                "max_chars": 2300,
                "location_id": f"loc_{ch}",
            }
            for ch in range(1, 6)
        ]
    }
    for fname, payload in (
        ("project.yaml", proj_yaml),
        ("characters.yaml", chars_yaml),
        ("assets.yaml", assets_yaml),
        ("foreshadows.yaml", foreshadows_yaml),
        ("style_profiles.yaml", styles_yaml),
        ("outline.yaml", scenes_yaml),
    ):
        (project_dir / fname).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _dummy_report() -> ScanReport:
    return ScanReport(
        passed=False,
        char_count=1900,
        paragraph_count=10,
        violations=[
            Violation(
                rule_id="SYM_001",
                category="SYMMETRY",
                severity="CRITICAL",
                matched_text="不是A，而是B",
                line_number=1,
                context_snippet="不是A，而是B",
                fix_hint="改为直白陈述",
            )
        ],
    )


class TestT5bCompiler(unittest.TestCase):
    def test_hardcoded_copper_coin_example_removed_from_all_prompts(self) -> None:
        forbidden_snippets = (
            "沾着湿泥的铜钱",
            "他把那枚沾着湿泥的铜钱按在红木桌沿",
            "撑不过今晚十二点",
        )

        # Tomato track chapters 1..4 (scene + rewrite)
        tomato_proj = _build_project(contract_track="serial_20k", genre_id="urban_brainhole")
        tomato_state = WorldStateManager(tomato_proj)
        tomato_compiler = PromptCompiler(tomato_proj)
        report = _dummy_report()

        for scene in tomato_proj.scenes[:4]:
            recent = tuple(f"第{i}章测试正文。" for i in range(max(1, scene.chapter - 3), scene.chapter))
            sys_p, usr_p = tomato_compiler.compile_scene_prompt(
                scene, tomato_state, recent_chapter_texts=recent
            )
            rw_sys, rw_usr = tomato_compiler.compile_rewrite_prompt(
                scene, tomato_state, "待重写草稿", report, recent_chapter_texts=recent
            )
            for snippet in forbidden_snippets:
                self.assertNotIn(snippet, sys_p)
                self.assertNotIn(snippet, usr_p)
                self.assertNotIn(snippet, rw_sys)
                self.assertNotIn(snippet, rw_usr)
            # Custom Style_Samples must remain the sole style example source
            self.assertIn("他把报废的红章按在回单空白处", usr_p)

        # Legacy project (baiwu_dukou)
        legacy_proj = load_project(ROOT / "projects" / "baiwu_dukou")
        legacy_state = WorldStateManager(legacy_proj)
        legacy_compiler = PromptCompiler(legacy_proj)
        leg_sys, leg_usr = legacy_compiler.compile_scene_prompt(
            legacy_proj.scenes[0], legacy_state, recent_chapter_texts=()
        )
        for snippet in forbidden_snippets:
            self.assertNotIn(snippet, leg_sys)
            self.assertNotIn(snippet, leg_usr)

    def test_anchor_cooldown_marks_only_recently_used_anchors(self) -> None:
        proj = _build_project(contract_track="serial_20k")
        state = WorldStateManager(proj)
        compiler = PromptCompiler(proj)

        # Chapter 1: no prior chapters -> zero cooldown tags
        _, usr_ch1 = compiler.compile_scene_prompt(
            proj.scenes[0], state, recent_chapter_texts=()
        )
        self.assertNotIn(COOLDOWN_TAG, usr_ch1)

        # Chapter 4: ch1 used mc.核对假账前, ch2 used police.听到玄乎说法时, ch3 clean
        ch1_text = "唐子墨食指中指并拢在左眼尾轻轻一按，随即把账本拍在桌上。"
        ch2_text = "冯念慈把执法记录仪往前推两厘米，直视对方眼睛。"
        ch3_text = "走廊尽头的应急灯亮着，两人交换了化验回执。"
        _, usr_ch4 = compiler.compile_scene_prompt(
            proj.scenes[3],
            state,
            recent_chapter_texts=(ch1_text, ch2_text, ch3_text),
        )
        self.assertIn(
            f"核对假账前时表现为「食指中指并拢在左眼尾轻轻一按，呼吸停半拍。」{COOLDOWN_TAG}",
            usr_ch4,
        )
        self.assertIn(
            f"听到玄乎说法时时表现为「把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。」{COOLDOWN_TAG}",
            usr_ch4,
        )
        # Unused anchors must NOT carry the cooldown tag
        self.assertIn("准备亮底牌时时表现为「把钢笔帽拧下来扣在台历右下角。」", usr_ch4)
        self.assertNotIn(
            f"准备亮底牌时时表现为「把钢笔帽拧下来扣在台历右下角。」{COOLDOWN_TAG}",
            usr_ch4,
        )
        self.assertIn("确认证据成立时时表现为「食指关节在封皮上顶一下，直接报出行动时间。」", usr_ch4)
        self.assertNotIn(
            f"确认证据成立时时表现为「食指关节在封皮上顶一下，直接报出行动时间。」{COOLDOWN_TAG}",
            usr_ch4,
        )

        # Rewrite prompt forwards recent_chapter_texts and keeps the exact cooldown tag
        _, rw_ch4 = compiler.compile_rewrite_prompt(
            proj.scenes[3],
            state,
            "草稿",
            _dummy_report(),
            recent_chapter_texts=(ch1_text, ch2_text, ch3_text),
        )
        self.assertIn(
            f"核对假账前时表现为「食指中指并拢在左眼尾轻轻一按，呼吸停半拍。」{COOLDOWN_TAG}",
            rw_ch4,
        )

        # Chapter 5: window is (ch2, ch3, ch4_clean) -> ch1 anchor cooled off, ch2 anchor still cooling
        ch4_clean = "档案室门口的铁锁换了新编号，窗外下起小雨。"
        _, usr_ch5 = compiler.compile_scene_prompt(
            proj.scenes[4],
            state,
            recent_chapter_texts=(ch2_text, ch3_text, ch4_clean),
        )
        self.assertNotIn(
            f"核对假账前时表现为「食指中指并拢在左眼尾轻轻一按，呼吸停半拍。」{COOLDOWN_TAG}",
            usr_ch5,
        )
        self.assertIn(
            f"听到玄乎说法时时表现为「把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。」{COOLDOWN_TAG}",
            usr_ch5,
        )

    def test_golden_three_directives_injected_only_for_tomato_ch1_to_ch3(self) -> None:
        tomato_proj = _build_project(contract_track="serial_20k")
        tomato_state = WorldStateManager(tomato_proj)
        tomato_compiler = PromptCompiler(tomato_proj)

        for ch in (1, 2, 3):
            scene = tomato_proj.scenes[ch - 1]
            recent = tuple(f"前序第{i}章正文。" for i in range(1, ch))
            _, usr_p = tomato_compiler.compile_scene_prompt(
                scene, tomato_state, recent_chapter_texts=recent
            )
            self.assertIn(GOLDEN_THREE_HEADER, usr_p)
            idx_sec3 = usr_p.index("## 3. 叙事禁令与题材风控")
            idx_g3 = usr_p.index(GOLDEN_THREE_HEADER)
            idx_sec4 = usr_p.index("## 4. 任务执行流")
            self.assertLess(idx_sec3, idx_g3)
            self.assertLess(idx_g3, idx_sec4)

            directives = writer_directives(ch)
            self.assertTrue(directives)
            for directive in directives:
                self.assertIn(f"- {directive}", usr_p[idx_g3:idx_sec4])

        # Chapter 4 on Tomato track must NOT include golden three header
        _, usr_ch4 = tomato_compiler.compile_scene_prompt(
            tomato_proj.scenes[3],
            tomato_state,
            recent_chapter_texts=("ch1", "ch2", "ch3"),
        )
        self.assertNotIn(GOLDEN_THREE_HEADER, usr_ch4)

        # Legacy track (both baiwu_dukou and custom legacy project) must NOT include golden three header
        legacy_proj = load_project(ROOT / "projects" / "baiwu_dukou")
        legacy_state = WorldStateManager(legacy_proj)
        legacy_compiler = PromptCompiler(legacy_proj)
        _, leg_usr1 = legacy_compiler.compile_scene_prompt(
            legacy_proj.scenes[0], legacy_state, recent_chapter_texts=()
        )
        self.assertNotIn(GOLDEN_THREE_HEADER, leg_usr1)

        custom_legacy = _build_project(contract_track="legacy", genre_id="")
        custom_legacy_state = WorldStateManager(custom_legacy)
        custom_legacy_compiler = PromptCompiler(custom_legacy)
        for ch in (1, 2, 3):
            recent = tuple(f"第{i}章" for i in range(1, ch))
            _, u = custom_legacy_compiler.compile_scene_prompt(
                custom_legacy.scenes[ch - 1],
                custom_legacy_state,
                recent_chapter_texts=recent,
            )
            self.assertNotIn(GOLDEN_THREE_HEADER, u)

    def test_recent_chapter_texts_strict_validation(self) -> None:
        proj = _build_project(contract_track="serial_20k", num_chapters=5)
        state = WorldStateManager(proj)
        compiler = PromptCompiler(proj)
        report = _dummy_report()

        # Missing required kw-only argument -> TypeError
        with self.assertRaises(TypeError):
            compiler.compile_scene_prompt(proj.scenes[0], state)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            compiler.compile_rewrite_prompt(proj.scenes[0], state, "草稿", report)  # type: ignore[call-arg]

        # Ch.1 non-empty -> ValueError
        with self.assertRaises(ValueError):
            compiler.compile_scene_prompt(
                proj.scenes[0], state, recent_chapter_texts=("多余的第0章",)
            )

        # Ch.2 with 0 or 2 items -> ValueError
        for bad_ch2 in ((), ("ch1", "extra")):
            with self.assertRaises(ValueError):
                compiler.compile_scene_prompt(
                    proj.scenes[1], state, recent_chapter_texts=bad_ch2
                )

        # Ch.5 with 2 or 4 items -> ValueError
        for bad_ch5 in (("ch3", "ch4"), ("ch1", "ch2", "ch3", "ch4")):
            with self.assertRaises(ValueError):
                compiler.compile_scene_prompt(
                    proj.scenes[4], state, recent_chapter_texts=bad_ch5
                )

        # str / bytes / None / non-str element -> ValueError
        for bad_type in ("直接传字符串", b"bytes", None, [123]):
            with self.assertRaises(ValueError):
                compiler.compile_scene_prompt(
                    proj.scenes[1], state, recent_chapter_texts=bad_type  # type: ignore[arg-type]
                )

        # compile_rewrite_prompt also enforces exact length
        with self.assertRaises(ValueError):
            compiler.compile_rewrite_prompt(
                proj.scenes[1], state, "草稿", report, recent_chapter_texts=()
            )

    def test_memory_pyramid_compile_context_requires_valid_current_virtual_day(self) -> None:
        pyramid = MemoryPyramid()
        pyramid.record_chapter(
            1,
            "第1章正文推进。夜色压过窗沿。",
            virtual_day=1,
            title="开端",
            character_ids=["mc"],
        )

        # Missing current_virtual_day -> TypeError
        with self.assertRaises(TypeError):
            pyramid.compile_context()  # type: ignore[call-arg]

        # Invalid values (0, -1, True, False, None, str) -> ValueError
        for bad_day in (0, -1, True, False, None, "3"):
            with self.assertRaises(ValueError):
                pyramid.compile_context(current_virtual_day=bad_day)  # type: ignore[arg-type]

        # Valid call renders time anchor and passes state.virtual_day via PromptCompiler
        proj = _build_project(contract_track="serial_20k")
        state = WorldStateManager(proj)
        state.virtual_day = 7
        compiler = PromptCompiler(proj)
        _, usr_p = compiler.compile_scene_prompt(
            proj.scenes[1],
            state,
            memory_pyramid=pyramid,
            recent_chapter_texts=("第1章正文。",),
        )
        self.assertIn("### [时间锚点] 当前第7天", usr_p)
        self.assertIn("第1章（第1天，距今6天）", usr_p)

    def test_engine_raises_production_gate_failure_when_recent_chapter_file_missing(self) -> None:
        proj = _build_project(contract_track="serial_20k", num_chapters=5)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            provider = _RecordingProvider(
                [
                    "唐子墨食指中指并拢在左眼尾轻轻一按，随即翻开第一页旧账单。",
                    "第二章正文内容，没有任何锚点复读。",
                ]
            )
            engine = NovelGenerationEngine(
                project=proj,
                provider=provider,
                output_dir=out_dir,
                checker=_PassChecker(),
            )
            # Run chapter 1 -> produces chapter_0001.txt & snapshot
            engine.run(target_chapters=[1])
            self.assertEqual(len(provider.requests), 1)
            self.assertNotIn(COOLDOWN_TAG, provider.requests[0].user_prompt)

            # Run chapter 2 normally -> chapter_0001.txt is read and cools mc's anchor
            engine2 = NovelGenerationEngine(
                project=proj,
                provider=provider,
                output_dir=out_dir,
                checker=_PassChecker(),
            )
            engine2.run(target_chapters=[2])
            self.assertEqual(len(provider.requests), 2)
            self.assertIn(
                f"核对假账前时表现为「食指中指并拢在左眼尾轻轻一按，呼吸停半拍。」{COOLDOWN_TAG}",
                provider.requests[1].user_prompt,
            )

            # Delete chapter_0001.txt while keeping chapter_0001_after.json snapshot -> Ch.2 must fail before LLM
            ch1_file = out_dir / "chapters" / "chapter_0001.txt"
            ch1_file.unlink()
            ch2_file = out_dir / "chapters" / "chapter_0002.txt"
            ch2_file.unlink()
            ch2_meta = out_dir / "chapters" / "chapter_0002.meta.json"
            if ch2_meta.exists():
                ch2_meta.unlink()

            call_count_before = len(provider.requests)
            engine_missing = NovelGenerationEngine(
                project=proj,
                provider=provider,
                output_dir=out_dir,
                checker=_PassChecker(),
            )
            with self.assertRaises(ProductionGateFailure):
                engine_missing.run(target_chapters=[2])
            self.assertEqual(len(provider.requests), call_count_before)

    def test_engine_chapter2_rewrite_keeps_cooldown_and_chapter5_missing_ch2_fails(self) -> None:
        proj = _build_project(contract_track="serial_20k", num_chapters=5)

        class _FailFirstAttemptOnCh2Checker(AITicChecker):
            def __init__(self) -> None:
                super().__init__()
                self.ch2_calls = 0

            def scan(self, text: str, **kwargs: Any) -> ScanReport:
                chapter = kwargs.get("chapter")
                if chapter == 2 and self.ch2_calls == 0:
                    self.ch2_calls += 1
                    return _dummy_report()
                return ScanReport(passed=True, char_count=len(text), paragraph_count=3, violations=[])

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            provider = _RecordingProvider(
                [
                    "唐子墨食指中指并拢在左眼尾轻轻一按，随即核对第一页流水。",
                    "第2章初稿触发重写。",
                    "第2章重写定稿，避开了冷却锚点。",
                    "第3章正文平稳推进。",
                    "第4章正文推进至尾声。",
                ]
            )
            engine = NovelGenerationEngine(
                project=proj,
                provider=provider,
                output_dir=out_dir,
                checker=_FailFirstAttemptOnCh2Checker(),
            )
            engine.run(target_chapters=[1, 2, 3, 4])

            # Requests: 0=ch1 draft, 1=ch2 draft, 2=ch2 rewrite, 3=ch3 draft, 4=ch4 draft
            self.assertEqual(len(provider.requests), 5)
            self.assertEqual(provider.requests[2].purpose, "rewrite")
            self.assertIn(
                f"核对假账前时表现为「食指中指并拢在左眼尾轻轻一按，呼吸停半拍。」{COOLDOWN_TAG}",
                provider.requests[1].user_prompt,
            )
            self.assertIn(
                f"核对假账前时表现为「食指中指并拢在左眼尾轻轻一按，呼吸停半拍。」{COOLDOWN_TAG}",
                provider.requests[2].user_prompt,
            )

            # Remove chapter_0002.txt while keeping chapter_0003.txt, chapter_0004.txt and ch4 snapshot:
            # Chapter 5 requires chapters 2, 3, 4 -> missing ch2 must raise ProductionGateFailure without LLM call
            (out_dir / "chapters" / "chapter_0002.txt").unlink()
            calls_before = len(provider.requests)
            engine_ch5 = NovelGenerationEngine(
                project=proj,
                provider=provider,
                output_dir=out_dir,
                checker=_PassChecker(),
            )
            with self.assertRaises(ProductionGateFailure):
                engine_ch5.run(target_chapters=[5])
            self.assertEqual(len(provider.requests), calls_before)

    def test_cli_compile_prompt_chapter1_and_chapter_gt1_output_dir_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj_dir = root / "proj"
            out_dir = root / "out"
            _write_project_yaml_dir(proj_dir)

            # 1. Chapter 1 succeeds without --output-dir
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc1 = cli_main(["compile-prompt", "--project", str(proj_dir), "--chapter", "1"])
            self.assertEqual(rc1, 0, buf_err.getvalue())
            self.assertIn(GOLDEN_THREE_HEADER, buf_out.getvalue())
            self.assertNotIn("沾着湿泥的铜钱", buf_out.getvalue())

            # 2. Chapter 2 without --output-dir fails with non-zero exit
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc2_no_dir = cli_main(["compile-prompt", "--project", str(proj_dir), "--chapter", "2"])
            self.assertNotEqual(rc2_no_dir, 0)
            self.assertIn("--output-dir", buf_err.getvalue())

            # 3. Chapter 2 with --output-dir but missing chapter_0001.txt fails with non-zero exit
            chapters_dir = out_dir / "chapters"
            chapters_dir.mkdir(parents=True, exist_ok=True)
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc2_missing = cli_main(
                    [
                        "compile-prompt",
                        "--project",
                        str(proj_dir),
                        "--chapter",
                        "2",
                        "--output-dir",
                        str(out_dir),
                    ]
                )
            self.assertNotEqual(rc2_missing, 0)
            self.assertIn("0001", buf_err.getvalue())

            # 4. Chapter 2 with chapter_0001.txt succeeds and reflects anchor cooldown
            (chapters_dir / "chapter_0001.txt").write_text(
                "唐子墨食指中指并拢在左眼尾轻轻一按，盯住对面的账册。",
                encoding="utf-8",
            )
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc2_ok = cli_main(
                    [
                        "compile-prompt",
                        "--project",
                        str(proj_dir),
                        "--chapter",
                        "2",
                        "--output-dir",
                        str(out_dir),
                    ]
                )
            self.assertEqual(rc2_ok, 0, buf_err.getvalue())
            self.assertIn(
                f"核对假账前时表现为「食指中指并拢在左眼尾轻轻一按，呼吸停半拍。」{COOLDOWN_TAG}",
                buf_out.getvalue(),
            )

            # 5. Chapter 5 missing chapter_0002.txt (even when ch3 & ch4 exist) must fail
            (chapters_dir / "chapter_0003.txt").write_text("第3章普通推进。", encoding="utf-8")
            (chapters_dir / "chapter_0004.txt").write_text("第4章普通推进。", encoding="utf-8")
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc5_missing = cli_main(
                    [
                        "compile-prompt",
                        "--project",
                        str(proj_dir),
                        "--chapter",
                        "5",
                        "--output-dir",
                        str(out_dir),
                    ]
                )
            self.assertNotEqual(rc5_missing, 0)
            self.assertIn("0002", buf_err.getvalue())

            # Adding chapter_0002.txt lets chapter 5 compile cleanly
            (chapters_dir / "chapter_0002.txt").write_text(
                "冯念慈把执法记录仪往前推两厘米，记下门牌号。",
                encoding="utf-8",
            )
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc5_ok = cli_main(
                    [
                        "compile-prompt",
                        "--project",
                        str(proj_dir),
                        "--chapter",
                        "5",
                        "--output-dir",
                        str(out_dir),
                    ]
                )
            self.assertEqual(rc5_ok, 0, buf_err.getvalue())
            self.assertIn(
                f"听到玄乎说法时时表现为「把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。」{COOLDOWN_TAG}",
                buf_out.getvalue(),
            )
            self.assertNotIn(GOLDEN_THREE_HEADER, buf_out.getvalue())


if __name__ == "__main__":
    unittest.main()
