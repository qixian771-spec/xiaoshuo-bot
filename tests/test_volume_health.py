from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
import unittest

from xiaoshuo_bot.blueprint import BookBlueprint, load_blueprint
from xiaoshuo_bot.judge import (
    JudgeResponseError,
    PairwiseJudge,
    SameModelJudgeError,
)
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.models import (
    CharacterAnchor,
    ForeshadowItem,
    NovelProject,
    ProjectMeta,
    SceneOutline,
    VolumeCapLock,
)
from xiaoshuo_bot.volume_health import (
    LOCK_STATE_LOCKED,
    LOCK_STATE_PASSED,
    VOLUME_HEALTH_CHECK_IDS,
    VolumeHealthCheckItem,
    VolumeHealthLockError,
    VolumeHealthReport,
    assert_previous_volume_unlocked,
    load_volume_locks,
    run_volume_health_check,
    save_volume_health_result,
)


FIXTURE_BLUEPRINT = Path(__file__).resolve().parent / "fixtures" / "blueprint_valid.yaml"


class _FakeProvider(LLMProvider):
    def __init__(self, responder: Callable[[LLMRequest, int], str]) -> None:
        self._responder = responder
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        content = self._responder(request, len(self.requests))
        return LLMResponse(
            request_id=request.request_id,
            content=content,
            model=str(request.metadata.get("model") or "wb/mimo-v2.6-pro"),
            provider="fake",
        )


def _make_judge_payload(
    *,
    fail_id: Optional[str] = None,
    fail_chapters: Optional[List[int]] = None,
    fail_hint: str = "请按卷卡返修该项缺陷后再复检。",
) -> str:
    checks: List[Dict[str, Any]] = []
    for cid in VOLUME_HEALTH_CHECK_IDS:
        if cid == fail_id:
            checks.append(
                {
                    "id": cid,
                    "status": "FAIL",
                    "chapters": fail_chapters if fail_chapters is not None else [2],
                    "evidence": f"{cid} 强模型语义体检未通过。",
                    "repair_hint": fail_hint,
                }
            )
        else:
            checks.append(
                {
                    "id": cid,
                    "status": "PASS",
                    "chapters": [],
                    "evidence": f"{cid} 语义复核达标。",
                    "repair_hint": "",
                }
            )
    verdict = "FAIL" if fail_id is not None else "PASS"
    return json.dumps(
        {
            "verdict": verdict,
            "checks": checks,
            "summary": "整卷体检通过。" if verdict == "PASS" else f"整卷体检未通过（{fail_id}）。",
        },
        ensure_ascii=False,
    )


def _make_judge(
    *,
    fail_id: Optional[str] = None,
    fail_chapters: Optional[List[int]] = None,
    responder: Optional[Callable[[LLMRequest, int], str]] = None,
) -> Tuple[PairwiseJudge, _FakeProvider]:
    fn = responder or (
        lambda req, n: _make_judge_payload(fail_id=fail_id, fail_chapters=fail_chapters)
    )
    provider = _FakeProvider(fn)
    judge = PairwiseJudge(
        provider,
        judge_model="wb/mimo-v2.6-pro",
        writer_model="wb/deepseek-v4.1-flash",
    )
    return judge, provider


