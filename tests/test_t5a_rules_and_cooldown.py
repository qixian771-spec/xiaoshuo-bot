from __future__ import annotations

import math
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.anchor_cooldown import (
    anchors_used_in,
    cooling_anchors,
    extract_anchor_fragments,
)
from xiaoshuo_bot.checker import AITicChecker, resolve_rhythm_burstiness_check
from xiaoshuo_bot.genres import (
    GENRE_REGISTRY,
    GenreRecipe,
    UnknownGenreError,
    get_genre,
    list_genres,
)
from xiaoshuo_bot.models import CharacterAnchor, ProjectMeta, ProjectValidationError, SceneOutline


def _make_meta(
    genre_id: str = "",
    *,
    pov_mode: str = "third_limited",
    panel_policy: str = "forbid",
    contract_track: str = "serial_20k",
) -> ProjectMeta:
    return ProjectMeta(
        project_id="t5a_test",
        title="测试书名",
        genre="测试题材",
        synopsis="测试简介。",
        default_pov_character="mc",
        pov_mode=pov_mode,
        panel_policy=panel_policy,
        contract_track=contract_track,
        genre_id=genre_id,
    )


def _make_scene(*, chapter: int = 2, min_chars: int = 2050, max_chars: int = 2300) -> SceneOutline:
    return SceneOutline(
        chapter=chapter,
        scene_id=f"ch{chapter:04d}_test",
        internal_title="边界测试章节",
        pov_character="mc",
        active_characters=["mc", "rival"],
        active_assets=[],
        style_profile_ids=[],
        location_time="白天，江城码头仓库",
        events=["当场对账拆穿假单"],
        stop_condition="反派签字画押时停笔",
        min_chars=min_chars,
        max_chars=max_chars,
    )


