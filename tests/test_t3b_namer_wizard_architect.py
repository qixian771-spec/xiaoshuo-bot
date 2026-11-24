"""Offline unit tests for T3b:
1. namer.py: remove hardcoded seed=42 in generate_cast while keeping explicit seed deterministic.
2. server/wizard.py: target_total_chars guidance & strict optional target_total_chars validation.
3. story_architect.py: StoryArchitect.draft_blueprint drafting, feedback retries, atomic YAML write & reload.
4. loader.py: remove silent max_rewrite_retries=3 default and enforce strict type/range checks.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest import mock
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.blueprint import BookBlueprint, load_blueprint  # noqa: E402
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse  # noqa: E402
from xiaoshuo_bot.loader import load_project  # noqa: E402
from xiaoshuo_bot.models import CONTRACT_TRACKS, ProjectValidationError  # noqa: E402
from xiaoshuo_bot.namer import CharacterNamer  # noqa: E402
from xiaoshuo_bot.server.stockpile_db import StockpileDB  # noqa: E402
from xiaoshuo_bot.server.wizard import (  # noqa: E402
    MIN_TARGET_TOTAL_CHARS,
    WIZARD_STEPS,
    NewBookWizard,
    WizardError,
    _validate_option,
)
from xiaoshuo_bot.story_architect import StoryArchitect, StoryArchitectError  # noqa: E402


BLUEPRINT_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_valid.yaml"


class _SeqProvider(LLMProvider):
    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        idx = min(len(self.requests) - 1, len(self._responses) - 1)
        return LLMResponse(
            request_id=request.request_id,
            provider="stub",
            model="stub-planner",
            content=self._responses[idx],
            latency_sec=0.01,
        )


def _load_valid_blueprint_dict() -> Dict[str, Any]:
    raw = yaml.safe_load(BLUEPRINT_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _write_minimal_project_dir(
    root: Path,
    *,
    contract_track: str,
    include_retries: bool = True,
    retries_value: Any = 3,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {
        "project_id": f"t3b_{contract_track}",
        "title": "夜市算卦奇案",
        "genre": "都市脑洞",
        "genre_id": "urban_brainhole",
        "synopsis": "测试简介",
        "default_pov_character": "mc",
        "author_level": "lv2_plus" if contract_track == "serial_20k" else "lv0_1",
        "contract_track": contract_track,
        "pov_mode": "third_limited",
        "panel_policy": "allow_system_tag",
    }
    if include_retries:
        meta["max_rewrite_retries"] = retries_value

    files = {
        "project.yaml": meta,
        "characters.yaml": {
            "characters": [
                {
                    "id": "mc",
                    "name": "许青川",
                    "role": "主角",
                    "personality": "冷静",
                    "speech_style": "干脆",
                }
            ]
        },
        "assets.yaml": {"assets": []},
        "foreshadows.yaml": {"foreshadows": []},
        "style_profiles.yaml": {
            "style_profiles": [
                {
                    "id": "fast",
                    "name": "快节奏",
                    "granularity_notes": "短句",
                    "sample_text": "灯泡刚亮，卦摊前就围满了人。",
                }
            ]
        },
        "outline.yaml": {
            "scenes": [
                {
                    "chapter": 1,
                    "scene_id": "ch0001",
                    "internal_title": "开摊",
                    "public_title": "开摊第一卦",
                    "pov_character": "mc",
                    "active_characters": ["mc"],
                    "active_assets": [],
                    "style_profile_ids": ["fast"],
                    "location_time": "夜市傍晚",
                    "events": ["支摊算卦"],
                    "stop_condition": "收款成功",
                    "emotion_polarity": "burst",
                }
            ]
        },
    }
    for fname, payload in files.items():
        (root / fname).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    return root


class TestNamerEntropyAndDeterminism(unittest.TestCase):
    def test_generate_cast_without_seed_does_not_lock_to_seed_42(self) -> None:
        namer = CharacterNamer()
        fixed_42 = namer.generate_cast(8, era="urban_brainhole", channel="male", seed=42)
        runs = [
            namer.generate_cast(8, era="urban_brainhole", channel="male")
            for _ in range(6)
        ]
        # High-entropy OS seeds should vary across calls and not lock onto seed=42
        self.assertGreater(len({tuple(r) for r in runs}), 1)
        self.assertTrue(any(r != fixed_42 for r in runs))
        for cast in runs:
            self.assertEqual(len(cast), 8)
            for idx, name in enumerate(cast):
                namer.validate_name(name, existing_names=cast[:idx])

    def test_generate_cast_uses_secrets_randbits_when_seed_is_none(self) -> None:
        namer = CharacterNamer()
        with mock.patch("xiaoshuo_bot.namer.secrets.randbits", side_effect=[1001, 2002]) as mocked:
            cast_a = namer.generate_cast(5, era="modern", channel="female")
            cast_b = namer.generate_cast(5, era="modern", channel="female")
        self.assertEqual(mocked.call_args_list, [mock.call(64), mock.call(64)])
        self.assertEqual(cast_a, namer.generate_cast(5, era="modern", channel="female", seed=1001))
        self.assertEqual(cast_b, namer.generate_cast(5, era="modern", channel="female", seed=2002))
        self.assertNotEqual(cast_a, cast_b)

    def test_generate_cast_explicit_seed_is_deterministic_and_rejects_bool(self) -> None:
        namer = CharacterNamer()
        c1 = namer.generate_cast(10, era="ancient_rebirth", channel="female", seed=20261124)
        c2 = namer.generate_cast(10, era="ancient_rebirth", channel="female", seed=20261124)
        self.assertEqual(c1, c2)
        for bad_seed in (True, False, "42"):
            with self.subTest(seed=bad_seed):
                with self.assertRaises(ValueError):
                    namer.generate_cast(3, seed=bad_seed)  # type: ignore[arg-type]


class TestWizardTargetTotalCharsScaleStep(unittest.TestCase):
    def test_scale_step_prompt_guides_target_total_chars_without_infinite_extension(self) -> None:
        scale_step = next(s for s in WIZARD_STEPS if s["key"] == "scale")
        step_blob = json.dumps(scale_step, ensure_ascii=False)
        self.assertIn("目标总字数", step_blob)
        self.assertIn("target_total_chars", step_blob)
        self.assertNotIn("300~700+", step_blob)
        self.assertNotIn("自动续卷", step_blob)

        captured_prompts: Dict[str, str] = {}

        def _respond(req: LLMRequest) -> str:
            stage = req.metadata["stage"]
            captured_prompts[stage] = req.user_prompt
            if stage == "wizard_direction":
                opts = [
                    {
                        "label": f"方向{i}",
                        "logline": "夜市摆摊算卦破奇案",
                        "genre_id": "urban_brainhole",
                        "selling_point": "一语断案",
                        "tone": "爽快",
                    }
                    for i in range(3)
                ]
            elif stage == "wizard_protagonist":
                opts = [
                    {
                        "label": f"主角{i}",
                        "persona": "独眼夜市算命摊主",
                        "golden_finger": "因果秤瞳",
                        "cost": "左眼灼痛",
                        "arc": "从小摊主到破局者",
                    }
                    for i in range(3)
                ]
            elif stage == "wizard_style":
                opts = [
                    {
                        "label": f"笔风{i}",
                        "description": "短句快节奏高密对白",
                        "sample": "卦签落地，满桌人瞬间变了脸色。",
                    }
                    for i in range(3)
                ]
            else:
                opts = [
                    {
                        "label": "80万字长篇",
                        "title": "让你算命，你全网连麦抓通缉犯",
                        "author_level": "lv2_plus",
                        "target_chapters": 0,
                        "target_total_chars": 800000,
                        "blurb": "夜市算命一卦千金，直播连麦抓通缉犯，步步收官。",
                    },
                    {
                        "label": "120万字长篇",
                        "title": "一卦千金：首富跪求我别闭麦",
                        "author_level": "lv2_plus",
                        "target_chapters": 0,
                        "target_total_chars": 1200000,
                        "blurb": "从南桥夜市一路杀到省城大局，有始有终完整收网。",
                    },
                    {
                        "label": "18万字完本",
                        "title": "下山摆摊算卦，巡捕房连夜借人",
                        "author_level": "lv0_1",
                        "target_chapters": 80,
                        "target_total_chars": 180000,
                        "blurb": "紧凑悬疑连环案，十八万字高燃完结。",
                    },
                ]
            return json.dumps({"options": opts}, ensure_ascii=False)

        class _FnProv(LLMProvider):
            def generate(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    request_id=request.request_id,
                    provider="stub",
                    model="stub",
                    content=_respond(request),
                )

        with tempfile.TemporaryDirectory() as tmp:
            db = StockpileDB(Path(tmp) / "m.db")
            created: List[Any] = []
            wiz = NewBookWizard(
                db=db,
                provider_fn=lambda: _FnProv(),
                create_book_fn=lambda req: created.append(req) or type("S", (), {"book_id": req.project_id})(),
            )
            view = wiz.start("夜市小摊主直播算命破奇案")
            sid = view["session_id"]
            for _ in range(3):
                view = wiz.answer(sid, {"choice": 0})

            scale_prompt = captured_prompts["wizard_scale"]
            self.assertIn("目标总字数", scale_prompt)
            self.assertIn("80 万字", scale_prompt)
            self.assertIn("120 万字", scale_prompt)
            self.assertIn("150 万字", scale_prompt)
            self.assertIn("target_total_chars", scale_prompt)
            self.assertIn("lv2_plus", scale_prompt)
            self.assertIn("三点十七分", scale_prompt)
            self.assertNotIn("300~700+", scale_prompt)
            self.assertNotIn("自动滚动续卷", scale_prompt)

            self.assertEqual(view["options"][0]["target_total_chars"], 800000)
            self.assertEqual(view["options"][1]["target_total_chars"], 1200000)
            self.assertEqual(view["options"][2]["target_total_chars"], 180000)

            view = wiz.answer(sid, {"choice": 1})
            self.assertEqual(view["status"], "READY_TO_CONFIRM")
            self.assertEqual(view["choices"][3]["option"]["target_total_chars"], 1200000)

            wiz.confirm(sid, pen_name="青川")
            self.assertEqual(len(created), 1)
            self.assertIn("目标总字数：1200000 字", created[0].creative_brief)
            db.close()

    def test_validate_option_scale_target_total_chars_positive_and_negative(self) -> None:
        base_opt = {
            "label": "120万字长篇",
            "title": "一卦算哭首富巡捕连夜借人",
            "author_level": "lv2_plus",
            "target_chapters": 0,
            "blurb": "三段式商业简介测试内容。",
        }
        # Legacy mock without optional target_total_chars remains accepted without inventing a default
        compat = _validate_option("scale", dict(base_opt))
        self.assertNotIn("target_total_chars", compat)

        # Boundary minimum and standard long-form values pass and are preserved
        for ok_val in (MIN_TARGET_TOTAL_CHARS, 180000, 800000, 1200000, 1500000):
            with self.subTest(ok_val=ok_val):
                validated = _validate_option("scale", dict(base_opt, target_total_chars=ok_val))
                self.assertEqual(validated["target_total_chars"], ok_val)

        # Negative / below-minimum / wrong types / bool / None must all raise WizardError
        bad_values: List[Any] = [
            MIN_TARGET_TOTAL_CHARS - 1,
            0,
            -800000,
            True,
            False,
            "800000",
            800000.5,
            None,
        ]
        for bad_val in bad_values:
            with self.subTest(bad_val=bad_val):
                with self.assertRaises(WizardError):
                    _validate_option("scale", dict(base_opt, target_total_chars=bad_val))

        # Bool target_chapters is also rejected
        with self.assertRaises(WizardError):
            _validate_option("scale", dict(base_opt, target_chapters=True))

    def test_wizard_feeds_back_invalid_target_total_chars_and_recovers(self) -> None:
        bad_scale = {
            "options": [
                {
                    "label": f"方案{i}",
                    "title": f"一卦算哭首富巡捕借人{i}",
                    "author_level": "lv2_plus",
                    "target_chapters": 0,
                    "target_total_chars": 50000,  # too small -> must fail validation
                    "blurb": "三段式简介。",
                }
                for i in range(3)
            ]
        }
        good_scale = {
            "options": [
                {
                    "label": f"120万字长篇{i}",
                    "title": f"一卦算哭首富巡捕借人{i}",
                    "author_level": "lv2_plus",
                    "target_chapters": 0,
                    "target_total_chars": 1200000,
                    "blurb": "三段式简介。",
                }
                for i in range(3)
            ]
        }
        provider = _SeqProvider(
            [
                json.dumps(
                    {
                        "options": [
                            {
                                "label": f"方向{i}",
                                "logline": "梗概",
                                "genre_id": "urban_brainhole",
                                "selling_point": "卖点",
                                "tone": "爽快",
                            }
                            for i in range(3)
                        ]
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "options": [
                            {
                                "label": f"主角{i}",
                                "persona": "处境",
                                "golden_finger": "外挂",
                                "cost": "代价",
                                "arc": "弧线",
                            }
                            for i in range(3)
                        ]
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "options": [
                            {"label": f"笔风{i}", "description": "短句", "sample": "风吹过摊布。"}
                            for i in range(3)
                        ]
                    },
                    ensure_ascii=False,
                ),
                json.dumps(bad_scale, ensure_ascii=False),
                json.dumps(good_scale, ensure_ascii=False),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = StockpileDB(Path(tmp) / "m.db")
            wiz = NewBookWizard(db=db, provider_fn=lambda: provider, create_book_fn=lambda req: None)
            view = wiz.start("夜市摆摊算卦破奇案")
            sid = view["session_id"]
            for _ in range(3):
                view = wiz.answer(sid, {"choice": 0})
            self.assertEqual(view["step"]["key"], "scale")
            self.assertEqual(view["options"][0]["target_total_chars"], 1200000)
            self.assertIn("target_total_chars", provider.requests[-1].user_prompt)
            db.close()


class TestStoryArchitectBlueprintDrafting(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_raw = _load_valid_blueprint_dict()
        self.architect = StoryArchitect()

    def test_draft_blueprint_first_pass_writes_yaml_and_reloads(self) -> None:
        provider = _SeqProvider([json.dumps(self.valid_raw, ensure_ascii=False)])
        existing = ["唐子墨", "刘敬尧", "冯念慈"]
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "nested" / "book_blueprint.yaml"
            bp = self.architect.draft_blueprint(
                idea="南桥夜市独眼摊主靠因果秤瞳直播断案",
                genre_id="urban_brainhole",
                target_total_chars=900000,
                existing_cast_names=existing,
                provider=provider,
                benchmark_notes="对标直播算命爆款，强化现实物证闭环",
                constraints="全书必须 6 卷收官，绝不写修仙飞升",
                output_path=out_path,
            )
            self.assertIsInstance(bp, BookBlueprint)
            self.assertEqual(len(provider.requests), 1)
            prompt = provider.requests[0].user_prompt
            self.assertIn("南桥夜市独眼摊主靠因果秤瞳直播断案", prompt)
            self.assertIn("900000", prompt)
            self.assertIn("唐子墨", prompt)
            self.assertIn("对标直播算命爆款", prompt)
            self.assertIn("全书必须 6 卷收官", prompt)

            self.assertTrue(out_path.is_file())
            self.assertEqual(list(out_path.parent.glob("*.tmp")), [])
            reloaded = load_blueprint(out_path)
            self.assertEqual(reloaded.to_dict(), bp.to_dict())
            self.assertEqual(reloaded.to_dict(), self.valid_raw)

    def test_draft_blueprint_retries_on_missing_field_and_homophone_collision_then_succeeds(self) -> None:
        broken = copy.deepcopy(self.valid_raw)
        del broken["ending"]
        del broken["adaptation"]["visual_hook"]
        # "唐紫沫" collides with existing cast name "唐子墨" by toneless pinyin (tang zi mo)
        broken["cast"][0]["name"] = "唐紫沫"

        provider = _SeqProvider(
            [
                json.dumps(broken, ensure_ascii=False),
                json.dumps(self.valid_raw, ensure_ascii=False),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "book_blueprint.yaml"
            bp = self.architect.draft_blueprint(
                idea="南桥夜市算命破案",
                genre_id="urban_brainhole",
                target_total_chars=900000,
                existing_cast_names=["唐子墨", "冯念慈"],
                provider=provider,
                max_retries=3,
                output_path=out_path,
            )
            self.assertEqual(len(provider.requests), 2)
            retry_prompt = provider.requests[1].user_prompt
            self.assertIn("ending", retry_prompt)
            self.assertIn("adaptation.visual_hook", retry_prompt)
            # Round 1 failed at BookBlueprint.from_dict (missing fields); let's also verify collision feedback separately
            self.assertTrue(out_path.is_file())
            self.assertEqual(load_blueprint(out_path).to_dict(), bp.to_dict())

        # Dedicated cross-book homophone collision retry test (passes from_dict, fails validate_blueprint)
        colliding = copy.deepcopy(self.valid_raw)
        colliding["cast"][0]["name"] = "唐紫沫"
        provider_col = _SeqProvider(
            [
                json.dumps(colliding, ensure_ascii=False),
                json.dumps(self.valid_raw, ensure_ascii=False),
            ]
        )
        bp2 = self.architect.draft_blueprint(
            idea="南桥夜市算命破案",
            genre_id="urban_brainhole",
            target_total_chars=900000,
            existing_cast_names=["唐子墨"],
            provider=provider_col,
            max_retries=3,
        )
        self.assertEqual(len(provider_col.requests), 2)
        col_retry_prompt = provider_col.requests[1].user_prompt
        self.assertIn("cast[0].name", col_retry_prompt)
        self.assertIn("同音冲突", col_retry_prompt)
        self.assertEqual(bp2.cast[0].name, "许青川")

    def test_draft_blueprint_exhausted_retries_raises_and_writes_no_partial_file(self) -> None:
        bad1 = copy.deepcopy(self.valid_raw)
        del bad1["ending"]
        bad2 = copy.deepcopy(self.valid_raw)
        bad2["cast"][1]["name"] = "林晚"  # blacklisted AI name

        provider = _SeqProvider(
            [
                json.dumps(bad1, ensure_ascii=False),
                json.dumps(bad2, ensure_ascii=False),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "should_not_exist" / "book_blueprint.yaml"
            with self.assertRaises(StoryArchitectError) as ctx:
                self.architect.draft_blueprint(
                    idea="夜市算卦",
                    genre_id="urban_brainhole",
                    target_total_chars=900000,
                    existing_cast_names=[],
                    provider=provider,
                    max_retries=2,
                    output_path=out_path,
                )
            self.assertEqual(len(provider.requests), 2)
            self.assertIn("2 attempt(s)", str(ctx.exception))
            self.assertIn("cast[1].name", str(ctx.exception))
            self.assertFalse(out_path.exists())
            self.assertFalse(out_path.parent.exists())

    def test_draft_blueprint_rejects_mismatched_genre_id_and_invalid_inputs(self) -> None:
        wrong_genre = copy.deepcopy(self.valid_raw)
        wrong_genre["style"]["genre_id"] = "ancient_rebirth"
        provider = _SeqProvider([json.dumps(wrong_genre, ensure_ascii=False)])
        with self.assertRaises(StoryArchitectError) as ctx:
            self.architect.draft_blueprint(
                idea="夜市算卦",
                genre_id="urban_brainhole",
                target_total_chars=900000,
                existing_cast_names=[],
                provider=provider,
                max_retries=1,
            )
        self.assertIn("style.genre_id", str(ctx.exception))

        # Invalid arguments fail immediately with zero LLM calls
        idle_provider = _SeqProvider([json.dumps(self.valid_raw, ensure_ascii=False)])
        bad_kwargs_list: List[Dict[str, Any]] = [
            {"idea": "   "},
            {"genre_id": "nonexistent_genre"},
            {"target_total_chars": 99999},
            {"target_total_chars": True},
            {"existing_cast_names": "唐子墨"},
            {"existing_cast_names": None},
            {"existing_cast_names": ["唐子墨", ""]},
            {"max_retries": 0},
            {"max_retries": True},
            {"provider": object()},
            {"output_path": "   "},
        ]
        for override in bad_kwargs_list:
            kwargs: Dict[str, Any] = {
                "idea": "有效创意",
                "genre_id": "urban_brainhole",
                "target_total_chars": 900000,
                "existing_cast_names": [],
                "provider": idle_provider,
                "max_retries": 2,
            }
            kwargs.update(override)
            with self.subTest(override=override):
                with self.assertRaises(StoryArchitectError):
                    self.architect.draft_blueprint(**kwargs)
        self.assertEqual(len(idle_provider.requests), 0)


class TestLoaderMaxRewriteRetriesStrictness(unittest.TestCase):
    def test_non_legacy_tracks_missing_max_rewrite_retries_always_raise(self) -> None:
        non_legacy_tracks = [t for t in CONTRACT_TRACKS if t != "legacy"]
        self.assertIn("serial_20k", non_legacy_tracks)
        self.assertIn("complete_150k", non_legacy_tracks)
        with tempfile.TemporaryDirectory() as tmp:
            for track in non_legacy_tracks:
                proj_dir = _write_minimal_project_dir(
                    Path(tmp) / track,
                    contract_track=track,
                    include_retries=False,
                )
                for allow_legacy in (False, True):
                    with self.subTest(track=track, allow_legacy_defaults=allow_legacy):
                        with self.assertRaises(ProjectValidationError) as ctx:
                            load_project(proj_dir, allow_legacy_defaults=allow_legacy)
                        self.assertIn(
                            "project.yaml missing required field: max_rewrite_retries",
                            str(ctx.exception),
                        )

    def test_invalid_max_rewrite_retries_types_and_negative_rejected(self) -> None:
        bad_values: List[Any] = [True, False, -1, -99, "3", 2.5, None]
        with tempfile.TemporaryDirectory() as tmp:
            for idx, bad_val in enumerate(bad_values):
                for track in ("serial_20k", "legacy"):
                    with self.subTest(track=track, bad_val=bad_val):
                        proj_dir = _write_minimal_project_dir(
                            Path(tmp) / f"{track}_{idx}",
                            contract_track=track,
                            include_retries=True,
                            retries_value=bad_val,
                        )
                        with self.assertRaises(ProjectValidationError) as ctx:
                            load_project(proj_dir, allow_legacy_defaults=True)
                        self.assertIn("max_rewrite_retries", str(ctx.exception))

            # Valid non-negative integers (including 0) load cleanly
            for valid_val in (0, 1, 5):
                proj_dir = _write_minimal_project_dir(
                    Path(tmp) / f"valid_{valid_val}",
                    contract_track="serial_20k",
                    include_retries=True,
                    retries_value=valid_val,
                )
                proj = load_project(proj_dir)
                self.assertEqual(proj.meta.max_rewrite_retries, valid_val)

    def test_legacy_track_missing_retries_strict_by_default_and_warns_when_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy_dir = _write_minimal_project_dir(
                Path(tmp) / "legacy_missing",
                contract_track="legacy",
                include_retries=False,
            )
            with self.assertRaises(ProjectValidationError) as ctx:
                load_project(legacy_dir)
            self.assertIn("project.yaml missing required field: max_rewrite_retries", str(ctx.exception))

            with self.assertLogs("xiaoshuo_bot.loader", level="WARNING") as logs:
                proj = load_project(legacy_dir, allow_legacy_defaults=True)
            self.assertEqual(proj.meta.max_rewrite_retries, 3)
            self.assertTrue(any("max_rewrite_retries" in msg for msg in logs.output))

            with self.assertRaises(ProjectValidationError):
                load_project(legacy_dir, allow_legacy_defaults="yes")  # type: ignore[arg-type]

        # Real legacy fixture baiwu_dukou declares max_rewrite_retries=3 and loads cleanly under strict mode
        baiwu = load_project(ROOT / "projects" / "baiwu_dukou")
        self.assertEqual(baiwu.meta.max_rewrite_retries, 3)


if __name__ == "__main__":
    unittest.main()