def _build_project_for_blueprint(
    bp: BookBlueprint,
    *,
    scenes: Optional[List[SceneOutline]] = None,
    volume_cap_locks: Optional[Dict[str, VolumeCapLock]] = None,
) -> NovelProject:
    characters: Dict[str, CharacterAnchor] = {}
    for member in bp.cast:
        characters[member.id] = CharacterAnchor(
            id=member.id,
            name=member.name,
            role=member.role,
            personality="沉稳克制",
            speech_style=member.voice_card.speech_patterns,
            behavioral_anchors={
                "thinking": f"{member.name}抬手扶了扶眼镜框边缘并默数三下",
            },
        )
    foreshadows: Dict[str, ForeshadowItem] = {}
    for idx, fs in enumerate(bp.foreshadow_plan, start=1):
        foreshadows[fs.id] = ForeshadowItem(
            id=fs.id,
            title=fs.title,
            plant_chapter=idx,
            due_chapter=idx + 10,
            plant_instruction="埋设物证",
            payoff_instruction="回收物证",
            plant_keywords=["旧木牌"],
            payoff_keywords=["公证封条"],
        )
    default_scenes = scenes if scenes is not None else [
        SceneOutline(
            chapter=1,
            scene_id="v1_ch01",
            internal_title="雨夜初摊",
            pov_character="mc",
            active_characters=["mc", "detective_ally", "vol1_boss"],
            active_assets=[],
            style_profile_ids=[],
            location_time="南桥夜市",
            events=["首卦立威"],
            stop_condition="摊位保住",
            volume=1,
        ),
        SceneOutline(
            chapter=41,
            scene_id="v2_ch41",
            internal_title="茶室首播",
            pov_character="mc",
            active_characters=["mc", "he_xiaoman", "qi_jingming"],
            active_assets=[],
            style_profile_ids=[],
            location_time="老街茶室",
            events=["连麦拆穿磁铁"],
            stop_condition="热度登顶",
            volume=2,
        ),
    ]
    return NovelProject(
        meta=ProjectMeta(
            project_id="yeshi_test",
            title="南桥算卦录",
            genre="都市脑洞",
            synopsis="夜市算卦追凶",
            default_pov_character="mc",
        ),
        characters=characters,
        assets={},
        foreshadows=foreshadows,
        style_profiles={},
        scenes=default_scenes,
        initial_balances={"cash": 5000.0, "followers": 1000.0},
        volume_cap_locks=volume_cap_locks or {},
    )


