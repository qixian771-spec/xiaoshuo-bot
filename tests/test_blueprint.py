from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.blueprint import (  # noqa: E402
    AdaptationSpec,
    BenchmarkRef,
    BlueprintValidationError,
    BookBlueprint,
    CastMember,
    ForeshadowPlanItem,
    GoldenFingerSpec,
    GoldenFingerUpgrade,
    MAX_VOLUMES,
    MIN_VOLUMES,
    PremiseSpec,
    ProtagonistSpec,
    ScaleRule,
    StyleConfig,
    VoiceCard,
    VolumeExit,
    VolumeMap,
    VolumeSpec,
    collect_existing_cast_names,
    load_blueprint,
    validate_blueprint,
)


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "blueprint_valid.yaml"
FIXTURE_30VOLS_PATH = ROOT / "tests" / "fixtures" / "blueprint_30vols_valid.yaml"
PROJECTS_ROOT = ROOT / "projects"

OLD_NIGHT_MARKET_CAST = [
    "唐子墨",
    "刘敬尧",
    "冯念慈",
    "潘怀安",
    "金丽娟",
    "老周",
]


def _load_valid_raw() -> dict:
    data = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class TestBlueprintValidFixture(unittest.TestCase):
    def test_valid_fixture_passes_and_roundtrips(self) -> None:
        bp = load_blueprint(FIXTURE_PATH)
        self.assertIsInstance(bp, BookBlueprint)
        self.assertEqual(bp.style.genre_id, "urban_brainhole")
        self.assertEqual(len(bp.volumes), 6)
        self.assertEqual(len(bp.cast), 10)

        existing = collect_existing_cast_names(PROJECTS_ROOT)
        self.assertIn("林晚", existing)
        combined_existing = existing + OLD_NIGHT_MARKET_CAST

        validate_blueprint(bp, existing_cast_names=combined_existing)

        roundtrip = BookBlueprint.from_dict(bp.to_dict())
        self.assertEqual(roundtrip.to_dict(), bp.to_dict())

        with tempfile.TemporaryDirectory() as tmp:
            dumped_path = Path(tmp) / "book_blueprint.yaml"
            dumped_path.write_text(
                yaml.safe_dump(bp.to_dict(), allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            reloaded = load_blueprint(dumped_path)
            validate_blueprint(reloaded, existing_cast_names=combined_existing)
            self.assertEqual(reloaded.to_dict(), bp.to_dict())

    def test_legacy_fixture_defaults_new_optional_fields(self) -> None:
        bp = load_blueprint(FIXTURE_PATH)
        for vol in bp.volumes:
            self.assertEqual(vol.antagonist_motive, "")
            self.assertEqual(vol.new_gameplay, "")
            self.assertEqual(vol.mainline_step, "")
            self.assertIsNone(vol.daily_case_quota)
            self.assertIsNone(vol.fugitive_case_quota)
            self.assertEqual(vol.forbidden_from_prev_volume, [])
        for member in bp.cast:
            self.assertEqual(member.independent_goal, "")
            self.assertEqual(member.flaw_or_setback, "")
            self.assertEqual(member.stage_arcs, [])
        self.assertEqual(bp.to_dict(), _load_valid_raw())

    def test_30vols_fixture_passes_and_deserializes_all_new_fields(self) -> None:
        self.assertEqual(MIN_VOLUMES, 3)
        self.assertEqual(MAX_VOLUMES, 40)

        bp = load_blueprint(FIXTURE_30VOLS_PATH)
        self.assertIsInstance(bp, BookBlueprint)
        self.assertEqual(len(bp.volumes), 30)

        # Verify 1000 contiguous chapters with 33~34 chapters per volume
        self.assertEqual(bp.volumes[0].chapter_start, 1)
        self.assertEqual(bp.volumes[-1].chapter_end, 1000)
        total_chapters = 0
        for idx, vol in enumerate(bp.volumes, start=1):
            self.assertEqual(vol.index, idx)
            vol_len = vol.chapter_end - vol.chapter_start + 1
            self.assertIn(vol_len, (33, 34))
            total_chapters += vol_len
            self.assertTrue(vol.antagonist_motive.strip())
            self.assertTrue(vol.new_gameplay.strip())
            self.assertTrue(vol.mainline_step.strip())
            self.assertIsInstance(vol.daily_case_quota, int)
            self.assertGreaterEqual(vol.daily_case_quota, 0)
            self.assertIsInstance(vol.fugitive_case_quota, int)
            self.assertGreaterEqual(vol.fugitive_case_quota, 0)
            if idx == 1:
                self.assertEqual(vol.forbidden_from_prev_volume, [])
            else:
                self.assertGreaterEqual(len(vol.forbidden_from_prev_volume), 1)
                for item in vol.forbidden_from_prev_volume:
                    self.assertTrue(item.strip())
        self.assertEqual(total_chapters, 1000)
        self.assertEqual(bp.volumes[-1].daily_case_quota, 0)

        # Verify 6 major supporting cast members have independent goals, flaws, and multi-stage arcs
        cast_by_id = {m.id: m for m in bp.cast}
        major_ally_ids = (
            "ally_detective",
            "ally_operator",
            "ally_forensic",
            "ally_mechanic",
            "ally_reporter",
            "ally_lawyer",
        )
        for cid in major_ally_ids:
            self.assertIn(cid, cast_by_id)
            member = cast_by_id[cid]
            self.assertTrue(member.independent_goal.strip())
            self.assertTrue(member.flaw_or_setback.strip())
            self.assertGreaterEqual(len(member.stage_arcs), 3)
            for stage in member.stage_arcs:
                self.assertTrue(stage.strip())

        existing = collect_existing_cast_names(PROJECTS_ROOT)
        combined_existing = existing + OLD_NIGHT_MARKET_CAST
        validate_blueprint(bp, existing_cast_names=combined_existing)

        roundtrip = BookBlueprint.from_dict(bp.to_dict())
        self.assertEqual(roundtrip.to_dict(), bp.to_dict())

        # Verify to_dict() preserves populated optional fields (including quota == 0)
        vol2_dict = bp.volumes[1].to_dict()
        for key in (
            "antagonist_motive",
            "new_gameplay",
            "mainline_step",
            "daily_case_quota",
            "fugitive_case_quota",
            "forbidden_from_prev_volume",
        ):
            self.assertIn(key, vol2_dict)
        self.assertEqual(bp.volumes[-1].to_dict()["daily_case_quota"], 0)

        ally_dict = cast_by_id["ally_detective"].to_dict()
        for key in ("independent_goal", "flaw_or_setback", "stage_arcs"):
            self.assertIn(key, ally_dict)

        with tempfile.TemporaryDirectory() as tmp:
            dumped_path = Path(tmp) / "blueprint_30vols_roundtrip.yaml"
            dumped_path.write_text(
                yaml.safe_dump(bp.to_dict(), allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            reloaded = load_blueprint(dumped_path)
            validate_blueprint(reloaded, existing_cast_names=combined_existing)
            self.assertEqual(reloaded.to_dict(), bp.to_dict())


class TestBlueprintLoaderSchema(unittest.TestCase):
    def test_load_blueprint_missing_or_corrupt_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            with self.assertRaises(BlueprintValidationError):
                load_blueprint(tmp_dir / "missing.yaml")

            empty_yaml = tmp_dir / "empty.yaml"
            empty_yaml.write_text("", encoding="utf-8")
            with self.assertRaises(BlueprintValidationError):
                load_blueprint(empty_yaml)

            list_yaml = tmp_dir / "list.yaml"
            list_yaml.write_text("- a\n- b\n", encoding="utf-8")
            with self.assertRaises(BlueprintValidationError):
                load_blueprint(list_yaml)

            bad_yaml = tmp_dir / "bad.yaml"
            bad_yaml.write_text("premise:\n  logline: [unclosed\n", encoding="utf-8")
            with self.assertRaises(BlueprintValidationError):
                load_blueprint(bad_yaml)

    def test_load_blueprint_aggregates_missing_required_keys_without_defaults(self) -> None:
        raw = _load_valid_raw()
        del raw["premise"]["logline"]
        del raw["premise"]["target_readers"]
        del raw["protagonist"]["voice_card"]["catchphrases"]
        del raw["scale_rules"][0]["hard_max"]
        del raw["volumes"][0]["map"]
        del raw["cast"][0]["exit_volume"]
        del raw["style"]["allow_pov_inner_monologue"]
        del raw["style"]["humor"]
        del raw["ending"]
        del raw["adaptation"]["visual_hook"]

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "incomplete.yaml"
            p.write_text(
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaises(BlueprintValidationError) as ctx:
                load_blueprint(p)

        msg = str(ctx.exception)
        for expected_path in (
            "premise.logline",
            "premise.target_readers",
            "protagonist.voice_card.catchphrases",
            "scale_rules[0].hard_max",
            "volumes[0].map",
            "cast[0].exit_volume",
            "style.allow_pov_inner_monologue",
            "style.humor",
            "ending",
            "adaptation.visual_hook",
        ):
            self.assertIn(expected_path, msg)

    def test_load_blueprint_rejects_wrong_types_and_bool_as_int(self) -> None:
        raw = _load_valid_raw()
        raw["target_total_chars"] = True
        raw["chapter_chars_range"][0] = False
        raw["style"]["allow_pov_inner_monologue"] = "true"
        raw["style"]["humor"] = 1
        raw["volumes"][0]["index"] = "1"
        raw["scale_rules"][0]["hard_max"] = "5000000"

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad_types.yaml"
            p.write_text(
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaises(BlueprintValidationError) as ctx:
                load_blueprint(p)

        msg = str(ctx.exception)
        for expected_path in (
            "target_total_chars",
            "chapter_chars_range[0]",
            "style.allow_pov_inner_monologue",
            "style.humor",
            "volumes[0].index",
            "scale_rules[0].hard_max",
        ):
            self.assertIn(expected_path, msg)

    def test_load_blueprint_rejects_invalid_optional_volume_and_cast_fields(self) -> None:
        cases = [
            (
                "daily_case_quota=-1",
                lambda r: r["volumes"][0].__setitem__("daily_case_quota", -1),
                "volumes[0].daily_case_quota",
            ),
            (
                "daily_case_quota=True",
                lambda r: r["volumes"][0].__setitem__("daily_case_quota", True),
                "volumes[0].daily_case_quota",
            ),
            (
                "fugitive_case_quota='1'",
                lambda r: r["volumes"][0].__setitem__("fugitive_case_quota", "1"),
                "volumes[0].fugitive_case_quota",
            ),
            (
                "fugitive_case_quota=-2",
                lambda r: r["volumes"][0].__setitem__("fugitive_case_quota", -2),
                "volumes[0].fugitive_case_quota",
            ),
            (
                "fugitive_case_quota=False",
                lambda r: r["volumes"][0].__setitem__("fugitive_case_quota", False),
                "volumes[0].fugitive_case_quota",
            ),
            (
                "forbidden_from_prev_volume=['']",
                lambda r: r["volumes"][0].__setitem__("forbidden_from_prev_volume", [""]),
                "volumes[0].forbidden_from_prev_volume[0]",
            ),
            (
                "forbidden_from_prev_volume=['   ']",
                lambda r: r["volumes"][0].__setitem__("forbidden_from_prev_volume", ["   "]),
                "volumes[0].forbidden_from_prev_volume[0]",
            ),
            (
                "forbidden_from_prev_volume='bad'",
                lambda r: r["volumes"][0].__setitem__("forbidden_from_prev_volume", "bad"),
                "volumes[0].forbidden_from_prev_volume",
            ),
            (
                "antagonist_motive=123",
                lambda r: r["volumes"][0].__setitem__("antagonist_motive", 123),
                "volumes[0].antagonist_motive",
            ),
            (
                "new_gameplay=None",
                lambda r: r["volumes"][0].__setitem__("new_gameplay", None),
                "volumes[0].new_gameplay",
            ),
            (
                "mainline_step=['x']",
                lambda r: r["volumes"][0].__setitem__("mainline_step", ["x"]),
                "volumes[0].mainline_step",
            ),
            (
                "stage_arcs='bad'",
                lambda r: r["cast"][0].__setitem__("stage_arcs", "bad"),
                "cast[0].stage_arcs",
            ),
            (
                "stage_arcs=['']",
                lambda r: r["cast"][0].__setitem__("stage_arcs", [""]),
                "cast[0].stage_arcs[0]",
            ),
            (
                "independent_goal=42",
                lambda r: r["cast"][0].__setitem__("independent_goal", 42),
                "cast[0].independent_goal",
            ),
            (
                "flaw_or_setback=True",
                lambda r: r["cast"][0].__setitem__("flaw_or_setback", True),
                "cast[0].flaw_or_setback",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for idx, (label, mutator, expected_path) in enumerate(cases):
                with self.subTest(case=label):
                    raw = _load_valid_raw()
                    mutator(raw)
                    p = tmp_dir / f"bad_opt_{idx}.yaml"
                    p.write_text(
                        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        encoding="utf-8",
                    )
                    with self.assertRaises(BlueprintValidationError) as ctx:
                        load_blueprint(p)
                    self.assertIn(expected_path, str(ctx.exception))


class TestBlueprintValidationRules(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_bp = load_blueprint(FIXTURE_PATH)

    def _assert_invalid(self, bp: BookBlueprint, *expected_paths: str, existing=()) -> BlueprintValidationError:
        with self.assertRaises(BlueprintValidationError) as ctx:
            validate_blueprint(bp, existing_cast_names=existing)
        msg = str(ctx.exception)
        for p in expected_paths:
            self.assertIn(p, msg, f"Expected field path {p!r} in error message:\n{msg}")
        return ctx.exception

    def test_premise_rules(self) -> None:
        bad_premise = replace(
            self.valid_bp.premise,
            logline="  ",
            selling_points=[],
            selling_point_keywords=["算卦", "夜市"],
            benchmark_refs=[BenchmarkRef(title="", why=" ")],
            target_readers="",
        )
        self._assert_invalid(
            replace(self.valid_bp, premise=bad_premise),
            "premise.logline",
            "premise.selling_points",
            "premise.selling_point_keywords",
            "premise.benchmark_refs[0].title",
            "premise.benchmark_refs[0].why",
            "premise.target_readers",
        )

        no_refs = replace(self.valid_bp.premise, benchmark_refs=[])
        self._assert_invalid(replace(self.valid_bp, premise=no_refs), "premise.benchmark_refs")

    def test_target_chars_and_range_and_15_percent_tolerance(self) -> None:
        # 400 chapters * avg(2100, 2500)=2300 -> estimated = 920000
        # Exact 15% boundary: 920000 / 1.15 = 800000 -> deviation = 120000 / 800000 = 0.15 -> passes
        at_boundary = replace(self.valid_bp, target_total_chars=800000)
        validate_blueprint(at_boundary, existing_cast_names=[])

        # Exceeds 15%: 799999 -> deviation > 0.15 -> rejected
        over_boundary = replace(self.valid_bp, target_total_chars=799999)
        self._assert_invalid(over_boundary, "target_total_chars", "chapter_chars_range")

        # Invalid target_total_chars <= 0
        self._assert_invalid(replace(self.valid_bp, target_total_chars=0), "target_total_chars")

        # Invalid chapter_chars_range length / ordering / zero
        self._assert_invalid(replace(self.valid_bp, chapter_chars_range=[2100]), "chapter_chars_range")
        self._assert_invalid(replace(self.valid_bp, chapter_chars_range=[2500, 2500]), "chapter_chars_range")
        self._assert_invalid(replace(self.valid_bp, chapter_chars_range=[0, 2500]), "chapter_chars_range[0]")

    def test_protagonist_and_voice_card_rules(self) -> None:
        bad_protag = replace(
            self.valid_bp.protagonist,
            character_id="missing_protag",
            desire="",
            flaw="",
            arc="",
            voice_card=VoiceCard(speech_patterns="", catchphrases=["口头禅一", "口头禅二"]),
        )
        self._assert_invalid(
            replace(self.valid_bp, protagonist=bad_protag),
            "protagonist.character_id",
            "protagonist.desire",
            "protagonist.flaw",
            "protagonist.arc",
            "protagonist.voice_card.speech_patterns",
            "protagonist.voice_card.catchphrases",
        )

    def test_golden_finger_rules(self) -> None:
        bad_gf = GoldenFingerSpec(
            rules="",
            cost=" ",
            limits="",
            upgrade_path=[GoldenFingerUpgrade(volume=99, unlock="")],
        )
        self._assert_invalid(
            replace(self.valid_bp, golden_finger=bad_gf),
            "golden_finger.rules",
            "golden_finger.cost",
            "golden_finger.limits",
            "golden_finger.upgrade_path[0].volume",
            "golden_finger.upgrade_path[0].unlock",
        )

    def test_scale_rules(self) -> None:
        self._assert_invalid(replace(self.valid_bp, scale_rules=[]), "scale_rules")

        # Scale rule unbounded_fantasy allows both None and positive hard_max
        valid_unbounded = replace(
            self.valid_bp,
            scale_rules=[
                ScaleRule(ledger_key="cash", policy="bounded_realistic", hard_max=5000000, unit="元"),
                ScaleRule(ledger_key="aura", policy="unbounded_fantasy", hard_max=None, unit="分"),
                ScaleRule(ledger_key="rank", policy="unbounded_fantasy", hard_max=999999, unit="阶"),
            ],
        )
        validate_blueprint(valid_unbounded, existing_cast_names=[])

        bad_rules = [
            ScaleRule(ledger_key="cash", policy="bounded_realistic", hard_max=None, unit="元"),
            ScaleRule(ledger_key="followers", policy="bounded_realistic", hard_max=0, unit="人"),
            ScaleRule(ledger_key="power", policy="invalid_policy", hard_max=10, unit="点"),
            ScaleRule(ledger_key="aura", policy="unbounded_fantasy", hard_max=-1, unit="分"),
        ]
        self._assert_invalid(
            replace(self.valid_bp, scale_rules=bad_rules),
            "scale_rules[0].hard_max",
            "scale_rules[1].hard_max",
            "scale_rules[2].policy",
            "scale_rules[3].hard_max",
        )

    def test_volume_count_bounds_and_index_and_chapter_continuity(self) -> None:
        # < 3 volumes
        two_vols = [replace(self.valid_bp.volumes[0]), replace(self.valid_bp.volumes[1])]
        self._assert_invalid(replace(self.valid_bp, volumes=two_vols), "volumes")

        # Exact MAX_VOLUMES (40) boundary passes when total chars align
        forty_vols = []
        for idx in range(1, 41):
            base = self.valid_bp.volumes[0 if idx == 1 else 1]
            forty_vols.append(
                replace(
                    base,
                    index=idx,
                    chapter_start=(idx - 1) * 30 + 1,
                    chapter_end=idx * 30,
                    foreshadows_plant=[],
                    foreshadows_payoff=[],
                    exits=[],
                )
            )
        validate_blueprint(
            replace(self.valid_bp, volumes=forty_vols, target_total_chars=40 * 30 * 2300),
            existing_cast_names=[],
        )

        # > 40 volumes (construct 41 contiguous volumes with aligned target_total_chars)
        forty_one_vols = list(forty_vols) + [
            replace(
                self.valid_bp.volumes[1],
                index=41,
                chapter_start=40 * 30 + 1,
                chapter_end=41 * 30,
                foreshadows_plant=[],
                foreshadows_payoff=[],
                exits=[],
            )
        ]
        err_41 = self._assert_invalid(
            replace(self.valid_bp, volumes=forty_one_vols, target_total_chars=41 * 30 * 2300),
            "volumes",
        )
        self.assertIn("3..40", str(err_41))
        self.assertIn("41", str(err_41))

        # Non-contiguous index + chapter gap + overlap + end < start + start != 1
        vols = list(self.valid_bp.volumes)
        vols[0] = replace(vols[0], index=2, chapter_start=2, chapter_end=40)
        vols[1] = replace(vols[1], index=1, chapter_start=42, chapter_end=100)  # gap (40 -> 42)
        vols[2] = replace(vols[2], chapter_start=100, chapter_end=99)  # overlap (100 <= 100) AND end < start
        self._assert_invalid(
            replace(self.valid_bp, volumes=vols),
            "volumes[0].index",
            "volumes[0].chapter_start",
            "volumes[1].index",
            "volumes[1].chapter_start",
            "volumes[2].chapter_start",
            "volumes[2].chapter_end",
        )

    def test_volume_enters_exits_antagonist_key_scenes_and_map(self) -> None:
        vols = list(self.valid_bp.volumes)
        vols[0] = replace(
            vols[0],
            title="",
            protagonist_goal="",
            antagonist_id="ghost_boss",
            cool_point_modes=[],
            climax="",
            payoff="",
            exits=[VolumeExit(id="unknown_exit", how="")],
            key_scenes=["只有一个场面"],
        )
        vols[1] = replace(
            vols[1],
            enters=[],
            map=VolumeMap(location_id="", name=""),
        )
        vols[2] = replace(
            vols[2],
            enters=["ghost_enter"],
        )
        self._assert_invalid(
            replace(self.valid_bp, volumes=vols),
            "volumes[0].title",
            "volumes[0].protagonist_goal",
            "volumes[0].antagonist_id",
            "volumes[0].cool_point_modes",
            "volumes[0].climax",
            "volumes[0].payoff",
            "volumes[0].exits[0].id",
            "volumes[0].exits[0].how",
            "volumes[0].key_scenes",
            "volumes[1].enters",
            "volumes[1].map.location_id",
            "volumes[1].map.name",
            "volumes[2].enters[0]",
        )

    def test_validate_blueprint_rejects_invalid_optional_volume_and_cast_fields(self) -> None:
        vols = list(self.valid_bp.volumes)
        vols[0] = replace(
            vols[0],
            antagonist_motive=123,  # type: ignore[arg-type]
            new_gameplay=None,  # type: ignore[arg-type]
            mainline_step=["bad"],  # type: ignore[arg-type]
            daily_case_quota=-1,
            fugitive_case_quota="1",  # type: ignore[arg-type]
            forbidden_from_prev_volume=[""],
        )
        vols[1] = replace(
            vols[1],
            daily_case_quota=True,
            fugitive_case_quota=-3,
            forbidden_from_prev_volume="not_a_list",  # type: ignore[arg-type]
        )

        cast = list(self.valid_bp.cast)
        cast[0] = replace(
            cast[0],
            independent_goal=99,  # type: ignore[arg-type]
            flaw_or_setback=False,  # type: ignore[arg-type]
            stage_arcs="bad",  # type: ignore[arg-type]
        )
        cast[1] = replace(
            cast[1],
            stage_arcs=["  "],
        )

        self._assert_invalid(
            replace(self.valid_bp, volumes=vols, cast=cast),
            "volumes[0].antagonist_motive",
            "volumes[0].new_gameplay",
            "volumes[0].mainline_step",
            "volumes[0].daily_case_quota",
            "volumes[0].fugitive_case_quota",
            "volumes[0].forbidden_from_prev_volume[0]",
            "volumes[1].daily_case_quota",
            "volumes[1].fugitive_case_quota",
            "volumes[1].forbidden_from_prev_volume",
            "cast[0].independent_goal",
            "cast[0].flaw_or_setback",
            "cast[0].stage_arcs",
            "cast[1].stage_arcs[0]",
        )

    def test_cast_count_id_uniqueness_volume_lifecycle_and_namer_rules(self) -> None:
        self._assert_invalid(replace(self.valid_bp, cast=[self.valid_bp.cast[0]]), "cast")

        cast = list(self.valid_bp.cast)
        cast[1] = replace(cast[1], id="mc", name="宋海潮", role="", appearance_card="")  # duplicate id + empty role/appearance
        cast[2] = replace(cast[2], name="林晚", first_volume=0, arc="")  # AI blacklist + invalid first_volume + empty arc
        cast[3] = replace(cast[3], name="张龘", first_volume=9)  # rare char + out-of-range first_volume
        cast[4] = replace(cast[4], first_volume=4, exit_volume=2)  # exit < first
        cast[5] = replace(cast[5], name="许青穿", exit_volume=99)  # intra homophone with mc + invalid exit_volume
        cast[6] = replace(
            cast[6],
            voice_card=VoiceCard(speech_patterns="", catchphrases=["一", "二"]),
        )

        self._assert_invalid(
            replace(self.valid_bp, cast=cast),
            "cast[1].id",
            "cast[1].role",
            "cast[1].appearance_card",
            "cast[2].name",
            "cast[2].first_volume",
            "cast[2].arc",
            "cast[3].name",
            "cast[3].first_volume",
            "cast[4].exit_volume",
            "cast[5].name",
            "cast[5].exit_volume",
            "cast[6].voice_card.speech_patterns",
            "cast[6].voice_card.catchphrases",
        )

    def test_intra_book_homophone_collision_blocked(self) -> None:
        cast = list(self.valid_bp.cast)
        cast[3] = replace(cast[3], name="许青穿")  # homophone of cast[0] "许青川"
        self._assert_invalid(replace(self.valid_bp, cast=cast), "cast[3].name")

    def test_cross_book_exact_and_homophone_collisions(self) -> None:
        # Exact name collision with old book four-name set ("唐子墨", "刘敬尧", "冯念慈", "潘怀安")
        for old_name in ("唐子墨", "刘敬尧", "冯念慈", "潘怀安"):
            cast = list(self.valid_bp.cast)
            cast[3] = replace(cast[3], name=old_name)
            err = self._assert_invalid(
                replace(self.valid_bp, cast=cast),
                "cast[3].name",
                existing=OLD_NIGHT_MARKET_CAST,
            )
            self.assertIn(old_name, str(err))

        # Homophone different-hanzi collisions ("汤子墨" vs "唐子墨", "刘净尧" vs "刘敬尧", etc.)
        homophone_cases = [
            ("汤子墨", "唐子墨"),
            ("刘净尧", "刘敬尧"),
            ("冯念词", "冯念慈"),
            ("潘淮安", "潘怀安"),
        ]
        for cand_name, colliding_old in homophone_cases:
            cast = list(self.valid_bp.cast)
            cast[4] = replace(cast[4], name=cand_name)
            err = self._assert_invalid(
                replace(self.valid_bp, cast=cast),
                "cast[4].name",
                existing=(n for n in OLD_NIGHT_MARKET_CAST),  # also verifies generator iterables
            )
            self.assertIn("同音", str(err))
            self.assertIn(colliding_old, str(err))

        # Bare string passed as existing_cast_names must fail loudly rather than iterate chars
        with self.assertRaises(BlueprintValidationError):
            validate_blueprint(self.valid_bp, existing_cast_names="唐子墨")  # type: ignore[arg-type]

    def test_foreshadow_plan_and_volume_references(self) -> None:
        fs_plan = list(self.valid_bp.foreshadow_plan)
        fs_plan[0] = replace(fs_plan[0], plant_volume=5, payoff_volume=2)  # plant > payoff
        fs_plan[1] = replace(fs_plan[1], id="fs_scale_origin", plant_volume=0, payoff_volume=99)  # duplicate id + invalid volumes

        vols = list(self.valid_bp.volumes)
        vols[0] = replace(vols[0], foreshadows_plant=["fs_nonexistent"])
        vols[2] = replace(vols[2], foreshadows_payoff=["fs_missing_payoff"])

        self._assert_invalid(
            replace(self.valid_bp, foreshadow_plan=fs_plan, volumes=vols),
            "foreshadow_plan[0].payoff_volume",
            "foreshadow_plan[1].id",
            "foreshadow_plan[1].plant_volume",
            "foreshadow_plan[1].payoff_volume",
            "volumes[0].foreshadows_plant[0]",
            "volumes[2].foreshadows_payoff[0]",
        )

    def test_ending_style_and_adaptation_rules(self) -> None:
        bad_style = StyleConfig(
            genre_id="unknown_genre_xyz",
            allow_pov_inner_monologue=1,  # type: ignore[arg-type]
            humor="no",  # type: ignore[arg-type]
            narration_rules="",
            sample_text="  ",
        )
        bad_adapt = AdaptationSpec(visual_hook="", protagonist_look="")
        self._assert_invalid(
            replace(self.valid_bp, ending="", style=bad_style, adaptation=bad_adapt),
            "ending",
            "style.genre_id",
            "style.allow_pov_inner_monologue",
            "style.humor",
            "style.narration_rules",
            "style.sample_text",
            "adaptation.visual_hook",
            "adaptation.protagonist_look",
        )

    def test_multiple_errors_aggregated_in_single_exception(self) -> None:
        bad_premise = replace(self.valid_bp.premise, logline="", selling_point_keywords=["算卦"])
        bad_protag = replace(self.valid_bp.protagonist, character_id="nope")
        bad_scales = [ScaleRule(ledger_key="cash", policy="bounded_realistic", hard_max=None, unit="元")]
        vols = list(self.valid_bp.volumes)
        vols[1] = replace(vols[1], chapter_start=45, enters=[])
        cast = list(self.valid_bp.cast)
        cast[2] = replace(cast[2], name="汤子墨")
        cast[3] = replace(cast[3], first_volume=4, exit_volume=2)
        fs_plan = list(self.valid_bp.foreshadow_plan)
        fs_plan[0] = replace(fs_plan[0], plant_volume=4, payoff_volume=2)
        bad_style = replace(self.valid_bp.style, genre_id="missing_genre")
        bad_adapt = replace(self.valid_bp.adaptation, visual_hook="")

        multi_bad = replace(
            self.valid_bp,
            premise=bad_premise,
            target_total_chars=100000,
            protagonist=bad_protag,
            scale_rules=bad_scales,
            volumes=vols,
            cast=cast,
            foreshadow_plan=fs_plan,
            ending="",
            style=bad_style,
            adaptation=bad_adapt,
        )

        err = self._assert_invalid(
            multi_bad,
            "premise.logline",
            "premise.selling_point_keywords",
            "target_total_chars",
            "protagonist.character_id",
            "scale_rules[0].hard_max",
            "volumes[1].chapter_start",
            "volumes[1].enters",
            "cast[2].name",
            "cast[3].exit_volume",
            "foreshadow_plan[0].payoff_volume",
            "ending",
            "style.genre_id",
            "adaptation.visual_hook",
            existing=OLD_NIGHT_MARKET_CAST,
        )
        self.assertGreaterEqual(len(err.errors), 13)


class TestCollectExistingCastNames(unittest.TestCase):
    def test_collect_existing_cast_names_and_exclude_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Book A: directory name matches project_id
            (root / "book_a").mkdir()
            (root / "book_a" / "project.yaml").write_text(
                "project_id: book_a\ntitle: 旧书甲\n", encoding="utf-8"
            )
            (root / "book_a" / "characters.yaml").write_text(
                yaml.safe_dump({"characters": [{"name": "唐子墨"}, {"name": "冯念慈"}]}, allow_unicode=True),
                encoding="utf-8",
            )

            # Book B: folder name differs from project.yaml project_id
            (root / "folder_b").mkdir()
            (root / "folder_b" / "project.yaml").write_text(
                "project_id: real_book_b\ntitle: 旧书乙\n", encoding="utf-8"
            )
            (root / "folder_b" / "characters.yaml").write_text(
                yaml.safe_dump({"characters": [{"name": "刘敬尧"}, {"name": "潘怀安"}]}, allow_unicode=True),
                encoding="utf-8",
            )

            # Book C: only characters.yaml (no project.yaml)
            (root / "book_c").mkdir()
            (root / "book_c" / "characters.yaml").write_text(
                yaml.safe_dump({"characters": [{"name": "金丽娟"}, {"name": "唐子墨"}]}, allow_unicode=True),
                encoding="utf-8",
            )

            # Ignored hidden/staging directories and dirs without characters.yaml
            (root / "_staging").mkdir()
            (root / "_staging" / "characters.yaml").write_text(
                yaml.safe_dump({"characters": [{"name": "临时角"}]}, allow_unicode=True),
                encoding="utf-8",
            )
            (root / "draft_only").mkdir()

            all_names = collect_existing_cast_names(root)
            self.assertEqual(all_names, ["唐子墨", "冯念慈", "金丽娟", "刘敬尧", "潘怀安"])

            excluded_b = collect_existing_cast_names(root, exclude_project_id="real_book_b")
            self.assertEqual(excluded_b, ["唐子墨", "冯念慈", "金丽娟"])

            excluded_c = collect_existing_cast_names(root, exclude_project_id="book_c")
            self.assertEqual(excluded_c, ["唐子墨", "冯念慈", "刘敬尧", "潘怀安"])

    def test_collect_existing_cast_names_rejects_missing_root_and_bad_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(BlueprintValidationError):
                collect_existing_cast_names(root / "does_not_exist")

            (root / "broken").mkdir()
            (root / "broken" / "characters.yaml").write_text(
                yaml.safe_dump({"characters": [{"id": "c1"}]}, allow_unicode=True),
                encoding="utf-8",
            )
            with self.assertRaises(BlueprintValidationError):
                collect_existing_cast_names(root)


if __name__ == "__main__":
    unittest.main()
