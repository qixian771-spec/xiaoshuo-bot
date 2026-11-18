import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.genres import (
    GENRE_REGISTRY,
    GenreRecipe,
    UnknownGenreError,
    get_genre,
    get_genre_ids,
    list_genres,
)
from xiaoshuo_bot.models import StyleProfile
from xiaoshuo_bot.namer import (
    BlacklistedNameError,
    CharacterNamer,
    HomophoneCollisionError,
    NamerError,
    RareCharacterError,
    is_common_simplified_hanzi,
    to_toneless_pinyin,
)
from xiaoshuo_bot.sanitizers import (
    FICTIONALIZATION_MAP,
    InvalidTweetAliasError,
    fictionalize_modern_terms,
    inspect_tweet_alias,
    scan_compliance,
    scan_modern_identifiers,
    scan_redlines,
    scan_symbol_evasion,
    validate_tweet_alias,
)


class TestGenresCatalog(unittest.TestCase):
    EXPECTED_MALE = {
        "urban_brainhole",
        "xuanhuan_brainhole",
        "apocalypse_hoarding",
        "suspense_rules",
        "history_brainhole",
    }
    EXPECTED_FEMALE = {
        "modern_mind_reading",
        "ancient_rebirth",
        "era_7080_wealth",
        "quick_transmigration",
        "ancient_mystery",
    }
    EXPECTED_GENERAL = {"literary_suspense"}

    def test_all_10_plus_1_genres_registered_with_complete_fields(self) -> None:
        self.assertEqual(set(get_genre_ids("male")), self.EXPECTED_MALE)
        self.assertEqual(set(get_genre_ids("female")), self.EXPECTED_FEMALE)
        self.assertEqual(set(get_genre_ids("general")), self.EXPECTED_GENERAL)
        self.assertEqual(
            len(GENRE_REGISTRY),
            len(self.EXPECTED_MALE) + len(self.EXPECTED_FEMALE) + len(self.EXPECTED_GENERAL),
        )

        for recipe in list_genres():
            self.assertIsInstance(recipe, GenreRecipe)
            self.assertTrue(recipe.display_name)
            self.assertIn(recipe.channel, {"male", "female", "general"})
            self.assertTrue(recipe.tomato_primary_category)
            self.assertGreaterEqual(len(recipe.tomato_tags), 3)
            self.assertIn(recipe.pov_mode, {"third_limited", "first_person"})
            self.assertIn(recipe.panel_policy, {"forbid", "allow_system_tag"})
            self.assertEqual(recipe.golden_word_count_range, (2050, 2300))

            self.assertGreaterEqual(len(recipe.title_templates), 3)
            for tpl in recipe.title_templates:
                self.assertLessEqual(len(tpl), 15, f"{recipe.genre_id} template >15: {tpl}")
                self.assertNotIn("《", tpl)
            for ex in recipe.title_examples:
                self.assertLessEqual(len(ex), 15, f"{recipe.genre_id} example >15: {ex}")
                self.assertNotIn("《", ex)

            self.assertGreaterEqual(len(recipe.blurb_templates), 1)
            for blurb in recipe.blurb_templates:
                paragraphs = [p for p in blurb.split("\n\n") if p.strip()]
                self.assertEqual(len(paragraphs), 3, f"{recipe.genre_id} blurb must be 3 paragraphs")

            self.assertGreaterEqual(len(recipe.opening_300w_directive), 20)
            self.assertEqual([b.chapter for b in recipe.golden_three_beats], [1, 2, 3])
            for beat in recipe.golden_three_beats:
                self.assertTrue(beat.opening_hook)
                self.assertTrue(beat.core_conflict)
                self.assertTrue(beat.payoff_or_twist)
                self.assertTrue(beat.chapter_end_hook)

            self.assertIsInstance(recipe.style_profile, StyleProfile)
            self.assertGreaterEqual(len(recipe.style_profile.sample_text), 80)

            card = recipe.common_sense_card
            self.assertTrue(card.domain)
            self.assertGreaterEqual(len(card.rules), 2)
            self.assertTrue(card.disclaimer)
            prompt_block = card.format_prompt_block()
            self.assertIn("题材硬常识防穿帮卡", prompt_block)

    def test_specific_hard_common_sense_anchors(self) -> None:
        era = get_genre("era_7080_wealth")
        block_7080 = era.common_sense_card.format_prompt_block()
        self.assertIn("肉票", block_7080)
        self.assertIn("粮票", block_7080)
        self.assertIn("33 元", block_7080)

        ancient = get_genre("ancient_rebirth")
        block_ancient = ancient.common_sense_card.format_prompt_block()
        self.assertIn("嫡母", block_ancient)
        self.assertIn("姨娘", block_ancient)

        rules = get_genre("suspense_rules")
        self.assertEqual(rules.pov_mode, "first_person")
        self.assertEqual(rules.panel_policy, "allow_system_tag")
        self.assertIn("相信科学", rules.common_sense_card.disclaimer)

        mystery = get_genre("ancient_mystery")
        self.assertIn("洗冤集录", mystery.common_sense_card.format_prompt_block())

    def test_unknown_genre_raises_error_without_fallback(self) -> None:
        with self.assertRaises(UnknownGenreError):
            get_genre("nonexistent_genre")
        with self.assertRaises(UnknownGenreError):
            get_genre("")
        with self.assertRaises(ValueError):
            list_genres(channel="invalid_channel")


