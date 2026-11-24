from __future__ import annotations

import json
import random
from typing import Callable, List
import unittest

from xiaoshuo_bot.judge import (
    ArcVerdict,
    JudgeResponseError,
    PairwiseJudge,
    PairwiseResult,
    SameModelJudgeError,
)
from xiaoshuo_bot.llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse


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
            model=str(request.metadata.get("model") or "fake"),
            provider="fake",
        )


def _pick_side_for_target(request: LLMRequest, target_marker: str) -> str:
    # Locate target_marker under "=== 样本 A ===" vs "=== 样本 B ==="
    prompt = request.user_prompt
    idx_a = prompt.index("=== 样本 A ===")
    idx_b = prompt.index("=== 样本 B ===")
    part_a = prompt[idx_a:idx_b]
    part_b = prompt[idx_b:]
    if target_marker in part_a:
        return "A"
    if target_marker in part_b:
        return "B"
    raise AssertionError("target_marker not found in prompt")


class TestSameModelGuard(unittest.TestCase):
    def test_same_model_slot_and_family_raise_same_model_judge_error(self) -> None:
        provider = _FakeProvider(lambda req, n: "{}")
        blocked_pairs = [
            ("hy4_flash", "hy4_flash"),
            ("wb/hy4flash-free", "hy4_flash"),
            ("hy4_flash", "wb/hy4flash-free"),
            ("wb/hy4flash-free(high)", "hy4_flash"),
            ("wb/hy4", "hy4"),
            ("wb/deepseek-v4.1-flash", "deepseek_v4_1_flash"),
            ("wb/mimo-v2.6-pro", "mimo_v2_6_pro"),
            ("wb/hy4", "hy4_flash"),
            ("hy4", "wb/hy4flash-free"),
            ("deepseek-v4.1-flash", "deepseek-chat"),
            ("gemini-2.5-pro", "gemini-2.5-flash"),
            ("wb/custom-v1", "custom_v1"),
        ]
        for j_model, w_model in blocked_pairs:
            with self.subTest(judge_model=j_model, writer_model=w_model):
                with self.assertRaises(SameModelJudgeError):
                    PairwiseJudge(provider, judge_model=j_model, writer_model=w_model)

    def test_cross_family_models_are_allowed(self) -> None:
        provider = _FakeProvider(lambda req, n: "{}")
        allowed_pairs = [
            ("wb/deepseek-v4.1-flash", "hy4_flash"),
            ("wb/mimo-v2.6-pro", "wb/hy4flash-free"),
            ("deepseek_v4_1_flash", "mimo_v2_6_pro"),
            ("claude-sonnet-4", "gemini-2.5-pro"),
            ("custom-judge-v1", "custom-writer-v1"),
        ]
        for j_model, w_model in allowed_pairs:
            with self.subTest(judge_model=j_model, writer_model=w_model):
                judge = PairwiseJudge(provider, judge_model=j_model, writer_model=w_model)
                self.assertEqual(judge.judge_model, j_model)

    def test_invalid_init_args_raise_configuration_error(self) -> None:
        provider = _FakeProvider(lambda req, n: "{}")
        with self.assertRaises(LLMConfigurationError):
            PairwiseJudge(None, judge_model="deepseek_v4_1_flash", writer_model="hy4_flash")  # type: ignore[arg-type]
        with self.assertRaises(LLMConfigurationError):
            PairwiseJudge(provider, judge_model="   ", writer_model="hy4_flash")
        with self.assertRaises(LLMConfigurationError):
            PairwiseJudge(provider, judge_model="deepseek_v4_1_flash", writer_model="")


