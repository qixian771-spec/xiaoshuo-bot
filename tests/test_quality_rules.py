from __future__ import annotations

import unittest

from xiaoshuo_bot.models import CharacterAnchor
from xiaoshuo_bot.quality_rules import (
    ANCHOR_MAX_CHAPTER_HITS,
    ANCHOR_MIN_FRAG_LEN,
    ANCHOR_WINDOW,
    SELLING_POINT_MIN_RATIO,
    SELLING_POINT_WINDOW,
    VERBATIM_MIN_OCCURRENCES,
    VERBATIM_MIN_PARA_LEN,
    VERBATIM_WINDOW,
    RuleHit,
    run_l1_checks,
)


def _make_char(
    cid: str,
    name: str,
    anchors: dict[str, str],
) -> CharacterAnchor:
    return CharacterAnchor(
        id=cid,
        name=name,
        role="配角",
        personality="冷静",
        speech_style="短句",
        behavioral_anchors=anchors,
    )


def _base_kwargs() -> dict:
    return {
        "characters": {},
        "blueprint_scale_rules": [],
        "selling_point_keywords": ["算卦"],
        "ledger_balances": {},
    }


class TestQualityRulesConstantsAndValidation(unittest.TestCase):
    def test_module_threshold_constants(self) -> None:
        self.assertEqual(ANCHOR_WINDOW, 10)
        self.assertEqual(ANCHOR_MIN_FRAG_LEN, 10)
        self.assertEqual(ANCHOR_MAX_CHAPTER_HITS, 2)
        self.assertEqual(VERBATIM_WINDOW, 30)
        self.assertEqual(VERBATIM_MIN_PARA_LEN, 15)
        self.assertEqual(VERBATIM_MIN_OCCURRENCES, 2)
        self.assertEqual(SELLING_POINT_WINDOW, 20)
        self.assertAlmostEqual(SELLING_POINT_MIN_RATIO, 0.40)

    def test_strict_parameter_validation_raises_value_error(self) -> None:
        base = _base_kwargs()
        valid_chapters = [(1, "夜市算卦开张第一晚。")]

        with self.assertRaises(ValueError):
            run_l1_checks([], **base)
        with self.assertRaises(ValueError):
            run_l1_checks(None, **base)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run_l1_checks([(1, "甲"), (1, "乙")], **base)
        with self.assertRaises(ValueError):
            run_l1_checks([(True, "甲")], **base)  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            run_l1_checks([(-1, "甲")], **base)
        with self.assertRaises(ValueError):
            run_l1_checks([(1, 123)], **base)  # type: ignore[list-item]

        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "characters": None})  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "characters": {"mc": "not-anchor"}})  # type: ignore[dict-item]

        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "blueprint_scale_rules": None})  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run_l1_checks(
                valid_chapters,
                **{
                    **base,
                    "blueprint_scale_rules": [
                        {"ledger_key": "followers", "policy": "bounded_realistic", "hard_max": 0}
                    ],
                },
            )
        with self.assertRaises(ValueError):
            run_l1_checks(
                valid_chapters,
                **{
                    **base,
                    "blueprint_scale_rules": [
                        {"ledger_key": "followers", "policy": "bounded_realistic", "hard_max": None}
                    ],
                },
            )
        with self.assertRaises(ValueError):
            run_l1_checks(
                valid_chapters,
                **{
                    **base,
                    "blueprint_scale_rules": [
                        {"ledger_key": "followers", "policy": "unknown_policy", "hard_max": 100}
                    ],
                },
            )

        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "selling_point_keywords": []})
        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "selling_point_keywords": ["  "]})
        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "selling_point_keywords": "算卦"})

        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "ledger_balances": None})  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run_l1_checks(valid_chapters, **{**base, "ledger_balances": {"followers": True}})  # type: ignore[dict-item]