class TestCharacterNamer(unittest.TestCase):
    def setUp(self) -> None:
        self.namer = CharacterNamer()

    def test_rejects_all_tomato_ai_blacklisted_names(self) -> None:
        banned = [
            "林晚",
            "苏清浅",
            "沈知意",
            "陆沉",
            "顾寒洲",
            "傅斯年",
            "厉霆琛",
            "裴寂",
            "林默",
            "柳如烟",
            "陆景深",
            # Regex-matched cliché combinations
            "顾夜白",
            "傅寒琛",
            "苏若雪",
            "慕容云风",
        ]
        for name in banned:
            with self.subTest(name=name):
                self.assertTrue(self.namer.is_blacklisted(name))
                with self.assertRaises(BlacklistedNameError):
                    self.namer.validate_name(name)

    def test_rejects_homophone_collision_using_pypinyin(self) -> None:
        self.assertEqual(to_toneless_pinyin("周叙"), ("zhou", "xu"))
        self.assertEqual(to_toneless_pinyin("周旭"), ("zhou", "xu"))

        # Validate statelessly against existing_names
        self.namer.validate_name("周叙")
        with self.assertRaises(HomophoneCollisionError):
            self.namer.validate_name("周旭", existing_names=["周叙"])

        # Another homophone pair
        with self.assertRaises(HomophoneCollisionError):
            self.namer.validate_name("陈紫沫", existing_names=["陈子墨"])

        # Stateful registration blocks future homophone
        stateful = CharacterNamer(existing_names=["周叙"])
        with self.assertRaises(HomophoneCollisionError):
            stateful.register_name("周旭")

    def test_rejects_rare_archaic_traditional_and_non_hanzi_characters(self) -> None:
        rare_or_invalid = [
            "张龘",
            "陈䶮",
            "李昳",
            "王玘",
            "赵彧",
            "刘龑",
            "周赟",
            "張偉",  # Traditional
            "Alex",  # ASCII
            "张3",   # Digit
            "王_敏", # Punctuation
        ]
        for bad_name in rare_or_invalid:
            with self.subTest(name=bad_name):
                with self.assertRaises(RareCharacterError):
                    self.namer.validate_name(bad_name)

        with self.assertRaises(NamerError):
            self.namer.validate_name("陈")
        with self.assertRaises(NamerError):
            self.namer.validate_name("欧阳大明远")

        self.assertTrue(is_common_simplified_hanzi("叙"))
        self.assertTrue(is_common_simplified_hanzi("珺"))
        self.assertFalse(is_common_simplified_hanzi("龘"))
        self.assertFalse(is_common_simplified_hanzi("龍"))

    def test_generates_distinct_realistic_cast_across_eras(self) -> None:
        for era in ("modern", "era_7080_wealth", "ancient_rebirth", "urban_brainhole", "suspense_rules"):
            with self.subTest(era=era):
                cast = self.namer.generate_cast(12, era=era, channel="female" if "rebirth" in era else "male", seed=101)
                self.assertEqual(len(cast), 12)
                pinyins = set()
                for idx, member in enumerate(cast):
                    py = self.namer.validate_name(member, existing_names=cast[:idx])
                    self.assertNotIn(py, pinyins)
                    pinyins.add(py)


