from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from xiaoshuo_bot.evolution import (
    BookMetrics,
    InvalidAlternateTitleError,
    MetricsNotSettledError,
    MotifWeightStore,
    OutlineAdjustmentDirective,
    PackagingPoolExhaustedError,
    PublishedChapterImmutableError,
    RacingAction,
    RacingDecisionEngine,
    ReaderComment,
    ReviewFeedbackOptimizer,
    assert_chapter_mutable,
    evaluate_book_racing,
    evaluate_project_racing,
)
from xiaoshuo_bot.genres import UnknownGenreError
from xiaoshuo_bot.models import ProjectMeta


class TestRacingDecisionEngine(unittest.TestCase):
    def test_promote_daily_update_on_ch2_and_bookshelf_threshold(self) -> None:
        metrics = BookMetrics(
            book_id="book_promote_1",
            ch2_read_through=0.62,
            bookshelf_rate=0.21,
            click_through_rate=0.19,
            impressions=2500,
            current_title="被裁后我靠账单暴富",
            current_daily_chapters=2,
        )
        decision = evaluate_book_racing(metrics)
        self.assertEqual(decision.action, RacingAction.PROMOTE_DAILY_UPDATE)
        self.assertIsNotNone(decision.promotion)
        assert decision.promotion is not None
        self.assertEqual(decision.promotion.previous_daily_chapters, 2)
        self.assertEqual(decision.promotion.new_daily_chapters, 3)
        self.assertTrue(decision.promotion.signal_long_serial_extension)

    def test_promote_daily_update_on_word100k_completion(self) -> None:
        metrics = BookMetrics(
            book_id="book_promote_100k",
            ch2_read_through=0.56,
            bookshelf_rate=0.18,
            click_through_rate=0.16,
            word100k_completion=0.28,
            impressions=12000,
            current_daily_chapters=3,
        )
        decision = evaluate_book_racing(metrics)
        self.assertEqual(decision.action, RacingAction.PROMOTE_DAILY_UPDATE)
        self.assertIsNotNone(decision.promotion)
        assert decision.promotion is not None
        self.assertEqual(decision.promotion.new_daily_chapters, 4)

    def test_rotate_packaging_pops_valid_title_and_blurb_when_unsigned(self) -> None:
        alt_titles = ["工位刷出百亿后", "分手后我买下整条街"]
        alt_blurbs = ["新版高冲突简介一", "新版高冲突简介二"]
        metrics = BookMetrics(
            book_id="book_rescue_1",
            ch2_read_through=0.61,
            bookshelf_rate=0.12,
            click_through_rate=0.11,
            impressions=3000,
            signed=False,
            current_title="旧平庸书名",
        )
        decision = evaluate_book_racing(
            metrics,
            alternate_titles=alt_titles,
            alternate_blurbs=alt_blurbs,
        )
        self.assertEqual(decision.action, RacingAction.ROTATE_PACKAGING)
        self.assertIsNotNone(decision.packaging_task)
        assert decision.packaging_task is not None
        self.assertEqual(decision.packaging_task.new_title, "工位刷出百亿后")
        self.assertLessEqual(len(decision.packaging_task.new_title), 15)
        self.assertEqual(decision.packaging_task.new_blurb, "新版高冲突简介一")
        self.assertTrue(decision.packaging_task.regenerate_cover)
        self.assertEqual(alt_titles, ["分手后我买下整条街"])
        self.assertEqual(alt_blurbs, ["新版高冲突简介二"])

    def test_rotate_packaging_blocked_when_signed_true(self) -> None:
        alt_titles = ["备用爆款短书名"]
        alt_blurbs = ["备用简介"]
        metrics = BookMetrics(
            book_id="book_signed_1",
            ch2_read_through=0.60,
            bookshelf_rate=0.11,
            click_through_rate=0.10,
            impressions=4000,
            signed=True,
            current_title="已签约锁定书名",
        )
        decision = evaluate_book_racing(
            metrics,
            alternate_titles=alt_titles,
            alternate_blurbs=alt_blurbs,
        )
        self.assertEqual(decision.action, RacingAction.HOLD_OBSERVE)
        self.assertIsNone(decision.packaging_task)
        self.assertEqual(alt_titles, ["备用爆款短书名"])
        self.assertIn("signed=True", decision.reason)

    def test_rotate_packaging_validates_title_length_and_marks(self) -> None:
        metrics = BookMetrics(
            book_id="book_bad_alt",
            ch2_read_through=0.59,
            bookshelf_rate=0.16,
            click_through_rate=0.12,
            impressions=2000,
            signed=False,
        )
        with self.assertRaises(InvalidAlternateTitleError):
            evaluate_book_racing(
                metrics,
                alternate_titles=["这是一个绝对超过了十五个汉字长度限制的测试备用书名"],
            )
        with self.assertRaises(InvalidAlternateTitleError):
            evaluate_book_racing(
                metrics,
                alternate_titles=["《带书名号不合规》"],
            )
        with self.assertRaises(PackagingPoolExhaustedError):
            evaluate_book_racing(metrics, alternate_titles=[])

    def test_evaluate_project_racing_updates_project_meta_when_unsigned(self) -> None:
        meta = ProjectMeta(
            project_id="p_rescue",
            title="平平无奇开局",
            genre="都市脑洞",
            synopsis="原简介",
            default_pov_character="c1",
            contract_track="serial_20k",
            signed=False,
            alternate_titles=["被裁当天到账百亿"],
            alternate_blurbs=["强钩子新简介"],
        )
        metrics = BookMetrics(
            book_id="p_rescue",
            ch2_read_through=0.59,
            bookshelf_rate=0.14,
            click_through_rate=0.13,
            impressions=1800,
        )
        decision = evaluate_project_racing(meta, metrics)
        self.assertEqual(decision.action, RacingAction.ROTATE_PACKAGING)
        self.assertEqual(meta.title, "被裁当天到账百亿")
        self.assertEqual(meta.synopsis, "强钩子新简介")
        self.assertEqual(meta.alternate_titles, [])

    def test_suggest_cut_requires_sufficient_sample_or_debut(self) -> None:
        # Low retention + sufficient impressions -> SUGGEST_CUT
        cut_metrics = BookMetrics(
            book_id="book_cut_1",
            ch2_read_through=0.38,
            bookshelf_rate=0.07,
            click_through_rate=0.11,
            impressions=1500,
        )
        decision = evaluate_book_racing(cut_metrics)
        self.assertEqual(decision.action, RacingAction.SUGGEST_CUT)
        self.assertIsNotNone(decision.cut_recommendation)
        assert decision.cut_recommendation is not None
        self.assertTrue(decision.cut_recommendation.requires_telegram_confirmation)

        # Low 100k completion + debut_reached -> SUGGEST_CUT
        cut_100k = BookMetrics(
            book_id="book_cut_100k",
            ch2_read_through=0.49,
            bookshelf_rate=0.12,
            click_through_rate=0.16,
            word100k_completion=0.08,
            debut_reached=True,
        )
        decision_100k = evaluate_book_racing(cut_100k)
        self.assertEqual(decision_100k.action, RacingAction.SUGGEST_CUT)

        # Low retention but small impressions (< 1000) and not debut -> HOLD_OBSERVE
        early_metrics = BookMetrics(
            book_id="book_early",
            ch2_read_through=0.38,
            bookshelf_rate=0.07,
            impressions=350,
            debut_reached=False,
        )
        early_decision = evaluate_book_racing(early_metrics)
        self.assertEqual(early_decision.action, RacingAction.HOLD_OBSERVE)

    def test_hold_observe_in_normal_middle_band(self) -> None:
        metrics = BookMetrics(
            book_id="book_mid",
            ch2_read_through=0.53,
            bookshelf_rate=0.18,
            click_through_rate=0.17,
            impressions=2200,
        )
        decision = evaluate_book_racing(metrics)
        self.assertEqual(decision.action, RacingAction.HOLD_OBSERVE)
        self.assertIsNone(decision.promotion)
        self.assertIsNone(decision.packaging_task)
        self.assertIsNone(decision.cut_recommendation)

    def test_unsettled_or_none_metrics_raise_strictly_never_zero(self) -> None:
        engine = RacingDecisionEngine()
        unsettled_cases = [
            BookMetrics(book_id="u1", ch2_read_through=None, bookshelf_rate=0.20),
            BookMetrics(book_id="u2", ch2_read_through=0.65, bookshelf_rate=None),
            BookMetrics(book_id="u3", ch2_read_through=0.65, bookshelf_rate=0.22, settled=False),
            BookMetrics(
                book_id="u4",
                ch2_read_through=0.65,
                bookshelf_rate=0.22,
                settlement_status="NOT_SETTLED",
            ),
            BookMetrics(book_id="u5", ch2_read_through="NOT_SETTLED", bookshelf_rate=0.20),
            BookMetrics(book_id="u6", ch2_read_through="--", bookshelf_rate=0.20),
            BookMetrics(book_id="u7", ch2_read_through=float("nan"), bookshelf_rate=0.20),
            BookMetrics(
                book_id="u8",
                ch2_read_through=0.59,
                bookshelf_rate=0.17,
                click_through_rate=None,
            ),
            BookMetrics(
                book_id="u9",
                ch2_read_through=0.35,
                bookshelf_rate=0.05,
                impressions=None,
                debut_reached=False,
            ),
        ]
        for case in unsettled_cases:
            with self.subTest(book_id=case.book_id):
                with self.assertRaises(MetricsNotSettledError):
                    engine.evaluate(case, alternate_titles=["合规短名"])


