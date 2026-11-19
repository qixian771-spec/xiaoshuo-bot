from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable, List, Optional
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.checker import AITicChecker
from xiaoshuo_bot.cli import _build_text_provider
from xiaoshuo_bot.critic_polisher import (
    AuthorNoteViolationError,
    CriticEvaluation,
    SurgicalPolishGateError,
    TomatoCriticPolisher,
)
from xiaoshuo_bot.engine import NovelGenerationEngine, ProductionGateFailure
from xiaoshuo_bot.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMStreamTruncatedError,
)
from xiaoshuo_bot.loader import ProjectValidationError, validate_project
from xiaoshuo_bot.models import (
    AssetState,
    CharacterAnchor,
    ForeshadowItem,
    NovelProject,
    ProjectMeta,
    ScanReport,
    SceneOutline,
    StyleProfile,
)
from xiaoshuo_bot.state import (
    CharacterStateViolationError,
    ForeshadowConfigurationError,
    ForeshadowEvidenceMissingError,
    WorldStateManager,
)


GOOD_TEXT = "风从江面吹来。\n\n陈屿摸到口袋里的铜铃，铃舌冰凉。\n\n他推门走进雨里。"
BAD_TEXT = "风从江面吹来。\n\n陈屿站在门口，一言不发。\n\n他推门走进雨里。"


def _scene(chapter: int, *, active: Optional[List[str]] = None) -> SceneOutline:
    return SceneOutline(
        chapter=chapter,
        scene_id=f"ch{chapter:04d}_scene",
        internal_title=f"第{chapter}节",
        pov_character="hero",
        active_characters=active if active is not None else ["hero"],
        active_assets=[],
        style_profile_ids=["default_style"],
        location_time="傍晚，老街",
        events=[f"第{chapter}章核心事件"],
        stop_condition="止于悬念",
        location_id="old_street",
        calendar_days_elapsed=1,
    )


def _project(
    *,
    foreshadows: Optional[dict] = None,
    scenes: Optional[List[SceneOutline]] = None,
    extra_characters: Optional[dict] = None,
    max_rewrite_retries: int = 1,
) -> NovelProject:
    characters = {
        "hero": CharacterAnchor(
            id="hero", name="陈屿", role="主角", personality="冷静", speech_style="简短"
        ),
    }
    if extra_characters:
        characters.update(extra_characters)
    meta = ProjectMeta(
        project_id="audit_e1e2",
        title="审计测试书",
        genre="都市",
        synopsis="测试",
        default_pov_character="hero",
        contract_track="complete_150k",
        max_rewrite_retries=max_rewrite_retries,
    )
    return NovelProject(
        meta=meta,
        characters=characters,
        assets={
            "token": AssetState(
                id="token",
                name="玄铁令牌",
                location="内袋",
                holder="陈屿",
                condition="完好",
                physical_details="旧徽记",
            )
        },
        foreshadows=foreshadows or {},
        style_profiles={
            "default_style": StyleProfile(
                id="default_style", name="测试文风", granularity_notes="短句", sample_text="风吹过门缝。"
            )
        },
        scenes=scenes or [_scene(1)],
    )


def _bell_foreshadow(plant_keywords: Optional[List[str]] = None) -> dict:
    return {
        "FS-BELL": ForeshadowItem(
            id="FS-BELL",
            title="铜铃",
            plant_chapter=1,
            due_chapter=2,
            plant_instruction="口袋里的铜铃",
            payoff_instruction="铜铃内壁刻字",
            plant_keywords=["铜铃"] if plant_keywords is None else plant_keywords,
            payoff_keywords=["内壁刻字"],
        )
    }


class _PassChecker:
    """Checker stub that always passes, isolating the world-state gate under test."""

    def scan(self, text: str, **_: object) -> ScanReport:
        return ScanReport(passed=True, char_count=len(text), paragraph_count=text.count("\n\n") + 1)


class _ScriptedProvider(LLMProvider):
    def __init__(self, script: List[Callable[[LLMRequest], LLMResponse]]) -> None:
        self.script = list(script)
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        step = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        return step(request)


def _text(content: str) -> Callable[[LLMRequest], LLMResponse]:
    return lambda req: LLMResponse(request_id=req.request_id, content=content, model="fake", provider="fake")


