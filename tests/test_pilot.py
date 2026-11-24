"""Offline unit & integration tests for golden-three-chapters pilot runner and CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
import unittest

import yaml

from xiaoshuo_bot.blueprint import BlueprintValidationError, BookBlueprint, load_blueprint
from xiaoshuo_bot.cli import (
    _build_workbuddy_slot_provider,
    _parse_chapters_arg,
    build_parser,
)
from xiaoshuo_bot.judge import (
    JudgeResponseError,
    PairwiseJudge,
    PairwiseResult,
    SameModelJudgeError,
)
from xiaoshuo_bot.llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.loader import load_project
from xiaoshuo_bot.models import NovelProject, format_chapter_filename
from xiaoshuo_bot.opening_rules import (
    OPENING_RUBRIC,
    OpeningItemResult,
    OpeningReviewer,
    OpeningVerdict,
)
from xiaoshuo_bot.pilot import (
    PilotScore,
    load_benchmark_chapters,
    load_case_summaries,
    parse_chapters_arg,
    run_pilot,
    score_chapters,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "blueprint_valid.yaml"

_BENCHMARK_SAMPLE_TEXT = """# 《测试对标书》第1～3章正文（测试夹具）
# 作者：测试作者
# 备注：跳过开头注释行

第1章 脑瘤只剩90天，我靠算命续命
“兄弟，挪挪窝，这儿不让摆摊。”保安敲了敲水泥地。
江枫坐在小马扎上，面色苍白，摊开蓝布专算姻缘。
【缺失：第1章结尾未截图，待补】

第2章 一卦更比一卦狠
金链大哥拍下一万块钱求问家事，江枫只看面相便指出孩子眉骨不肖。
保姆推着老大爷凑上前，也被当场拆穿假结婚图谋房产。

