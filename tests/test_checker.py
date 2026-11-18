import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.checker import AITicChecker
from xiaoshuo_bot.loader import load_project


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


if __name__ == "__main__":
    unittest.main()