class TestQRAnchorRepeat(unittest.TestCase):
    def test_anchor_repeat_triggers_when_gt_2_chapters_in_10_window(self) -> None:
        frag = "右手拇指反复捻过铜钱崩口的毛边"  # 16 chars >= 10
        chars = {
            "mc": _make_char("mc", "林玄", {"被人骂时": f"{frag}，眼神冷下来。"})
        }
        # Out-of-order chapters; hits at 2, 5, 11 (within 10-chapter window [2..11])
        chapters = [
            (12, "算卦继续进行。"),
            (5, f"第5章算卦：{frag}。"),
            (1, "算卦第1章开局。"),
            (11, f"第11章算卦：{frag}。"),
            (2, f"第2章算卦：{frag}。"),
            (25, f"第25章孤立再现：{frag}。"),
        ]
        hits = run_l1_checks(
            chapters,
            characters=chars,
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦"],
            ledger_balances={},
        )
        anchor_hits = [h for h in hits if h.rule_id == "QR_ANCHOR_REPEAT"]
        self.assertEqual(len(anchor_hits), 1)
        hit = anchor_hits[0]
        self.assertIsInstance(hit, RuleHit)
        self.assertEqual(hit.severity, "CRITICAL")
        self.assertEqual(hit.chapters, [2, 5, 11])
        self.assertIn(frag, hit.evidence)
        self.assertIn("林玄", hit.evidence)

    def test_anchor_repeat_negative_boundaries(self) -> None:
        frag_10 = "食指关节在封皮上顶下"  # exact 10 chars
        frag_9 = "九字短动作不算数"  # 8 chars < 10
        chars = {
            "c1": _make_char("c1", "周衡", {"a": f"{frag_10}，{frag_9}。"})
        }
        # frag_10 hits at ch 1, 6, 12 -> [1..10] has {1,6} (2), [6..15] has {6,12} (2), never >2
        chapters = [
            (i, f"第{i}章算卦正常推进。") for i in range(1, 16)
        ]
        for ch_idx in (1, 6, 12):
            chapters[ch_idx - 1] = (ch_idx, f"第{ch_idx}章算卦：{frag_10}。")
        # Short frag (< 10) repeated in every chapter should NOT trigger
        chapters = [(c, txt + f"\n{frag_9}。") for c, txt in chapters]

        hits = run_l1_checks(
            chapters,
            characters=chars,
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦"],
            ledger_balances={},
        )
        self.assertEqual([h for h in hits if h.rule_id == "QR_ANCHOR_REPEAT"], [])


class TestQRVerbatimRepeat(unittest.TestCase):
    def test_verbatim_repeat_triggers_within_30_chapters_and_same_chapter(self) -> None:
        para = "他把执法记录仪往前推两厘米，笔尖在笔记本上敲三下。"  # 25 chars >= 15
        chapters = [(i, f"第{i}章算卦正文。") for i in range(1, 32)]
        chapters[0] = (1, f"第1章算卦。\n{para}")
        chapters[29] = (30, f"第30章算卦。\n{para}")  # 30 - 1 = 29 < 30 -> within window

        hits = run_l1_checks(
            chapters,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦"],
            ledger_balances={},
        )
        v_hits = [h for h in hits if h.rule_id == "QR_VERBATIM_REPEAT"]
        self.assertEqual(len(v_hits), 1)
        self.assertEqual(v_hits[0].severity, "CRITICAL")
        self.assertEqual(v_hits[0].chapters, [1, 30])
        self.assertIn(para[:20], v_hits[0].evidence)

        # Same-chapter twice also triggers
        same_ch_hits = run_l1_checks(
            [(4, f"算卦开始。\n{para}\n中间过渡。\n{para}")],
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦"],
            ledger_balances={},
        )
        self.assertEqual(len([h for h in same_ch_hits if h.rule_id == "QR_VERBATIM_REPEAT"]), 1)

    def test_verbatim_repeat_negative_outside_30_or_short_para(self) -> None:
        para_15 = "一二三四五六七八九十一二三四五"  # 15 chars
        para_14 = "一二三四五六七八九十一二三四"  # 14 chars < 15
        chapters = [(i, f"第{i}章算卦。\n{para_14}") for i in range(1, 35)]
        chapters[0] = (1, f"第1章算卦。\n{para_15}")
        chapters[30] = (31, f"第31章算卦。\n{para_15}")  # 31 - 1 = 30 -> outside 30-chapter window [1..30]

        hits = run_l1_checks(
            chapters,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦"],
            ledger_balances={},
        )
        self.assertEqual([h for h in hits if h.rule_id == "QR_VERBATIM_REPEAT"], [])


