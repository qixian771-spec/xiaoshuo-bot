from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.loader import ProjectValidationError, load_project
from xiaoshuo_bot.models import (
    SHANGHAI_TZ,
    AmbiguousChapterArtifactError,
    AssetState,
    ChapterArtifact,
    CharacterWorldState,
    ExpectationState,
    ForeshadowItem,
    ProjectMeta,
    VolumeCapLock,
    WorldStateSnapshot,
    format_chapter_filename,
    resolve_chapter_path,
)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _build_minimal_project(
    root: Path,
    *,
    meta_overrides: dict | None = None,
    scenes: list | None = None,
    foreshadows: list | None = None,
    multi_volume: dict[str, dict] | None = None,
) -> Path:
    meta = {
        "project_id": "tomato_test",
        "title": "短剧神豪系统",
        "genre": "都市脑洞",
        "genre_id": "urban_brainhole",
        "synopsis": "测试简介",
        "default_pov_character": "protagonist",
        "max_rewrite_retries": 3,
        "author_level": "lv0_1",
        "contract_track": "complete_150k",
        "pov_mode": "third_limited",
        "panel_policy": "allow_system_tag",
    }
    if meta_overrides:
        meta.update(meta_overrides)
    _write_yaml(root / "project.yaml", meta)

    _write_yaml(
        root / "characters.yaml",
        {
            "characters": [
                {
                    "id": "protagonist",
                    "name": "陈屿",
                    "role": "主角",
                    "personality": "果断",
                    "speech_style": "干脆",
                },
                {
                    "id": "rival",
                    "name": "赵坤",
                    "role": "反派",
                    "personality": "嚣张",
                    "speech_style": "刻薄",
                },
            ]
        },
    )
    _write_yaml(
        root / "assets.yaml",
        {
            "assets": [
                {
                    "id": "phone",
                    "name": "旧手机",
                    "location": "裤兜",
                    "holder": "陈屿",
                    "condition": "完好",
                    "physical_details": "黑色直板机",
                }
            ]
        },
    )
    _write_yaml(root / "foreshadows.yaml", {"foreshadows": foreshadows or []})
    _write_yaml(
        root / "style_profiles.yaml",
        {
            "style_profiles": [
                {
                    "id": "fast_urban",
                    "name": "快节奏都市",
                    "granularity_notes": "短句推进",
                    "sample_text": "门刚推开，账单就拍在了桌上。",
                }
            ]
        },
    )

    default_scenes = scenes if scenes is not None else [
        {
            "chapter": 1,
            "scene_id": "ch01_hook",
            "internal_title": "开局",
            "public_title": "退婚现场",
            "pov_character": "protagonist",
            "active_characters": ["protagonist", "rival"],
            "active_assets": ["phone"],
            "style_profile_ids": ["fast_urban"],
            "location_time": "白天，大厅",
            "events": ["冲突爆发"],
            "stop_condition": "合同拍桌",
            "emotion_polarity": "suppress",
        },
        {
            "chapter": 2,
            "scene_id": "ch02_burst",
            "internal_title": "反击",
            "public_title": "当场打脸",
            "pov_character": "protagonist",
            "active_characters": ["protagonist", "rival"],
            "active_assets": ["phone"],
            "style_profile_ids": ["fast_urban"],
            "location_time": "中午，大厅",
            "events": ["打脸"],
            "stop_condition": "反派吃瘪",
            "emotion_polarity": "burst",
        },
    ]

    if multi_volume is not None:
        for rel_name, payload in multi_volume.items():
            _write_yaml(root / "outline" / rel_name, payload)
    else:
        _write_yaml(root / "outline.yaml", {"scenes": default_scenes})

    return root


