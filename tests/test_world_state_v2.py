from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.memory import MemoryPyramid
from xiaoshuo_bot.models import (
    AssetMutation,
    AssetState,
    CharacterAnchor,
    CharacterMutation,
    ExpectationDirective,
    ForeshadowItem,
    LedgerMutation,
    NovelProject,
    ProjectMeta,
    SceneOutline,
    StyleProfile,
    VolumeCapLock,
    format_chapter_filename,
)
from xiaoshuo_bot.state import (
    CharacterStateViolationError,
    ForeshadowEvidenceMissingError,
    LedgerUnderflowError,
    LockedChapterTamperedError,
    VolumeCapViolationError,
    WorldStateManager,
)


def _make_scene(
    chapter: int,
    *,
    pov: str = "hero",
    active_chars: list[str] | None = None,
    active_assets: list[str] | None = None,
    location_id: str = "old_street",
    calendar_days_elapsed: int = 1,
    volume: int = 1,
    locked: bool = False,
    beat_type: str = "",
    location_time: str = "傍晚，老街",
    internal_title: str = "",
    ledger_mutations: list[LedgerMutation] | None = None,
    character_mutations: list[CharacterMutation] | None = None,
    expectation_directives: list[ExpectationDirective] | None = None,
    post_scene_asset_mutations: list[AssetMutation] | None = None,
) -> SceneOutline:
    return SceneOutline(
        chapter=chapter,
        scene_id=f"ch{chapter:04d}_scene",
        internal_title=internal_title or f"第{chapter}节",
        pov_character=pov,
        active_characters=active_chars if active_chars is not None else [pov],
        active_assets=active_assets if active_assets is not None else [],
        style_profile_ids=["default_style"],
        location_time=location_time,
        events=[f"第{chapter}章核心事件"],
        stop_condition="止于悬念",
        volume=volume,
        locked=locked,
        beat_type=beat_type,
        location_id=location_id,
        calendar_days_elapsed=calendar_days_elapsed,
        ledger_mutations=ledger_mutations or [],
        character_mutations=character_mutations or [],
        expectation_directives=expectation_directives or [],
        post_scene_asset_mutations=post_scene_asset_mutations or [],
    )


def _make_project(
    *,
    initial_balances: dict[str, float] | None = None,
    volume_cap_locks: dict[str, VolumeCapLock] | None = None,
    foreshadows: dict[str, ForeshadowItem] | None = None,
    extra_characters: dict[str, CharacterAnchor] | None = None,
    scenes: list[SceneOutline] | None = None,
) -> NovelProject:
    characters = {
        "hero": CharacterAnchor(
            id="hero",
            name="陈屿",
            role="主角",
            personality="果断冷静",
            speech_style="简短有力",
        ),
        "villain": CharacterAnchor(
            id="villain",
            name="赵坤",
            role="反派",
            personality="阴狠",
            speech_style="挑衅",
        ),
        "ex_wife": CharacterAnchor(
            id="ex_wife",
            name="苏晴",
            role="前妻",
            personality="现实",
            speech_style="尖锐",
            relations={"hero": "married"},
        ),
        "disciple": CharacterAnchor(
            id="disciple",
            name="韩青",
            role="徒弟",
            personality="野心大",
            speech_style="恭敬中带刺",
            faction="青云宗",
            relations={"hero": "master"},
        ),
    }
    if extra_characters:
        characters.update(extra_characters)

    assets = {
        "token": AssetState(
            id="token",
            name="玄铁令牌",
            location="陈屿内袋",
            holder="陈屿",
            condition="完好",
            physical_details="刻有旧徽记",
        )
    }
    styles = {
        "default_style": StyleProfile(
            id="default_style",
            name="测试文风",
            granularity_notes="短句推进",
            sample_text="风吹过门缝。",
        )
    }
    meta = ProjectMeta(
        project_id="ws_v2_test",
        title="五维测试书",
        genre="都市脑洞",
        synopsis="测试用工程",
        default_pov_character="hero",
        contract_track="complete_150k",
    )
    return NovelProject(
        meta=meta,
        characters=characters,
        assets=assets,
        foreshadows=foreshadows or {},
        style_profiles=styles,
        scenes=scenes or [_make_scene(1)],
        initial_balances=initial_balances or {},
        volume_cap_locks=volume_cap_locks or {},
    )