def _clean_snapshot_for_volume(
    bp: BookBlueprint,
    vol_index: int,
    *,
    numeric_balances: Optional[Dict[str, float]] = None,
    volume_cap_locks: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fs_map: Dict[str, Dict[str, Any]] = {}
    vol = bp.volumes[vol_index - 1]
    for fs in bp.foreshadow_plan:
        status = "pending"
        if fs.id in vol.foreshadows_plant:
            status = "planted"
        if fs.id in vol.foreshadows_payoff:
            status = "resolved"
        fs_map[fs.id] = {
            "id": fs.id,
            "title": fs.title,
            "status": status,
            "plant_chapter": vol.chapter_start,
            "due_chapter": vol.chapter_end,
        }
    return {
        "current_volume": vol_index,
        "foreshadows": fs_map,
        "numeric_balances": (
            dict(numeric_balances)
            if numeric_balances is not None
            else {"cash": 120000.0, "followers": 500000.0, "merit_aura": 80.0}
        ),
        "volume_cap_locks": dict(volume_cap_locks or {}),
    }


def _clean_chapters(start_ch: int, count: int) -> List[Tuple[int, str]]:
    kws = ("算卦", "因果秤", "观气", "夜市", "直播", "通缉")
    out: List[Tuple[int, str]] = []
    for offset in range(count):
        ch = start_ch + offset
        kw1 = kws[offset % len(kws)]
        kw2 = kws[(offset + 2) % len(kws)]
        text = (
            f"第{ch}章夜色沉沉，许青川在街口亮出{kw1}木牌，借{kw2}线索当场点破涉案物证编号{ch * 17}。\n"
            f"宋海潮随即带队核验第{ch}号现场笔录，整段推理严密闭环。"
        )
        out.append((ch, text))
    return out


class TestVolumeHealthStateMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.blueprint = load_blueprint(FIXTURE_BLUEPRINT)

    def test_all_six_checks_pass_unlocks_and_allows_next_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            vol1 = self.blueprint.volumes[0]
            project = _build_project_for_blueprint(self.blueprint)
            snap = _clean_snapshot_for_volume(self.blueprint, 1)
            judge, provider = _make_judge()

            report = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 5),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )

            self.assertIsInstance(report, VolumeHealthReport)
            self.assertEqual(report.volume_index, 1)
            self.assertEqual(report.verdict, "PASS")
            self.assertEqual(report.lock_state, LOCK_STATE_PASSED)
            self.assertEqual(report.repair_actions, [])
            self.assertEqual([c.id for c in report.checks], list(VOLUME_HEALTH_CHECK_IDS))
            self.assertTrue(all(c.status == "PASS" and c.deterministic_passed and c.semantic_passed for c in report.checks))
            self.assertEqual(report.judge_model, "wb/mimo-v2.6-pro")
            self.assertEqual(report.writer_model, "wb/deepseek-v4.1-flash")
            self.assertEqual(len(provider.requests), 1)

            report_path = Path(report.report_file)
            lock_path = Path(report.lock_file)
            self.assertTrue(report_path.is_file())
            self.assertEqual(report_path.name, "volume_health_v0001.json")
            self.assertTrue(lock_path.is_file())
            self.assertEqual(lock_path.name, "volume_locks.json")

            locks = load_volume_locks(audit_dir)
            self.assertEqual(locks["volumes"]["1"]["lock_state"], LOCK_STATE_PASSED)
            self.assertEqual(locks["volumes"]["1"]["verdict"], "PASS")
            self.assertEqual(locks["volumes"]["1"]["failed_checks"], [])

            # Volume 1 never blocks; Volume 2 is now unlocked!
            assert_previous_volume_unlocked(audit_dir, 1)
            assert_previous_volume_unlocked(audit_dir, 2)

    def test_deterministic_fail_vh02_unresolved_and_unplanted_foreshadow_locks_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            # Volume 3 plants fs_blind_left_eye and pays off fs_fake_jade_seal.
            vol3 = self.blueprint.volumes[2]
            project = _build_project_for_blueprint(self.blueprint)
            snap = _clean_snapshot_for_volume(self.blueprint, 3)
            snap["foreshadows"]["fs_fake_jade_seal"]["status"] = "planted"  # NOT resolved!
            judge, _ = _make_judge()

            report = run_volume_health_check(
                volume_spec=vol3,
                chapters=_clean_chapters(101, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )
            self.assertEqual(report.verdict, "FAIL")
            self.assertEqual(report.lock_state, LOCK_STATE_LOCKED)
            vh2 = [c for c in report.checks if c.id == "VH_02_FORESHADOW_AND_REWARD"][0]
            self.assertEqual(vh2.status, "FAIL")
            self.assertFalse(vh2.deterministic_passed)
            self.assertTrue(vh2.semantic_passed)
            self.assertIn("fs_fake_jade_seal", vh2.evidence)
            self.assertEqual([a["check_id"] for a in report.repair_actions], ["VH_02_FORESHADOW_AND_REWARD"])

            with self.assertRaises(VolumeHealthLockError) as ctx:
                assert_previous_volume_unlocked(audit_dir, 4)
            self.assertEqual(ctx.exception.volume_index, 3)
            self.assertIn("VH_02_FORESHADOW_AND_REWARD", ctx.exception.failed_checks)
            self.assertTrue(ctx.exception.lock_file.endswith("volume_locks.json"))
            self.assertTrue(ctx.exception.report_file.endswith("volume_health_v0003.json"))
            self.assertEqual(len(ctx.exception.repair_actions), 1)

            # Also verify unplanted foreshadows_plant fails VH_02.
            snap_unplanted = _clean_snapshot_for_volume(self.blueprint, 3)
            snap_unplanted["foreshadows"]["fs_blind_left_eye"]["status"] = "pending"
            report2 = run_volume_health_check(
                volume_spec=vol3,
                chapters=_clean_chapters(101, 3),
                world_state_snapshot=snap_unplanted,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )
            self.assertEqual(report2.verdict, "FAIL")
            self.assertEqual(report2.lock_state, LOCK_STATE_LOCKED)
            vh2_2 = [c for c in report2.checks if c.id == "VH_02_FORESHADOW_AND_REWARD"][0]
            self.assertFalse(vh2_2.deterministic_passed)
            self.assertIn("fs_blind_left_eye", vh2_2.evidence)

    def test_deterministic_fail_vh03_exited_character_in_active_characters_locks_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            vol2 = self.blueprint.volumes[1]  # chapters 41..100; vol1_boss exited in vol 1
            bad_scenes = [
                SceneOutline(
                    chapter=45,
                    scene_id="v2_ch45",
                    internal_title="不该出现的老反派",
                    pov_character="mc",
                    active_characters=["mc", "he_xiaoman", "vol1_boss"],  # vol1_boss exited vol 1!
                    active_assets=[],
                    style_profile_ids=[],
                    location_time="茶室",
                    events=["越界出场"],
                    stop_condition="结束",
                    volume=2,
                )
            ]
            project = _build_project_for_blueprint(self.blueprint, scenes=bad_scenes)
            snap = _clean_snapshot_for_volume(self.blueprint, 2)
            judge, _ = _make_judge()

            report = run_volume_health_check(
                volume_spec=vol2,
                chapters=_clean_chapters(41, 5),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
                prev_volume_spec=self.blueprint.volumes[0],
            )
            self.assertEqual(report.verdict, "FAIL")
            self.assertEqual(report.lock_state, LOCK_STATE_LOCKED)
            vh3 = [c for c in report.checks if c.id == "VH_03_CHARACTER_AND_ARC"][0]
            self.assertEqual(vh3.status, "FAIL")
            self.assertFalse(vh3.deterministic_passed)
            self.assertIn(45, vh3.chapters)
            self.assertIn("vol1_boss", vh3.evidence)

            with self.assertRaises(VolumeHealthLockError) as ctx:
                assert_previous_volume_unlocked(audit_dir, 3)
            self.assertEqual(ctx.exception.volume_index, 2)
            self.assertIn("VH_03_CHARACTER_AND_ARC", ctx.exception.failed_checks)

            # Also verify cast.exit_volume < volume_spec.index triggers VH_03 even if absent from prev_volume.exits
            vol3 = self.blueprint.volumes[2]  # chapters 101..170; qi_jingming has exit_volume=2
            bp_no_exits = SimpleNamespace(
                premise=self.blueprint.premise,
                scale_rules=self.blueprint.scale_rules,
                volumes=[
                    SimpleNamespace(**{**v.to_dict(), "exits": []})
                    for v in self.blueprint.volumes
                ],
                cast=self.blueprint.cast,
            )
            bad_scenes_v3 = [
                SceneOutline(
                    chapter=105,
                    scene_id="v3_ch105",
                    internal_title="退场角色按exit_volume越界",
                    pov_character="mc",
                    active_characters=["mc", "qi_jingming"],  # qi_jingming.exit_volume == 2 < 3
                    active_assets=[],
                    style_profile_ids=[],
                    location_time="码头",
                    events=["越界"],
                    stop_condition="结束",
                    volume=3,
                )
            ]
            project_v3 = _build_project_for_blueprint(self.blueprint, scenes=bad_scenes_v3)
            snap_v3 = _clean_snapshot_for_volume(self.blueprint, 3)
            rep_v3 = run_volume_health_check(
                volume_spec=vol3,
                chapters=_clean_chapters(101, 3),
                world_state_snapshot=snap_v3,
                project=project_v3,
                blueprint=bp_no_exits,
                judge=judge,
                audit_dir=audit_dir,
            )
            self.assertEqual(rep_v3.verdict, "FAIL")
            vh3_v3 = [c for c in rep_v3.checks if c.id == "VH_03_CHARACTER_AND_ARC"][0]
            self.assertFalse(vh3_v3.deterministic_passed)
            self.assertIn("qi_jingming", vh3_v3.evidence)
            self.assertIn(105, vh3_v3.chapters)

    def test_deterministic_fail_vh06_negative_balance_hard_max_cap_lock_and_prose_scale(self) -> None:
        vol1 = self.blueprint.volumes[0]
        project = _build_project_for_blueprint(self.blueprint)
        judge, _ = _make_judge()

        # Case 1: negative balance
        with tempfile.TemporaryDirectory() as tmp:
            snap = _clean_snapshot_for_volume(
                self.blueprint, 1, numeric_balances={"cash": -50.0, "followers": 1000.0}
            )
            rep = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep.verdict, "FAIL")
            self.assertEqual(rep.lock_state, LOCK_STATE_LOCKED)
            vh6 = [c for c in rep.checks if c.id == "VH_06_LEDGER_AND_DEBT_MATH"][0]
            self.assertFalse(vh6.deterministic_passed)
            self.assertIn("负值", vh6.evidence)

        # Case 2: exceeds blueprint.scale_rules hard_max (cash > 5,000,000)
        with tempfile.TemporaryDirectory() as tmp:
            snap = _clean_snapshot_for_volume(
                self.blueprint, 1, numeric_balances={"cash": 8_000_000.0, "followers": 1000.0}
            )
            rep = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep.verdict, "FAIL")
            vh6 = [c for c in rep.checks if c.id == "VH_06_LEDGER_AND_DEBT_MATH"][0]
            self.assertFalse(vh6.deterministic_passed)
            self.assertIn("scale_rules", vh6.evidence)

        # Case 3: exceeds current volume volume_cap_locks (locked=False -> FAIL; unlocked=True -> PASS)
        with tempfile.TemporaryDirectory() as tmp:
            locks_dict = {
                "1:cash": {
                    "ledger_key": "cash",
                    "volume": 1,
                    "max_value": 50000.0,
                    "unlocked": False,
                    "unit": "元",
                }
            }
            snap = _clean_snapshot_for_volume(
                self.blueprint,
                1,
                numeric_balances={"cash": 80000.0, "followers": 1000.0},
                volume_cap_locks=locks_dict,
            )
            rep_locked = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep_locked.verdict, "FAIL")
            vh6 = [c for c in rep_locked.checks if c.id == "VH_06_LEDGER_AND_DEBT_MATH"][0]
            self.assertFalse(vh6.deterministic_passed)
            self.assertIn("volume_cap_locks", vh6.evidence)

            # Once breakthrough unlocked=True (and <= hard_max), VH_06 passes.
            snap["volume_cap_locks"]["1:cash"]["unlocked"] = True
            rep_unlocked = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep_unlocked.verdict, "PASS")
            self.assertEqual(rep_unlocked.lock_state, LOCK_STATE_PASSED)

        # Case 3b: project.volume_cap_locks enforced even when snapshot omits volume_cap_locks
        with tempfile.TemporaryDirectory() as tmp:
            project_with_cap = _build_project_for_blueprint(
                self.blueprint,
                volume_cap_locks={
                    "1:cash": VolumeCapLock(
                        ledger_key="cash", volume=1, max_value=30000.0, unlocked=False, unit="元"
                    )
                },
            )
            snap = _clean_snapshot_for_volume(
                self.blueprint,
                1,
                numeric_balances={"cash": 45000.0, "followers": 1000.0},
                volume_cap_locks={},
            )
            rep_proj_cap = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project_with_cap,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep_proj_cap.verdict, "FAIL")
            vh6_cap = [c for c in rep_proj_cap.checks if c.id == "VH_06_LEDGER_AND_DEBT_MATH"][0]
            self.assertFalse(vh6_cap.deterministic_passed)
            self.assertIn("volume_cap_locks", vh6_cap.evidence)

        # Case 4: L1 QR_SCALE prose 亿级粉丝 triggers VH_06 deterministic failure
        with tempfile.TemporaryDirectory() as tmp:
            snap = _clean_snapshot_for_volume(self.blueprint, 1)
            bad_chs = _clean_chapters(1, 2) + [
                (3, "这一晚直播间在线人数直接突破三亿，许青川淡定摆好算卦木牌。")
            ]
            rep_yi = run_volume_health_check(
                volume_spec=vol1,
                chapters=bad_chs,
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep_yi.verdict, "FAIL")
            vh6 = [c for c in rep_yi.checks if c.id == "VH_06_LEDGER_AND_DEBT_MATH"][0]
            self.assertFalse(vh6.deterministic_passed)
            self.assertIn(3, vh6.chapters)
            self.assertIn("QR_SCALE", vh6.evidence)

    def test_deterministic_fail_vh01_and_vh04_l1_violations_across_merged_window(self) -> None:
        vol2 = self.blueprint.volumes[1]
        project = _build_project_for_blueprint(self.blueprint)
        snap = _clean_snapshot_for_volume(self.blueprint, 2)
        judge, _ = _make_judge()

        # 1. Cross-volume verbatim repeat (ch 40 in prior_volume_chapters + ch 41 in current volume) -> VH_01 FAIL
        dup_para = "这是一段长度超过十五个字且完全一字不差的原样复读段落，用于触发L1检查。"
        prior = [(40, f"第40章开篇算卦观气。\n{dup_para}")]
        curr = [(41, f"第41章继续直播算卦。\n{dup_para}"), (42, "第42章夜市通缉新线索。")]
        with tempfile.TemporaryDirectory() as tmp:
            rep = run_volume_health_check(
                volume_spec=vol2,
                chapters=curr,
                prior_volume_chapters=prior,
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep.verdict, "FAIL")
            vh1 = [c for c in rep.checks if c.id == "VH_01_NO_ROUTINE_REPEAT"][0]
            self.assertFalse(vh1.deterministic_passed)
            self.assertIn("QR_VERBATIM_REPEAT", vh1.evidence)
            self.assertIn(41, vh1.chapters)

        # 1b. Cross-volume anchor/catchphrase repeat (3 hits in 10-ch window) -> VH_01 FAIL
        anchor_frag = "许青川抬手扶了扶眼镜框边缘并默数三下"
        prior_anchor = [
            (39, f"第39章夜市算卦。{anchor_frag}，点出线索甲。"),
            (40, f"第40章直播观气。{anchor_frag}，点出线索乙。"),
        ]
        curr_anchor = [
            (41, f"第41章茶室通缉。{anchor_frag}，点出线索丙。"),
            (42, "第42章夜市因果秤对峙。"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rep_anchor = run_volume_health_check(
                volume_spec=vol2,
                chapters=curr_anchor,
                prior_volume_chapters=prior_anchor,
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
            )
            self.assertEqual(rep_anchor.verdict, "FAIL")
            vh1_a = [c for c in rep_anchor.checks if c.id == "VH_01_NO_ROUTINE_REPEAT"][0]
            self.assertFalse(vh1_a.deterministic_passed)
            self.assertIn("QR_ANCHOR_REPEAT", vh1_a.evidence)

        # 2. Selling-point monopoly + core selling-point drift over 20-chapter window -> VH_04 FAIL
        monopoly_chs = [
            (41 + i, f"第{41 + i}章全程只写直播带货聊天，完全不提其他核心元素，编号{i * 13}。")
            for i in range(20)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rep_drift = run_volume_health_check(
                volume_spec=vol2,
                chapters=monopoly_chs,
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=Path(tmp) / "audit",
                core_selling_point_keywords=["算卦", "因果秤", "观气"],
            )
            self.assertEqual(rep_drift.verdict, "FAIL")
            vh4 = [c for c in rep_drift.checks if c.id == "VH_04_CORE_FLAVOR_QUOTA"][0]
            self.assertFalse(vh4.deterministic_passed)
            self.assertIn("QR_SELLING_POINT_MONOPOLY", vh4.evidence)
            self.assertIn("QR_CORE_SELLING_POINT_DRIFT", vh4.evidence)

    def test_semantic_fail_from_strong_judge_locks_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            vol1 = self.blueprint.volumes[0]
            project = _build_project_for_blueprint(self.blueprint)
            snap = _clean_snapshot_for_volume(self.blueprint, 1)
            judge, _ = _make_judge(
                fail_id="VH_05_THREE_ACT_AND_HOOK",
                fail_chapters=[4, 5],
            )

            report = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 5),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )
            self.assertEqual(report.verdict, "FAIL")
            self.assertEqual(report.lock_state, LOCK_STATE_LOCKED)
            vh5 = [c for c in report.checks if c.id == "VH_05_THREE_ACT_AND_HOOK"][0]
            self.assertEqual(vh5.status, "FAIL")
            self.assertTrue(vh5.deterministic_passed)
            self.assertFalse(vh5.semantic_passed)
            self.assertEqual(vh5.chapters, [4, 5])
            self.assertEqual(report.repair_actions[0]["source"], "semantic")

            with self.assertRaises(VolumeHealthLockError) as ctx:
                assert_previous_volume_unlocked(audit_dir, 2)
            self.assertEqual(ctx.exception.failed_checks, ["VH_05_THREE_ACT_AND_HOOK"])

    def test_missing_or_tampered_previous_volume_report_blocks_next_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            # Empty audit dir: target_volume=1 allowed, target_volume=2 blocked!
            assert_previous_volume_unlocked(audit_dir, 1)
            with self.assertRaises(VolumeHealthLockError) as ctx_missing:
                assert_previous_volume_unlocked(audit_dir, 2)
            self.assertEqual(ctx_missing.exception.volume_index, 1)

            # Generate genuine PASS for vol 1 first.
            vol1 = self.blueprint.volumes[0]
            project = _build_project_for_blueprint(self.blueprint)
            snap = _clean_snapshot_for_volume(self.blueprint, 1)
            judge, _ = _make_judge()
            report = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )
            assert_previous_volume_unlocked(audit_dir, 2)

            # Tamper report_file verdict to FAIL while leaving volume_locks.json as UNLOCKED_PASSED -> MUST BLOCK!
            report_path = Path(report.report_file)
            tampered = json.loads(report_path.read_text(encoding="utf-8"))
            tampered["verdict"] = "FAIL"
            report_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(VolumeHealthLockError):
                assert_previous_volume_unlocked(audit_dir, 2)

            # Delete report_file while volume_locks.json still says UNLOCKED_PASSED -> MUST BLOCK!
            report_path.unlink()
            with self.assertRaises(VolumeHealthLockError):
                assert_previous_volume_unlocked(audit_dir, 2)

    def test_repair_and_recheck_pass_automatically_unlocks_and_preserves_other_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            vol1 = self.blueprint.volumes[0]
            vol2 = self.blueprint.volumes[1]
            project = _build_project_for_blueprint(self.blueprint)
            judge, _ = _make_judge()

            # Vol 1 initially fails because fs_scale_origin is still pending.
            snap_v1 = _clean_snapshot_for_volume(self.blueprint, 1)
            snap_v1["foreshadows"]["fs_scale_origin"]["status"] = "pending"
            rep_fail = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap_v1,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )
            self.assertEqual(rep_fail.lock_state, LOCK_STATE_LOCKED)
            with self.assertRaises(VolumeHealthLockError):
                assert_previous_volume_unlocked(audit_dir, 2)

            # Repair foreshadow -> re-run health check -> automatically unlocked!
            snap_v1["foreshadows"]["fs_scale_origin"]["status"] = "planted"
            rep_pass = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap_v1,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )
            self.assertEqual(rep_pass.verdict, "PASS")
            self.assertEqual(rep_pass.lock_state, LOCK_STATE_PASSED)
            assert_previous_volume_unlocked(audit_dir, 2)

            # Saving Vol 2 preserves Vol 1 in volume_locks.json.
            snap_v2 = _clean_snapshot_for_volume(self.blueprint, 2)
            rep_v2 = run_volume_health_check(
                volume_spec=vol2,
                chapters=_clean_chapters(41, 3),
                world_state_snapshot=snap_v2,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
                prev_volume_spec=vol1,
            )
            self.assertEqual(rep_v2.verdict, "PASS")
            locks = load_volume_locks(audit_dir)
            self.assertIn("1", locks["volumes"])
            self.assertIn("2", locks["volumes"])
            assert_previous_volume_unlocked(audit_dir, 2)
            assert_previous_volume_unlocked(audit_dir, 3)

    def test_corrupted_volume_locks_json_raises_value_error_strictly(self) -> None:
        bad_contents = [
            "",
            "{not valid json",
            "[]",
            json.dumps({"wrong_key": {}}),
            json.dumps({"volumes": []}),
            json.dumps({"volumes": {"1": "not-an-object"}}),
        ]
        for content in bad_contents:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as tmp:
                    audit_dir = Path(tmp) / "audit"
                    audit_dir.mkdir(parents=True, exist_ok=True)
                    (audit_dir / "volume_locks.json").write_text(content, encoding="utf-8")

                    with self.assertRaises(ValueError):
                        load_volume_locks(audit_dir)
                    with self.assertRaises(ValueError):
                        assert_previous_volume_unlocked(audit_dir, 2)

                    dummy_report = VolumeHealthReport(
                        volume_index=1,
                        volume_title="t",
                        chapter_start=1,
                        chapter_end=2,
                        verdict="PASS",
                        lock_state=LOCK_STATE_PASSED,
                        checks=[
                            VolumeHealthCheckItem(
                                id=cid,
                                title=cid,
                                status="PASS",
                                deterministic_passed=True,
                                semantic_passed=True,
                                chapters=[],
                                evidence="ok",
                                repair_hint="",
                            )
                            for cid in VOLUME_HEALTH_CHECK_IDS
                        ],
                        repair_actions=[],
                        summary="ok",
                        judge_model="mimo_v2_6_pro",
                        writer_model="deepseek_v4_1_flash",
                        report_file="",
                        lock_file="",
                        recorded_at="2026-11-25T21:00:00+08:00",
                    )
                    with self.assertRaises(ValueError):
                        save_volume_health_result(audit_dir, dummy_report)

    def test_same_family_judge_raises_same_model_judge_error(self) -> None:
        provider = _FakeProvider(lambda req, n: _make_judge_payload())
        for j_model, w_model in (
            ("wb/mimo-v2.6-pro", "mimo_v2_6_pro"),
            ("hy4", "wb/hy4flash-free"),
            ("wb/deepseek-v4.1-flash", "deepseek-chat"),
        ):
            with self.subTest(judge_model=j_model, writer_model=w_model):
                with self.assertRaises(SameModelJudgeError):
                    PairwiseJudge(provider, judge_model=j_model, writer_model=w_model)

    def test_format_retry_boundary_in_end_to_end_volume_health_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            vol1 = self.blueprint.volumes[0]
            project = _build_project_for_blueprint(self.blueprint)
            snap = _clean_snapshot_for_volume(self.blueprint, 1)

            # Succeeds on 2nd attempt (1 bounded format retry)
            judge_ok, provider_ok = _make_judge(
                responder=lambda req, n: (
                    '{"verdict":"PASS","checks":[],"summary":"坏"引号"}'
                    if n == 1
                    else _make_judge_payload()
                )
            )
            rep = run_volume_health_check(
                volume_spec=vol1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge_ok,
                audit_dir=audit_dir,
            )
            self.assertEqual(rep.verdict, "PASS")
            self.assertEqual(len(provider_ok.requests), 2)

            # Fails strictly on 2nd consecutive contract violation
            judge_bad, provider_bad = _make_judge(
                responder=lambda req, n: '{"verdict":"PASS","checks":[],"summary":"missing"}'
            )
            with self.assertRaises(JudgeResponseError):
                run_volume_health_check(
                    volume_spec=vol1,
                    chapters=_clean_chapters(1, 3),
                    world_state_snapshot=snap,
                    project=project,
                    blueprint=self.blueprint,
                    judge=judge_bad,
                    audit_dir=audit_dir,
                )
            self.assertEqual(len(provider_bad.requests), 2)

    def test_u1_optional_fields_forwarded_via_getattr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            vol1_base = self.blueprint.volumes[0].to_dict()
            vol1_u1 = SimpleNamespace(
                **vol1_base,
                antagonist_motive="垄断南桥夜市的地下销赃渠道",
                new_gameplay="夜间古玩真假盲盒连麦鉴定",
                mainline_step="摸出二十年前渡江印章第一条线索",
                daily_case_quota=3,
                fugitive_case_quota=1,
                forbidden_from_prev_volume=["重复收保护费"],
                to_dict=lambda: {
                    **vol1_base,
                    "antagonist_motive": "垄断南桥夜市的地下销赃渠道",
                    "new_gameplay": "夜间古玩真假盲盒连麦鉴定",
                    "mainline_step": "摸出二十年前渡江印章第一条线索",
                    "daily_case_quota": 3,
                    "fugitive_case_quota": 1,
                    "forbidden_from_prev_volume": ["重复收保护费"],
                },
            )
            project = _build_project_for_blueprint(self.blueprint)
            snap = _clean_snapshot_for_volume(self.blueprint, 1)
            judge, provider = _make_judge()

            report = run_volume_health_check(
                volume_spec=vol1_u1,
                chapters=_clean_chapters(1, 3),
                world_state_snapshot=snap,
                project=project,
                blueprint=self.blueprint,
                judge=judge,
                audit_dir=audit_dir,
            )
            self.assertEqual(report.verdict, "PASS")
            sent_prompt = provider.requests[0].user_prompt
            self.assertIn("夜间古玩真假盲盒连麦鉴定", sent_prompt)
            self.assertIn("daily_case_quota", sent_prompt)
            self.assertIn("forbidden_from_prev_volume", sent_prompt)

    def test_relative_audit_dir_and_target_volume_le_1(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            prev_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                rel_audit = Path("rel_out") / "audit"
                # target_volume <= 1 returns directly even before any files exist
                assert_previous_volume_unlocked(rel_audit, 0)
                assert_previous_volume_unlocked(rel_audit, 1)

                vol1 = self.blueprint.volumes[0]
                project = _build_project_for_blueprint(self.blueprint)
                snap = _clean_snapshot_for_volume(self.blueprint, 1)
                judge, _ = _make_judge()
                rep = run_volume_health_check(
                    volume_spec=vol1,
                    chapters=_clean_chapters(1, 3),
                    world_state_snapshot=snap,
                    project=project,
                    blueprint=self.blueprint,
                    judge=judge,
                    audit_dir=rel_audit,
                )
                self.assertEqual(rep.verdict, "PASS")
                assert_previous_volume_unlocked(rel_audit, 2)
            finally:
                os.chdir(prev_cwd)


if __name__ == "__main__":
    unittest.main()
