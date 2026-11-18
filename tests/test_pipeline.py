import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.cli import main as cli_main
from xiaoshuo_bot.compiler import PromptCompiler
from xiaoshuo_bot.engine import NovelGenerationEngine
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.state import WorldStateManager


class _MustNotCallProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError(f"LLM should not be called during verified --resume, got {request.request_id}")


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
            out_dir = Path(tmpdir)
            self.assertTrue((out_dir / "audit" / "incidents.jsonl").exists())
            self.assertTrue((out_dir / "chapters" / "chapter_0001.txt").exists())
            self.assertTrue((out_dir / "chapters" / "chapter_0001.meta.json").exists())
            self.assertTrue((out_dir / "snapshots" / "chapter_0000_initial.json").exists())
            self.assertTrue((out_dir / "snapshots" / "chapter_0001_after.json").exists())
            self.assertTrue((out_dir / "snapshots" / "memory_0001_after.json").exists())

            meta = json.loads((out_dir / "chapters" / "chapter_0001.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["chapter"], 1)
            self.assertEqual(len(meta["sha256"]), 64)

    def test_resume_legacy_two_digit_baiwu_dukou_readonly_copy(self) -> None:
        source_proj = ROOT / "projects" / "baiwu_dukou"
        with tempfile.TemporaryDirectory() as tmpdir:
            copied_proj = Path(tmpdir) / "baiwu_dukou"
            shutil.copytree(source_proj, copied_proj)

            # Confirm legacy :02d files exist in the copied fixture
            self.assertTrue((copied_proj / "output" / "chapters" / "chapter_01.txt").is_file())
            self.assertTrue((copied_proj / "output" / "chapters" / "chapter_05.txt").is_file())
            self.assertTrue((copied_proj / "output" / "snapshots" / "chapter_00_initial.json").is_file())

            project = load_project(copied_proj)
            engine = NovelGenerationEngine(
                project=project,
                provider=_MustNotCallProvider(),
                output_dir=copied_proj / "output",
            )
            manifest = engine.run(resume=True)

            self.assertEqual(manifest["chapter_count"], 5)
            self.assertEqual(manifest["all_completed_chapters_on_disk"], [1, 2, 3, 4, 5])
            self.assertTrue(manifest["all_gates_passed"])
            # Legacy :02d chapter files must stay intact with zero ambiguous :04d duplicates
            for ch in range(1, 6):
                self.assertTrue((copied_proj / "output" / "chapters" / f"chapter_{ch:02d}.txt").is_file())
                self.assertFalse((copied_proj / "output" / "chapters" / f"chapter_{ch:04d}.txt").exists())

            # Also verify CLI `resume` subcommand works cleanly on the copied project
            rc = cli_main(
                [
                    "resume",
                    "--project",
                    str(copied_proj),
                    "--provider",
                    "bridge",
                    "--output-dir",
                    str(copied_proj / "output"),
                ]
            )
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
