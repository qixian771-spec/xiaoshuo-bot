from __future__ import annotations

import json
from typing import Callable, List
import unittest

from xiaoshuo_bot.judge import JudgeResponseError, SameModelJudgeError
from xiaoshuo_bot.llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse
from xiaoshuo_bot.opening_rules import (
    OPENING_RUBRIC,
    EvidenceNotFoundError,
    OpeningItemResult,
    OpeningReviewer,
    OpeningRubricItem,
    OpeningVerdict,
    locate_evidence,
    split_paragraphs,
    writer_directives,
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


# Self-contained Chapter 1 fixture combining verbatim short snippets (each <= 300 chars)
# from the benchmark text with concise synthetic middle transitions so relative ratios match.
_CH1_BOOK1_FIXTURE = "\n".join(
    [
        "“兄弟，挪挪窝，这儿不让摆摊。”穿着制服的保安，用手里的橡胶棍敲了敲水泥地，发出沉闷的梆梆声。",
        "江枫坐在个小马扎上，闻言掀了掀眼皮。",
        "午后的太阳有些毒，照得他那张没什么血色的脸更加苍白。",
        "脑子里的瘤子又在作祟，一阵尖锐的痛楚穿过，让他的视野边缘带上一圈暗影。",
        "他铺开蓝布写着专算姻缘，旁边还有三卦定乾坤过时不候的小字。",
        "【算命系统已绑定】",
        "【地点】：市民政局登记处门口",
        "【时间】：每日14:00-15:00",
        "【方式】：传统相面",
        "【领域】：姻缘",
        "【目标】：接待三位顾客，并成功算准",
        "保安队长拍住同事肩膀摇摇头，低声劝他别再驱赶，两人转身回到岗亭边继续值守。",
        "民政局门口人来人往，路过的行人侧目偷瞄这冷清小摊，窃窃私语着这年头还有人摆摊算卦。",
        "一个二十出头的女孩，被身边的闺蜜推搡着，半开玩笑地走到他的摊子前。",
        "女孩指着排队领证的男友问婚事，江枫抬眼看清她鼻尖气色暗沉，声音平淡地开了口。",
        "“此为‘财帛宫蒙尘’，意味着你所托付之人，财运已呈败相，外强中干，债台高筑。”",
        "她那个男朋友，最近确实提过几次公司资金周转困难的事，但每次都说问题不大，已经解决了。",
        "不远处排队的男人听见议论，脸色微变，大步走过来把女孩拉到身后质问算命摊别胡说。",
        "江枫抬眸扫过男人眉心黑气与太阳穴旁深陷竖纹，指出他官非临门且远途大单已埋下大患。",
        "男人额角渗出冷汗，想起前阵子强行签下的外地订单暗藏违约风险，脚底像灌了铅般僵住。",
        "江枫声音平静却字字敲在心口，直接点明欺诈二字已刻在脸上，再往下走只怕牢饭吃定了。",
        "男人脑中轰然一响，自己伪造流水与资产证明骗婚转债的秘密，明明世上只有他一人知晓。",
        "围观路人越聚越多，纷纷交头接耳指点男人脸色铁青的模样，几名原本想领证的情侣也停下脚步看热闹。",
        "女孩闺蜜气得一把拽住女孩胳膊，急声提醒她千万别被几句甜言蜜语糊弄过去，当场把账本核对清楚。",
        "男人急忙上前想拉住女孩解释，却在众目睽睽下越描越黑，额头冷汗顺着鬓角滚落，连手腕都在发抖。",
        "女孩看着未婚夫惨白躲闪的脸和不断冒出的冷汗，心里那座用甜言蜜语搭起的城堡寸寸坍塌。",
        "她忽然想起了什么，猛地抢过男人的公文包，不顾他的阻拦，拉开拉链，在里面疯狂地翻找。",
    ]
)

_CH2_FIXTURE = "\n".join(
    [
        "民警赶到把假借结婚转移债务的男人带走，周围吃瓜群众看江枫的眼神瞬间从看笑话变成敬畏。",
        "一名戴粗金链的中年老板挤到摊前，啪地拍下一万块现金求问家里近况。",
        "江枫扫都没扫那沓钞票一眼，只淡淡说先把人看准再谈酬金，规矩不能破。",
        "他先夸老板子女宫饱满红润，老板得意大笑说儿子上月刚给自己换了新车。",
        "紧接着江枫话锋一转，点出儿子瓜子脸与夫妻俩方脸宽颌根本不肖，像好田种了别家庄稼。",
        "老板笑容僵死，猛然想起邻里多年暗笑与妻子日益冷淡，整个人瘫坐在水泥地上。",
        "不等围观者回神，花布衫保姆又拽着白发大爷凑上来要算晚年姻缘。",
        "江枫冷声揭穿她三白眼配奸狡痣相，分明想哄老人假结婚骗房产抵押套贷给儿子买婚房。",
        "大爷大妈一听瞬间炸锅，七手八脚扑上去将刚想溜走的保姆死死按在马路边。",
    ]
)

_CH3_FIXTURE = "\n".join(
    [
        "最后两分钟里有人出十万、五十万求加塞，环球集团董事长甚至递上黑卡直接喊到五百万。",
        "江枫看了一眼手机屏幕显示两点五十九，当众叠好蓝布收起马扎，淡定说名额已满到点下班。",
        "离开喧嚣后他钻进街角火锅店痛快涮完一桌毛肚肥牛，久违的辛辣烫味驱散了化疗后的麻木。",
        "手机短信亮起银行入账一百万整、当前余额一百零万零二十四块五毛，他先去医院缴清五万块欠费。",
        "回到漏风地下室他把剩余款项安稳存进卡里，盘算先换个向阳出租屋和几身干净体面的衣裳。",
        "他沉沉睡去时，网友把民政局门口视频剪上热搜，网红骗婚案、亲子鉴定与警方诈骗团伙通报全数实锤。",
        "弹幕里有人疯狂刷屏喊着自家百亿家产愿出半副身家求算一卦，主角却始终安安静静躺在出租屋养神。",
        "直到第三天深夜睡得正沉，毫无感情的机械提示音带着新一轮摊位任务再次在江枫脑海响起。",
    ]
)


def _ch1_passing_payload() -> str:
    return json.dumps(
        {
            "items": [
                {
                    "id": "OPEN_FIRST_LINE_CONFLICT",
                    "found_or_pass": True,
                    "evidence": "“兄弟，挪挪窝，这儿不让摆摊。”",
                    "reason": "首段第一句即保安上门驱赶摊位，直接进入冲突。",
                },
                {
                    "id": "OPEN_STAKE_BY_P4",
                    "found_or_pass": True,
                    "evidence": "脑子里的瘤子又在作祟，一阵尖锐的痛楚穿过，让他的视野边缘带上一圈暗影。",
                    "reason": "第4段点出脑瘤作祟的生命危机。",
                },
                {
                    "id": "OPEN_RULE_BY_25",
                    "found_or_pass": True,
                    "evidence": "【目标】：接待三位顾客，并成功算准",
                    "reason": "前25%内以系统面板明确交代摊规与算准三卦目标。",
                },
                {
                    "id": "OPEN_FIRST_CLIENT_BY_40",
                    "found_or_pass": True,
                    "evidence": "一个二十出头的女孩，被身边的闺蜜推搡着，半开玩笑地走到他的摊子前。",
                    "reason": "前40%内第一位求卦女孩出场。",
                },
                {
                    "id": "OPEN_VERDICT_3STEP",
                    "found_or_pass": True,
                    "evidence": "她那个男朋友，最近确实提过几次公司资金周转困难的事，但每次都说问题不大，已经解决了。",
                    "reason": "完成财帛宫蒙尘术语->外强中干债台高筑后果->女孩回忆内心印证三步法。",
                },
                {
                    "id": "OPEN_END_MID_ACTION",
                    "found_or_pass": True,
                    "evidence": "她忽然想起了什么，猛地抢过男人的公文包，不顾他的阻拦，拉开拉链，在里面疯狂地翻找。",
                    "reason": "章末停在抢过公文包翻找证据的动作中途。",
                },
            ]
        },
        ensure_ascii=False,
    )


class TestRubricAndHelpers(unittest.TestCase):
    def test_rubric_structure_and_writer_directives(self) -> None:
        self.assertEqual([item.id for item in OPENING_RUBRIC[1]], [
            "OPEN_FIRST_LINE_CONFLICT",
            "OPEN_STAKE_BY_P4",
            "OPEN_RULE_BY_25",
            "OPEN_FIRST_CLIENT_BY_40",
            "OPEN_VERDICT_3STEP",
            "OPEN_END_MID_ACTION",
        ])
        self.assertEqual([item.id for item in OPENING_RUBRIC[2]], [
            "OPEN_VERDICT_3STEP",
            "OPEN_END_MID_ACTION",
            "CASE_DISTINCT",
            "PROTAGONIST_RESTRAINT",
        ])
        self.assertEqual([item.id for item in OPENING_RUBRIC[3]], [
            "EXTERNAL_PAYOFF",
            "NEW_HOOK_END",
            "SCALE_REALISTIC",
        ])

        self.assertEqual(len(writer_directives(1)), 6)
        self.assertEqual(len(writer_directives(2)), 4)
        self.assertEqual(len(writer_directives(3)), 3)
        self.assertIn("第一段就是冲突", writer_directives(1)[0])
        self.assertIn("克制并守住摊规", writer_directives(2)[-1])
        self.assertIn("现实有界量级", writer_directives(3)[-1])

        for bad_chapter in (4, 0, -1, True, False, "1", 1.5, None):
            with self.subTest(chapter=bad_chapter):
                with self.assertRaises(ValueError):
                    writer_directives(bad_chapter)  # type: ignore[arg-type]

    def test_rubric_item_validation(self) -> None:
        with self.assertRaises(ValueError):
            OpeningRubricItem(
                id="BAD_KIND",
                chapter=1,
                kind="fuzzy",
                question="q",
                writer_directive="d",
            )
        with self.assertRaises(ValueError):
            OpeningRubricItem(
                id="POS_NO_LIMIT",
                chapter=1,
                kind="positional",
                question="q",
                writer_directive="d",
            )
        with self.assertRaises(ValueError):
            OpeningRubricItem(
                id="JUDG_WITH_LIMIT",
                chapter=1,
                kind="judgment",
                question="q",
                writer_directive="d",
                max_paragraph=2,
            )
        with self.assertRaises(ValueError):
            OpeningRubricItem(
                id="BAD_PARA_BOOL",
                chapter=1,
                kind="positional",
                question="q",
                writer_directive="d",
                max_paragraph=True,  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            OpeningRubricItem(
                id="BAD_RATIO_ZERO",
                chapter=1,
                kind="positional",
                question="q",
                writer_directive="d",
                max_char_ratio=0.0,
            )
        with self.assertRaises(ValueError):
            OpeningRubricItem(
                id="BAD_RATIO_GT_ONE",
                chapter=1,
                kind="positional",
                question="q",
                writer_directive="d",
                max_char_ratio=1.2,
            )

    def test_split_paragraphs_and_locate_evidence(self) -> None:
        sample = "\n\u3000\u3000第一段有 空格。\r\n\r\n   \n  第二段开头，紧接下文。\n第三段结束。  \n"
        paras = split_paragraphs(sample)
        self.assertEqual(paras, ["第一段有 空格。", "第二段开头，紧接下文。", "第三段结束。"])

        # Exact match ignoring intra-line whitespace
        p_idx, ratio = locate_evidence(sample, "第一段 有空格。")
        self.assertEqual(p_idx, 1)
        self.assertEqual(ratio, 0.0)

        # Cross-newline / cross-paragraph exact match starts in paragraph 1
        cross_p, cross_ratio = locate_evidence(sample, "空格。\n  第二段\u3000开头")
        self.assertEqual(cross_p, 1)
        self.assertAlmostEqual(cross_ratio, 4 / 24)

        # Start at paragraph 2
        p2_idx, p2_ratio = locate_evidence(sample, "第二段开头")
        self.assertEqual(p2_idx, 2)
        self.assertAlmostEqual(p2_ratio, 7 / 24)

        # Strict no-fuzzy negative cases (single character mismatch -> EvidenceNotFoundError)
        for bad_ev in ("", "   \n\t ", "第一段有空位。", "完全不存在的句子"):
            with self.subTest(evidence=bad_ev):
                with self.assertRaises(EvidenceNotFoundError):
                    locate_evidence(sample, bad_ev)

        with self.assertRaises(EvidenceNotFoundError):
            locate_evidence("   \n\n  ", "第一段")

        with self.assertRaises(ValueError):
            split_paragraphs(123)  # type: ignore[arg-type]


class TestOpeningReviewerGuards(unittest.TestCase):
    def test_same_model_slot_and_family_rejected(self) -> None:
        provider = _FakeProvider(lambda req, n: "{}")
        blocked = [
            ("hy4", "hy4_flash"),
            ("wb/hy4flash-free", "hy4_flash"),
            ("hy4_flash", "hy4_flash"),
            ("wb/deepseek-v4.1-flash", "deepseek_v4_1_flash"),
            ("gemini-2.5-pro", "gemini-2.5-flash"),
        ]
        for j_model, w_model in blocked:
            with self.subTest(judge_model=j_model, writer_model=w_model):
                with self.assertRaises(SameModelJudgeError):
                    OpeningReviewer(provider=provider, judge_model=j_model, writer_model=w_model)

        with self.assertRaises(LLMConfigurationError):
            OpeningReviewer(provider=None, judge_model="deepseek_v4_1_flash", writer_model="hy4_flash")  # type: ignore[arg-type]
        with self.assertRaises(LLMConfigurationError):
            OpeningReviewer(provider=provider, judge_model="   ", writer_model="hy4_flash")
        with self.assertRaises(LLMConfigurationError):
            OpeningReviewer(provider=provider, judge_model="deepseek_v4_1_flash", writer_model="")

    def test_input_validation_and_chapter2_summary_required(self) -> None:
        provider = _FakeProvider(lambda req, n: _ch1_passing_payload())
        reviewer = OpeningReviewer(
            provider=provider,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="hy4_flash",
        )
        for bad_summary in (None, "", "   \n\t "):
            with self.subTest(previous_case_summary=bad_summary):
                with self.assertRaises(ValueError):
                    reviewer.review(_CH2_FIXTURE, 2, previous_case_summary=bad_summary)
        self.assertEqual(len(provider.requests), 0)

        with self.assertRaises(ValueError):
            reviewer.review("   \n ", 1)
        with self.assertRaises(ValueError):
            reviewer.review(_CH1_BOOK1_FIXTURE, 4)
        with self.assertRaises(ValueError):
            reviewer.review(_CH1_BOOK1_FIXTURE, True)  # type: ignore[arg-type]

    def test_positional_thresholds_override_optimistic_judge(self) -> None:
        # Build a 10-paragraph chapter where conflict appears only in paragraph 6 and
        # system rule appears after 35% of text, even though judge claims found_or_pass=True.
        late_text = "\n".join(
            [
                "今天早晨阳光明媚，微风轻轻吹过老城区的街道与梧桐树梢。",
                "主角从小是个孤儿，在街边巷尾吃百家饭长大，性格有些孤僻。",
                "他背着旧帆布包走过两条青石板巷子，顺路买了两根油条当早饭。",
                "来到市民政局门口树荫下，他慢吞吞把小马扎摆正坐下来歇脚。",
                "四周人群来来往往喜气洋洋，大家都在排队领证根本没人搭理他。",
                "直到第六段保安终于走过来呵斥：兄弟，挪挪窝，这儿不让摆摊。",
                "脑子里的瘤子隐隐作痛，生命只剩三个月，必须尽快想办法凑齐药费。",
                "摊主抬头看天默默等待时机，盘算着先看看路过行人里有没有合适目标。",
                "过了半晌，系统提示才亮起：每日限算三卦，算准即可获得酬劳奖励。",
                "一个二十出头的女孩走近摊前询问姻缘，江枫以财帛宫蒙尘断出债务危机，她震惊想起男友近况，随即抢过公文包翻找。",
            ]
        )

        # Judge says found_or_pass=True for EVERY item, using real late quotes!
        late_payload = json.dumps(
            {
                "items": [
                    {
                        "id": "OPEN_FIRST_LINE_CONFLICT",
                        "found_or_pass": True,
                        "evidence": "直到第六段保安终于走过来呵斥：兄弟，挪挪窝，这儿不让摆摊。",
                        "reason": "模型误以为有冲突就行。",
                    },
                    {
                        "id": "OPEN_STAKE_BY_P4",
                        "found_or_pass": True,
                        "evidence": "脑子里的瘤子隐隐作痛，生命只剩三个月",
                        "reason": "模型声称赌注在第7段已出现。",
                    },
                    {
                        "id": "OPEN_RULE_BY_25",
                        "found_or_pass": True,
                        "evidence": "系统提示才亮起：每日限算三卦",
                        "reason": "规则已在第9段给出。",
                    },
                    {
                        "id": "OPEN_FIRST_CLIENT_BY_40",
                        "found_or_pass": True,
                        "evidence": "一个二十出头的女孩走近摊前询问姻缘",
                        "reason": "首位客人已出场。",
                    },
                    {
                        "id": "OPEN_VERDICT_3STEP",
                        "found_or_pass": True,
                        "evidence": "她震惊想起男友近况",
                        "reason": "具备三步断语印证。",
                    },
                    {
                        "id": "OPEN_END_MID_ACTION",
                        "found_or_pass": True,
                        "evidence": "随即抢过公文包翻找。",
                        "reason": "停在翻包动作中途。",
                    },
                ]
            },
            ensure_ascii=False,
        )
        reviewer = OpeningReviewer(
            provider=_FakeProvider(lambda req, n: late_payload),
            judge_model="wb/mimo-v2.6-pro",
            writer_model="wb/hy4flash-free",
        )
        verdict = reviewer.review(late_text, 1)
        by_id = {item.id: item for item in verdict.items}

        self.assertFalse(verdict.passed_all)
        self.assertEqual(by_id["OPEN_FIRST_LINE_CONFLICT"].paragraph_index, 6)
        self.assertFalse(by_id["OPEN_FIRST_LINE_CONFLICT"].passed)
        self.assertEqual(by_id["OPEN_STAKE_BY_P4"].paragraph_index, 7)
        self.assertFalse(by_id["OPEN_STAKE_BY_P4"].passed)
        self.assertGreater(by_id["OPEN_RULE_BY_25"].char_ratio or 0.0, 0.35)
        self.assertFalse(by_id["OPEN_RULE_BY_25"].passed)
        self.assertGreater(by_id["OPEN_FIRST_CLIENT_BY_40"].char_ratio or 0.0, 0.40)
        self.assertFalse(by_id["OPEN_FIRST_CLIENT_BY_40"].passed)
        # Judgment items still passed because evidence is real and found_or_pass=True
        self.assertTrue(by_id["OPEN_VERDICT_3STEP"].passed)
        self.assertTrue(by_id["OPEN_END_MID_ACTION"].passed)

        # Also test positional item with found_or_pass=False -> passed=False, locations None
        not_found_payload = json.loads(_ch1_passing_payload())
        not_found_payload["items"][1]["found_or_pass"] = False
        not_found_payload["items"][1]["evidence"] = ""
        not_found_payload["items"][1]["reason"] = "未出现主角生死赌注。"
        reviewer_nf = OpeningReviewer(
            provider=_FakeProvider(
                lambda req, n, p=not_found_payload: json.dumps(p, ensure_ascii=False)
            ),
            judge_model="wb/mimo-v2.6-pro",
            writer_model="wb/hy4flash-free",
        )
        verdict_nf = reviewer_nf.review(_CH1_BOOK1_FIXTURE, 1)
        self.assertFalse(verdict_nf.passed_all)
        nf_item = next(i for i in verdict_nf.items if i.id == "OPEN_STAKE_BY_P4")
        self.assertFalse(nf_item.passed)
        self.assertIsNone(nf_item.paragraph_index)
        self.assertIsNone(nf_item.char_ratio)

    def test_fabricated_evidence_raises_judge_response_error(self) -> None:
        # Case A: fabricated evidence in positional item
        bad_pos = json.loads(_ch1_passing_payload())
        bad_pos["items"][0]["evidence"] = "原文里根本不存在的冲突句子"

        # Case B: fabricated evidence in judgment item with found_or_pass=True
        bad_judg = json.loads(_ch1_passing_payload())
        bad_judg["items"][4]["evidence"] = "编造的当事人内心印证句"

        # Case C: empty evidence when found_or_pass=True
        empty_ev = json.loads(_ch1_passing_payload())
        empty_ev["items"][5]["evidence"] = "   "

        for payload in (bad_pos, bad_judg, empty_ev):
            with self.subTest(payload=payload):
                reviewer = OpeningReviewer(
                    provider=_FakeProvider(lambda req, n, p=payload: json.dumps(p, ensure_ascii=False)),
                    judge_model="wb/deepseek-v4.1-flash",
                    writer_model="hy4_flash",
                )
                with self.assertRaises(JudgeResponseError):
                    reviewer.review(_CH1_BOOK1_FIXTURE, 1)

    def test_malformed_json_and_id_set_mismatches_raise_judge_response_error(self) -> None:
        valid = json.loads(_ch1_passing_payload())

        missing_id = {"items": valid["items"][:-1]}
        extra_id = {
            "items": valid["items"]
            + [{"id": "UNKNOWN_RULE", "found_or_pass": True, "evidence": "江枫", "reason": "多余"}]
        }
        dup_id = {"items": valid["items"][:-1] + [valid["items"][0]]}
        non_bool = json.loads(_ch1_passing_payload())
        non_bool["items"][0]["found_or_pass"] = 1
        blank_reason = json.loads(_ch1_passing_payload())
        blank_reason["items"][0]["reason"] = "  "

        bad_responses = [
            "",
            "not json",
            "{}",
            json.dumps({"items": "not a list"}),
            json.dumps(missing_id, ensure_ascii=False),
            json.dumps(extra_id, ensure_ascii=False),
            json.dumps(dup_id, ensure_ascii=False),
            json.dumps(non_bool, ensure_ascii=False),
            json.dumps(blank_reason, ensure_ascii=False),
            LLMResponse(request_id="r", content="", model="m", provider="fake"),
            "RAW_NON_LLM_RESPONSE",
        ]
        for bad in bad_responses:
            with self.subTest(bad=bad):
                if bad == "RAW_NON_LLM_RESPONSE":
                    provider = _FakeProvider(lambda req, n: "")
                    provider.generate = lambda req: "plain string"  # type: ignore[method-assign,assignment]
                else:
                    provider = _FakeProvider(lambda req, n, r=bad: r)
                reviewer = OpeningReviewer(
                    provider=provider,
                    judge_model="wb/deepseek-v4.1-flash",
                    writer_model="hy4_flash",
                )
                with self.assertRaises(JudgeResponseError):
                    reviewer.review(_CH1_BOOK1_FIXTURE, 1)


class TestGoldenThreeChaptersIntegration(unittest.TestCase):
    def test_book1_chapter1_real_snippets_all_positional_pass(self) -> None:
        provider = _FakeProvider(lambda req, n: "```json\n" + _ch1_passing_payload() + "\n```")
        reviewer = OpeningReviewer(
            provider=provider,
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="hy4_flash",
        )
        verdict = reviewer.review(_CH1_BOOK1_FIXTURE, 1)

        self.assertIsInstance(verdict, OpeningVerdict)
        self.assertEqual(verdict.chapter, 1)
        self.assertTrue(verdict.passed_all)
        self.assertEqual(len(verdict.items), 6)

        # Verify single LLM request matches PairwiseJudge._invoke_judge conventions
        self.assertEqual(len(provider.requests), 1)
        req = provider.requests[0]
        self.assertEqual(req.purpose, "critic")
        self.assertEqual(req.temperature, 0.2)
        self.assertEqual(req.max_tokens, 2048)
        self.assertEqual(req.metadata, {"model": "wb/deepseek-v4.1-flash"})
        self.assertEqual(req.chapter, 1)

        by_id = {item.id: item for item in verdict.items}
        self.assertEqual(by_id["OPEN_FIRST_LINE_CONFLICT"].paragraph_index, 1)
        self.assertEqual(by_id["OPEN_FIRST_LINE_CONFLICT"].char_ratio, 0.0)
        self.assertTrue(by_id["OPEN_FIRST_LINE_CONFLICT"].passed)

        self.assertEqual(by_id["OPEN_STAKE_BY_P4"].paragraph_index, 4)
        self.assertAlmostEqual(by_id["OPEN_STAKE_BY_P4"].char_ratio or 0.0, 91 / 876)
        self.assertTrue(by_id["OPEN_STAKE_BY_P4"].passed)

        self.assertEqual(by_id["OPEN_RULE_BY_25"].paragraph_index, 11)
        self.assertAlmostEqual(by_id["OPEN_RULE_BY_25"].char_ratio or 0.0, 212 / 876)
        self.assertLessEqual(by_id["OPEN_RULE_BY_25"].char_ratio or 1.0, 0.25)
        self.assertTrue(by_id["OPEN_RULE_BY_25"].passed)

        self.assertEqual(by_id["OPEN_FIRST_CLIENT_BY_40"].paragraph_index, 14)
        self.assertAlmostEqual(by_id["OPEN_FIRST_CLIENT_BY_40"].char_ratio or 0.0, 307 / 876)
        self.assertLessEqual(by_id["OPEN_FIRST_CLIENT_BY_40"].char_ratio or 1.0, 0.40)
        self.assertTrue(by_id["OPEN_FIRST_CLIENT_BY_40"].passed)

        verdict_dict = verdict.to_dict()
        self.assertTrue(verdict_dict["passed_all"])
        self.assertEqual(verdict_dict["chapter"], 1)
        self.assertEqual(len(verdict_dict["items"]), 6)
        self.assertIsInstance(verdict.items[0], OpeningItemResult)

    def test_chapter2_and_chapter3_rubrics_pass_and_fail(self) -> None:
        ch2_pass_payload = json.dumps(
            {
                "items": [
                    {
                        "id": "OPEN_VERDICT_3STEP",
                        "found_or_pass": True,
                        "evidence": "老板笑容僵死，猛然想起邻里多年暗笑与妻子日益冷淡，整个人瘫坐在水泥地上。",
                        "reason": "给出子女宫与骨相术语及非亲生疑点后果，并有老板当场回忆印证。",
                    },
                    {
                        "id": "OPEN_END_MID_ACTION",
                        "found_or_pass": True,
                        "evidence": "大爷大妈一听瞬间炸锅，七手八脚扑上去将刚想溜走的保姆死死按在马路边。",
                        "reason": "停在群众抓获保姆的冲突动作现场。",
                    },
                    {
                        "id": "CASE_DISTINCT",
                        "found_or_pass": True,
                        "evidence": "紧接着江枫话锋一转，点出儿子瓜子脸与夫妻俩方脸宽颌根本不肖，像好田种了别家庄稼。",
                        "reason": "前章为骗婚转债，本章转为非亲生血脉案与保姆套贷骗房案，雷点明显不同。",
                    },
                    {
                        "id": "PROTAGONIST_RESTRAINT",
                        "found_or_pass": True,
                        "evidence": "江枫扫都没扫那沓钞票一眼，只淡淡说先把人看准再谈酬金，规矩不能破。",
                        "reason": "面对一万元现金诱惑保持克制，优先守住摊规。",
                    },
                ]
            },
            ensure_ascii=False,
        )
        reviewer2 = OpeningReviewer(
            provider=_FakeProvider(lambda req, n: ch2_pass_payload),
            judge_model="wb/mimo-v2.6-pro",
            writer_model="hy4_flash",
        )
        verdict2 = reviewer2.review(
            _CH2_FIXTURE,
            2,
            previous_case_summary="第1章：江枫在民政局门口摆摊，拆穿网红男友伪造资产流水骗婚转嫁巨债。",
        )
        self.assertTrue(verdict2.passed_all)
        self.assertEqual(len(verdict2.items), 4)
        self.assertIn("前情摘要", reviewer2.provider.requests[0].user_prompt)  # type: ignore[attr-defined]

        # Chapter 2 judgment failure: empty evidence -> None locations; real evidence -> located
        ch2_fail_payload = json.dumps(
            {
                "items": [
                    {
                        "id": "OPEN_VERDICT_3STEP",
                        "found_or_pass": False,
                        "evidence": "",
                        "reason": "未完成三步印证。",
                    },
                    {
                        "id": "OPEN_END_MID_ACTION",
                        "found_or_pass": False,
                        "evidence": "大爷大妈一听瞬间炸锅，七手八脚扑上去将刚想溜走的保姆死死按在马路边。",
                        "reason": "按假设规则视为不通过示例，但附了末段原句。",
                    },
                    {
                        "id": "CASE_DISTINCT",
                        "found_or_pass": True,
                        "evidence": "像好田种了别家庄稼。",
                        "reason": "案件类型不同。",
                    },
                    {
                        "id": "PROTAGONIST_RESTRAINT",
                        "found_or_pass": True,
                        "evidence": "江枫扫都没扫那沓钞票一眼",
                        "reason": "守住摊规。",
                    },
                ]
            },
            ensure_ascii=False,
        )
        reviewer2_fail = OpeningReviewer(
            provider=_FakeProvider(lambda req, n: ch2_fail_payload),
            judge_model="wb/mimo-v2.6-pro",
            writer_model="hy4_flash",
        )
        verdict2_fail = reviewer2_fail.review(
            _CH2_FIXTURE,
            2,
            previous_case_summary="第1章：骗婚转债案。",
        )
        self.assertFalse(verdict2_fail.passed_all)
        self.assertFalse(verdict2_fail.items[0].passed)
        self.assertIsNone(verdict2_fail.items[0].paragraph_index)
        self.assertIsNone(verdict2_fail.items[0].char_ratio)
        self.assertFalse(verdict2_fail.items[1].passed)
        self.assertEqual(verdict2_fail.items[1].paragraph_index, 9)
        self.assertIsNotNone(verdict2_fail.items[1].char_ratio)

        ch3_pass_payload = json.dumps(
            {
                "items": [
                    {
                        "id": "EXTERNAL_PAYOFF",
                        "found_or_pass": True,
                        "evidence": "网友把民政局门口视频剪上热搜，网红骗婚案、亲子鉴定与警方诈骗团伙通报全数实锤。",
                        "reason": "前3案均在舆论热搜、当事人验证与警方通报层面获得外部实锤。",
                    },
                    {
                        "id": "NEW_HOOK_END",
                        "found_or_pass": True,
                        "evidence": "毫无感情的机械提示音带着新一轮摊位任务再次在江枫脑海响起。",
                        "reason": "章末以深夜系统新任务挂出下一阶段钩子。",
                    },
                    {
                        "id": "SCALE_REALISTIC",
                        "found_or_pass": True,
                        "evidence": "弹幕里有人疯狂刷屏喊着自家百亿家产愿出半副身家求算一卦，主角却始终安安静静躺在出租屋养神。",
                        "reason": "主角自身余额仅百万且缴五万医药费，“百亿”夸张数字只留在网友弹幕中。",
                    },
                ]
            },
            ensure_ascii=False,
        )
        reviewer3 = OpeningReviewer(
            provider=_FakeProvider(lambda req, n: ch3_pass_payload),
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="wb/mimo-v2.6-pro",
        )
        verdict3 = reviewer3.review(_CH3_FIXTURE, 3)
        self.assertTrue(verdict3.passed_all)
        self.assertEqual(len(verdict3.items), 3)
        self.assertEqual(
            [item.id for item in verdict3.items],
            ["EXTERNAL_PAYOFF", "NEW_HOOK_END", "SCALE_REALISTIC"],
        )

        ch3_fail_payload = json.dumps(
            {
                "items": [
                    {
                        "id": "EXTERNAL_PAYOFF",
                        "found_or_pass": True,
                        "evidence": "网友把民政局门口视频剪上热搜",
                        "reason": "已有外部印证。",
                    },
                    {
                        "id": "NEW_HOOK_END",
                        "found_or_pass": True,
                        "evidence": "毫无感情的机械提示音带着新一轮摊位任务再次在江枫脑海响起。",
                        "reason": "章末挂出新任务。",
                    },
                    {
                        "id": "SCALE_REALISTIC",
                        "found_or_pass": False,
                        "evidence": "",
                        "reason": "假设主角直接入账百亿，超出合理量级。",
                    },
                ]
            },
            ensure_ascii=False,
        )
        reviewer3_fail = OpeningReviewer(
            provider=_FakeProvider(lambda req, n: ch3_fail_payload),
            judge_model="wb/deepseek-v4.1-flash",
            writer_model="wb/mimo-v2.6-pro",
        )
        verdict3_fail = reviewer3_fail.review(_CH3_FIXTURE, 3)
        self.assertFalse(verdict3_fail.passed_all)
        self.assertFalse(verdict3_fail.items[2].passed)
        self.assertFalse(verdict3_fail.to_dict()["passed_all"])

    def test_locate_evidence_normalizes_quote_variants_without_fuzzy_matching(self) -> None:
        text = "第一段铺垫。\n许青川语速仍慢：“你没有助学发起人的账。外省协查化名‘善舟’，卷走两百多户老人救命钱。”\n第三段。"
        p1, r1 = locate_evidence(
            text,
            '你没有助学发起人的账。外省协查化名“善舟”，卷走两百多户老人救命钱。',
        )
        p2, r2 = locate_evidence(
            text,
            '你没有助学发起人的账。外省协查化名"善舟"，卷走两百多户老人救命钱。',
        )
        p3, r3 = locate_evidence(
            text,
            "你没有助学发起人的账。外省协查化名「善舟」，卷走两百多户老人救命钱。",
        )
        self.assertEqual((p1, p2, p3), (2, 2, 2))
        self.assertAlmostEqual(r1, r2)
        self.assertAlmostEqual(r1, r3)

        # Single altered hanzi still strictly fails (no fuzzy matching)
        with self.assertRaises(EvidenceNotFoundError):
            locate_evidence(
                text,
                '你没有助学发起人的账。外省协查化名“善周”，卷走两百多户老人救命钱。',
            )

    def test_opening_reviewer_retries_once_on_empty_response(self) -> None:
        # 1st call empty, 2nd call valid -> succeeds in exactly 2 calls
        provider_recover = _FakeProvider(
            lambda req, n: "" if n == 1 else _ch1_passing_payload()
        )
        reviewer = OpeningReviewer(
            provider=provider_recover,
            judge_model="wb/mimo-v2.6-pro",
            writer_model="hy4_flash",
        )
        verdict = reviewer.review(_CH1_BOOK1_FIXTURE, 1)
        self.assertTrue(verdict.passed_all)
        self.assertEqual(len(provider_recover.requests), 2)
        self.assertEqual(provider_recover.requests[1].metadata.get("empty_response_retry"), 1)

        # Consecutive empty responses exhaust the single retry cap and still raise
        provider_always_empty = _FakeProvider(lambda req, n: "   ")
        reviewer_fail = OpeningReviewer(
            provider=provider_always_empty,
            judge_model="wb/mimo-v2.6-pro",
            writer_model="hy4_flash",
        )
        with self.assertRaises(JudgeResponseError):
            reviewer_fail.review(_CH1_BOOK1_FIXTURE, 1)
        self.assertEqual(len(provider_always_empty.requests), 2)


if __name__ == "__main__":
    unittest.main()