class TestPairwiseCompare(unittest.TestCase):
    def test_consistent_candidate_win_and_reference_win_across_swapped_orders(self) -> None:
        cand = "CANDIDATE_TEXT：门轴一响，他先把铜钱按进桌面裂纹。"
        ref = "REFERENCE_TEXT：今天天气真好，他在屋里坐了一会儿。"

        # Always pick CANDIDATE_TEXT regardless of whether it is A or B
        provider_cand = _FakeProvider(
            lambda req, n: json.dumps(
                {
                    "winner": _pick_side_for_target(req, "CANDIDATE_TEXT"),
                    "reason": f"第{n}轮候选节奏紧凑、开篇动作明确",
                    "drop_risk_point": "第2段略急，可补一句环境细节",
                },
                ensure_ascii=False,
            )
        )
        # Test both initial random directions via different seeds
        for seed in (0, 1, 2, 3):
            judge = PairwiseJudge(
                provider_cand,
                judge_model="wb/deepseek-v4.1-flash",
                writer_model="hy4_flash",
                rng=random.Random(seed),
            )
            res = judge.compare(cand, ref, context={"chapter": 1, "goal": "开篇冲突"})
            self.assertIsInstance(res, PairwiseResult)
            self.assertEqual(res.winner, "candidate")
            self.assertEqual(res.round_1_winner, "candidate")
            self.assertEqual(res.round_2_winner, "candidate")
            self.assertEqual(len(res.raw_responses), 2)
            self.assertTrue(res.reason)
            self.assertTrue(res.drop_risk_point)

        # Verify model metadata & A/B swap across the 2 rounds
        self.assertEqual(provider_cand.requests[0].metadata["model"], "wb/deepseek-v4.1-flash")
        self.assertEqual(
            _pick_side_for_target(provider_cand.requests[0], "CANDIDATE_TEXT")
            != _pick_side_for_target(provider_cand.requests[1], "CANDIDATE_TEXT"),
            True,
        )

        # Always pick REFERENCE_TEXT -> winner == "reference" (wrapped in markdown json fence)
        provider_ref = _FakeProvider(
            lambda req, n: "```json\n"
            + json.dumps(
                {
                    "winner": _pick_side_for_target(req, "REFERENCE_TEXT"),
                    "reason": "对照样本人物动机更立得住",
                    "drop_risk_point": "候选样本结尾平淡",
                },
                ensure_ascii=False,
            )
            + "\n```"
        )
        judge_ref = PairwiseJudge(
            provider_ref,
            judge_model="wb/mimo-v2.6-pro",
            writer_model="wb/hy4flash-free",
            rng=random.Random(7),
        )
        res_ref = judge_ref.compare(cand, ref)
        self.assertEqual(res_ref.winner, "reference")
        self.assertEqual(res_ref.round_1_winner, "reference")
        self.assertEqual(res_ref.round_2_winner, "reference")

    def test_position_bias_inconsistent_rounds_resolve_to_tie(self) -> None:
        # Judge blindly picks "A" on BOTH rounds -> one round candidate, one round reference -> tie
        provider_bias = _FakeProvider(
            lambda req, n: json.dumps(
                {
                    "winner": "A",
                    "reason": f"第{n}轮选A",
                    "drop_risk_point": "样本B过渡略干",
                },
                ensure_ascii=False,
            )
        )
        judge = PairwiseJudge(
            provider_bias,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="hy4_flash",
            rng=random.Random(1),
        )
        res = judge.compare("候选正文", "对照正文")
        self.assertEqual(res.winner, "tie")
        self.assertNotEqual(res.round_1_winner, res.round_2_winner)
        self.assertEqual({res.round_1_winner, res.round_2_winner}, {"candidate", "reference"})

    def test_compare_rejects_bad_json_and_missing_fields(self) -> None:
        bad_payloads = [
            "",
            "我觉得A更好",
            "[]",
            json.dumps({"winner": "C", "reason": "ok", "drop_risk_point": "none"}),
            json.dumps({"winner": "tie", "reason": "ok", "drop_risk_point": "none"}),
            json.dumps({"winner": "A", "drop_risk_point": "none"}),
            json.dumps({"winner": "A", "reason": "   ", "drop_risk_point": "none"}),
            json.dumps({"winner": "A", "reason": "ok", "drop_risk_point": ""}),
            json.dumps({"winner": "A", "reason": "ok"}),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                provider = _FakeProvider(lambda req, n, p=payload: p)
                judge = PairwiseJudge(
                    provider,
                    judge_model="wb/deepseek-v4.1-flash",
                    writer_model="hy4_flash",
                )
                with self.assertRaises(JudgeResponseError):
                    judge.compare("候选", "对照")

        # Also fail if round 1 is valid JSON and round 2 is malformed
        provider_r2_bad = _FakeProvider(
            lambda req, n: (
                json.dumps({"winner": "A", "reason": "好", "drop_risk_point": "第2段"})
                if n == 1
                else "not json"
            )
        )
        judge2 = PairwiseJudge(
            provider_r2_bad,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="hy4_flash",
        )
        with self.assertRaises(JudgeResponseError):
            judge2.compare("候选", "对照")

    def test_compare_rejects_empty_inputs(self) -> None:
        provider = _FakeProvider(lambda req, n: "{}")
        judge = PairwiseJudge(
            provider, judge_model="wb/deepseek-v4.1-flash", writer_model="hy4_flash"
        )
        with self.assertRaises(ValueError):
            judge.compare("", "对照")
        with self.assertRaises(ValueError):
            judge.compare("候选", "   ")


class TestArcReview(unittest.TestCase):
    def test_arc_review_pass_and_fail(self) -> None:
        pass_provider = _FakeProvider(
            lambda req, n: json.dumps(
                {
                    "verdict": "PASS",
                    "problems": [],
                    "summary": "十章推进紧凑，伏笔与爽点节拍契合卷卡目标。",
                },
                ensure_ascii=False,
            )
        )
        judge = PairwiseJudge(
            pass_provider,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="hy4_flash",
        )
        verdict_pass = judge.arc_review(
            ["第一章正文。", "第二章正文。"],
            {"index": 1, "protagonist_goal": "站稳夜市摊位"},
        )
        self.assertIsInstance(verdict_pass, ArcVerdict)
        self.assertEqual(verdict_pass.verdict, "PASS")
        self.assertEqual(verdict_pass.problems, [])
        self.assertEqual(pass_provider.requests[0].max_tokens, 8192)

        fail_provider = _FakeProvider(
            lambda req, n: json.dumps(
                {
                    "verdict": "FAIL",
                    "problems": [
                        {
                            "type": "同质化",
                            "chapters": [3, 4, 5],
                            "evidence": "连续三章都在夜市重复打脸同一类找茬混混。",
                        },
                        {
                            "type": "跑偏",
                            "chapters": [6],
                            "evidence": "脱离卷卡主线，突然写长段无关相亲。",
                        },
                    ],
                    "summary": "第3至6章出现结构性同质化与跑偏，需重整章纲。",
                },
                ensure_ascii=False,
            )
        )
        judge_fail = PairwiseJudge(
            fail_provider,
            judge_model="wb/mimo-v2.6-pro",
            writer_model="hy4_flash",
        )
        verdict_fail = judge_fail.arc_review(
            [f"第{i}章正文。" for i in range(1, 7)],
            {"index": 1, "protagonist_goal": "破局"},
        )
        self.assertEqual(verdict_fail.verdict, "FAIL")
        self.assertEqual(len(verdict_fail.problems), 2)
        self.assertEqual(verdict_fail.problems[0]["type"], "同质化")
        self.assertEqual(verdict_fail.problems[1]["type"], "跑偏")

    def test_arc_review_rejects_bad_enums_and_malformed_responses(self) -> None:
        bad_payloads = [
            "非JSON",
            json.dumps({"verdict": "WARN", "problems": [], "summary": "ok"}),
            json.dumps({"verdict": "FAIL", "problems": [], "summary": "fail without problems"}),
            json.dumps(
                {
                    "verdict": "PASS",
                    "problems": [{"type": "跑偏", "chapters": [1], "evidence": "矛盾"}],
                    "summary": "pass with problems",
                }
            ),
            json.dumps(
                {
                    "verdict": "FAIL",
                    "problems": [{"type": "注水流水账", "chapters": [1], "evidence": "不在枚举内"}],
                    "summary": "bad enum",
                }
            ),
            json.dumps(
                {
                    "verdict": "FAIL",
                    "problems": [{"type": "同质化", "chapters": [], "evidence": "缺章号"}],
                    "summary": "empty chapters",
                }
            ),
            json.dumps(
                {
                    "verdict": "FAIL",
                    "problems": [{"type": "同质化", "chapters": [True], "evidence": "bool章号"}],
                    "summary": "bool chapter",
                }
            ),
            json.dumps(
                {
                    "verdict": "FAIL",
                    "problems": [{"type": "同质化", "chapters": [1], "evidence": " "}],
                    "summary": "blank evidence",
                }
            ),
            json.dumps(
                {
                    "verdict": "PASS",
                    "problems": [],
                    "summary": "",
                }
            ),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                provider = _FakeProvider(lambda req, n, p=payload: p)
                judge = PairwiseJudge(
                    provider,
                    judge_model="wb/deepseek-v4.1-flash",
                    writer_model="hy4_flash",
                )
                with self.assertRaises(JudgeResponseError):
                    judge.arc_review(["第一章正文。"], {"index": 1})

    def test_arc_review_validates_inputs(self) -> None:
        provider = _FakeProvider(lambda req, n: "{}")
        judge = PairwiseJudge(
            provider,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="hy4_flash",
        )
        with self.assertRaises(ValueError):
            judge.arc_review([], {"index": 1})
        with self.assertRaises(ValueError):
            judge.arc_review(["   "], {"index": 1})
        with self.assertRaises(ValueError):
            judge.arc_review(["第一章"], {})

    def test_pairwise_judge_retries_once_on_empty_response(self) -> None:
        # Round 1 returns empty once then valid JSON; round 2 returns valid JSON -> total 3 calls
        def _responder(req, n: int) -> str:
            if n == 1:
                return "   "
            winner = "A" if n == 2 else "B"
            return json.dumps(
                {"winner": winner, "reason": "候选节奏更紧凑", "drop_risk_point": "无明显弃书点"},
                ensure_ascii=False,
            )

        provider = _FakeProvider(_responder)
        judge = PairwiseJudge(
            provider,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="mimo_v2_6_pro",
            rng=random.Random(0),
        )
        res = judge.compare("候选正文", "对照正文")
        self.assertIn(res.winner, ("candidate", "reference"))
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(provider.requests[1].metadata.get("empty_response_retry"), 1)

        # Two consecutive empties stop after bounded 1 retry and raise JudgeResponseError
        provider_empty = _FakeProvider(lambda req, n: "")
        judge_fail = PairwiseJudge(
            provider_empty,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="mimo_v2_6_pro",
        )
        with self.assertRaises(JudgeResponseError):
            judge_fail.compare("候选正文", "对照正文")
        self.assertEqual(len(provider_empty.requests), 2)


if __name__ == "__main__":
    unittest.main()