第3章 全网通缉那个算命的
五百万黑卡递到摊前，江枫看表两点五十九准时收摊下班。
#民政局门口惊现神算子，三句话断三段姻缘#
#全网寻找最嚣张打工人#
三个实锤登上热搜，第三天深夜脑海里再次响起新任务提示音。
"""

_CANDIDATE_CH1 = "\n".join(
    [
        "“把你那破布摊收了，南桥夜市今晚不准摆摊！”城管协管一脚踹翻了塑料凳。",
        "许青川扶住歪倒的折叠桌，左眼眼眶一阵尖锐灼痛。",
        "离师父留下的二十万旧债到期只剩七天，还不上就得被断手抵账。",
        "他咬紧牙关忍住视线重影，左眼因果秤瞳缓缓显出淡金色刻度。",
        "【因果秤瞳规则】：每日只算三卦，先扫码后听话，绝不超额开瞳。",
        "夜市入口灯牌下人潮涌动，烤串烟气裹着叫卖声扑面而来。",
        "穿碎花裙的姑娘被闺蜜拉着凑到蓝布摊前，半信半疑地问未婚夫靠不靠谱。",
        "许青川抬眼看清姑娘眼角夫妻宫灰暗，又扫向远处排队买奶茶的西装男人，眉心一皱。",
        "“此为财帛宫蒙尘，意味着你托付之人外强中干、债台高筑，正打算借婚事转嫁债务。”",
        "姑娘想起男友近来屡次借口周转紧张索要彩礼，还催着自己共同签字担保，瞬间脸色惨白。",
        "西装男人听见围观议论快步冲过来把姑娘护到身后，满脸涨红地喝斥算命摊别血口喷人。",
        "许青川指尖轻点桌面铜秤，平静点出对方印堂黑气下压且迁移宫竖纹深陷，上月外地订单早已违约爆雷。",
        "男人额角瞬间渗出冷汗，想起自己伪造银行流水和假房产证糊弄女方家长的底细，双脚僵在原地动弹不得。",
        "围观的夜市食客越聚越多，好几部手机举起来对准两人猛拍，闺蜜急得一把拽住姑娘胳膊提醒她核对账目。",
        "男人手忙脚乱想去夺回斜挎公文包遮掩，越解释声音越发抖，领口早已被冷汗浸透了一大片。",
        "姑娘盯着未婚夫惨白躲闪的脸，把刚才所有的甜言蜜语和催签合同串在一起，整个人如坠冰窖。",
        "未婚夫冲过来想捂住她的嘴，姑娘却猛地夺过公文包拉开拉链翻出借条。",
    ]
)

_CANDIDATE_CH2 = "\n".join(
    [
        "夜市围观群众把假借结婚转债的男人堵在原地，片警很快赶到带走调查。",
        "戴金表的珠宝行老板甩出两万现金塞到摊上，催着许青川先算自家翡翠货源。",
        "许青川连钞票都没碰，只敲了敲牌子强调每日三卦先来后到、绝不插队加价。",
        "他盯着对方左袖旧泥印与面相奸纹，直言这批翡翠全是注胶假货且藏在后备箱。",
        "老板冷汗顺着下巴滴下，想起昨晚刚在城郊黑窝点提货，腿一软差点跪地。",
        "话音未落，穿花袄的保姆搀着白发老伯挤进来，被许青川当众点破图谋房产抵押。",
        "热心大妈一拥而上拦住想溜的保姆，现场瞬间乱成一团。",
    ]
)

_CANDIDATE_CH3 = "\n".join(
    [
        "收摊前最后两分钟，有人加价到五十万求加塞，许青川看了一眼表直接收摊走人。",
        "他回出租屋吃了一碗牛肉面，看着账上刚到账的五万卦金，先还了债主第一笔利息。",
        "次日清晨，夜市直播切片冲上同城热搜，警方通告证实假翡翠团伙与骗房保姆全部落网。",
        "评论区网友起哄喊着自家百亿家业愿出一半求算一卦，许青川关掉手机继续擦拭铜秤。",
        "深夜十一点，门缝下被人塞进一张印着二十年前渡江沉案编号的旧船票。",
    ]
)


class _FakeProvider(LLMProvider):
    def __init__(self, responder: Callable[[LLMRequest, int], object]) -> None:
        self._responder = responder
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        result = self._responder(request, len(self.requests))
        if isinstance(result, LLMResponse):
            return result
        return LLMResponse(
            request_id=request.request_id,
            content=str(result),
            model=str(request.metadata.get("model") or "fake"),
            provider="fake",
        )


class _FakeOpeningReviewer:
    """Offline fake OpeningReviewer returning configurable per-chapter OpeningVerdict."""

    def __init__(self, verdicts_by_chapter: Mapping[int, OpeningVerdict]) -> None:
        self._verdicts = dict(verdicts_by_chapter)
        self.calls: List[Tuple[int, Optional[str]]] = []

    def review(
        self,
        text: str,
        chapter: int,
        *,
        previous_case_summary: Optional[str] = None,
    ) -> OpeningVerdict:
        self.calls.append((chapter, previous_case_summary))
        return self._verdicts[chapter]


class _FakePairwiseJudge:
    """Offline fake PairwiseJudge returning configurable per-chapter PairwiseResult."""

    def __init__(self, winners_by_chapter: Mapping[int, str]) -> None:
        self._winners = dict(winners_by_chapter)
        self.calls: List[Tuple[str, str, Mapping[str, Any]]] = []

    def compare(
        self,
        candidate: str,
        reference: str,
        *,
        context: Mapping[str, Any] | str = "",
    ) -> PairwiseResult:
        ctx_dict = dict(context) if isinstance(context, Mapping) else {"raw": context}
        self.calls.append((candidate, reference, ctx_dict))
        ch = int(ctx_dict["chapter"])
        winner = self._winners[ch]
        return PairwiseResult(
            winner=winner,
            reason=f"第 {ch} 章对比结论：{winner}",
            drop_risk_point=f"第 {ch} 章无明显弃书风险",
            round_1_winner=winner,
            round_2_winner=winner,
            raw_responses=["{}", "{}"],
        )


def _make_opening_verdict(chapter: int, *, fail_item_id: Optional[str] = None) -> OpeningVerdict:
    items: List[OpeningItemResult] = []
    for rubric_item in OPENING_RUBRIC[chapter]:
        passed = rubric_item.id != fail_item_id
        items.append(
            OpeningItemResult(
                id=rubric_item.id,
                passed=passed,
                evidence="原文引文",
                paragraph_index=1 if rubric_item.kind == "positional" else None,
                char_ratio=0.1 if rubric_item.kind == "positional" else None,
                reason="通过" if passed else f"{rubric_item.id} 未达标",
            )
        )
    return OpeningVerdict(chapter=chapter, items=items)


def _build_temp_project(root: Path, *, include_blueprint: bool = True) -> Path:
    proj_dir = root / "pilot_proj"
    proj_dir.mkdir(parents=True, exist_ok=True)

    (proj_dir / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "project_id": "nanqiao_suangua",
                "title": "南桥夜市因果秤",
                "genre": "都市脑洞",
                "synopsis": "南桥夜市独眼算命青年凭因果秤瞳破局还债。",
                "default_pov_character": "mc",
                "max_rewrite_retries": 2,
                "initial_balances": {"cash": 50000, "followers": 12000},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (proj_dir / "characters.yaml").write_text(
        yaml.safe_dump(
            {
                "characters": [
                    {
                        "id": "mc",
                        "name": "许青川",
                        "role": "主角",
                        "personality": "市井克制",
                        "speech_style": "短促带痞气",
                        "behavioral_anchors": {
                            "开瞳": "左眼眼眶微微抽紧并敲响铜秤杆三次确认因果"
                        },
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (proj_dir / "assets.yaml").write_text(
        yaml.safe_dump({"assets": []}, allow_unicode=True), encoding="utf-8"
    )
    (proj_dir / "foreshadows.yaml").write_text(
        yaml.safe_dump({"foreshadows": []}, allow_unicode=True), encoding="utf-8"
    )
    (proj_dir / "style_profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "style_profiles": [
                    {
                        "id": "street",
                        "name": "市井白描",
                        "granularity_notes": "短句强节奏",
                        "sample_text": "风吹起蓝布摊角，铜秤叮当轻响。",
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (proj_dir / "outline.yaml").write_text(
        yaml.safe_dump(
            {
                "volume": 1,
                "scenes": [
                    {
                        "chapter": ch,
                        "scene_id": f"S0{ch}",
                        "internal_title": f"第{ch}章测试场景",
                        "pov_character": "mc",
                        "active_characters": ["mc"],
                        "active_assets": [],
                        "style_profile_ids": ["street"],
                        "location_time": "南桥夜市",
                        "events": [f"第{ch}章事件"],
                        "stop_condition": "停在冲突高点",
                    }
                    for ch in (1, 2, 3)
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if include_blueprint:
        shutil.copy2(BLUEPRINT_FIXTURE, proj_dir / "blueprint.yaml")
    return proj_dir


def _build_opening_json_for_candidate(chapter: int, *, fail_first: bool = False) -> str:
    if chapter == 1:
        items = [
            {
                "id": "OPEN_FIRST_LINE_CONFLICT",
                "found_or_pass": not fail_first,
                "evidence": "“把你那破布摊收了，南桥夜市今晚不准摆摊！”城管协管一脚踹翻了塑料凳。",
                "reason": "首段直接爆发驱赶冲突。" if not fail_first else "未命中首段冲突。",
            },
            {
                "id": "OPEN_STAKE_BY_P4",
                "found_or_pass": True,
                "evidence": "离师父留下的二十万旧债到期只剩七天，还不上就得被断手抵账。",
                "reason": "第3段亮出七天还债断手赌注。",
            },
            {
                "id": "OPEN_RULE_BY_25",
                "found_or_pass": True,
                "evidence": "【因果秤瞳规则】：每日只算三卦，先扫码后听话，绝不超额开瞳。",
                "reason": "前25%内交代因果秤瞳与摊规。",
            },
            {
                "id": "OPEN_FIRST_CLIENT_BY_40",
                "found_or_pass": True,
                "evidence": "穿碎花裙的姑娘被闺蜜拉着凑到蓝布摊前，半信半疑地问未婚夫靠不靠谱。",
                "reason": "前40%内首位求卦者出场。",
            },
            {
                "id": "OPEN_VERDICT_3STEP",
                "found_or_pass": True,
                "evidence": "姑娘想起男友近来屡次借口周转紧张索要彩礼，还催着自己共同签字担保，瞬间脸色惨白。",
                "reason": "完成术语到白话后果到内心印证。",
            },
            {
                "id": "OPEN_END_MID_ACTION",
                "found_or_pass": True,
                "evidence": "未婚夫冲过来想捂住她的嘴，姑娘却猛地夺过公文包拉开拉链翻出借条。",
                "reason": "章末停在翻找借条动作中途。",
            },
        ]
    elif chapter == 2:
        items = [
            {
                "id": "OPEN_VERDICT_3STEP",
                "found_or_pass": not fail_first,
                "evidence": "老板冷汗顺着下巴滴下，想起昨晚刚在城郊黑窝点提货，腿一软差点跪地。",
                "reason": "完成断语三步并引发当事人内心印证。",
            },
            {
                "id": "OPEN_END_MID_ACTION",
                "found_or_pass": True,
                "evidence": "热心大妈一拥而上拦住想溜的保姆，现场瞬间乱成一团。",
                "reason": "章末停在拦截保姆冲突中途。",
            },
            {
                "id": "CASE_DISTINCT",
                "found_or_pass": True,
                "evidence": "他盯着对方左袖旧泥印与面相奸纹，直言这批翡翠全是注胶假货且藏在后备箱。",
                "reason": "本章注胶假翡翠与骗房案明显区别于上一章骗婚转债案。",
            },
            {
                "id": "PROTAGONIST_RESTRAINT",
                "found_or_pass": True,
                "evidence": "许青川连钞票都没碰，只敲了敲牌子强调每日三卦先来后到、绝不插队加价。",
                "reason": "面对两万现金仍守住三卦摊规。",
            },
        ]
    else:
        items = [
            {
                "id": "EXTERNAL_PAYOFF",
                "found_or_pass": not fail_first,
                "evidence": "次日清晨，夜市直播切片冲上同城热搜，警方通告证实假翡翠团伙与骗房保姆全部落网。",
                "reason": "警方通告与热搜实锤印证前案。",
            },
            {
                "id": "NEW_HOOK_END",
                "found_or_pass": True,
                "evidence": "深夜十一点，门缝下被人塞进一张印着二十年前渡江沉案编号的旧船票。",
                "reason": "章末挂出二十年前沉案旧船票新钩子。",
            },
            {
                "id": "SCALE_REALISTIC",
                "found_or_pass": True,
                "evidence": "他回出租屋吃了一碗牛肉面，看着账上刚到账的五万卦金，先还了债主第一笔利息。",
                "reason": "主角自身金额五万元现实有界，百亿仅出现在网友评论。",
            },
        ]
    return json.dumps({"items": items}, ensure_ascii=False)


def _pairwise_json_for_request(req: LLMRequest, desired_winner: str) -> str:
    """Return an A/B/TIE JSON response that maps cleanly to ``desired_winner`` regardless of swap order."""
    if desired_winner == "tie":
        raw_winner = "TIE"
    else:
        _, rest = req.user_prompt.split("=== 样本 A ===", 1)
        sample_a, _ = rest.split("=== 样本 B ===", 1)
        a_is_candidate = "许青川" in sample_a
        if desired_winner == "candidate":
            raw_winner = "A" if a_is_candidate else "B"
        else:
            raw_winner = "B" if a_is_candidate else "A"
    return json.dumps(
        {
            "winner": raw_winner,
            "reason": f"盲评判定 {raw_winner} 节奏更紧。",
            "drop_risk_point": "中段过渡略快，无严重弃书点。",
        },
        ensure_ascii=False,
    )


class TestLoadBenchmarkChapters(unittest.TestCase):
    def test_parses_header_comments_three_chapters_and_strips_missing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench_path = Path(tmp) / "book1_ch1_3.txt"
            bench_path.write_text(_BENCHMARK_SAMPLE_TEXT, encoding="utf-8")

            parsed = load_benchmark_chapters(bench_path, [1, 2, 3])
            self.assertEqual(list(parsed.keys()), [1, 2, 3])
            self.assertNotIn("# 《测试对标书》", parsed[1])
            self.assertNotIn("第1章 ", parsed[1])
            self.assertNotIn("【缺失：", parsed[1])
            self.assertIn("保安敲了敲水泥地", parsed[1])
            self.assertIn("金链大哥拍下一万块钱", parsed[2])
            # Hashtags inside chapter 3 body must NOT be stripped as header comments.
            self.assertIn("#民政局门口惊现神算子，三句话断三段姻缘#", parsed[3])
            self.assertIn("新任务提示音", parsed[3])

    def test_missing_chapter_or_empty_chapter_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench_path = Path(tmp) / "book1_ch1_3.txt"
            bench_path.write_text(_BENCHMARK_SAMPLE_TEXT, encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                load_benchmark_chapters(bench_path, [1, 2, 4])
            self.assertIn("chapter 4", str(ctx.exception))

            empty_ch_path = Path(tmp) / "empty_ch.txt"
            empty_ch_path.write_text(
                "# header\n\n第1章 标题\n【缺失：全章未转录】\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_benchmark_chapters(empty_ch_path, [1])

            for bad_chapters in ([], [0], [-1], [True], [1, 1]):
                with self.subTest(bad_chapters=bad_chapters):
                    with self.assertRaises(ValueError):
                        load_benchmark_chapters(bench_path, bad_chapters)  # type: ignore[arg-type]


class TestScoreChapters(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_dir = _build_temp_project(Path(self._tmp.name))
        self.project: NovelProject = load_project(self.proj_dir)
        self.blueprint: BookBlueprint = load_blueprint(self.proj_dir / "blueprint.yaml")
        self.chapter_texts = {1: _CANDIDATE_CH1, 2: _CANDIDATE_CH2, 3: _CANDIDATE_CH3}
        self.benchmark = {
            1: "对标第一章正文",
            2: "对标第二章正文",
            3: "对标第三章正文",
        }
        self.case_summaries = {1: "第1章：拆穿未婚夫伪造流水骗婚转债案。"}
        self.valid_balances = {"cash": 50000.0, "followers": 12000.0}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _passing_verdicts(self) -> Dict[int, OpeningVerdict]:
        return {ch: _make_opening_verdict(ch) for ch in (1, 2, 3)}

    def test_pass_with_two_wins_one_loss_and_with_one_win_one_tie_one_loss(self) -> None:
        for winners in (
            {1: "candidate", 2: "candidate", 3: "reference"},
            {1: "candidate", 2: "tie", 3: "reference"},
        ):
            with self.subTest(winners=winners):
                reviewer = _FakeOpeningReviewer(self._passing_verdicts())
                judge = _FakePairwiseJudge(winners)
                score = score_chapters(
                    chapter_texts=self.chapter_texts,
                    benchmark=self.benchmark,
                    project=self.project,
                    blueprint=self.blueprint,
                    ledger_balances=self.valid_balances,
                    reviewer=reviewer,  # type: ignore[arg-type]
                    judge=judge,  # type: ignore[arg-type]
                    case_summaries=self.case_summaries,
                )
                self.assertIsInstance(score, PilotScore)
                self.assertEqual(score.l1_critical_count, 0)
                self.assertTrue(score.opening_all_passed)
                self.assertEqual(score.not_lose_count, 2)
                self.assertTrue(score.passed)
                self.assertEqual(reviewer.calls[0], (1, None))
                self.assertEqual(reviewer.calls[1], (2, self.case_summaries[1]))
                self.assertEqual(
                    judge.calls[0][2],
                    {
                        "chapter": 1,
                        "title": self.project.meta.title,
                        "genre_id": self.blueprint.style.genre_id,
                    },
                )
                as_dict = score.to_dict()
                self.assertTrue(as_dict["passed"])
                self.assertEqual(as_dict["not_lose_count"], 2)

    def test_fail_when_pairwise_loses_two_chapters(self) -> None:
        reviewer = _FakeOpeningReviewer(self._passing_verdicts())
        judge = _FakePairwiseJudge({1: "candidate", 2: "reference", 3: "reference"})
        score = score_chapters(
            chapter_texts=self.chapter_texts,
            benchmark=self.benchmark,
            project=self.project,
            blueprint=self.blueprint,
            ledger_balances=self.valid_balances,
            reviewer=reviewer,  # type: ignore[arg-type]
            judge=judge,  # type: ignore[arg-type]
            case_summaries=self.case_summaries,
        )
        self.assertEqual(score.l1_critical_count, 0)
        self.assertTrue(score.opening_all_passed)
        self.assertEqual(score.not_lose_count, 1)
        self.assertFalse(score.passed)

    def test_fail_when_l1_has_critical_hit(self) -> None:
        reviewer = _FakeOpeningReviewer(self._passing_verdicts())
        judge = _FakePairwiseJudge({1: "candidate", 2: "candidate", 3: "candidate"})
        # Exceeds blueprint.scale_rules cash hard_max=5,000,000 -> QR_SCALE CRITICAL
        score = score_chapters(
            chapter_texts=self.chapter_texts,
            benchmark=self.benchmark,
            project=self.project,
            blueprint=self.blueprint,
            ledger_balances={"cash": 9_999_999.0, "followers": 1000.0},
            reviewer=reviewer,  # type: ignore[arg-type]
            judge=judge,  # type: ignore[arg-type]
            case_summaries=self.case_summaries,
        )
        self.assertEqual(score.l1_critical_count, 1)
        self.assertEqual(score.l1_hits[0].rule_id, "QR_SCALE")
        self.assertTrue(score.opening_all_passed)
        self.assertEqual(score.not_lose_count, 3)
        self.assertFalse(score.passed)

    def test_fail_when_any_opening_item_fails(self) -> None:
        verdicts = self._passing_verdicts()
        verdicts[2] = _make_opening_verdict(2, fail_item_id="PROTAGONIST_RESTRAINT")
        reviewer = _FakeOpeningReviewer(verdicts)
        judge = _FakePairwiseJudge({1: "candidate", 2: "candidate", 3: "candidate"})
        score = score_chapters(
            chapter_texts=self.chapter_texts,
            benchmark=self.benchmark,
            project=self.project,
            blueprint=self.blueprint,
            ledger_balances=self.valid_balances,
            reviewer=reviewer,  # type: ignore[arg-type]
            judge=judge,  # type: ignore[arg-type]
            case_summaries=self.case_summaries,
        )
        self.assertEqual(score.l1_critical_count, 0)
        self.assertFalse(score.opening_all_passed)
        self.assertEqual(score.not_lose_count, 3)
        self.assertFalse(score.passed)

    def test_chapter_2_missing_or_blank_case_summary_raises_value_error(self) -> None:
        reviewer = _FakeOpeningReviewer(self._passing_verdicts())
        judge = _FakePairwiseJudge({1: "candidate", 2: "candidate", 3: "candidate"})
        for bad_summaries in ({}, {1: "   "}, {2: "有第2章但缺第1章"}):
            with self.subTest(bad_summaries=bad_summaries):
                with self.assertRaises(ValueError):
                    score_chapters(
                        chapter_texts=self.chapter_texts,
                        benchmark=self.benchmark,
                        project=self.project,
                        blueprint=self.blueprint,
                        ledger_balances=self.valid_balances,
                        reviewer=reviewer,  # type: ignore[arg-type]
                        judge=judge,  # type: ignore[arg-type]
                        case_summaries=bad_summaries,
                    )

    def test_missing_benchmark_chapter_raises_value_error(self) -> None:
        reviewer = _FakeOpeningReviewer(self._passing_verdicts())
        judge = _FakePairwiseJudge({1: "candidate", 2: "candidate", 3: "candidate"})
        with self.assertRaises(ValueError):
            score_chapters(
                chapter_texts=self.chapter_texts,
                benchmark={1: "对标1", 2: "对标2"},
                project=self.project,
                blueprint=self.blueprint,
                ledger_balances=self.valid_balances,
                reviewer=reviewer,  # type: ignore[arg-type]
                judge=judge,  # type: ignore[arg-type]
                case_summaries=self.case_summaries,
            )


class TestRunPilot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.proj_dir = _build_temp_project(self.tmp_root)
        self.bench_path = self.tmp_root / "book1_ch1_3.txt"
        self.bench_path.write_text(_BENCHMARK_SAMPLE_TEXT, encoding="utf-8")
        self.case_summaries = {1: "第1章：拆穿未婚夫骗婚转债案。"}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_fake_engine_factory(
        self,
        *,
        balances_by_writer: Optional[Mapping[str, Dict[str, float]]] = None,
    ) -> Callable[[NovelProject, LLMProvider, Path], Any]:
        fixtures = {1: _CANDIDATE_CH1, 2: _CANDIDATE_CH2, 3: _CANDIDATE_CH3}

        class _FakeEngine:
            def __init__(self, output_dir: Path, writer_name: str) -> None:
                self.output_dir = output_dir
                self.writer_name = writer_name

            def run(self, target_chapters: List[int]) -> Dict[str, Any]:
                chapters_dir = self.output_dir / "chapters"
                chapters_dir.mkdir(parents=True, exist_ok=True)
                for ch in target_chapters:
                    (chapters_dir / format_chapter_filename(ch)).write_text(
                        fixtures[ch], encoding="utf-8"
                    )
                if balances_by_writer and self.writer_name in balances_by_writer:
                    snaps_dir = self.output_dir / "snapshots"
                    snaps_dir.mkdir(parents=True, exist_ok=True)
                    max_ch = max(target_chapters)
                    (snaps_dir / f"chapter_{max_ch:04d}_after.json").write_text(
                        json.dumps(
                            {"numeric_balances": balances_by_writer[self.writer_name]},
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                return {"chapter_count": len(target_chapters)}

        def _factory(project: NovelProject, provider: LLMProvider, writer_out: Path) -> Any:
            return _FakeEngine(writer_out, writer_out.name)

        return _factory

    def test_run_pilot_writes_scorecard_json_and_chinese_markdown(self) -> None:
        # Writer 1 ("hy4_flash"): passes (L1=0, opening all pass, pairwise 2 wins 1 loss)
        # Writer 2 ("deepseek_v4_1_flash"): fails via snapshot cash over hard_max & pairwise losses
        writer_call_counter = {"idx": 0}
        # Each writer consumes: 3 opening_review calls + 6 pairwise calls (2 swapped rounds x 3 chapters) = 9 calls.
        writer_pairwise_plan = [
            ["candidate", "tie", "reference"],  # writer 1 -> not_lose=2 (PASS)
            ["reference", "reference", "candidate"],  # writer 2 -> not_lose=1 (FAIL)
        ]

        def _judge_responder(req: LLMRequest, call_no: int) -> str:
            writer_idx = (call_no - 1) // 9
            step_in_writer = (call_no - 1) % 9
            writer_call_counter["idx"] = writer_idx
            if req.request_id.startswith("opening-review-ch"):
                ch = req.chapter
                return _build_opening_json_for_candidate(ch)
            # pairwise-r1 / pairwise-r2 for chapters 1, 2, 3
            pair_ch_idx = (step_in_writer - 3) // 2
            desired = writer_pairwise_plan[writer_idx][pair_ch_idx]
            return _pairwise_json_for_request(req, desired)

        judge_provider = _FakeProvider(_judge_responder)
        dummy_writer_a = _FakeProvider(lambda req, n: "")
        dummy_writer_b = _FakeProvider(lambda req, n: "")
        out_dir = self.tmp_root / "pilot_out"

        result = run_pilot(
            project_dir=self.proj_dir,
            chapters=[1, 2, 3],
            writers={
                "hy4_flash": dummy_writer_a,
                "deepseek_v4_1_flash": dummy_writer_b,
            },
            judge_provider=judge_provider,
            judge_model="mimo_v2_6_pro",
            benchmark_path=self.bench_path,
            case_summaries=self.case_summaries,
            out_dir=out_dir,
            engine_factory=self._make_fake_engine_factory(
                balances_by_writer={
                    "hy4_flash": {"cash": 50000.0, "followers": 10000.0},
                    "deepseek_v4_1_flash": {"cash": 8_000_000.0, "followers": 10000.0},
                }
            ),
        )

        json_path = out_dir / "scorecard.json"
        md_path = out_dir / "scorecard.md"
        partial_path = out_dir / "scorecard.partial.json"
        self.assertTrue(json_path.is_file())
        self.assertTrue(md_path.is_file())
        self.assertFalse(partial_path.exists())

        loaded_json = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded_json["status"], "completed")
        self.assertEqual(loaded_json["passed_writers"], ["hy4_flash"])
        self.assertEqual(loaded_json["failed_writers"], ["deepseek_v4_1_flash"])
        self.assertEqual(result["passed_writers"], ["hy4_flash"])
        self.assertTrue(loaded_json["writers"]["hy4_flash"]["passed"])
        self.assertFalse(loaded_json["writers"]["deepseek_v4_1_flash"]["passed"])
        self.assertEqual(loaded_json["writers"]["deepseek_v4_1_flash"]["l1_critical_count"], 1)

        md_text = md_path.read_text(encoding="utf-8")
        self.assertIn("## 结论", md_text)
        self.assertIn("过线写手（✅）", md_text)
        self.assertIn("未过线写手（❌）", md_text)
        self.assertIn("hy4_flash", md_text)
        self.assertIn("deepseek_v4_1_flash", md_text)
        self.assertIn("✅ 过线", md_text)
        self.assertIn("❌ 未过线", md_text)
        self.assertIn("跨模型两两盲比明细", md_text)

    def test_same_family_writer_and_judge_raises_and_writes_partial_json(self) -> None:
        out_dir = self.tmp_root / "pilot_same_family"
        judge_provider = _FakeProvider(lambda req, n: "{}")
        writer_provider = _FakeProvider(lambda req, n: "")

        with self.assertRaises(SameModelJudgeError):
            run_pilot(
                project_dir=self.proj_dir,
                chapters=[1, 2, 3],
                writers={"hy4_flash": writer_provider},
                judge_provider=judge_provider,
                judge_model="hy4",
                benchmark_path=self.bench_path,
                case_summaries=self.case_summaries,
                out_dir=out_dir,
                engine_factory=self._make_fake_engine_factory(),
            )

        partial_path = out_dir / "scorecard.partial.json"
        self.assertTrue(partial_path.is_file())
        self.assertFalse((out_dir / "scorecard.json").exists())
        partial = json.loads(partial_path.read_text(encoding="utf-8"))
        self.assertEqual(partial["status"], "partial_error")
        self.assertEqual(partial["completed_writers"], [])
        self.assertEqual(partial["error"]["writer"], "hy4_flash")
        self.assertEqual(partial["error"]["type"], "SameModelJudgeError")
        self.assertEqual(len(judge_provider.requests), 0)

    def test_mid_run_judge_error_preserves_completed_writer_in_partial_json_and_reraises(
        self,
    ) -> None:
        out_dir = self.tmp_root / "pilot_partial_crash"

        def _crashing_judge(req: LLMRequest, call_no: int) -> str:
            # First writer takes 9 calls (3 opening + 6 pairwise) and completes cleanly.
            if call_no <= 9:
                if req.request_id.startswith("opening-review-ch"):
                    return _build_opening_json_for_candidate(req.chapter)
                return _pairwise_json_for_request(req, "candidate")
            # Second writer fails immediately on judge call #10 with invalid non-JSON output.
            return "NOT_VALID_JSON_AT_ALL"

        judge_provider = _FakeProvider(_crashing_judge)
        writer_1 = _FakeProvider(lambda req, n: "")
        writer_2 = _FakeProvider(lambda req, n: "")

        with self.assertRaises(JudgeResponseError):
            run_pilot(
                project_dir=self.proj_dir,
                chapters=[1, 2, 3],
                writers={
                    "hy4_flash": writer_1,
                    "deepseek_v4_1_flash": writer_2,
                },
                judge_provider=judge_provider,
                judge_model="mimo_v2_6_pro",
                benchmark_path=self.bench_path,
                case_summaries=self.case_summaries,
                out_dir=out_dir,
                engine_factory=self._make_fake_engine_factory(),
            )

        partial_path = out_dir / "scorecard.partial.json"
        self.assertTrue(partial_path.is_file())
        self.assertFalse((out_dir / "scorecard.json").exists())
        partial = json.loads(partial_path.read_text(encoding="utf-8"))
        self.assertEqual(partial["status"], "partial_error")
        self.assertEqual(partial["completed_writers"], ["hy4_flash"])
        self.assertIn("hy4_flash", partial["writers"])
        self.assertTrue(partial["writers"]["hy4_flash"]["passed"])
        self.assertNotIn("deepseek_v4_1_flash", partial["writers"])
        self.assertEqual(partial["error"]["writer"], "deepseek_v4_1_flash")
        self.assertEqual(partial["error"]["stage"], "score")
        self.assertEqual(partial["error"]["type"], "JudgeResponseError")

    def test_missing_blueprint_raises_validation_error(self) -> None:
        no_bp_dir = _build_temp_project(self.tmp_root / "no_bp", include_blueprint=False)
        with self.assertRaises(BlueprintValidationError):
            run_pilot(
                project_dir=no_bp_dir,
                chapters=[1, 2, 3],
                writers={"hy4_flash": _FakeProvider(lambda r, n: "")},
                judge_provider=_FakeProvider(lambda r, n: "{}"),
                judge_model="mimo_v2_6_pro",
                benchmark_path=self.bench_path,
                case_summaries=self.case_summaries,
                out_dir=self.tmp_root / "out_no_bp",
                engine_factory=self._make_fake_engine_factory(),
            )


class TestPilotCliAndHelpers(unittest.TestCase):
    def test_build_parser_parses_pilot_subcommand_and_repeatable_writer(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "pilot",
                "--project",
                "projects/demo",
                "--writer",
                "hy4_flash",
                "--writer",
                "deepseek_v4_1_flash",
                "--judge",
                "mimo_v2_6_pro",
                "--benchmark-text",
                "bench/book1_ch1_3.txt",
                "--case-summaries",
                "bench/summaries.yaml",
                "--out",
                "out/pilot_1",
                "--base-url",
                "http://127.0.0.1:8317/v1",
                "--api-key",
                "dummy-offline-key",
                "--workbuddy-hy4-flash",
                "wb/hy4flash-free",
                "--workbuddy-hy4",
                "wb/hy4",
                "--workbuddy-deepseek-v4-1-flash",
                "wb/deepseek-v4.1-flash",
                "--workbuddy-mimo-v2-6-pro",
                "wb/mimo-v2.6-pro",
            ]
        )
        self.assertEqual(args.command, "pilot")
        self.assertEqual(args.project, "projects/demo")
        self.assertEqual(args.chapters, "1-3")
        self.assertEqual(args.writer, ["hy4_flash", "deepseek_v4_1_flash"])
        self.assertEqual(args.judge, "mimo_v2_6_pro")
        self.assertEqual(args.benchmark_text, "bench/book1_ch1_3.txt")
        self.assertEqual(args.case_summaries, "bench/summaries.yaml")
        self.assertEqual(args.out, "out/pilot_1")
        self.assertTrue(callable(args.func))

    def test_parse_chapters_arg_ranges_csv_and_invalid_inputs(self) -> None:
        self.assertEqual(_parse_chapters_arg("1-3"), [1, 2, 3])
        self.assertEqual(_parse_chapters_arg("1,2,3"), [1, 2, 3])
        self.assertEqual(_parse_chapters_arg("1-2, 3"), [1, 2, 3])
        self.assertEqual(parse_chapters_arg("2"), [2])

        for bad in ("", "   ", "0-3", "3-1", "1,,2", "1,1", "1-3,2", "abc", "-1", "1-"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _parse_chapters_arg(bad)

    def test_build_workbuddy_slot_provider_pins_single_slot_and_rejects_unknown_slot(self) -> None:
        args = argparse.Namespace(
            base_url="http://127.0.0.1:8317/v1",
            cpa_url=None,
            api_key="dummy-offline-key",
            workbuddy_hy4_flash="wb/hy4flash-free",
            workbuddy_hy4="wb/hy4",
            workbuddy_deepseek_v4_1_flash="wb/deepseek-v4.1-flash",
            workbuddy_mimo_v2_6_pro="wb/mimo-v2.6-pro",
            workbuddy_rate_limit_backoff="5,10",
            workbuddy_model_cooldown_sec=120.0,
        )
        provider, upstream = _build_workbuddy_slot_provider(args, "hy4_flash", role="writer")
        self.assertEqual(upstream, "wb/hy4flash-free")
        # Single-slot router must pin strictly to that slot with no cross-model auto-rotation.
        router = provider._router  # type: ignore[attr-defined]
        cfg = router.get_role_config("writer")
        self.assertEqual(cfg.model, "wb/hy4flash-free")
        self.assertEqual(cfg.rotation_models, ())
        self.assertIn("wb/hy4flash-free", cfg.model_whitelist)

        judge_prov, judge_upstream = _build_workbuddy_slot_provider(
            args, "mimo_v2_6_pro", role="critic"
        )
        self.assertEqual(judge_upstream, "wb/mimo-v2.6-pro")
        judge_cfg = judge_prov._router.get_role_config("critic")  # type: ignore[attr-defined]
        self.assertEqual(judge_cfg.model, "wb/mimo-v2.6-pro")
        self.assertEqual(judge_cfg.rotation_models, ())

        with self.assertRaises(LLMConfigurationError) as ctx:
            _build_workbuddy_slot_provider(args, "unknown_slot_xyz", role="writer")
        self.assertIn("unknown_slot_xyz", str(ctx.exception))

    def test_load_case_summaries_yaml_strict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "summaries.yaml"
            p.write_text("1: 拆穿假结婚转债案\n2: 拆穿假翡翠与保姆骗房案\n", encoding="utf-8")
            self.assertEqual(
                load_case_summaries(p),
                {1: "拆穿假结婚转债案", 2: "拆穿假翡翠与保姆骗房案"},
            )

            bad_p = Path(tmp) / "bad.yaml"
            bad_p.write_text("1: '   '\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_case_summaries(bad_p)


if __name__ == "__main__":
    unittest.main()