def _build_clean_prose_exact_chars(target_non_ws_chars: int) -> str:
    """Construct clean prose with exact non-whitespace character count and zero unrelated violations."""
    unit_paras = [
        "陈默把那份旧船运底单平铺在铁皮桌上，指尖顺着蓝黑签字的折痕慢慢划过。",
        "“这页调度单的日期不对，三号驳船当晚根本没出港。”陈默抬头盯住对面的周成。",
        "周成捏着茶杯的手腕往回缩了半寸，桌面上的搪瓷壶盖哐当一声滑进托盘里。",
        "“账房印章都在这里摆着，你凭什么说日期作假？”周成把茶杯重重墩在木桌角。",
    ]
    paras: list[str] = []
    current_len = 0
    idx = 0
    while current_len < target_non_ws_chars:
        candidate = unit_paras[idx % len(unit_paras)]
        idx += 1
        if current_len + len(candidate) <= target_non_ws_chars:
            paras.append(candidate)
            current_len += len(candidate)
        else:
            remain = target_non_ws_chars - current_len
            if remain == 1:
                # Avoid splitting the last paragraph into a 1-char fragment; adjust previous paragraph by trimming 1 char.
                last = paras.pop()
                paras.append(last[:-2] + "。")
                current_len -= 1
                remain = 2
            if remain == 2:
                paras.append("他站。")
            elif remain <= 5:
                paras.append("他点头。"[: remain - 1] + "。")
            else:
                # Pad cleanly inside a short narration+dialogue line without exceeding 120 chars.
                prefix = "“对。”"
                filler = "陈默收起单据" * ((remain - len(prefix)) // 6 + 1)
                tail = prefix + filler[: remain - len(prefix) - 1] + "。"
                paras.append(tail)
            current_len = sum(len(re.sub(r"\s+", "", p)) for p in paras)
    text = "\n\n".join(paras)
    actual = len(re.sub(r"\s+", "", text))
    assert actual == target_non_ws_chars, f"Expected {target_non_ws_chars}, got {actual}"
    return text


class TestT5aGenreSwitches(unittest.TestCase):
    def test_all_11_genres_have_exact_burstiness_and_inner_monologue_switches(self) -> None:
        self.assertEqual(len(GENRE_REGISTRY), 11)
        expected_burstiness_true = {"literary_suspense", "suspense_rules", "ancient_mystery"}
        expected_monologue_false = {"literary_suspense"}

        for recipe in list_genres():
            expected_burst = recipe.genre_id in expected_burstiness_true
            expected_mono = recipe.genre_id not in expected_monologue_false
            self.assertIs(recipe.rhythm_burstiness_check, expected_burst, recipe.genre_id)
            self.assertIs(recipe.allow_pov_inner_monologue, expected_mono, recipe.genre_id)
            serialized = recipe.to_dict()
            self.assertIs(serialized["rhythm_burstiness_check"], expected_burst)
            self.assertIs(serialized["allow_pov_inner_monologue"], expected_mono)

    def test_genre_recipe_new_fields_have_no_defaults(self) -> None:
        sample = get_genre("literary_suspense")
        with self.assertRaises(TypeError):
            GenreRecipe(  # type: ignore[call-arg]
                genre_id="x",
                display_name="x",
                channel="general",
                tomato_primary_category="悬疑脑洞",
                tomato_tags=["a", "b", "c"],
                pov_mode="third_limited",
                panel_policy="forbid",
                golden_word_count_range=(2050, 2300),
                title_templates=sample.title_templates,
                title_examples=sample.title_examples,
                blurb_templates=sample.blurb_templates,
                opening_300w_directive=sample.opening_300w_directive,
                golden_three_beats=sample.golden_three_beats,
                style_profile=sample.style_profile,
                common_sense_card=sample.common_sense_card,
            )


class TestT5aBurstinessSwitchAndLengthTolerance(unittest.TestCase):
    UNIFORM_TEXT = "\n\n".join(
        [
            "陈默左手捏着鉴定书大步走到拍卖台前。陈默抬起右手把支票重重拍在红木桌上。",
            "全场众人瞬间鸦雀无声，齐齐瞪大了双眼。拍卖师看着签字盖章的印泥彻底呆住了。",
            "王总捧着那块残缺的玉佩站在原地发愣。围观人群全都张大了嘴巴，现场一片哗然。",
            "陈默拎起手提箱转身走出拍卖厅大门。门外停着的黑色轿车立刻按响了双闪灯。",
        ]
    )

    def test_explicit_parameter_precedence_and_legacy_defaults(self) -> None:
        urban_meta = _make_meta("urban_brainhole")
        literary_meta = _make_meta("literary_suspense")
        legacy_meta = _make_meta("")
        scene = _make_scene(chapter=2, min_chars=20, max_chars=5000)

        # init=False + scan=True -> enabled (overrides both init and urban_brainhole genre)
        checker_false = AITicChecker(rhythm_burstiness_check=False)
        rep_on = checker_false.scan(
            self.UNIFORM_TEXT,
            scene=scene,
            project_meta=urban_meta,
            rhythm_burstiness_check=True,
        )
        self.assertIn("RHYTHM_BURSTINESS", {v.rule_id for v in rep_on.violations})
        self.assertTrue(rep_on.diagnostics["rhythm_burstiness_check"])

        # init=True + scan=False -> disabled (overrides both init and literary_suspense genre)
        checker_true = AITicChecker(rhythm_burstiness_check=True)
        rep_off = checker_true.scan(
            self.UNIFORM_TEXT,
            scene=scene,
            project_meta=literary_meta,
            rhythm_burstiness_check=False,
        )
        self.assertNotIn("RHYTHM_BURSTINESS", {v.rule_id for v in rep_off.violations})
        self.assertLess(rep_off.diagnostics["burstiness_sigma"], 10.0)
        self.assertEqual(rep_off.diagnostics["burstiness_paragraph_indices"], [])
        self.assertEqual(rep_off.diagnostics["warning_paragraph_indices"], [1, 2])
        self.assertEqual(rep_off.diagnostics["flagged_paragraph_indices"], [1, 2])
        self.assertFalse(rep_off.diagnostics["rhythm_burstiness_check"])

        # init=False alone overrides None/literary_meta
        rep_init_off = checker_false.scan(self.UNIFORM_TEXT, scene=scene, project_meta=literary_meta)
        self.assertNotIn("RHYTHM_BURSTINESS", {v.rule_id for v in rep_init_off.violations})

        # init=True alone overrides urban_meta
        rep_init_on = checker_true.scan(self.UNIFORM_TEXT, scene=scene, project_meta=urban_meta)
        self.assertIn("RHYTHM_BURSTINESS", {v.rule_id for v in rep_init_on.violations})

        # Legacy project (genre_id == "") and project_meta=None keep burstiness check enabled
        default_checker = AITicChecker()
        self.assertTrue(resolve_rhythm_burstiness_check(project_meta=legacy_meta))
        self.assertTrue(resolve_rhythm_burstiness_check(project_meta=None))
        rep_legacy = default_checker.scan(self.UNIFORM_TEXT, scene=scene, project_meta=legacy_meta)
        self.assertIn("RHYTHM_BURSTINESS", {v.rule_id for v in rep_legacy.violations})

        # diagnose() forwards rhythm_burstiness_check identically
        diag_off = checker_true.diagnose(
            self.UNIFORM_TEXT,
            scene=scene,
            project_meta=literary_meta,
            rhythm_burstiness_check=False,
        )
        self.assertFalse(diag_off["rhythm_burstiness_check"])
        self.assertLess(diag_off["burstiness_sigma"], 10.0)
        self.assertEqual(diag_off["burstiness_paragraph_indices"], [])

    def test_unknown_genre_id_raises_unknown_genre_error_and_zero_arg_scan_passes(self) -> None:
        checker = AITicChecker()
        bad_meta = _make_meta("unknown_genre_xyz")
        with self.assertRaises(UnknownGenreError):
            checker.scan("陈默推开玻璃门，将纸条放在桌面边缘。", project_meta=bad_meta)
        with self.assertRaises(UnknownGenreError):
            checker.diagnose("陈默推开玻璃门，将纸条放在桌面边缘。", project_meta=bad_meta)
        with self.assertRaises(UnknownGenreError):
            checker.scan("", project_meta=bad_meta)

        with self.assertRaises(ProjectValidationError):
            AITicChecker(rhythm_burstiness_check="false")  # type: ignore[arg-type]

        zero_arg_rep = AITicChecker().scan("陈默推开铁门，将铜尺摆在柜台边缘。")
        self.assertTrue(zero_arg_rep.passed, f"Unexpected violations: {zero_arg_rep.violations}")

    def test_len_001_and_len_001w_exact_boundaries(self) -> None:
        checker = AITicChecker()
        meta = _make_meta("urban_brainhole")
        min_chars = 2050
        max_chars = 2300
        floor_chars = math.floor(min_chars * 0.85)  # 1742
        self.assertEqual(floor_chars, 1742)
        scene = _make_scene(chapter=2, min_chars=min_chars, max_chars=max_chars)

        # 1. floor - 1 (1741) -> CRITICAL LEN_001, passed=False
        text_crit = _build_clean_prose_exact_chars(floor_chars - 1)
        rep_crit = checker.scan(text_crit, scene=scene, project_meta=meta)
        self.assertFalse(rep_crit.passed)
        crit_ids = {v.rule_id for v in rep_crit.violations if v.severity == "CRITICAL"}
        warn_ids = {v.rule_id for v in rep_crit.violations if v.severity == "WARNING"}
        self.assertIn("LEN_001", crit_ids)
        self.assertNotIn("LEN_001W", warn_ids)

        # 2. exact floor (1742) -> WARNING LEN_001W, passed=True
        text_floor = _build_clean_prose_exact_chars(floor_chars)
        rep_floor = checker.scan(text_floor, scene=scene, project_meta=meta)
        self.assertTrue(rep_floor.passed, f"Unexpected CRITICAL violations: {rep_floor.violations}")
        self.assertIn("LEN_001W", {v.rule_id for v in rep_floor.violations if v.severity == "WARNING"})
        self.assertNotIn("LEN_001", {v.rule_id for v in rep_floor.violations})
        self.assertEqual(rep_floor.diagnostics["warning_paragraph_indices"], [])

        # 3. min_chars - 1 (2049) -> WARNING LEN_001W, passed=True
        text_near_min = _build_clean_prose_exact_chars(min_chars - 1)
        rep_near_min = checker.scan(text_near_min, scene=scene, project_meta=meta)
        self.assertTrue(rep_near_min.passed)
        self.assertIn("LEN_001W", {v.rule_id for v in rep_near_min.violations if v.severity == "WARNING"})
        self.assertNotIn("LEN_001", {v.rule_id for v in rep_near_min.violations})

        # 4. exact min_chars (2050) -> clean pass, neither LEN_001 nor LEN_001W
        text_min = _build_clean_prose_exact_chars(min_chars)
        rep_min = checker.scan(text_min, scene=scene, project_meta=meta)
        self.assertTrue(rep_min.passed)
        self.assertNotIn("LEN_001", {v.rule_id for v in rep_min.violations})
        self.assertNotIn("LEN_001W", {v.rule_id for v in rep_min.violations})

        # 5. max_chars (2300) passes; max_chars + 1 (2301) triggers CRITICAL LEN_002
        text_max = _build_clean_prose_exact_chars(max_chars)
        rep_max = checker.scan(text_max, scene=scene, project_meta=meta)
        self.assertTrue(rep_max.passed)
        self.assertNotIn("LEN_002", {v.rule_id for v in rep_max.violations})

        text_over = _build_clean_prose_exact_chars(max_chars + 1)
        rep_over = checker.scan(text_over, scene=scene, project_meta=meta)
        self.assertFalse(rep_over.passed)
        self.assertIn("LEN_002", {v.rule_id for v in rep_over.violations if v.severity == "CRITICAL"})


class TestT5aAnchorCooldown(unittest.TestCase):
    def setUp(self) -> None:
        self.characters = {
            "police": CharacterAnchor(
                id="police",
                name="冯念慈",
                role="刑侦支队副队长",
                personality="克制务实",
                speech_style="短陈述句",
                behavioral_anchors={
                    "听到玄乎说法时": "把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。",
                    "确认证据成立时": "合上本子，食指关节在封皮上顶一下，直接报出下一步行动时间。",
                },
            ),
            "mc": CharacterAnchor(
                id="mc",
                name="唐子墨",
                role="夜市小道士",
                personality="嘴硬心善",
                speech_style="市井短句",
                behavioral_anchors={
                    "准备开瞳前": "食指中指并拢在左眼尾轻轻一按，喉结上下滚一下，呼吸停半拍。",
                    "短促动作": "点头，微笑。",
                },
            ),
        }

    def test_extract_anchor_fragments_splits_punctuation_and_filters_short_fragments(self) -> None:
        anchor = "把执法记录仪往前推两厘米， 笔尖在笔记本上敲三下。点头! ok, 12345; 食指关节在封皮上顶一下。"
        frags = extract_anchor_fragments(anchor)
        self.assertEqual(
            frags,
            [
                "把执法记录仪往前推两厘米",
                "笔尖在笔记本上敲三下",
                "食指关节在封皮上顶一下",
            ],
        )
        self.assertEqual(extract_anchor_fragments("合上本子，点头！"), [])
        self.assertEqual(extract_anchor_fragments(""), [])

    def test_real_old_book_repetition_hits_and_unrelated_text_no_false_positive(self) -> None:
        repeated_sentence = "冯念慈把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。"
        hits = anchors_used_in(repeated_sentence, self.characters)
        self.assertEqual(hits, {"police": {"听到玄乎说法时"}})

        # Single >=6-char clause match also triggers
        partial_hit = "她没有立刻接话，只是把执法记录仪往前推两厘米。"
        self.assertEqual(anchors_used_in(partial_hit, self.characters), {"police": {"听到玄乎说法时"}})

        # Unrelated text and <6-char coincidence ("合上本子" / "点头") must not false-positive
        unrelated = "冯念慈合上本子，朝门口点了点头，随后转身拿起桌上的茶杯喝了一口水。"
        self.assertEqual(anchors_used_in(unrelated, self.characters), {})

    def test_cooling_anchors_three_chapter_window_and_deterministic_dedup(self) -> None:
        ch1 = "冯念慈把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。"
        ch2 = "唐子墨食指中指并拢在左眼尾轻轻一按，盯住对面的账本。"
        ch3 = (
            "冯念慈把执法记录仪往前推两厘米，接着食指关节在封皮上顶一下，宣布十分钟后出发。"
        )
        ch4 = "街口路灯亮起，众人收好物证离开现场。"
        ch5 = "次日清晨，档案室门外排起长队。"

        # 3 chapters: ch1, ch2, ch3 -> deduplicated deterministic list per cid
        cool_1_to_3 = cooling_anchors([ch1, ch2, ch3], self.characters)
        self.assertEqual(
            cool_1_to_3,
            {
                "police": ["听到玄乎说法时", "确认证据成立时"],
                "mc": ["准备开瞳前"],
            },
        )

        # 4 chapters: window takes last 3 [ch2, ch3, ch4] -> ch1 excluded, ch3 still keeps police
        cool_1_to_4 = cooling_anchors([ch1, ch2, ch3, ch4], self.characters)
        self.assertEqual(
            cool_1_to_4,
            {
                "mc": ["准备开瞳前"],
                "police": ["听到玄乎说法时", "确认证据成立时"],
            },
        )

        # After 3 clean chapters [ch3_clean, ch4, ch5], earlier ch1/ch2 anchors cool off completely
        cool_after_3_clean = cooling_anchors([ch1, ch2, ch4, ch5, "风吹过空荡的走廊。"], self.characters)
        self.assertEqual(cool_after_3_clean, {})


if __name__ == "__main__":
    unittest.main()
