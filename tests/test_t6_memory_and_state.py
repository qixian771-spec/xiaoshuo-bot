from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.engine import NovelGenerationEngine
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.memory import (
    ChapterMemoryEntry,
    EntityMemoryRecord,
    MemoryMigrationError,
    MemoryPyramid,
    StageChronicle,
    VolumeChronicle,
    migrate_legacy_memory,
)
from xiaoshuo_bot.models import (
    CharacterAnchor,
    CharacterMutation,
    CharacterWorldState,
    NovelProject,
    ProjectMeta,
    ProjectValidationError,
    SceneOutline,
    StyleProfile,
    WorldStateSnapshot,
)
from xiaoshuo_bot.state import (
    CharacterStateViolationError,
    WorldStateError,
    WorldStateManager,
)


class _NoCallProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError(f"LLM should not be called in offline state/memory test: {request.request_id}")


def _build_state_project() -> NovelProject:
    characters = {
        "mc": CharacterAnchor(
            id="mc",
            name="林墨",
            role="外门杂役",
            personality="沉稳隐忍",
            speech_style="简练克制",
        ),
        "elder": CharacterAnchor(
            id="elder",
            name="沈长老",
            role="执法堂长老",
            personality="威严公正",
            speech_style="言简意赅",
        ),
    }
    scenes = [
        SceneOutline(
            chapter=1,
            scene_id="ch0001_s",
            internal_title="杂役房风波",
            pov_character="mc",
            active_characters=["mc", "elder"],
            active_assets=[],
            style_profile_ids=["default"],
            location_time="清晨，外门杂役房",
            events=["林墨初入杂役房"],
            stop_condition="止于夜色",
            calendar_days_elapsed=3,
        ),
        SceneOutline(
            chapter=2,
            scene_id="ch0002_s",
            internal_title="内门大比",
            pov_character="mc",
            active_characters=["mc", "elder"],
            active_assets=[],
            style_profile_ids=["default"],
            location_time="五日后，演武场",
            events=["林墨晋升内门弟子"],
            stop_condition="止于拜师礼毕",
            calendar_days_elapsed=5,
        ),
    ]
    return NovelProject(
        meta=ProjectMeta(
            project_id="t6_test",
            title="角色记忆测试",
            genre="仙侠",
            synopsis="测试角色身份演化与记忆时间锚点",
            default_pov_character="mc",
        ),
        characters=characters,
        assets={},
        foreshadows={},
        style_profiles={
            "default": StyleProfile(
                id="default",
                name="默认",
                granularity_notes="客观克制",
                sample_text="夜风掠过青石阶。",
            )
        },
        scenes=scenes,
    )