class TestWorldStateV2(unittest.TestCase):
    def test_numeric_ledger_underflow_raises_error_atomically(self) -> None:
        proj = _make_project(initial_balances={"cash": 3_000_000.0})
        state = WorldStateManager(proj)
        self.assertEqual(state.get_balance("cash"), 3_000_000.0)

        scene = _make_scene(
            1,
            ledger_mutations=[
                LedgerMutation(ledger_key="cash", delta=-5_000_000.0, reason="强行竞拍")
            ],
        )
        with self.assertRaises(LedgerUnderflowError):
            state.advance_after_chapter(scene, "陈屿站在拍卖台前，尝试拍下整座大楼。")

        # State must remain untouched after rejected transition
        self.assertEqual(state.get_balance("cash"), 3_000_000.0)
        self.assertEqual(state.completed_chapters, [])

    def test_volume_cap_lock_enforced_and_breakthrough_unlocks(self) -> None:
        locks = {
            "1:power_tier": VolumeCapLock(
                ledger_key="power_tier", volume=1, max_value=3.0, unlocked=False, unit="阶"
            )
        }
        proj = _make_project(
            initial_balances={"power_tier": 2.0},
            volume_cap_locks=locks,
        )
        state = WorldStateManager(proj)

        blocked_scene = _make_scene(
            1,
            volume=1,
            ledger_mutations=[
                LedgerMutation(ledger_key="power_tier", delta=2.0, reason="强行跨阶", breakthrough=False)
            ],
        )
        with self.assertRaises(VolumeCapViolationError):
            state.advance_after_chapter(blocked_scene, "陈屿周身灵气翻涌，企图跨入四阶。")
        self.assertEqual(state.get_balance("power_tier"), 2.0)

        breakthrough_scene = _make_scene(
            1,
            volume=1,
            ledger_mutations=[
                LedgerMutation(ledger_key="power_tier", delta=2.0, reason="破境飞升", breakthrough=True)
            ],
        )
        state.advance_after_chapter(breakthrough_scene, "雷光劈落之际，陈屿彻底冲破第一卷桎梏。")
        self.assertEqual(state.get_balance("power_tier"), 4.0)
        cap_lock = state.get_volume_cap_lock("power_tier", volume=1)
        self.assertIsNotNone(cap_lock)
        assert cap_lock is not None
        self.assertTrue(cap_lock.unlocked)

    def test_deceased_irreversible_and_flashback_marker(self) -> None:
        proj = _make_project()
        state = WorldStateManager(proj)

        # Chapter 1: villain is alive on stage and dies at the end of Chapter 1
        ch1 = _make_scene(
            1,
            active_chars=["hero", "villain"],
            character_mutations=[CharacterMutation(character_id="villain", status="deceased")],
        )
        state.advance_after_chapter(ch1, "赵坤倒在雨水里，彻底没了呼吸。")
        self.assertEqual(state.character_states["villain"].status, "deceased")
        self.assertEqual(state.character_states["villain"].deceased_chapter, 1)
        self.assertIn("赵坤", state.to_snapshot().get_deceased_names())

        # Chapter 2: sending deceased character on stage without flashback marker MUST fail
        ch2_bad = _make_scene(2, active_chars=["hero", "villain"])
        with self.assertRaises(CharacterStateViolationError):
            state.advance_after_chapter(ch2_bad, "赵坤又冷笑着推门走进来。")

        # Attempting to resurrect a deceased character via mutation MUST fail
        ch2_revive = _make_scene(
            2,
            active_chars=["hero"],
            character_mutations=[CharacterMutation(character_id="villain", status="alive")],
        )
        with self.assertRaises(CharacterStateViolationError):
            state.advance_after_chapter(ch2_revive, "赵坤忽然复活。")

        # Flashback marker allows deceased character appearance without violating lifecycle
        ch2_flashback = _make_scene(
            2,
            active_chars=["hero", "villain"],
            beat_type="flashback",
            location_time="【回忆】三年前，旧码头",
        )
        state.advance_after_chapter(ch2_flashback, "陈屿想起三年前赵坤站在码头上的模样。")
        self.assertEqual(state.character_states["villain"].status, "deceased")

    def test_relation_changes_push_obsolete_titles_to_forbidden_titles(self) -> None:
        proj = _make_project()
        state = WorldStateManager(proj)

        ch1 = _make_scene(
            1,
            active_chars=["hero", "ex_wife", "disciple"],
            character_mutations=[
                CharacterMutation(
                    character_id="ex_wife",
                    relation_target_id="hero",
                    relation_label="divorced",
                ),
                CharacterMutation(
                    character_id="disciple",
                    faction="散修",
                    relation_target_id="hero",
                    relation_label="逐出师门",
                ),
            ],
        )
        state.advance_after_chapter(ch1, "民政局门口签完离婚协议，韩青也被当众收回了宗门腰牌。")

        snap = state.to_snapshot()
        self.assertIn("老公", snap.forbidden_titles["ex_wife"])
        self.assertIn("丈夫", snap.forbidden_titles["ex_wife"])
        self.assertIn("老公", snap.get_all_forbidden_titles())
        self.assertIn("师父", snap.forbidden_titles["disciple"])
        self.assertIn("青云宗弟子", snap.forbidden_titles["disciple"])
        self.assertTrue(
            any(
                e["source"] == "ex_wife" and e["target"] == "hero" and e["relation"] == "divorced"
                for e in snap.relationship_edges
            )
        )

    def test_foreshadow_overdue_same_chapter_and_missing_evidence_error(self) -> None:
        fs_overdue = ForeshadowItem(
            id="FS-OVERDUE",
            title="旧船票",
            plant_chapter=1,
            due_chapter=2,
            plant_instruction="出现旧船票",
            payoff_instruction="揭示船票日期",
            plant_keywords=["旧船票"],
            payoff_keywords=["1998年"],
        )
        fs_same = ForeshadowItem(
            id="FS-SAME",
            title="同章埋收铜铃",
            plant_chapter=1,
            due_chapter=1,
            plant_instruction="门框挂铜铃",
            payoff_instruction="铜铃内壁藏字",
            plant_keywords=["铜铃"],
            payoff_keywords=["内壁刻字"],
        )
        proj = _make_project(
            foreshadows={"FS-OVERDUE": fs_overdue, "FS-SAME": fs_same},
            scenes=[_make_scene(1), _make_scene(2), _make_scene(3)],
        )
        state = WorldStateManager(proj)

        # Missing evidence keywords MUST raise ForeshadowEvidenceMissingError (no fake receipt!)
        with self.assertRaises(ForeshadowEvidenceMissingError):
            state.advance_after_chapter(_make_scene(1), "今天风平浪静，陈屿吃了一碗面。")
        self.assertEqual(state.foreshadows["FS-OVERDUE"].planted_evidence, "")
        self.assertEqual(state.foreshadows["FS-SAME"].status, "pending")

        # Valid Chapter 1 text plants FS-OVERDUE and both plants+resolves FS-SAME in same chapter
        ch1_text = "门槛上挂着一只旧铜铃。陈屿翻过铜铃，看见内壁刻字写着三号仓，兜里还揣着半张旧船票。"
        diff1 = state.advance_after_chapter(_make_scene(1), ch1_text)
        self.assertEqual(state.foreshadows["FS-OVERDUE"].status, "planted")
        self.assertEqual(state.foreshadows["FS-SAME"].status, "resolved")
        self.assertNotIn("已埋设", state.foreshadows["FS-OVERDUE"].planted_evidence)
        self.assertIn("铜铃", state.foreshadows["FS-SAME"].planted_evidence)
        self.assertIn("内壁刻字", state.foreshadows["FS-SAME"].payoff_evidence)
        self.assertEqual(len(diff1["foreshadow_updates"]), 3)

        # Overdue foreshadow check: FS-OVERDUE (due_chapter=2) stays exposed at Ch.2 AND Ch.3+
        self.assertEqual([f.id for f in state.get_due_foreshadows(2)], ["FS-OVERDUE"])
        self.assertEqual([f.id for f in state.get_due_foreshadows(3)], ["FS-OVERDUE"])
        self.assertEqual([f.id for f in state.get_due_foreshadows(5)], ["FS-OVERDUE"])

        # Chapter 3 resolves overdue FS-OVERDUE
        state.advance_after_chapter(_make_scene(3), "灯光下，那张船票背面印着1998年冬的钢印。")
        self.assertEqual(state.foreshadows["FS-OVERDUE"].status, "resolved")
        self.assertEqual(state.get_due_foreshadows(4), [])

    def test_expectation_directives_overdue_and_fulfilled(self) -> None:
        proj = _make_project()
        state = WorldStateManager(proj)

        ch1 = _make_scene(
            1,
            expectation_directives=[
                ExpectationDirective(
                    id="EXP-01",
                    title="三章内打脸反派",
                    due_chapter=3,
                    description="当众亮出银行黑卡",
                    action="open",
                    evidence_keywords=["黑卡"],
                )
            ],
        )
        # Missing open keyword raises ForeshadowEvidenceMissingError
        with self.assertRaises(ForeshadowEvidenceMissingError):
            state.advance_after_chapter(ch1, "陈屿转身走了。")

        state.advance_after_chapter(ch1, "陈屿指间夹着一张黑卡。")
        self.assertEqual(state.active_expectations["EXP-01"].status, "active")

        # Advance chapter 2, 3, 4 without fulfilling -> stays exposed and transitions to overdue
        state.advance_after_chapter(_make_scene(2), "第二章风波渐起。")
        state.advance_after_chapter(_make_scene(3), "第三章尚未摊牌。")
        self.assertEqual(state.active_expectations["EXP-01"].status, "overdue")
        self.assertEqual([e.id for e in state.get_due_expectations(4)], ["EXP-01"])

        # Chapter 4 fulfills EXP-01
        ch4 = _make_scene(
            4,
            expectation_directives=[
                ExpectationDirective(
                    id="EXP-01",
                    title="三章内打脸反派",
                    due_chapter=3,
                    action="fulfill",
                    evidence_keywords=["三千万"],
                )
            ],
        )
        state.advance_after_chapter(ch4, "刷卡机吐出回执，账面整整三千万。")
        self.assertEqual(state.active_expectations["EXP-01"].status, "fulfilled")
        self.assertEqual(state.get_due_expectations(5), [])

    def test_virtual_calendar_injury_cooldown_and_recent_locations(self) -> None:
        proj = _make_project()
        state = WorldStateManager(proj)

        ch1 = _make_scene(
            1,
            location_id="dock_03",
            calendar_days_elapsed=10,
            character_mutations=[
                CharacterMutation(
                    character_id="hero",
                    injury="左肩刀伤",
                    injury_recovery_chapters=2,
                )
            ],
        )
        state.advance_after_chapter(ch1, "三号码头夜战，陈屿左肩挨了一刀。")
        self.assertEqual(state.virtual_day, 11)
        self.assertEqual(state.virtual_season, "秋")
        self.assertEqual(state.unhealed_injuries["hero"]["左肩刀伤"], 2)
        self.assertEqual(state.recent_locations, ["dock_03"])

        # Chapter 2: stays at dock_03, 85 days elapse -> crosses day 91 -> season changes 秋 -> 冬; injury -> 1
        ch2 = _make_scene(2, location_id="dock_03", calendar_days_elapsed=85)
        state.advance_after_chapter(ch2, "陈屿养伤两个多月，入冬时分仍在码头盯梢。")
        self.assertEqual(state.virtual_day, 96)
        self.assertEqual(state.virtual_season, "冬")
        self.assertEqual(state.unhealed_injuries["hero"]["左肩刀伤"], 1)
        self.assertEqual(state.get_consecutive_location_count(), 2)

        # Chapter 3: moves to warehouse, injury heals to 0 and is cleared
        ch3 = _make_scene(3, location_id="warehouse", calendar_days_elapsed=1)
        diff3 = state.advance_after_chapter(ch3, "伤势彻底愈合，陈屿潜入仓库。")
        self.assertNotIn("hero", state.unhealed_injuries)
        self.assertEqual(state.character_states["hero"].injuries, {})
        self.assertEqual(diff3["healed_injuries"], [{"character_id": "hero", "injury": "左肩刀伤"}])
        self.assertEqual(state.recent_locations, ["dock_03", "dock_03", "warehouse"])
        self.assertEqual(state.get_consecutive_location_count(), 1)

    def test_snapshot_roundtrip_and_locked_chapter_tamper_detection(self) -> None:
        proj = _make_project(initial_balances={"cash": 5000.0})
        state = WorldStateManager(proj)
        ch1_text = "这是第一卷第一章被锁定的正文内容。"
        ch1 = _make_scene(
            1,
            locked=True,
            location_id="clock_shop",
            ledger_mutations=[LedgerMutation(ledger_key="cash", delta=-1200.0)],
        )
        state.advance_after_chapter(ch1, ch1_text)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapters_dir = root / "chapters"
            chapters_dir.mkdir(parents=True, exist_ok=True)
            ch1_file = chapters_dir / format_chapter_filename(1)
            ch1_file.write_text(ch1_text, encoding="utf-8")

            snap_path = root / "snapshots" / "chapter_0001_after.json"
            state.save_snapshot(snap_path)

            # Valid roundtrip load
            restored = WorldStateManager(proj)
            restored.load_snapshot(snap_path, chapters_dir=chapters_dir)
            self.assertEqual(restored.to_snapshot(), state.to_snapshot())
            self.assertEqual(restored.get_balance("cash"), 3800.0)
            self.assertEqual(
                restored.chapter_hashes[1],
                hashlib.sha256(ch1_text.encode("utf-8")).hexdigest(),
            )

            # Tamper locked chapter file on disk -> LockedChapterTamperedError
            ch1_file.write_text("被人恶意篡改过的锁章正文。", encoding="utf-8")
            tampered_mgr = WorldStateManager(proj)
            with self.assertRaises(LockedChapterTamperedError):
                tampered_mgr.load_snapshot(snap_path, chapters_dir=chapters_dir)

            # Tamper snapshot JSON hash directly against expected hash -> LockedChapterTamperedError
            ch1_file.write_text(ch1_text, encoding="utf-8")
            snap_data = json.loads(snap_path.read_text(encoding="utf-8"))
            snap_data["chapter_hashes"]["1"] = "0" * 64
            snap_path.write_text(json.dumps(snap_data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(LockedChapterTamperedError):
                tampered_mgr.load_snapshot(snap_path, chapters_dir=chapters_dir)

    def test_memory_pyramid_120_and_500_chapters_budget_and_entity_recall(self) -> None:
        pyramid = MemoryPyramid(max_context_chars=5200)
        pyramid.register_entity_name("hero", "陈屿", "character")
        pyramid.register_entity_name("ch05_mentor", "沈老", "character")
        pyramid.register_entity_name("jade_seal", "残缺玉玺", "asset")

        # Advance 120 chapters; ch05_mentor and jade_seal appear ONLY at Chapter 5
        for ch in range(1, 121):
            chars = ["hero"]
            assets: list[str] = []
            facts: dict[str, str] = {}
            summary = f"第{ch}章陈屿在江湖推进势力版图并处理商路冲突。"
            if ch == 5:
                chars.append("ch05_mentor")
                assets.append("jade_seal")
                facts["ch05_mentor"] = "第5章在古庙交给陈屿半枚残缺玉玺，并嘱咐十年后去北境认亲"
                facts["jade_seal"] = "第5章由沈老交付陈屿贴身保管，背面刻有北境王印"
                summary = "第5章古庙雨夜，沈老托付残缺玉玺并揭示北境密约。"
            body = f"第{ch}章正文开始。" + ("细雨落在青石巷口，灯笼影晃动不止。" * 12) + f"第{ch}章结尾收束于夜风之中。"
            pyramid.record_chapter(
                ch,
                body,
                virtual_day=ch + 1,
                title=f"章名{ch}",
                summary=summary,
                character_ids=chars,
                asset_ids=assets,
                location_id=f"loc_{ch % 7}",
                entity_facts=facts,
            )

        self.assertEqual(len(pyramid.l1_recent), 5)
        self.assertEqual([e.chapter for e in pyramid.l1_recent], [116, 117, 118, 119, 120])
        self.assertEqual(len(pyramid.l2_stages), 12)
        self.assertEqual(len(pyramid.l3_volumes), 1)
        self.assertLessEqual(len(pyramid.l0_tail), 260)

        ctx_120 = pyramid.compile_context(
            active_character_ids=["hero", "ch05_mentor"],
            active_asset_ids=["jade_seal"],
            current_virtual_day=122,
        )
        self.assertLessEqual(len(ctx_120), 5200)
        self.assertIn("沈老(ch05_mentor)", ctx_120)
        self.assertIn("第5章在古庙交给陈屿半枚残缺玉玺", ctx_120)
        self.assertIn("残缺玉玺(jade_seal)", ctx_120)
        self.assertIn("第120章", ctx_120)

        # Continue advancing up to Chapter 500 -> strict <= 5200 chars preserved
        for ch in range(121, 501):
            body = f"第{ch}章正文推进。" + ("山风掠过铁甲，远处传来号角余音。" * 12) + f"第{ch}章尾声停在城门前。"
            pyramid.record_chapter(
                ch,
                body,
                virtual_day=ch + 1,
                title=f"大章{ch}",
                summary=f"第{ch}章万界征伐持续推进，陈屿收编第{ch}处关隘。",
                character_ids=["hero"],
                location_id=f"loc_{ch % 11}",
            )

        self.assertEqual(len(pyramid.l3_volumes), 5)
        self.assertEqual(len(pyramid.l2_stages), 50)
        ctx_500 = pyramid.compile_context(
            active_character_ids=["hero", "ch05_mentor"],
            active_asset_ids=["jade_seal"],
            current_virtual_day=502,
        )
        self.assertLessEqual(len(ctx_500), 5200)
        self.assertIn("沈老(ch05_mentor)", ctx_500)
        self.assertIn("第5章", ctx_500)
        self.assertIn("第500章", ctx_500)

        # Persistence roundtrip check
        with tempfile.TemporaryDirectory() as tmp:
            mem_path = Path(tmp) / "memory_pyramid.json"
            pyramid.save(mem_path)
            loaded = MemoryPyramid.load(mem_path)
            self.assertEqual(
                loaded.compile_context(
                    active_character_ids=["hero", "ch05_mentor"],
                    active_asset_ids=["jade_seal"],
                    current_virtual_day=502,
                ),
                ctx_500,
            )


if __name__ == "__main__":
    unittest.main()