class TestSanitizers(unittest.TestCase):
    def test_detects_real_modern_locations_and_law_enforcement_orgs(self) -> None:
        sample = "他从清华大学毕业后，直接去北京市公安局和最高人民法院门口等消息。"
        hits = scan_modern_identifiers(sample)
        matched = {h.matched_text for h in hits}
        self.assertIn("清华大学", matched)
        self.assertIn("北京市", matched)
        self.assertIn("公安局", matched)
        self.assertIn("最高人民法院", matched)

        fictionalized = fictionalize_modern_terms("在上海北京大学门口遇到警察和检察院督察组。")
        self.assertIn("海城", fictionalized)
        self.assertIn("江城大学", fictionalized)
        self.assertIn("巡捕", fictionalized)
        self.assertIn("督察署", fictionalized)
        self.assertIn("大夏", FICTIONALIZATION_MAP.values())

    def test_harmless_time_and_vintage_brand_not_false_positived(self) -> None:
        clean = "北京时间十二点半，她低头看了一眼腕上的上海牌老手表，顺手接过报警电话，察觉窗外有异响。"
        hits = scan_compliance(clean)
        self.assertEqual(hits, [], f"Unexpected false positives: {hits}")

    def test_detects_symbol_insertion_evasion_zero_tolerance(self) -> None:
        evasion_samples = [
            "门口站着两个警*察。",
            "她的肉.体微微发抖。",
            "有人伪造了警_察证件。",
            "凶手涉嫌杀-人案。",
            "走廊里传来警 察的脚步声。",
            "派*出*所今天不开门。",
        ]
        for text in evasion_samples:
            with self.subTest(text=text):
                hits = scan_symbol_evasion(text)
                self.assertGreaterEqual(len(hits), 1, f"Failed to catch evasion in {text!r}")
                self.assertEqual(hits[0].category, "SYMBOL_EVASION")

    def test_detects_high_risk_redline_content(self) -> None:
        redline_samples = [
            "反派扬言要把人碎尸万段。",
            "绝不允许出现未成年被侵犯的描写。",
            "网上流传的制毒配方极其危险。",
        ]
        for text in redline_samples:
            with self.subTest(text=text):
                hits = scan_redlines(text)
                self.assertGreaterEqual(len(hits), 1, f"Failed to catch redline in {text!r}")
                self.assertEqual(hits[0].category, "RED_LINE")

    def test_validate_tweet_alias_strictly_4_to_8_common_simplified_hanzi(self) -> None:
        for valid in ("暗夜迷踪", "重生逆袭记", "七零赶海致富记", "渡口铜表旧案"):
            self.assertEqual(validate_tweet_alias(valid), valid)
            self.assertIsNone(inspect_tweet_alias(valid))

        invalid_cases = [
            "短别名",            # < 4 chars
            "九个汉字的推文新别名", # > 8 chars
            "暗夜·迷踪",         # punctuation
            "重生，逆袭",         # Chinese punctuation
            "重生70年代",         # digits
            "重生VIP记",          # ASCII letters
            "暗夜 迷踪",          # whitespace
            "暗夜迷龘",           # rare char
            "暗夜迷蹤",           # traditional char
            "",                  # empty
        ]
        for bad in invalid_cases:
            with self.subTest(alias=bad):
                self.assertIsNotNone(inspect_tweet_alias(bad))
                with self.assertRaises(InvalidTweetAliasError):
                    validate_tweet_alias(bad)


if __name__ == "__main__":
    unittest.main()
