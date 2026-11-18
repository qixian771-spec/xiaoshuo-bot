import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.checker import AITicChecker
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.models import ProjectValidationError


class TestAITicChecker(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = AITicChecker()
        self.project = load_project(ROOT / "projects" / "baiwu_dukou")

    def test_clean_prose_with_first_person_inside_dialogue_passes(self) -> None:
        clean = (
            "雾气漫过石阶时，林晚把铜壳怀表平放在绒布垫上，左手拇指轻轻刮过食指侧面的旧疤。\n"
            "“我昨天去过三号码头，没见到他。”周叙摘下眼镜，用衣角擦了擦镜片边缘的水汽。"
        )
        report = self.checker.scan(
            text=clean,
            scene=None,
            characters=self.project.characters,
        )
        self.assertTrue(report.passed, f"Unexpected violations: {report.violations}")

    def test_catches_format_summary_body_symmetry_and_pov_violations(self) -> None:
        bad = (
            "# 第一章 渡口\n"
            "林晚推门进来时不禁一愣，心中一紧。\n"
            "周叙眸光微闪，周叙心想这回怕是藏不住了。\n"
            "这不是一块普通的铜表，而是沉甸甸的罪证。它是铁证，是呼唤，是宿命的回响。\n"
            "这一刻，命运的齿轮开始转动。总而言之，我一定要查到底。"
        )
        scene = self.project.scenes[0]
        report = self.checker.scan(
            text=bad,
            scene=scene,
            characters=self.project.characters,
        )
        self.assertFalse(report.passed)
        categories = {v.category for v in report.violations}
        self.assertIn("FORMAT_VIOLATION", categories)
        self.assertIn("SUMMARY_CLICHE", categories)
        self.assertIn("BODY_TIC_CLICHE", categories)
        self.assertIn("STRUCTURAL_SYMMETRY", categories)
        self.assertIn("POV_VIOLATION", categories)

    def test_baiwu_dukou_all_five_chapters_pass_with_zero_violations(self) -> None:
        chapters_dir = ROOT / "projects" / "baiwu_dukou" / "output" / "chapters"
        for idx, scene in enumerate(self.project.scenes, start=1):
            chapter_path = chapters_dir / f"chapter_{idx:02d}.txt"
            text = chapter_path.read_text(encoding="utf-8").rstrip()
            report = self.checker.scan(
                text=text,
                scene=scene,
                characters=self.project.characters,
                project_meta=self.project.meta,
            )
            self.assertTrue(
                report.passed,
                f"Chapter {idx} failed unexpectedly: {report.violations}",
            )
            self.assertEqual(
                report.violations,
                [],
                f"Chapter {idx} produced false-positive violations: {report.violations}",
            )

    def test_zero_arg_scan_signature_and_unknown_enum_rejection(self) -> None:
        report = AITicChecker().scan("林晚推开木门，将铜尺摆在柜台边缘。")
        self.assertTrue(report.passed)
        with self.assertRaises(ProjectValidationError):
            AITicChecker(pov_mode="omniscient_god")
        with self.assertRaises(ProjectValidationError):
            self.checker.scan("正文。", panel_policy="allow_anything")

    def test_fixes_eight_preexisting_false_positives(self) -> None:
        # 1. FMT_001: legal left quotes “「『 at start + 【 when allow_system_tag
        for left_quote in ("“", "「", "『"):
            rep = self.checker.scan(f"{left_quote}你终于来了。”林晚把钥匙放在桌上。")
            self.assertTrue(rep.passed, f"FMT_001 false positive for {left_quote}: {rep.violations}")

        # 2. FMT_001 + FMT_006 + FMT_007 + danmaku/dialogue masking under allow_system_tag + short_paragraph_mode
        sys_text = (
            "【叮！检测到宿主银行卡余额清零，神豪返利系统已激活！】\n\n"
            "【提示：本日返利倍率十倍】\n\n"
            "【弹幕：我哭死，主播这不是送人头，而是直接单刷全场，我心中一紧都看呆了！】\n\n"
            "“我不是来求你的，而是来通知你的。”陈默把银行卡拍在桌上，“是我太天真，是我瞎了眼，是我活该。”"
        )
        rep_sys = self.checker.scan(
            sys_text,
            panel_policy="allow_system_tag",
            contract_track="serial_20k",
            chapter=2,
        )
        self.assertTrue(rep_sys.passed, f"System tag / masking false positive: {rep_sys.violations}")

        # 3. FMT_006 still blocks metadata leaks like 【本章完】 / 【作者注】 / 【阅读提示】 even with allow_system_tag
        for leak in ("【本章完】", "【作者注：明日三更】", "【阅读提示：架空背景】"):
            rep_leak = self.checker.scan(
                f"陈默推门走了出去。\n\n{leak}",
                panel_policy="allow_system_tag",
                contract_track="serial_20k",
            )
            self.assertFalse(rep_leak.passed)
            self.assertIn("FMT_006", {v.rule_id for v in rep_leak.violations})

        # 4. FMT_004 catches ==== and ++++ dividers
        for div in ("====", "++++"):
            rep_div = self.checker.scan(f"陈默关上房门。\n{div}\n半小时后，门外响起脚步声。")
            self.assertFalse(rep_div.passed)
            self.assertIn("FMT_004", {v.rule_id for v in rep_div.violations})

        # 5. FMT_005 allows "第一部，是……" narrative sentence, while catching "第一千两百章 破局"
        rep_narrative_num = self.checker.scan("第一部，是三年前留在港务局档案室里的旧胶片，他一直收在抽屉最底格。")
        self.assertTrue(rep_narrative_num.passed, f"FMT_005 false positive: {rep_narrative_num.violations}")
        rep_ch_header = self.checker.scan("第一千两百章 破局\n陈默推开殿门。")
        self.assertFalse(rep_ch_header.passed)
        self.assertIn("FMT_005", {v.rule_id for v in rep_ch_header.violations})

        # 6. FMT_007 allows 「」『』【】《》 on line 1 in legacy mode, and short line 1 in short_paragraph_mode
        for short_first in ("「快走」", "《港务旧档》", "陈默没说话"):
            use_short = short_first == "陈默没说话"
            rep_fmt7 = self.checker.scan(short_first, short_paragraph_mode=use_short)
            self.assertTrue(rep_fmt7.passed, f"FMT_007 false positive for {short_first}: {rep_fmt7.violations}")

        # 7. POV_001: first_person allows "我"; third_limited exempts the 8 idioms
        rep_fp = self.checker.scan(
            "我拎起工具箱迈进窄巷，把钥匙插进锈死的铁锁里。",
            pov_mode="first_person",
        )
        self.assertTrue(rep_fp.passed, f"POV_001 false positive in first_person: {rep_fp.violations}")

        idiom_text = (
            "那老者生性我行我素，行事向来唯我独尊，从不理会江湖上的尔虞我诈。"
            "他深知时不我待，这桩差事舍我其谁，绝不学旁人卿卿我我，更非我族类莫入此门，最终练就物我两忘之境。"
        )
        rep_idioms = self.checker.scan(idiom_text, pov_mode="third_limited")
        self.assertTrue(rep_idioms.passed, f"POV_001 false positive on idioms: {rep_idioms.violations}")

        # 8. POV_005 & TIC_010 fixes
        rep_pov5_ok = self.checker.scan(
            "我看了一眼周叙，我心里明白这账单肯定动过手脚。"
            "周叙似乎心里清楚，陈敬山大概心里明白，显然陆敏心里盘算着别的事，我猜到，周叙心里明白退路没了。",
            scene=self.project.scenes[0].__class__(
                **{**self.project.scenes[0].to_dict(), "min_chars": 10}
            ),
            characters=self.project.characters,
            pov_mode="first_person",
        )
        self.assertTrue(rep_pov5_ok.passed, f"POV_005 false positive: {rep_pov5_ok.violations}")

        rep_tic10_ok = self.checker.scan(
            "陈敬山听完这话忍俊不禁，那瘦弱书生看似弱不禁风，城门夜里不禁行，众人照常进出。"
        )
        self.assertTrue(rep_tic10_ok.passed, f"TIC_010 false positive: {rep_tic10_ok.violations}")


if __name__ == "__main__":
    unittest.main()
