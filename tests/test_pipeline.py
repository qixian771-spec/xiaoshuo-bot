import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.cli import main as cli_main
from xiaoshuo_bot.compiler import PromptCompiler
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.state import WorldStateManager


class TestPipelineAndFaultSmoke(unittest.TestCase):
    def test_prompt_compilation_and_state_evolution(self) -> None:
        project = load_project(ROOT / "projects" / "baiwu_dukou")
        state = WorldStateManager(project)
        compiler = PromptCompiler(project)

        # Chapter 1 prompt must inject FS-01 & FS-02 planting rules and initial pocket_watch location
        sys_p1, usr_p1 = compiler.compile_scene_prompt(project.scenes[0], state)
        self.assertIn("唯一视点人物：林晚", sys_p1)
        self.assertIn("周叙携带的牛皮纸公文信封内", usr_p1)
        self.assertIn("FS-01", usr_p1)
        self.assertIn("FS-02", usr_p1)

        # Simulate Chapter 1 completion and verify Chapter 2 sees mutated pocket_watch location
        fake_ch1 = "雾气从江面涌上来。门垫上沾着红铁丹防锈漆，周叙左袖口有一道蓝黑墨渍。"
        state.advance_after_chapter(project.scenes[0], fake_ch1)
        _, usr_p2 = compiler.compile_scene_prompt(project.scenes[1], state)
        self.assertIn("敬山钟表铺木柜台绒布垫上", usr_p2)
        self.assertIn("FS-03", usr_p2)

    def test_fault_injection_incident_and_auto_rewrite_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rc = cli_main(
                [
                    "smoke-fault",
                    "--project",
                    str(ROOT / "projects" / "baiwu_dukou"),
                    "--output-dir",
                    tmpdir,
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue((Path(tmpdir) / "audit" / "incidents.jsonl").exists())
            self.assertTrue((Path(tmpdir) / "chapters" / "chapter_01.txt").exists())


if __name__ == "__main__":
    unittest.main()