class TestMotifWeightStore(unittest.TestCase):
    def test_bayesian_update_persistence_and_ranking_across_genres(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store_path = Path(tmpdir) / "motif_weights.json"
            store = MotifWeightStore(store_path)

            # Record outcomes in urban_brainhole
            store.record_outcome(
                "urban_brainhole",
                "被裁反杀",
                "十倍返现账单",
                ch2_read_through=0.66,
                bookshelf_rate=0.24,
            )
            store.record_outcome(
                "urban_brainhole",
                "被裁反杀",
                "十倍返现账单",
                ch2_read_through=0.63,
                bookshelf_rate=0.22,
            )
            store.record_outcome(
                "urban_brainhole",
                "退婚打脸",
                "鉴宝神瞳",
                ch2_read_through=0.40,
                bookshelf_rate=0.08,
            )
            # Record outcome in female genre
            store.record_outcome(
                "ancient_rebirth",
                "灵堂重生",
                "前世账本记忆",
                success=True,
                ch2_read_through=0.64,
            )

            # Verify Beta(1+2, 1+0) = 3/4 = 0.75 vs Beta(1+0, 1+1) = 1/3
            top_mean = store.get_posterior_mean("urban_brainhole", "被裁反杀", "十倍返现账单")
            low_mean = store.get_posterior_mean("urban_brainhole", "退婚打脸", "鉴宝神瞳")
            self.assertAlmostEqual(top_mean, 0.75, places=6)
            self.assertAlmostEqual(low_mean, 1.0 / 3.0, places=6)

            # Verify JSON saved with SHANGHAI_TZ (+08:00) and reloads cleanly
            raw = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertTrue(raw["updated_at"].endswith("+08:00"))

            reloaded = MotifWeightStore(store_path)
            ranked_urban = reloaded.rank_motifs("urban_brainhole")
            self.assertEqual(len(ranked_urban), 2)
            self.assertEqual(ranked_urban[0].opening_motif, "被裁反杀")
            self.assertEqual(ranked_urban[1].opening_motif, "退婚打脸")

            sampled = reloaded.sample_top_motifs(
                "urban_brainhole",
                top_k=2,
                candidates=[
                    ("退婚打脸", "鉴宝神瞳"),
                    ("冷门开局", "签到系统"),
                    ("被裁反杀", "十倍返现账单"),
                ],
                mode="deterministic",
            )
            self.assertEqual(sampled[0].opening_motif, "被裁反杀")
            self.assertEqual(sampled[1].opening_motif, "冷门开局")

            # Check female genre isolation
            ranked_ancient = reloaded.rank_motifs("ancient_rebirth")
            self.assertEqual(len(ranked_ancient), 1)
            self.assertAlmostEqual(ranked_ancient[0].posterior_mean, 2.0 / 3.0, places=6)

    def test_rejects_unknown_genre_and_unsettled_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MotifWeightStore(Path(tmpdir) / "weights.json")
            with self.assertRaises(UnknownGenreError):
                store.record_outcome("nonexistent_genre", "开局", "系统", success=True)
            with self.assertRaises(MetricsNotSettledError):
                store.record_outcome(
                    "apocalypse_hoarding",
                    "极寒重生",
                    "静止空间",
                    ch2_read_through=0.65,
                    settled=False,
                )
            with self.assertRaises(MetricsNotSettledError):
                store.record_outcome(
                    "apocalypse_hoarding",
                    "极寒重生",
                    "静止空间",
                    success=None,
                    ch2_read_through=None,
                )

    def test_record_racing_decision_links_actions_to_posteriors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MotifWeightStore(Path(tmpdir) / "weights.json")
            promote = evaluate_book_racing(
                BookMetrics(
                    book_id="b1",
                    ch2_read_through=0.65,
                    bookshelf_rate=0.22,
                    impressions=2000,
                )
            )
            cut = evaluate_book_racing(
                BookMetrics(
                    book_id="b2",
                    ch2_read_through=0.39,
                    bookshelf_rate=0.06,
                    impressions=2000,
                )
            )
            hold = evaluate_book_racing(
                BookMetrics(
                    book_id="b3",
                    ch2_read_through=0.52,
                    bookshelf_rate=0.17,
                    click_through_rate=0.16,
                    impressions=2000,
                )
            )
            e1 = store.record_racing_decision(
                promote,
                genre_id="xuanhuan_brainhole",
                opening_motif="杂役劈柴",
                golden_finger_type="百倍熟练度",
            )
            e2 = store.record_racing_decision(
                cut,
                genre_id="xuanhuan_brainhole",
                opening_motif="废柴退婚",
                golden_finger_type="老爷爷戒指",
            )
            e3 = store.record_racing_decision(
                hold,
                genre_id="xuanhuan_brainhole",
                opening_motif="杂役劈柴",
                golden_finger_type="百倍熟练度",
            )
            self.assertIsNotNone(e1)
            self.assertIsNotNone(e2)
            self.assertIsNone(e3)
            self.assertGreater(
                store.get_posterior_mean("xuanhuan_brainhole", "杂役劈柴", "百倍熟练度"),
                store.get_posterior_mean("xuanhuan_brainhole", "废柴退婚", "老爷爷戒指"),
            )


class TestReviewFeedbackOptimizer(unittest.TestCase):
    def test_aggregates_comments_into_future_outline_directives(self) -> None:
        optimizer = ReviewFeedbackOptimizer()
        comments = [
            ReaderComment(chapter=4, content="前面铺垫节奏慢了点，有点拖沓", likes=25),
            ReaderComment(chapter=5, content="反派蹦跶太久了还不杀，看着憋屈", likes=40),
            ReaderComment(chapter=6, content="反派蹦跶太久，打脸太慢了", likes=10),
            ReaderComment(chapter=6, content="苏小暖太讨喜了，求多点戏份加戏！", likes=30),
            ReaderComment(chapter=7, content="主角这里千万别圣母心软放过他", likes=5),
        ]
        report = optimizer.optimize(
            "book_101",
            comments,
            max_published_chapter=10,
        )
        self.assertEqual(report.max_published_chapter, 10)
        self.assertGreater(report.target_start_chapter, 10)
        self.assertEqual(report.summary.total_comments, 5)
        self.assertIn("苏小暖", report.summary.mentioned_entities)

        signal_types = [d.signal_type for d in report.directives]
        self.assertIn("RESOLVE_VILLAIN_SOONER", signal_types)
        self.assertIn("ACCELERATE_PACE", signal_types)
        self.assertIn("BOOST_SUPPORTING_CHARACTER", signal_types)
        self.assertIn("ENFORCE_DECISIVE_PROTAGONIST", signal_types)

        for d in report.directives:
            self.assertGreater(d.target_chapter, 10)
            if d.signal_type == "BOOST_SUPPORTING_CHARACTER":
                self.assertIn("苏小暖", d.instruction)

    def test_strict_immutability_guard_blocks_published_chapters(self) -> None:
        optimizer = ReviewFeedbackOptimizer()
        comments = [ReaderComment(chapter=3, content="节奏慢，拖沓", likes=15)]

        # Equal to max_published_chapter -> MUST raise
        with self.assertRaises(PublishedChapterImmutableError):
            optimizer.optimize(
                "book_guard",
                comments,
                max_published_chapter=10,
                target_chapter=10,
            )

        # Less than max_published_chapter -> MUST raise even if comments are empty
        with self.assertRaises(PublishedChapterImmutableError):
            optimizer.optimize(
                "book_guard",
                [],
                max_published_chapter=10,
                target_chapter=5,
            )

        # Multi-target list containing a published chapter -> MUST raise
        with self.assertRaises(PublishedChapterImmutableError):
            optimizer.optimize(
                "book_guard",
                comments,
                max_published_chapter=10,
                target_chapters=[11, 10, 12],
            )

        # Direct directive construction targeting published chapter -> MUST raise
        with self.assertRaises(PublishedChapterImmutableError):
            OutlineAdjustmentDirective(
                directive_id="OPT-BAD",
                book_id="book_guard",
                target_chapter=8,
                max_published_chapter=8,
                signal_type="ACCELERATE_PACE",
                priority=1,
                trigger_keywords=["节奏慢"],
                mention_count=1,
                instruction="违规修改已发布章节",
            )

        # Standalone guard
        assert_chapter_mutable(11, 10)
        with self.assertRaises(PublishedChapterImmutableError):
            assert_chapter_mutable(10, 10)


if __name__ == "__main__":
    unittest.main()