class TestQRScale(unittest.TestCase):
    def test_ledger_balance_bounded_realistic_and_unbounded_fantasy(self) -> None:
        rules = [
            {"ledger_key": "followers", "policy": "bounded_realistic", "hard_max": 50000000, "unit": "人"},
            {"ledger_key": "spirit_qi", "policy": "unbounded_fantasy", "hard_max": 1000, "unit": "点"},
        ]
        # Exceeded followers triggers; unbounded_fantasy does not trigger
        hits = run_l1_checks(
            [(1, "夜市算卦正常进行。")],
            characters={},
            blueprint_scale_rules=rules,
            selling_point_keywords=["算卦"],
            ledger_balances={"followers": 6884787000.0, "spirit_qi": 999999999.0},
        )
        scale_hits = [h for h in hits if h.rule_id == "QR_SCALE"]
        self.assertEqual(len(scale_hits), 1)
        self.assertEqual(scale_hits[0].severity, "CRITICAL")
        self.assertIn("followers", scale_hits[0].evidence)

        # Exact boundary hard_max does NOT trigger
        boundary_hits = run_l1_checks(
            [(1, "夜市算卦正常进行。")],
            characters={},
            blueprint_scale_rules=rules,
            selling_point_keywords=["算卦"],
            ledger_balances={"followers": 50000000.0},
        )
        self.assertEqual([h for h in boundary_hits if h.rule_id == "QR_SCALE"], [])

    def test_prose_yi_scale_positive_and_negative_exemptions(self) -> None:
        bad_chapters = [
            (1, "算卦后【直播间在线人数跳到六十亿】。"),
            (2, "算卦后推送条弹出，后台关注数跳涨二亿二千万。"),
            (3, "算卦后屏幕闪出【新增关注一亿二千万】。"),
            (4, "算卦时弹幕里五亿粉丝同时刷屏，直播间十亿观众看呆了。"),
        ]
        bad_hits = [
            h
            for h in run_l1_checks(
                bad_chapters,
                characters={},
                blueprint_scale_rules=[],
                selling_point_keywords=["算卦"],
                ledger_balances={},
            )
            if h.rule_id == "QR_SCALE"
        ]
        self.assertEqual(len(bad_hits), 5)
        self.assertEqual([h.chapters[0] for h in bad_hits], [1, 2, 3, 4, 4])

        # Normal monetary, market-cap, box-office, history, and million-level online counts must NOT trigger
        safe_chapters = [
            (1, "算卦摊前他冷笑：“听见没有？玄学大师开口几十亿。”"),
            (2, "她一直关注着那家百亿市值的上市集团。"),
            (3, "直播间观众看着他当场掏出十亿元买下整栋旧楼。"),
            (4, "他的人气堪比当年的百亿票房影帝，今天在线人数刚破三百万。"),
            (5, "全网观众都在议论那桩百亿大案，教授正关注十亿年前的陨石化石。"),
            (6, "他关注这笔五亿资金的去向，同时留意着百亿资产重组消息。"),
        ]
        safe_hits = [
            h
            for h in run_l1_checks(
                safe_chapters,
                characters={},
                blueprint_scale_rules=[],
                selling_point_keywords=["算卦"],
                ledger_balances={},
            )
            if h.rule_id == "QR_SCALE"
        ]
        self.assertEqual(safe_hits, [])


class TestQRSellingPointDrift(unittest.TestCase):
    def test_selling_point_drift_triggers_below_40_percent_and_passes_at_boundary(self) -> None:
        # 7/20 = 35% < 40% -> trigger
        chapters_fail = [
            (i, "他在夜市摆摊算卦。" if i <= 7 else "今天去菜市场买菜聊天。")
            for i in range(1, 21)
        ]
        hits_fail = run_l1_checks(
            chapters_fail,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦", "观气"],
            ledger_balances={},
        )
        drift_hits = [h for h in hits_fail if h.rule_id == "QR_SELLING_POINT_DRIFT"]
        self.assertEqual(len(drift_hits), 1)
        self.assertEqual(drift_hits[0].severity, "CRITICAL")
        self.assertAlmostEqual(drift_hits[0].value["ratio"], 0.35)

        # 8/20 = 40% >= 40% -> pass
        chapters_pass = [
            (i, "他开瞳观气断案。" if i <= 8 else "今天去菜市场买菜聊天。")
            for i in range(1, 21)
        ]
        hits_pass = run_l1_checks(
            chapters_pass,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦", "观气"],
            ledger_balances={},
        )
        self.assertEqual([h for h in hits_pass if h.rule_id == "QR_SELLING_POINT_DRIFT"], [])

        # Fewer than 20 chapters -> skipped (0 hits)
        short_chapters = [(i, "日常买菜散步。") for i in range(1, 15)]
        hits_short = run_l1_checks(
            short_chapters,
            characters={},
            blueprint_scale_rules=[],
            selling_point_keywords=["算卦"],
            ledger_balances={},
        )
        self.assertEqual([h for h in hits_short if h.rule_id == "QR_SELLING_POINT_DRIFT"], [])


if __name__ == "__main__":
    unittest.main()