class TestModelsAndLoader(unittest.TestCase):
    def test_shanghai_tz_without_tzdata(self) -> None:
        dt = datetime(2026, 11, 18, 12, 30, 0, tzinfo=SHANGHAI_TZ)
        self.assertEqual(dt.utcoffset(), timedelta(hours=8))
        self.assertTrue(dt.isoformat().endswith("+08:00"))
        self.assertEqual(SHANGHAI_TZ.tzname(dt), "Asia/Shanghai")

        kst = timezone(timedelta(hours=9), name="Asia/Seoul")
        kst_late = datetime(2026, 11, 18, 23, 30, 0, tzinfo=kst)
        cst_converted = kst_late.astimezone(SHANGHAI_TZ)
        self.assertEqual(cst_converted.strftime("%Y-%m-%d %H:%M"), "2026-11-18 22:30")

    def test_legacy_baiwu_dukou_loads_cleanly(self) -> None:
        project = load_project(ROOT / "projects" / "baiwu_dukou")
        self.assertEqual(project.meta.project_id, "baiwu_dukou")
        self.assertEqual(project.meta.contract_track, "legacy")
        self.assertEqual(project.meta.author_level, "lv0_1")
        self.assertEqual(project.meta.pov_mode, "third_limited")
        self.assertEqual(project.meta.panel_policy, "forbid")
        self.assertEqual(len(project.scenes), 5)

    def test_tomato_title_length_and_book_marks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p_ok = _build_minimal_project(
                Path(tmp) / "ok15",
                meta_overrides={"title": "一二三四五六七八九十一二三四五"},  # 15 chars
            )
            proj = load_project(p_ok)
            self.assertEqual(len(proj.meta.title), 15)

            p_long = _build_minimal_project(
                Path(tmp) / "long16",
                meta_overrides={"title": "一二三四五六七八九十一二三四五六"},  # 16 chars
            )
            with self.assertRaises(ProjectValidationError):
                load_project(p_long)

            p_marks = _build_minimal_project(
                Path(tmp) / "marks",
                meta_overrides={"title": "《短剧神豪》"},
            )
            with self.assertRaises(ProjectValidationError):
                load_project(p_marks)

            p_alt_long = _build_minimal_project(
                Path(tmp) / "alt_long",
                meta_overrides={"alternate_titles": ["正常书名", "这个备选书名明显超过了十五个字限制"]},
            )
            with self.assertRaises(ProjectValidationError):
                load_project(p_alt_long)

    def test_unknown_enums_raise_project_validation_error(self) -> None:
        with self.assertRaises(ProjectValidationError):
            ProjectMeta(
                project_id="x",
                title="短书名",
                genre="都市",
                synopsis="",
                default_pov_character="p1",
                pov_mode="second_person",
            )

        bad_enum_cases = [
            {"pov_mode": "omniscient"},
            {"author_level": "lv99"},
            {"contract_track": "magic_track"},
            {"panel_policy": "allow_all"},
            {"pov_mode": None},
            {"author_level": ""},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for idx, override in enumerate(bad_enum_cases):
                proj_dir = _build_minimal_project(
                    Path(tmp) / f"case_{idx}",
                    meta_overrides=override,
                )
                with self.assertRaises(ProjectValidationError, msg=f"Failed to reject {override}"):
                    load_project(proj_dir)

            # Invalid emotion_polarity in scene
            bad_scene_dir = _build_minimal_project(
                Path(tmp) / "bad_polarity",
                scenes=[
                    {
                        "chapter": 1,
                        "scene_id": "ch01",
                        "internal_title": "一",
                        "pov_character": "protagonist",
                        "active_characters": ["protagonist"],
                        "active_assets": [],
                        "style_profile_ids": ["fast_urban"],
                        "location_time": "今",
                        "events": ["e1"],
                        "stop_condition": "stop",
                        "emotion_polarity": "invalid_mood",
                    }
                ],
            )
            with self.assertRaises(ProjectValidationError):
                load_project(bad_scene_dir)

    def test_consecutive_pure_suppression_chapters_rejected(self) -> None:
        def _mk_scene(ch: int, polarity: str) -> dict:
            return {
                "chapter": ch,
                "scene_id": f"ch{ch:02d}",
                "internal_title": f"第{ch}节",
                "pov_character": "protagonist",
                "active_characters": ["protagonist"],
                "active_assets": ["phone"],
                "style_profile_ids": ["fast_urban"],
                "location_time": "地点",
                "events": ["事件"],
                "stop_condition": "停止",
                "emotion_polarity": polarity,
            }

        with tempfile.TemporaryDirectory() as tmp:
            # 2 suppress + 1 burst + 2 suppress -> OK
            ok_dir = _build_minimal_project(
                Path(tmp) / "suppress_ok",
                scenes=[
                    _mk_scene(1, "suppress"),
                    _mk_scene(2, "suppress"),
                    _mk_scene(3, "burst"),
                    _mk_scene(4, "suppress"),
                    _mk_scene(5, "suppress"),
                ],
            )
            proj = load_project(ok_dir)
            self.assertEqual(len(proj.scenes), 5)

            # 3 consecutive suppress -> ProjectValidationError
            bad_dir = _build_minimal_project(
                Path(tmp) / "suppress_3",
                scenes=[
                    _mk_scene(1, "suppress"),
                    _mk_scene(2, "suppress"),
                    _mk_scene(3, "suppress"),
                    _mk_scene(4, "burst"),
                ],
            )
            with self.assertRaises(ProjectValidationError):
                load_project(bad_dir)

    def test_multi_volume_loading_and_cross_volume_foreshadow(self) -> None:
        def _mk_scene(ch: int, polarity: str = "burst") -> dict:
            return {
                "chapter": ch,
                "scene_id": f"ch{ch:02d}",
                "internal_title": f"第{ch}节",
                "pov_character": "protagonist",
                "active_characters": ["protagonist"],
                "active_assets": ["phone"],
                "style_profile_ids": ["fast_urban"],
                "location_time": "地点",
                "events": ["事件"],
                "stop_condition": "停止",
                "emotion_polarity": polarity,
            }

        cross_fs = [
            {
                "id": "FS-CROSS-01",
                "title": "神秘青铜令牌",
                "plant_chapter": 1,
                "due_chapter": 65,
                "plant_instruction": "第1章出现",
                "payoff_instruction": "第65章新地图揭晓",
                "plant_keywords": ["青铜令牌"],
                "payoff_keywords": ["青铜令牌"],
                "cross_volume": True,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            mv_dir = _build_minimal_project(
                Path(tmp) / "multi_vol",
                foreshadows=cross_fs,
                multi_volume={
                    "vol_01.yaml": {"volume": 1, "scenes": [_mk_scene(1, "suppress"), _mk_scene(2, "burst")]},
                    "vol_02.yaml": {"volume": 2, "scenes": [_mk_scene(3, "burst")]},
                    "vol_10.yaml": {"volume": 10, "scenes": [_mk_scene(4, "burst")]},
                },
            )
            # Non-final volume allows cross_volume due_chapter outside current outline
            proj = load_project(mv_dir)
            self.assertEqual([s.chapter for s in proj.scenes], [1, 2, 3, 4])
            self.assertEqual([s.volume for s in proj.scenes], [1, 1, 2, 10])

            # Final volume forbids unresolved foreshadow whose due_chapter is missing
            with self.assertRaises(ProjectValidationError) as ctx:
                load_project(mv_dir, is_final_volume=True)
            self.assertIn("FS-CROSS-01", str(ctx.exception))

            # Also test is_final_volume inside final volume YAML
            mv_final_dir = _build_minimal_project(
                Path(tmp) / "multi_vol_final_yaml",
                foreshadows=cross_fs,
                multi_volume={
                    "vol_01.yaml": {"volume": 1, "scenes": [_mk_scene(1, "suppress")]},
                    "vol_02.yaml": {"volume": 2, "is_final_volume": True, "scenes": [_mk_scene(2, "burst")]},
                },
            )
            with self.assertRaises(ProjectValidationError) as ctx2:
                load_project(mv_final_dir)
            self.assertIn("FS-CROSS-01", str(ctx2.exception))

            # Non-cross_volume foreshadow with missing due_chapter fails even on non-final volume
            non_cross_dir = _build_minimal_project(
                Path(tmp) / "non_cross",
                foreshadows=[
                    {
                        "id": "FS-LOCAL",
                        "title": "本地伏笔",
                        "plant_chapter": 1,
                        "due_chapter": 99,
                        "plant_instruction": "埋",
                        "payoff_instruction": "收",
                        "cross_volume": False,
                    }
                ],
            )
            with self.assertRaises(ProjectValidationError):
                load_project(non_cross_dir)

    def test_chapter_filename_and_resolve_chapter_path(self) -> None:
        self.assertEqual(format_chapter_filename(1), "chapter_0001.txt")
        self.assertEqual(format_chapter_filename(12), "chapter_0012.txt")
        self.assertEqual(format_chapter_filename(1234), "chapter_1234.txt")
        with self.assertRaises(ValueError):
            format_chapter_filename(-1)
        with self.assertRaises(ValueError):
            format_chapter_filename(True)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertIsNone(resolve_chapter_path(d, 1))

            # Fallback to legacy :02d
            p2 = d / "chapter_01.txt"
            p2.write_text("legacy", encoding="utf-8")
            self.assertEqual(resolve_chapter_path(d, 1), p2)

            # Both :04d and :02d coexist -> AmbiguousChapterArtifactError
            p4 = d / "chapter_0001.txt"
            p4.write_text("new", encoding="utf-8")
            with self.assertRaises(AmbiguousChapterArtifactError):
                resolve_chapter_path(d, 1)

            # Prefer :04d when only :04d exists
            p4_ch2 = d / "chapter_0002.txt"
            p4_ch2.write_text("ch2", encoding="utf-8")
            self.assertEqual(resolve_chapter_path(d, 2), p4_ch2)

            # >= 1000 chapters produce identical :04d and :02d path -> must NOT raise ambiguous
            p1000 = d / "chapter_1000.txt"
            p1000.write_text("ch1000", encoding="utf-8")
            self.assertEqual(resolve_chapter_path(d, 1000), p1000)

    def test_world_state_snapshot_and_chapter_artifact_roundtrip(self) -> None:
        snap = WorldStateSnapshot(
            current_chapter=5,
            current_volume=1,
            completed_chapters=[1, 2, 3, 4, 5],
            previous_chapter_tail="他合上保险箱。",
            assets={
                "watch": AssetState(
                    id="watch",
                    name="怀表",
                    location="手中",
                    holder="陈屿",
                    condition="完好",
                    physical_details="铜壳",
                    last_updated_chapter=5,
                    history=["[第5章] 取回"],
                )
            },
            foreshadows={
                "FS-1": ForeshadowItem(
                    id="FS-1",
                    title="钥匙",
                    plant_chapter=1,
                    due_chapter=10,
                    plant_instruction="埋",
                    payoff_instruction="收",
                    status="planted",
                    cross_volume=True,
                )
            },
            numeric_balances={"cash": 3000000.0, "power_rank": 2.0},
            volume_cap_locks={
                "1:power_rank": VolumeCapLock(
                    ledger_key="power_rank", volume=1, max_value=3.0, unlocked=False, unit="阶"
                )
            },
            character_states={
                "ex_wife": CharacterWorldState(
                    character_id="ex_wife",
                    name="苏某",
                    status="alive",
                    relations={"protagonist": "divorced"},
                    forbidden_titles=["老公", "丈夫"],
                ),
                "villain": CharacterWorldState(
                    character_id="villain",
                    name="赵坤",
                    status="deceased",
                    deceased_chapter=4,
                ),
            },
            relationship_edges=[
                {"source": "ex_wife", "target": "protagonist", "relation": "divorced"}
            ],
            forbidden_titles={"ex_wife": ["老公"]},
            active_expectations={
                "EXP-1": ExpectationState(
                    id="EXP-1",
                    title="三日之约",
                    planted_chapter=2,
                    due_chapter=6,
                    status="active",
                )
            },
            virtual_day=7,
            virtual_season="冬",
            virtual_date_label="腊月初三",
            unhealed_injuries={"protagonist": {"左肩刀伤": 2}},
            recent_locations=["clock_shop", "dock"],
            chapter_hashes={1: "hash1", 2: "hash2"},
            locked_chapters=[1, 2],
        )

        raw = json.loads(json.dumps(snap.to_dict(), ensure_ascii=False))
        restored = WorldStateSnapshot.from_dict(raw)
        self.assertEqual(restored, snap)
        self.assertIn(1, restored.chapter_hashes)
        self.assertEqual(restored.get_deceased_names(), ["赵坤"])
        self.assertEqual(set(restored.get_all_forbidden_titles()), {"老公", "丈夫"})

        art = ChapterArtifact(chapter=1, public_title="雨夜拆迁", body="雨水顺着屋檐往下滴。")
        self.assertEqual(len(art.sha256), 64)
        self.assertEqual(ChapterArtifact.from_dict(art.to_dict()), art)

    def test_pyproject_and_gitignore_contract(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as f:
            pyproject = tomllib.load(f)
        deps = pyproject["project"]["dependencies"]
        self.assertIn("PyYAML>=6.0", deps)
        self.assertIn("Pillow>=10.0", deps)
        self.assertIn("pypinyin>=0.51.0", deps)

        companion_deps = pyproject["project"]["optional-dependencies"]["companion"]
        self.assertIn("playwright>=1.48.0", companion_deps)
        self.assertIn("fonttools[woff]>=4.50.0", companion_deps)

        pkg_data = pyproject["tool"]["setuptools"]["package-data"]["xiaoshuo_bot"]
        self.assertIn("fonts/*.otf", pkg_data)
        self.assertIn("fonts/*.ttf", pkg_data)
        self.assertIn("fonts/OFL.txt", pkg_data)
        self.assertIn("server/static/*", pkg_data)

        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for expected in [".venv/", "*.db", "*.sqlite3*", "auth-files/", "chrome_profiles/", "_archive/", "*.env"]:
            self.assertIn(expected, gitignore)


if __name__ == "__main__":
    unittest.main()