class TestT6MemoryAndState(unittest.TestCase):
    def test_1200_chapters_no_permanent_ch1_fact_and_compile_context_time_anchors(self) -> None:
        pyramid = MemoryPyramid(max_context_chars=5200)
        pyramid.register_entity_name("mc", "林墨", "character")

        for ch in range(1, 1201):
            if ch == 1:
                fact = "第1章刚下山三个月，身无长物在青石镇落脚"
                summary = "林墨初入青石镇落脚。"
            elif ch == 1199:
                fact = "第1199章晋升化神境坐镇北境王庭"
                summary = "北境王庭议事，林墨统御诸宗。"
            elif ch == 1200:
                fact = "第1200章率领大军出征星海裂隙"
                summary = "星海裂隙开启，林墨亲率飞舟出征。"
            else:
                fact = f"第{ch}章历练修行突破第{ch}重关隘"
                summary = f"第{ch}章林墨在灵脉秘境稳步推进。"

            body = (
                f"第{ch}章正文。" + ("风雪掠过山巅阵纹，灵光隐现。" * 6) + f"第{ch}章夜幕合拢。"
            )
            # Virtual day advances by 1 per chapter starting at day 2 (1 + ch)
            pyramid.record_chapter(
                ch,
                body,
                virtual_day=ch + 1,
                title=f"章{ch}",
                summary=summary,
                character_ids=["mc"],
                entity_facts={"mc": fact},
            )

            if ch == 1199:
                recalled_1199 = pyramid.recall_entity("mc")
                self.assertIn("首现Ch.1", recalled_1199)
                self.assertIn("最近Ch.1199", recalled_1199)
                self.assertNotIn("下山三个月", recalled_1199)
                self.assertNotIn("第1章:", recalled_1199)
                self.assertIn("第1197章:", recalled_1199)
                self.assertIn("第1198章:", recalled_1199)
                self.assertIn("第1199章:", recalled_1199)

        rec = pyramid.entity_index["mc"]
        self.assertEqual(rec.first_chapter, 1)
        self.assertEqual(rec.last_chapter, 1200)
        # No permanent appearances[0] retention; only recent 8 chapters kept
        self.assertEqual([a["chapter"] for a in rec.appearances], list(range(1193, 1201)))

        recalled_1200 = pyramid.recall_entity("mc")
        self.assertIn("首现Ch.1", recalled_1200)
        self.assertIn("最近Ch.1200", recalled_1200)
        self.assertNotIn("下山三个月", recalled_1200)
        self.assertIn("第1198章:", recalled_1200)
        self.assertIn("第1199章:", recalled_1200)
        self.assertIn("第1200章:", recalled_1200)

        # Compile context with current_virtual_day=2200
        ctx = pyramid.compile_context(
            active_character_ids=["mc"],
            current_virtual_day=2200,
        )
        self.assertLessEqual(len(ctx), 5200)
        self.assertTrue(ctx.startswith("### [时间锚点] 当前第2200天"))
        # L1 recent 5 chapters (1196..1200, virtual_day 1197..1201)
        self.assertIn("第1200章（第1201天，距今999天）", ctx)
        self.assertIn("第1196章（第1197天，距今1003天）", ctx)
        # L2 recent 3 stages (1171-1180, 1181-1190, 1191-1200)
        self.assertIn("第1191-1200章阶段（第1192–1201天）", ctx)
        # L3 volume chronicles include day range
        self.assertIn("第1-100章大卷（第2–101天）", ctx)
        # Entity recall keeps numeric first appearance without Ch.1 old fact
        self.assertIn("首现Ch.1，最近Ch.1200", ctx)
        self.assertNotIn("下山三个月", ctx)

        # Transitional compatibility: current_virtual_day=None omits time anchor header & day tags
        ctx_compat = pyramid.compile_context(active_character_ids=["mc"])
        self.assertNotIn("### [时间锚点]", ctx_compat)
        self.assertNotIn("距今", ctx_compat)

    def test_record_chapter_and_compile_context_parameter_validation(self) -> None:
        pyr = MemoryPyramid()
        with self.assertRaises(TypeError):
            pyr.record_chapter(1, "正文")  # type: ignore[call-arg]
        for bad_day in (0, -5, True, "3"):
            with self.assertRaises(ValueError):
                pyr.record_chapter(1, "正文", virtual_day=bad_day)  # type: ignore[arg-type]
        for bad_day in (0, -1, True, "10"):
            with self.assertRaises(ValueError):
                pyr.compile_context(current_virtual_day=bad_day)  # type: ignore[arg-type]

    def test_legacy_memory_entry_rejects_missing_virtual_day_and_migrates_explicitly(self) -> None:
        # Missing virtual_day in ChapterMemoryEntry.from_dict raises MemoryMigrationError (subclass of ValueError)
        with self.assertRaises(MemoryMigrationError) as ctx_err:
            ChapterMemoryEntry.from_dict({"chapter": 3, "summary": "旧章摘要"})
        self.assertIsInstance(ctx_err.exception, ValueError)

        for bad_val in (None, 0, -2, True, "5", 2.5):
            with self.assertRaises(MemoryMigrationError):
                ChapterMemoryEntry.from_dict(
                    {"chapter": 3, "summary": "旧章摘要", "virtual_day": bad_val}
                )

        legacy_payload = {
            "max_context_chars": 5200,
            "last_chapter": 12,
            "l0_tail": "尾锚点正文。",
            "l1_recent": [
                {"chapter": 8, "summary": "Ch8摘要"},
                {"chapter": 9, "summary": "Ch9摘要"},
                {"chapter": 10, "summary": "Ch10摘要"},
                {"chapter": 11, "summary": "Ch11摘要"},
                {"chapter": 12, "summary": "Ch12摘要"},
            ],
            "l2_stages": [
                {"start_chapter": 1, "end_chapter": 10, "summary": "第一阶段", "entity_ids": ["mc"]},
            ],
            "_l2_staging": [
                {"chapter": 11, "summary": "Ch11摘要"},
                {"chapter": 12, "summary": "Ch12摘要"},
            ],
            "l3_volumes": [
                {"start_chapter": 1, "end_chapter": 10, "summary": "第一大卷", "entity_ids": ["mc"]},
            ],
            "_l3_staging": [],
            "entity_index": {},
        }

        # Direct MemoryPyramid.from_dict on legacy payload must raise
        with self.assertRaises(MemoryMigrationError):
            MemoryPyramid.from_dict(legacy_payload)

        # Incomplete chapter_day_map must raise explicitly
        incomplete_map = {ch: ch + 1 for ch in range(1, 12)}  # missing chapter 12
        with self.assertRaises(MemoryMigrationError):
            migrate_legacy_memory(legacy_payload, incomplete_map)

        # Complete chapter_day_map migrates cleanly and logs warning
        full_map = {ch: ch * 2 for ch in range(1, 13)}
        with self.assertLogs("xiaoshuo_bot.memory", level="WARNING") as cm:
            migrated = migrate_legacy_memory(legacy_payload, full_map)
        self.assertTrue(any("Migrated legacy MemoryPyramid" in msg for msg in cm.output))
        self.assertEqual([e.virtual_day for e in migrated.l1_recent], [16, 18, 20, 22, 24])
        self.assertEqual(migrated.l2_stages[0].start_day, 2)
        self.assertEqual(migrated.l2_stages[0].end_day, 20)
        self.assertEqual(migrated.l3_volumes[0].start_day, 2)
        self.assertEqual(migrated.l3_volumes[0].end_day, 20)

        rendered = migrated.compile_context(current_virtual_day=30)
        self.assertIn("### [时间锚点] 当前第30天", rendered)
        self.assertIn("第12章（第24天，距今6天）", rendered)
        self.assertIn("第1-10章阶段（第2–20天）", rendered)
        self.assertIn("第1-10章大卷（第2–20天）", rendered)

    def test_legacy_snapshot_migrates_current_role_with_warning_and_role_mutation_persists(self) -> None:
        proj = _build_state_project()
        mgr = WorldStateManager(proj)
        self.assertEqual(mgr.character_states["mc"].current_role, "外门杂役")
        self.assertEqual(mgr.character_states["elder"].current_role, "执法堂长老")

        # CharacterWorldState.from_dict without current_role yields None (no silent default)
        raw_cs = CharacterWorldState.from_dict({"character_id": "mc", "name": "林墨"})
        self.assertIsNone(raw_cs.current_role)

        # Build legacy snapshot JSON lacking current_role
        legacy_snap_dict = mgr.snapshot_dict()
        del legacy_snap_dict["character_states"]["mc"]["current_role"]
        del legacy_snap_dict["character_states"]["elder"]["current_role"]

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = Path(tmpdir) / "legacy_snapshot.json"
            snap_path.write_text(
                json.dumps(legacy_snap_dict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            restored = WorldStateManager(proj)
            with self.assertLogs("xiaoshuo_bot.state", level="WARNING") as logs:
                restored.load_snapshot(snap_path)
            self.assertEqual(restored.character_states["mc"].current_role, "外门杂役")
            self.assertEqual(restored.character_states["elder"].current_role, "执法堂长老")
            self.assertIn("legacy snapshot migrated current_role for mc", "\n".join(logs.output))
            self.assertIn("legacy snapshot migrated current_role for elder", "\n".join(logs.output))

        # Unknown character in legacy snapshot missing current_role must raise WorldStateError
        unknown_snap_dict = mgr.snapshot_dict()
        unknown_snap_dict["character_states"]["ghost"] = {
            "character_id": "ghost",
            "name": "无名客",
            "status": "alive",
        }
        with self.assertRaises(WorldStateError):
            mgr.apply_snapshot(WorldStateSnapshot.from_dict(unknown_snap_dict))

        # Blank role mutation rejected
        with self.assertRaises(ProjectValidationError):
            CharacterMutation(character_id="mc", role="   ")
        bad_mut = CharacterMutation(character_id="mc", role="内门弟子")
        bad_mut.role = ""
        with self.assertRaises(CharacterStateViolationError):
            mgr.apply_character_mutation(bad_mut, chapter=1)

        # Valid role mutation updates state, returns current_role, and roundtrips via snapshot
        mut_from_dict = CharacterMutation.from_dict({"character_id": "mc", "role": "内门首席弟子"})
        self.assertEqual(mut_from_dict.to_dict()["role"], "内门首席弟子")
        diff = mgr.apply_character_mutation(mut_from_dict, chapter=2)
        self.assertEqual(diff["current_role"], "内门首席弟子")
        self.assertEqual(mgr.character_states["mc"].current_role, "内门首席弟子")

        with tempfile.TemporaryDirectory() as tmpdir:
            snap2_path = Path(tmpdir) / "updated_snapshot.json"
            mgr.save_snapshot(snap2_path)
            mgr2 = WorldStateManager(proj)
            mgr2.load_snapshot(snap2_path)
            self.assertEqual(mgr2.character_states["mc"].current_role, "内门首席弟子")

    def test_engine_record_chapter_virtual_day_and_legacy_restore(self) -> None:
        source_proj = ROOT / "projects" / "baiwu_dukou"
        with tempfile.TemporaryDirectory() as tmpdir:
            copied_proj = Path(tmpdir) / "baiwu_dukou"
            shutil.copytree(source_proj, copied_proj)

            project = load_project(copied_proj)
            # Customize calendar_days_elapsed to verify non-1 day accumulation in engine
            project.scenes[0].calendar_days_elapsed = 3
            project.scenes[1].calendar_days_elapsed = 5

            out_dir = copied_proj / "output"
            engine = NovelGenerationEngine(
                project=project,
                provider=_NoCallProvider(),
                output_dir=out_dir,
            )
            manifest = engine.run(resume=True)
            self.assertTrue(manifest["all_gates_passed"])
            # state.virtual_day: 1 + 3 + 5 + 1 + 1 + 1 = 12
            self.assertEqual(engine.state.virtual_day, 12)
            self.assertEqual(
                [e.virtual_day for e in engine.memory.l1_recent],
                [4, 9, 10, 11, 12],
            )

            # _restore_predecessor_state without memory_*.json rebuilds virtual_day from day 1
            chapters_dir = out_dir / "chapters"
            snapshots_dir = out_dir / "snapshots"
            engine_rebuilt = NovelGenerationEngine(
                project=project,
                provider=_NoCallProvider(),
                output_dir=out_dir,
            )
            engine_rebuilt._restore_predecessor_state(4, chapters_dir, snapshots_dir)
            self.assertEqual(
                [e.virtual_day for e in engine_rebuilt.memory.l1_recent],
                [4, 9, 10, 11],
            )

            # _restore_predecessor_state with legacy memory_0004_after.json (missing virtual_day) migrates it
            legacy_mem_path = snapshots_dir / "memory_0004_after.json"
            legacy_dict = engine_rebuilt.memory.to_dict()
            for entry in legacy_dict["l1_recent"]:
                entry.pop("virtual_day", None)
            for entry in legacy_dict["_l2_staging"]:
                entry.pop("virtual_day", None)
            legacy_mem_path.write_text(
                json.dumps(legacy_dict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            engine_migrated = NovelGenerationEngine(
                project=project,
                provider=_NoCallProvider(),
                output_dir=out_dir,
            )
            with self.assertLogs("xiaoshuo_bot.memory", level="WARNING"):
                engine_migrated._restore_predecessor_state(4, chapters_dir, snapshots_dir)
            self.assertEqual(
                [e.virtual_day for e in engine_migrated.memory.l1_recent],
                [4, 9, 10, 11],
            )


if __name__ == "__main__":
    unittest.main()
