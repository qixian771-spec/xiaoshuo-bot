"""Length target wording (2026-11-18 A/B on Gemini 3.8 Flash: drafts landed at 1033-1926 chars
against a 1900 floor because the prompt only said "min~max, stop when events are done").

The hard gate (LEN_001 at scene.min_chars) is unchanged; the prompt now names an explicit target,
and a LEN_001 rewrite states the exact shortfall and the events to expand.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from xiaoshuo_bot.compiler import PromptCompiler, target_chapter_chars
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.models import ScanReport, Violation
from xiaoshuo_bot.state import WorldStateManager

ROOT = Path(__file__).resolve().parents[1]


def _report(char_count: int, rule_id: str) -> ScanReport:
    return ScanReport(
        passed=False,
        char_count=char_count,
        paragraph_count=10,
        violations=[
            Violation(
                rule_id=rule_id,
                category="LENGTH_VIOLATION",
                severity="CRITICAL",
                matched_text=f"{char_count} chars",
                line_number=1,
                context_snippet="……",
                fix_hint="篇幅不符",
            )
        ],
    )


class LengthTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = load_project(ROOT / "projects" / "baiwu_dukou")
        self.scene = self.project.scenes[0]
        self.compiler = PromptCompiler(self.project)
        self.state = WorldStateManager(self.project)

    def test_target_is_range_midpoint_rounded_down(self) -> None:
        self.assertEqual(target_chapter_chars(replace(self.scene, min_chars=1900, max_chars=3600)), 2700)
        self.assertEqual(target_chapter_chars(replace(self.scene, min_chars=2000, max_chars=2600)), 2300)
        # Never below the hard floor even for a degenerate range.
        self.assertEqual(target_chapter_chars(replace(self.scene, min_chars=1950, max_chars=1950)), 1950)

    def test_scene_prompt_names_target_and_drops_stop_early_wording(self) -> None:
        sys_p, usr_p = self.compiler.compile_scene_prompt(
            self.scene, self.state, recent_chapter_texts=()
        )
        target = target_chapter_chars(self.scene)
        self.assertIn(f"目标约 {target} 字", sys_p)
        self.assertIn(f"低于 {self.scene.min_chars} 字会被系统直接打回重写", sys_p)
        self.assertIn(f"约 {target} 汉字", usr_p)
        self.assertNotIn("事件写尽即止", sys_p)
        self.assertNotIn("事件写尽即止", usr_p)

    def test_short_draft_rewrite_states_gap_and_events(self) -> None:
        short = self.scene.min_chars - 47
        _, rewrite = self.compiler.compile_rewrite_prompt(
            self.scene, self.state, "草稿", _report(short, "LEN_001"), recent_chapter_texts=()
        )
        self.assertIn("### 篇幅缺口", rewrite)
        self.assertIn(f"上一版只有 {short} 字", rewrite)
        self.assertIn("还少 47 字", rewrite)
        self.assertIn(f"本次必须写到约 {target_chapter_chars(self.scene)} 字", rewrite)
        for event in self.scene.events:
            self.assertIn(event, rewrite)

    def test_other_violations_get_no_length_block(self) -> None:
        _, rewrite = self.compiler.compile_rewrite_prompt(
            self.scene, self.state, "草稿", _report(2500, "SYM_001"), recent_chapter_texts=()
        )
        self.assertNotIn("### 篇幅缺口", rewrite)
        self.assertNotIn("### 篇幅超限裁剪", rewrite)

    def test_overlong_draft_rewrite_states_excess_and_safe_range(self) -> None:
        over = self.scene.max_chars + 1
        _, rewrite = self.compiler.compile_rewrite_prompt(
            self.scene, self.state, "草稿", _report(over, "LEN_002"), recent_chapter_texts=()
        )
        self.assertIn("### 篇幅超限裁剪", rewrite)
        self.assertIn(f"上一版共 {over} 字", rewrite)
        self.assertIn("共 1 字", rewrite)
        self.assertIn(f"{self.scene.min_chars}～{self.scene.max_chars} 字", rewrite)

    def test_solo_scene_prompt_and_narration_wall_rewrite_explain_quote_reset(self) -> None:
        solo_meta = replace(self.project.meta, contract_track="complete_150k", panel_policy="forbid")
        solo_scene = replace(self.scene, active_characters=[self.scene.pov_character])
        solo_proj = replace(self.project, meta=solo_meta, scenes=[solo_scene])
        solo_compiler = PromptCompiler(solo_proj)
        sys_p, _ = solo_compiler.compile_scene_prompt(
            solo_scene, self.state, recent_chapter_texts=()
        )
        self.assertIn("单人独角戏", sys_p)
        self.assertIn("纯动作描写无法打断纯旁白计数", sys_p)

        _, rewrite = solo_compiler.compile_rewrite_prompt(
            solo_scene,
            self.state,
            "草稿",
            _report(2200, "RHYTHM_NARRATION_WALL"),
            recent_chapter_texts=(),
        )
        self.assertIn("### 纯旁白墙切分指引", rewrite)
        self.assertIn("纯动作或景物描写即使再短也不会清零连续纯旁白字数", rewrite)
        self.assertIn("低声自语", rewrite)


if __name__ == "__main__":
    unittest.main()