def _raise(exc: Exception) -> Callable[[LLMRequest], LLMResponse]:
    def _f(_req: LLMRequest) -> LLMResponse:
        raise exc

    return _f


class _StubCriticPolisher:
    def __init__(
        self,
        *,
        eval_exc: Optional[Exception] = None,
        note_exc: Optional[Exception] = None,
    ) -> None:
        self.eval_exc = eval_exc
        self.note_exc = note_exc

    def evaluate_chapter(self, text: str, **_: object) -> CriticEvaluation:
        if self.eval_exc is not None:
            raise self.eval_exc
        return CriticEvaluation(
            first_screen_hook=80, cool_point_density=80, comment_interactivity=80, pacing_drag=80,
            overall_score=80, weak_paragraphs=[],
        )

    def surgical_polish(self, *a: object, **k: object) -> None:  # pragma: no cover - not reached
        raise AssertionError("polish should not be called without weak paragraphs")

    def generate_author_note(self, text: str, **_: object) -> str:
        if self.note_exc is not None:
            raise self.note_exc
        return "下一章见分晓！"


class TestAuditE1StateGate(unittest.TestCase):
    def test_validate_chapter_is_pure_and_matches_advance(self) -> None:
        proj = _project(foreshadows=_bell_foreshadow(), scenes=[_scene(1), _scene(2)])
        state = WorldStateManager(proj)
        before = json.dumps(state.snapshot_dict(), sort_keys=True, ensure_ascii=False)
        with self.assertRaises(ForeshadowEvidenceMissingError):
            state.validate_chapter(proj.scenes[0], BAD_TEXT)
        validated = state.validate_chapter(proj.scenes[0], GOOD_TEXT)
        self.assertIn("FS-BELL", validated["plant_evidences"])
        self.assertEqual(before, json.dumps(state.snapshot_dict(), sort_keys=True, ensure_ascii=False))
        diff = state.advance_after_chapter(proj.scenes[0], GOOD_TEXT)
        self.assertEqual(state.foreshadows["FS-BELL"].status, "planted")
        self.assertEqual(diff["chapter_sha256"], validated["chapter_sha256"])

    def test_state_gate_failure_leaves_no_orphan_chapter(self) -> None:
        proj = _project(foreshadows=_bell_foreshadow(), scenes=[_scene(1), _scene(2)], max_rewrite_retries=1)
        provider = _ScriptedProvider([_text(BAD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(proj, provider, tmp, checker=_PassChecker())
            with self.assertRaises(ProductionGateFailure):
                engine.run(target_chapters=[1])
            out = Path(tmp)
            self.assertEqual(len(provider.requests), 2)  # draft + 1 rewrite, state gate fed the loop
            self.assertFalse(list((out / "chapters").glob("chapter_*.txt")))
            self.assertFalse(list((out / "chapters").glob("chapter_*.meta.json")))
            self.assertFalse((out / "snapshots" / "chapter_0001_after.json").exists())
            scan = json.loads((out / "audit" / "ch0001_attempt1_scan.json").read_text(encoding="utf-8"))
            self.assertFalse(scan["passed"])
            self.assertEqual(scan["violations"][0]["rule_id"], "STATE_EVIDENCE_MISSING")

    def test_state_gate_failure_triggers_rewrite_then_succeeds(self) -> None:
        proj = _project(foreshadows=_bell_foreshadow(), scenes=[_scene(1), _scene(2)], max_rewrite_retries=2)
        provider = _ScriptedProvider([_text(BAD_TEXT), _text(GOOD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(proj, provider, tmp, checker=_PassChecker())
            manifest = engine.run(target_chapters=[1])
            self.assertEqual(len(provider.requests), 2)
            self.assertEqual(provider.requests[1].purpose, "rewrite")
            self.assertEqual(manifest["chapters"][0]["attempts_used"], 2)
            self.assertTrue((Path(tmp) / "chapters" / "chapter_0001.txt").is_file())
            self.assertEqual(engine.state.foreshadows["FS-BELL"].status, "planted")

    def test_preflight_dead_character_on_stage_blocks_before_llm(self) -> None:
        ghost = CharacterAnchor(
            id="ghost", name="老周", role="已故", personality="-", speech_style="-",
            initial_status="deceased",
        )
        proj = _project(scenes=[_scene(1, active=["hero", "ghost"])], extra_characters={"ghost": ghost})
        provider = _ScriptedProvider([_text(GOOD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(proj, provider, tmp, checker=_PassChecker())
            with self.assertRaises(CharacterStateViolationError):
                engine.run(target_chapters=[1])
            self.assertEqual(len(provider.requests), 0)
            self.assertFalse(list((Path(tmp) / "chapters").glob("chapter_*.txt")))

    def test_preflight_empty_foreshadow_keywords_blocks_before_llm(self) -> None:
        proj = _project(foreshadows=_bell_foreshadow(plant_keywords=[]), scenes=[_scene(1), _scene(2)])
        provider = _ScriptedProvider([_text(GOOD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(proj, provider, tmp, checker=_PassChecker())
            with self.assertRaises(ForeshadowConfigurationError):
                engine.run(target_chapters=[1])
            self.assertEqual(len(provider.requests), 0)

    def test_loader_rejects_empty_foreshadow_keywords(self) -> None:
        proj = _project(foreshadows=_bell_foreshadow(plant_keywords=["  "]), scenes=[_scene(1), _scene(2)])
        with self.assertRaises(ProjectValidationError):
            validate_project(proj)
        # Blank keyword entries are normalized away at parse time (checker & state agree).
        fs = ForeshadowItem.from_dict(
            {
                "id": "X", "title": "t", "plant_chapter": 1, "due_chapter": 2,
                "plant_instruction": "p", "payoff_instruction": "q",
                "plant_keywords": ["", " 铜铃 "], "payoff_keywords": [None, "刻字"],
            }
        )
        self.assertEqual(fs.plant_keywords, ["铜铃"])
        self.assertEqual(fs.payoff_keywords, ["刻字"])


class TestAuditE2PaidResults(unittest.TestCase):
    def test_writer_max_tokens_scaled_and_truncation_retried_once(self) -> None:
        proj = _project()
        trunc = LLMStreamTruncatedError("cut", usage={"completion_tokens": 10}, partial_content="风")
        provider = _ScriptedProvider([_raise(trunc), _text(GOOD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(proj, provider, tmp, checker=_PassChecker())
            engine.run(target_chapters=[1])
            self.assertEqual(len(provider.requests), 2)
            expected = max(6000, int(proj.scenes[0].max_chars * 2.5) + 2000)
            self.assertEqual(provider.requests[0].max_tokens, expected)
            self.assertGreater(provider.requests[1].max_tokens, expected)
            self.assertTrue((Path(tmp) / "chapters" / "chapter_0001.txt").is_file())
            self.assertTrue(any("WRITER_TRUNCATED" in i.incident_id for i in engine.incidents))

    def test_critic_llm_failure_degrades_and_keeps_chapter(self) -> None:
        proj = _project()
        provider = _ScriptedProvider([_text(GOOD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj, provider, tmp, checker=_PassChecker(),
                critic_polisher=_StubCriticPolisher(eval_exc=LLMRequestError("critic 502", status_code=502)),
            )
            manifest = engine.run(target_chapters=[1])
            self.assertIsNone(manifest["chapters"][0]["critic_evaluation"])
            self.assertEqual(manifest["chapters"][0]["author_note"], "下一章见分晓！")
            self.assertTrue((Path(tmp) / "chapters" / "chapter_0001.txt").is_file())
            self.assertTrue(any("CRITIC_FAILED" in i.incident_id for i in engine.incidents))

    def test_author_note_failure_degrades_to_empty(self) -> None:
        proj = _project()
        provider = _ScriptedProvider([_text(GOOD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj, provider, tmp, checker=_PassChecker(),
                critic_polisher=_StubCriticPolisher(note_exc=AuthorNoteViolationError("导流词")),
            )
            manifest = engine.run(target_chapters=[1])
            self.assertEqual(manifest["chapters"][0]["author_note"], "")
            meta = json.loads(
                (Path(tmp) / "chapters" / "chapter_0001.meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(meta["author_note"], "")
            self.assertTrue(any("AUTHOR_NOTE_FAILED" in i.incident_id for i in engine.incidents))
            incidents = (Path(tmp) / "audit" / "incidents.jsonl").read_text(encoding="utf-8")
            self.assertIn("AUTHOR_NOTE_FAILED", incidents)

    def test_critic_out_of_range_weak_index_filtered(self) -> None:
        draft = "陈牧推门而入。\n\n他把合同放在桌上。\n\n他拿起印章盖了下去。"
        stub = _ScriptedProvider(
            [
                _text(
                    json.dumps(
                        {
                            "first_screen_hook": 80,
                            "cool_point_density": 75,
                            "comment_interactivity": 70,
                            "pacing_drag": 68,
                            "weak_paragraphs": [1, 99, -3],
                            "critique": "ok",
                        }
                    )
                )
            ]
        )
        cp = TomatoCriticPolisher(critic_provider=stub, checker=AITicChecker(contract_track="complete_150k"))
        ev = cp.evaluate_chapter(draft, chapter=1)
        self.assertEqual(ev.llm_weak_paragraphs, [1])
        self.assertNotIn(99, ev.weak_paragraphs)
        self.assertNotIn(-3, ev.weak_paragraphs)

    def test_engine_keeps_already_gated_draft_when_optional_surgical_polish_breaks_gate(self) -> None:
        class _BrokenPolishStub(_StubCriticPolisher):
            def evaluate_chapter(self, text: str, **_: object) -> CriticEvaluation:
                return CriticEvaluation(
                    first_screen_hook=72,
                    cool_point_density=60,
                    comment_interactivity=68,
                    pacing_drag=55,
                    overall_score=63.0,
                    weak_paragraphs=[0],
                )

            def surgical_polish(self, *a: object, **k: object) -> None:
                raise SurgicalPolishGateError(
                    "Post-polish missing FS-02",
                    polished_text="被精修弄丢伏笔的残次品",
                )

        proj = _project(foreshadows=_bell_foreshadow(), scenes=[_scene(1)], max_rewrite_retries=3)
        provider = _ScriptedProvider([_text(GOOD_TEXT)])
        with tempfile.TemporaryDirectory() as tmp:
            engine = NovelGenerationEngine(
                proj,
                provider,
                tmp,
                checker=_PassChecker(),
                critic_polisher=_BrokenPolishStub(),
            )
            manifest = engine.run(target_chapters=[1])
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(manifest["chapters"][0]["attempts_used"], 1)
            written = (Path(tmp) / "chapters" / "chapter_0001.txt").read_text(encoding="utf-8")
            self.assertEqual(written.strip(), GOOD_TEXT)
            self.assertTrue((Path(tmp) / "audit" / "ch0001_attempt1_polish_rejected.txt").is_file())
            self.assertTrue(any("POLISH_GATE_REJECTED" in i.incident_id for i in engine.incidents))


class TestAuditM12ProviderEnv(unittest.TestCase):
    def test_provider_specific_api_key_env(self) -> None:
        env = {
            "XIAOSHUO_API_KEY": "",
            "OPENAI_API_KEY": "sk-openai-key-111",
            "ANTHROPIC_API_KEY": "sk-ant-key-222",
            "XIAOSHUO_BASE_URL": "",
            "OPENAI_BASE_URL": "https://openai.example/v1",
            "ANTHROPIC_BASE_URL": "https://anthropic.example",
            "XIAOSHUO_MODEL": "",
            "OPENAI_MODEL": "gpt-x",
            "ANTHROPIC_MODEL": "claude-x",
        }
        with mock.patch.dict(os.environ, env):
            ns = argparse.Namespace(provider="anthropic", api_key=None, base_url=None, model=None)
            ant = _build_text_provider(ns)
            self.assertEqual(ant.api_key, "sk-ant-key-222")
            self.assertEqual(ant.model, "claude-x")
            ns = argparse.Namespace(provider="openai", api_key=None, base_url=None, model=None)
            oai = _build_text_provider(ns)
            self.assertEqual(oai.api_key, "sk-openai-key-111")
            self.assertEqual(oai.model, "gpt-x")


if __name__ == "__main__":
    unittest.main()
