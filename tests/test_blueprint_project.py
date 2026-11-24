from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.blueprint import BlueprintValidationError, load_blueprint  # noqa: E402
from xiaoshuo_bot.blueprint_project import (  # noqa: E402
    format_character_speech_style,
    materialize_project,
)
from xiaoshuo_bot.loader import ProjectValidationError, load_project  # noqa: E402


BLUEPRINT_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_valid.yaml"
EXTRAS_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_project_extras.yaml"
OUTLINE_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_project_outline.yaml"

EXPECTED_OUTPUT_FILES = (
    "blueprint.yaml",
    "project.yaml",
    "characters.yaml",
    "assets.yaml",
    "foreshadows.yaml",
    "style_profiles.yaml",
    "outline.yaml",
)


def _load_yaml_dict(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _write_yaml(path: Path, data: object) -> Path:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    return path


class TestMaterializeProjectPositive(unittest.TestCase):
    def test_materialize_project_from_fixtures_and_load_project_roundtrip(self) -> None:
        bp = load_blueprint(BLUEPRINT_FIXTURE)
        extras_raw = _load_yaml_dict(EXTRAS_FIXTURE)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "nanqiao_project"
            result = materialize_project(
                blueprint_path=BLUEPRINT_FIXTURE,
                extras_path=EXTRAS_FIXTURE,
                outline_path=OUTLINE_FIXTURE,
                out_dir=out_dir,
                existing_cast_names=[],
            )
            self.assertEqual(result, out_dir.resolve())

            for fname in EXPECTED_OUTPUT_FILES:
                self.assertTrue((out_dir / fname).is_file(), f"Missing output file: {fname}")

            self.assertEqual(
                (out_dir / "blueprint.yaml").read_bytes(),
                BLUEPRINT_FIXTURE.read_bytes(),
            )
            self.assertEqual(
                (out_dir / "outline.yaml").read_bytes(),
                OUTLINE_FIXTURE.read_bytes(),
            )

            project = load_project(out_dir)
            self.assertEqual(project.meta.project_id, "nanqiao_cause_eye")
            self.assertEqual(project.meta.title, "夜市算命：我的左眼能称因果")
            self.assertEqual(project.meta.genre, "都市脑洞")
            self.assertEqual(project.meta.genre_id, bp.style.genre_id)
            self.assertEqual(project.meta.pen_name, "青川夜市客")
            self.assertEqual(project.meta.max_rewrite_retries, 3)
            self.assertEqual(project.meta.target_word_count, 900000)
            self.assertEqual(project.meta.contract_track, "complete_150k")
            self.assertEqual(project.meta.author_level, "lv0_1")
            self.assertEqual(project.meta.pov_mode, "third_limited")
            self.assertEqual(project.meta.panel_policy, "allow_system_tag")
            self.assertEqual(project.initial_balances["cash"], 500.0)
            self.assertIn("1:cash", project.volume_cap_locks)

            self.assertEqual(list(project.characters.keys()), [m.id for m in bp.cast])
            for member in bp.cast:
                anchor = project.characters[member.id]
                self.assertEqual(anchor.name, member.name)
                self.assertEqual(anchor.role, member.role)
                if member.voice_card.catchphrases:
                    expected_speech = (
                        f"{member.voice_card.speech_patterns}；口头禅："
                        f"{'、'.join(member.voice_card.catchphrases)}"
                    )
                else:
                    expected_speech = f"{member.voice_card.speech_patterns}；口头禅：无"
                self.assertEqual(anchor.speech_style, expected_speech)
                self.assertEqual(format_character_speech_style(member.voice_card), expected_speech)

                char_extra = extras_raw["characters"][member.id]
                self.assertEqual(anchor.personality, char_extra["personality"])
                self.assertEqual(anchor.behavioral_anchors, char_extra["behavioral_anchors"])
                self.assertEqual(anchor.forbidden_ooc, char_extra["forbidden_ooc"])
                self.assertEqual(anchor.initial_status, char_extra["initial_status"])
                self.assertEqual(anchor.faction, char_extra["faction"])
                self.assertEqual(anchor.relations, char_extra["relations"])

            self.assertIn("book_style", project.style_profiles)
            book_style = project.style_profiles["book_style"]
            self.assertEqual(book_style.id, "book_style")
            self.assertEqual(book_style.name, extras_raw["project_meta"]["title"])
            self.assertEqual(book_style.granularity_notes, bp.style.narration_rules)
            self.assertEqual(book_style.sample_text, bp.style.sample_text.strip())

            self.assertEqual(len(project.assets), 4)
            self.assertEqual(len(project.foreshadows), 2)
            self.assertEqual([s.chapter for s in project.scenes], [1, 2, 3])

    def test_empty_existing_out_dir_allowed_and_omitted_optionals_not_defaulted(self) -> None:
        extras = _load_yaml_dict(EXTRAS_FIXTURE)
        for opt_key in (
            "tags",
            "target_word_count",
            "initial_balances",
            "volume_cap_locks",
            "alternate_titles",
            "primary_category",
            "volume_index",
            "is_final_volume",
        ):
            extras["project_meta"].pop(opt_key, None)
        extras["assets"] = []
        extras["foreshadows"] = []

        outline = _load_yaml_dict(OUTLINE_FIXTURE)
        for scene in outline["scenes"]:
            scene["active_assets"] = []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            out_dir = tmp_dir / "preexisting_empty"
            out_dir.mkdir()
            extras_path = _write_yaml(tmp_dir / "extras.yaml", extras)
            outline_path = _write_yaml(tmp_dir / "outline.yaml", outline)

            materialize_project(
                blueprint_path=BLUEPRINT_FIXTURE,
                extras_path=extras_path,
                outline_path=outline_path,
                out_dir=out_dir,
                existing_cast_names=[],
            )

            raw_project_yaml = _load_yaml_dict(out_dir / "project.yaml")
            self.assertNotIn("tags", raw_project_yaml)
            self.assertNotIn("alternate_titles", raw_project_yaml)
            self.assertNotIn("target_word_count", raw_project_yaml)
            self.assertNotIn("initial_balances", raw_project_yaml)
            self.assertNotIn("volume_cap_locks", raw_project_yaml)
            self.assertEqual(raw_project_yaml["title"], "夜市算命：我的左眼能称因果")

            loaded = load_project(out_dir)
            self.assertEqual(loaded.assets, {})
            self.assertEqual(loaded.foreshadows, {})

    def test_existing_cast_names_is_required_and_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "should_not_exist"
            with self.assertRaises(TypeError):
                materialize_project(  # type: ignore[call-arg]
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=EXTRAS_FIXTURE,
                    outline_path=OUTLINE_FIXTURE,
                    out_dir=out_dir,
                )

            with self.assertRaises(BlueprintValidationError) as ctx:
                materialize_project(
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=EXTRAS_FIXTURE,
                    outline_path=OUTLINE_FIXTURE,
                    out_dir=out_dir,
                    existing_cast_names=["许青川"],
                )
            self.assertIn("许青川", str(ctx.exception))
            self.assertFalse(out_dir.exists())


class TestMaterializeProjectValidationErrors(unittest.TestCase):
    def _materialize_with_mutated_extras(self, mutate_fn) -> tuple[BlueprintValidationError, Path]:
        extras = copy.deepcopy(_load_yaml_dict(EXTRAS_FIXTURE))
        mutate_fn(extras)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            extras_path = _write_yaml(tmp_dir / "extras.yaml", extras)
            out_dir = tmp_dir / "out"
            with self.assertRaises(BlueprintValidationError) as ctx:
                materialize_project(
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=extras_path,
                    outline_path=OUTLINE_FIXTURE,
                    out_dir=out_dir,
                    existing_cast_names=[],
                )
            self.assertFalse(
                out_dir.exists(),
                "Validation failures in extras must reject before creating/writing out_dir",
            )
            return ctx.exception, out_dir

    def test_missing_character_fields_rejected_without_defaults(self) -> None:
        def drop_personality(extras: dict) -> None:
            del extras["characters"]["mc"]["personality"]

        err, _ = self._materialize_with_mutated_extras(drop_personality)
        self.assertIn("extras.characters.mc.personality", str(err))

        def drop_multiple_char_fields(extras: dict) -> None:
            del extras["characters"]["detective_ally"]["behavioral_anchors"]
            del extras["characters"]["detective_ally"]["forbidden_ooc"]
            del extras["characters"]["detective_ally"]["initial_status"]
            del extras["characters"]["detective_ally"]["faction"]
            del extras["characters"]["detective_ally"]["relations"]

        err_multi, _ = self._materialize_with_mutated_extras(drop_multiple_char_fields)
        msg = str(err_multi)
        for field in (
            "extras.characters.detective_ally.behavioral_anchors",
            "extras.characters.detective_ally.forbidden_ooc",
            "extras.characters.detective_ally.initial_status",
            "extras.characters.detective_ally.faction",
            "extras.characters.detective_ally.relations",
        ):
            self.assertIn(field, msg)

    def test_missing_or_invalid_project_meta_and_assets_foreshadows_rejected(self) -> None:
        def drop_pen_name_and_retries(extras: dict) -> None:
            del extras["project_meta"]["pen_name"]
            del extras["project_meta"]["max_rewrite_retries"]
            del extras["assets"]
            del extras["foreshadows"]

        err, _ = self._materialize_with_mutated_extras(drop_pen_name_and_retries)
        msg = str(err)
        self.assertIn("extras.project_meta.pen_name", msg)
        self.assertIn("extras.project_meta.max_rewrite_retries", msg)
        self.assertIn("extras.assets", msg)
        self.assertIn("extras.foreshadows", msg)

        def bool_retries(extras: dict) -> None:
            extras["project_meta"]["max_rewrite_retries"] = True

        err_bool, _ = self._materialize_with_mutated_extras(bool_retries)
        self.assertIn("extras.project_meta.max_rewrite_retries", str(err_bool))

        def neg_retries(extras: dict) -> None:
            extras["project_meta"]["max_rewrite_retries"] = -1

        err_neg, _ = self._materialize_with_mutated_extras(neg_retries)
        self.assertIn("extras.project_meta.max_rewrite_retries", str(err_neg))

    def test_characters_extra_id_or_missing_cast_id_rejected(self) -> None:
        def add_extra_id(extras: dict) -> None:
            extras["characters"]["ghost_npc"] = copy.deepcopy(extras["characters"]["mc"])

        err_extra, _ = self._materialize_with_mutated_extras(add_extra_id)
        self.assertIn("ghost_npc", str(err_extra))

        def drop_cast_id(extras: dict) -> None:
            del extras["characters"]["yuan_boheng"]

        err_missing, _ = self._materialize_with_mutated_extras(drop_cast_id)
        self.assertIn("yuan_boheng", str(err_missing))

    def test_genre_id_mismatch_with_blueprint_rejected(self) -> None:
        def mismatch_genre(extras: dict) -> None:
            extras["project_meta"]["genre_id"] = "literary_suspense"

        err, _ = self._materialize_with_mutated_extras(mismatch_genre)
        msg = str(err)
        self.assertIn("genre_id", msg)
        self.assertIn("literary_suspense", msg)
        self.assertIn("urban_brainhole", msg)

    def test_non_empty_out_dir_and_file_out_dir_raise_file_exists_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            non_empty = tmp_dir / "existing_dir"
            non_empty.mkdir()
            sentinel = non_empty / "sentinel.txt"
            sentinel.write_text("keep intact", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                materialize_project(
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=EXTRAS_FIXTURE,
                    outline_path=OUTLINE_FIXTURE,
                    out_dir=non_empty,
                    existing_cast_names=[],
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep intact")
            self.assertEqual([p.name for p in non_empty.iterdir()], ["sentinel.txt"])

            existing_file = tmp_dir / "file_target"
            existing_file.write_text("not a dir", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                materialize_project(
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=EXTRAS_FIXTURE,
                    outline_path=OUTLINE_FIXTURE,
                    out_dir=existing_file,
                    existing_cast_names=[],
                )
            self.assertEqual(existing_file.read_text(encoding="utf-8"), "not a dir")


class TestMaterializeProjectLoadFailureQuarantine(unittest.TestCase):
    def _assert_quarantined_into_failed_dir(self, out_dir: Path) -> None:
        entries = list(out_dir.iterdir())
        self.assertEqual(
            len(entries),
            1,
            f"Expected only one _failed_* quarantine directory in {out_dir}, found: {[p.name for p in entries]}",
        )
        failed_dir = entries[0]
        self.assertTrue(failed_dir.is_dir())
        self.assertTrue(
            failed_dir.name.startswith("_failed_"),
            f"Expected quarantine dir prefix '_failed_', got {failed_dir.name!r}",
        )
        moved_names = {p.name for p in failed_dir.iterdir()}
        self.assertEqual(moved_names, set(EXPECTED_OUTPUT_FILES))
        for fname in EXPECTED_OUTPUT_FILES:
            self.assertFalse((out_dir / fname).exists(), f"{fname} must not remain in {out_dir}")

    def test_invalid_outline_character_moves_written_files_to_failed_dir(self) -> None:
        bad_outline = copy.deepcopy(_load_yaml_dict(OUTLINE_FIXTURE))
        bad_outline["scenes"][0]["active_characters"].append("nonexistent_char_id")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            outline_path = _write_yaml(tmp_dir / "bad_outline.yaml", bad_outline)
            out_dir = tmp_dir / "quarantine_char"

            with self.assertRaises(ProjectValidationError) as ctx:
                materialize_project(
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=EXTRAS_FIXTURE,
                    outline_path=outline_path,
                    out_dir=out_dir,
                    existing_cast_names=[],
                )
            self.assertNotIsInstance(ctx.exception, BlueprintValidationError)
            self.assertIn("nonexistent_char_id", str(ctx.exception))
            self._assert_quarantined_into_failed_dir(out_dir)

    def test_illegal_tomato_title_moves_written_files_to_failed_dir(self) -> None:
        for bad_title in ("《夜市算命》", "这个测试书名故意超过了十五个字的上限"):
            with self.subTest(bad_title=bad_title):
                bad_extras = copy.deepcopy(_load_yaml_dict(EXTRAS_FIXTURE))
                bad_extras["project_meta"]["title"] = bad_title
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_dir = Path(tmp)
                    extras_path = _write_yaml(tmp_dir / "bad_extras.yaml", bad_extras)
                    out_dir = tmp_dir / "quarantine_title"

                    with self.assertRaises(ProjectValidationError) as ctx:
                        materialize_project(
                            blueprint_path=BLUEPRINT_FIXTURE,
                            extras_path=extras_path,
                            outline_path=OUTLINE_FIXTURE,
                            out_dir=out_dir,
                            existing_cast_names=[],
                        )
                    self.assertNotIsInstance(ctx.exception, BlueprintValidationError)
                    self.assertIn("Tomato title", str(ctx.exception))
                    self._assert_quarantined_into_failed_dir(out_dir)

    def test_empty_outline_rejected_prewrite_and_broken_outline_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            empty_outline = tmp_dir / "empty_outline.yaml"
            empty_outline.write_text("   \n", encoding="utf-8")
            out_pre = tmp_dir / "should_not_create"
            with self.assertRaises(BlueprintValidationError) as ctx_empty:
                materialize_project(
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=EXTRAS_FIXTURE,
                    outline_path=empty_outline,
                    out_dir=out_pre,
                    existing_cast_names=[],
                )
            self.assertIn("empty", str(ctx_empty.exception).lower())
            self.assertFalse(out_pre.exists())

            empty_scenes_outline = _write_yaml(tmp_dir / "no_scenes.yaml", {"volume": 1, "scenes": []})
            out_quarantine = tmp_dir / "quarantine_empty_scenes"
            with self.assertRaises(ProjectValidationError) as ctx_scenes:
                materialize_project(
                    blueprint_path=BLUEPRINT_FIXTURE,
                    extras_path=EXTRAS_FIXTURE,
                    outline_path=empty_scenes_outline,
                    out_dir=out_quarantine,
                    existing_cast_names=[],
                )
            self.assertNotIsInstance(ctx_scenes.exception, BlueprintValidationError)
            self.assertIn("at least one scene", str(ctx_scenes.exception))
            self._assert_quarantined_into_failed_dir(out_quarantine)


if __name__ == "__main__":
    unittest.main()
