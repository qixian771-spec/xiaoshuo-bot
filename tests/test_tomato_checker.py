import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.checker import AITicChecker
from xiaoshuo_bot.models import (
    CharacterWorldState,
    ExpectationDirective,
    ExpectationState,
    LedgerMutation,
    ProjectMeta,
    SceneOutline,
    WorldStateSnapshot,
)


def _make_tomato_scene(
    chapter: int = 1,
    location_id: str = "loc_market",
    min_chars: int = 20,
    max_chars: int = 5000,
    ledger_mutations: list[LedgerMutation] | None = None,
    expectation_directives: list[ExpectationDirective] | None = None,
) -> SceneOutline:
    return SceneOutline(
        chapter=chapter,
        scene_id=f"ch{chapter:02d}_tomato",
        internal_title="首屏打脸",
        pov_character="chen_mo",
        active_characters=["chen_mo", "wang_hu"],
        active_assets=[],
        style_profile_ids=[],
        location_time="白天，老街药铺",
        events=["当众打脸退婚对象"],
        stop_condition="反派跪地求饶时停笔",
        min_chars=min_chars,
        max_chars=max_chars,
        location_id=location_id,
        ledger_mutations=ledger_mutations or [],
        expectation_directives=expectation_directives or [],
    )


def _make_tomato_meta(
    pov_mode: str = "third_limited",
    panel_policy: str = "allow_system_tag",
    contract_track: str = "serial_20k",
) -> ProjectMeta:
    return ProjectMeta(
        project_id="tomato_demo",
        title="天价鉴宝神眼",
        genre="都市脑洞",
        synopsis="退婚当日激活神眼，当场捡漏千亿。",
        default_pov_character="chen_mo",
        author_level="lv2_plus",
        contract_track=contract_track,
        pov_mode=pov_mode,
        panel_policy=panel_policy,
        genre_id="urban_brainhole",
    )


class TestTomatoCheckerGates(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = AITicChecker()
        self.meta = _make_tomato_meta()

    def test_hook_001_blocks_weather_and_resume_openings_in_ch1_but_allows_conflict(self) -> None:
        weather_open = (
            "深秋的清晨，乌云密布，冷风卷着落叶吹过空荡的街口。\n\n"
            "陈默推开玻璃门走进大厅。"
        )
        rep_w = self.checker.scan(weather_open, scene=_make_tomato_scene(chapter=1), project_meta=self.meta)
        self.assertFalse(rep_w.passed)
        self.assertIn("HOOK_001", {v.rule_id for v in rep_w.violations})

        resume_open = (
            "陈默今年二十四岁，毕业于江城大学，是一名普通的外卖员。\n\n"
            "他从小在孤儿院长大，日子过得很苦。"
        )
        rep_r = self.checker.scan(resume_open, scene=_make_tomato_scene(chapter=1), project_meta=self.meta)
        self.assertFalse(rep_r.passed)
        self.assertIn("HOOK_001", {v.rule_id for v in rep_r.violations})

        # Conflict / system opening in ch1 passes cleanly
        good_open = (
            "【叮！神级鉴宝系统已激活，宿主当前账户余额：三百万元！】\n\n"
            "“把退婚书签了！”林家大少把一叠钞票狠狠甩在陈默脸上。\n\n"
            "陈默抬脚踩住那张支票，抓起桌上的紫砂壶直接砸在对方脚边。"
        )
        rep_ok = self.checker.scan(good_open, scene=_make_tomato_scene(chapter=1), project_meta=self.meta)
        self.assertTrue(rep_ok.passed, f"Unexpected violations: {rep_ok.violations}")

        # Spoken dialogue mentioning weather during an active Ch.1 conflict opening passes HOOK_001
        dialogue_weather_open = (
            "“别看外面乌云密布，这单生意今天必须谈成。”陈默把合同摔在桌上。\n\n"
            "对方脸色一僵，伸手去按桌角的警铃，被陈默一把扣住手腕。"
        )
        rep_dlg = self.checker.scan(
            dialogue_weather_open,
            scene=_make_tomato_scene(chapter=1),
            project_meta=self.meta,
        )
        self.assertTrue(rep_dlg.passed, f"Dialogue weather triggered HOOK_001: {rep_dlg.violations}")

    def test_hook_002_blocks_deflating_ending(self) -> None:
        bad_end = (
            "陈默一脚踹翻了桌案，冷声报出底价。\n\n"
            "这一夜无话，众人各自散去，他拉上被子沉沉睡去，期待明天。"
        )
        rep = self.checker.scan(bad_end, scene=_make_tomato_scene(chapter=2), project_meta=self.meta)
        self.assertFalse(rep.passed)
        self.assertIn("HOOK_002", {v.rule_id for v in rep.violations})

    def test_safe_001_to_safe_004_compliance_and_cliches(self) -> None:
        # SAFE_001: real modern location + law enforcement org
        rep_loc = self.checker.scan("陈默连夜赶到北京市公安局报案，又去了清华大学找教授。")
        self.assertFalse(rep_loc.passed)
        self.assertIn("SAFE_001", {v.rule_id for v in rep_loc.violations})

        # SAFE_002: symbol evasion 警*察 / 肉.体
        rep_evade = self.checker.scan("巷口站着几个警*察，地上倒着一具焦黑的肉.体。")
        self.assertFalse(rep_evade.passed)
        self.assertIn("SAFE_002", {v.rule_id for v in rep_evade.violations})

        # SAFE_003: NSFW / gore / minor redlines
        rep_red = self.checker.scan("反派狂笑着要把对手碎尸万段，手段极其残忍。")
        self.assertFalse(rep_red.passed)
        self.assertIn("SAFE_003", {v.rule_id for v in rep_red.violations})

        # SAFE_004: AI clichés
        rep_cliche = self.checker.scan("他眼底带着三分薄凉，一时间周围的空气仿佛凝固了。")
        self.assertFalse(rep_cliche.passed)
        self.assertIn("SAFE_004", {v.rule_id for v in rep_cliche.violations})

    def test_ooc_saint_blocks_uncompensated_forgiveness_and_moralizing(self) -> None:
        saint_text = (
            "虽然他害死了陈默的父亲，但陈默叹了口气：“算了，冤冤相报何时了，我决定原谅他，放他一马吧。”\n\n"
            "陈默转头又哭着替凶手求情，大声说道：暴力不能解决任何问题，每个人都有活下去的权利。"
        )
        rep = self.checker.scan(saint_text)
        self.assertFalse(rep.passed)
        self.assertIn("OOC_SAINT", {v.rule_id for v in rep.violations})

    def test_state_consistency_cross_gates(self) -> None:
        snapshot = WorldStateSnapshot(
            current_chapter=4,
            numeric_balances={"cash": 3_000_000.0},
            character_states={
                "wang_hu": CharacterWorldState(
                    character_id="wang_hu",
                    name="王虎",
                    status="deceased",
                    deceased_chapter=2,
                ),
                "su_qing": CharacterWorldState(
                    character_id="su_qing",
                    name="苏晴",
                    status="alive",
                    forbidden_titles=["老公"],
                ),
            },
            forbidden_titles={"su_qing": ["老公"]},
            active_expectations={
                "EXP_01": ExpectationState(
                    id="EXP_01",
                    title="当众打脸拍卖会主事",
                    planted_chapter=1,
                    due_chapter=3,
                    status="active",
                )
            },
            recent_locations=["loc_auction", "loc_auction"],
        )

        # Trigger all 5 STATE_* violations at once
        bad_state_text = (
            "王虎推门走了进来，冷笑道：“你们都没想到吧？”\n\n"
            "苏晴抓着他的袖子喊了一声：“老公，我错了。”\n\n"
            "陈默随手刷卡支付了500万买下整栋商铺。"
        )
        scene_ch5 = _make_tomato_scene(chapter=5, location_id="loc_auction", min_chars=10)
        rep = self.checker.scan(bad_state_text, scene=scene_ch5, world_state=snapshot, project_meta=self.meta)
        self.assertFalse(rep.passed)
        rule_ids = {v.rule_id for v in rep.violations}
        self.assertIn("STATE_DECEASED", rule_ids)
        self.assertIn("STATE_FORBIDDEN_TITLE", rule_ids)
        self.assertIn("STATE_OVERDRAFT", rule_ids)
        self.assertIn("STATE_EXPECTATION_OVERDUE", rule_ids)
        self.assertIn("STATE_STALE_LOCATION", rule_ids)

        # Positive counterfactual: flashback mention of deceased, within-budget spend, different location, fulfilled expectation
        clean_snapshot = WorldStateSnapshot(
            current_chapter=4,
            numeric_balances={"cash": 3_000_000.0},
            character_states={
                "wang_hu": CharacterWorldState(
                    character_id="wang_hu",
                    name="王虎",
                    status="deceased",
                    deceased_chapter=2,
                ),
            },
            active_expectations={
                "EXP_01": ExpectationState(
                    id="EXP_01",
                    title="已兑现",
                    planted_chapter=1,
                    due_chapter=3,
                    status="fulfilled",
                )
            },
            recent_locations=["loc_auction", "loc_auction"],
        )
        ok_text = (
            "陈默看着王虎生前留下的旧账本，冷冷合上封面。\n\n"
            "随后他拿出黑卡，直接支付了200万盘下新码头仓库。"
        )
        rep_ok = self.checker.scan(
            ok_text,
            scene=_make_tomato_scene(chapter=5, location_id="loc_warehouse", min_chars=10),
            world_state=clean_snapshot,
            project_meta=self.meta,
        )
        self.assertTrue(rep_ok.passed, f"Unexpected state violations: {rep_ok.violations}")

        # Chinese numeral expenditure & spirit_stones unit match
        cn_snapshot = WorldStateSnapshot(
            current_chapter=4,
            numeric_balances={"cash": 3_000_000.0, "spirit_stones": 100.0},
        )
        rep_cn = self.checker.scan(
            "陈默直接花掉五百万买下地皮，又一口气消耗了三百块灵石。",
            scene=_make_tomato_scene(chapter=5, min_chars=10),
            world_state=cn_snapshot,
            project_meta=self.meta,
        )
        self.assertFalse(rep_cn.passed)
        overdrafts = [v for v in rep_cn.violations if v.rule_id == "STATE_OVERDRAFT"]
        self.assertEqual(len(overdrafts), 2)

    def test_tts_001_panel_length_pipe_and_progress_slash(self) -> None:
        long_panel = "【系统属性面板：" + "战力暴涨" * 16 + "】\n\n陈默握紧长刀。"
        rep_long = self.checker.scan(long_panel, panel_policy="allow_system_tag")
        self.assertFalse(rep_long.passed)
        self.assertIn("TTS_001", {v.rule_id for v in rep_long.violations})

        pipe_panel = "【宿主：陈默 | 等级：宗师】\n\n陈默抬手格挡。"
        rep_pipe = self.checker.scan(pipe_panel, panel_policy="allow_system_tag")
        self.assertFalse(rep_pipe.passed)
        self.assertIn("TTS_001", {v.rule_id for v in rep_pipe.violations})

        slash_panel = "【经验进度：380/500】\n\n陈默一脚踹飞沙袋。"
        rep_slash = self.checker.scan(slash_panel, panel_policy="allow_system_tag")
        self.assertFalse(rep_slash.passed)
        self.assertIn("TTS_001", {v.rule_id for v in rep_slash.violations})

        clean_panel = "【叮！当前功德值增加五百点，解锁神眼透视权限。】\n\n陈默抬眼看向柜台。"
        rep_ok = self.checker.scan(clean_panel, panel_policy="allow_system_tag")
        self.assertTrue(rep_ok.passed, f"Unexpected TTS_001 failure: {rep_ok.violations}")

    def test_rhythm_critical_brick_narration_wall_and_bare_dialogue(self) -> None:
        # 1. >120 char solid brick paragraph in Tomato short-para mode -> CRITICAL
        brick = "陈默把桌面上的账本翻了一遍又一遍，指尖顺着红笔圈出来的数字往下滑，窗外货车卸货的引擎声轰轰响着，他把那枚旧铜钥匙捏在掌心反复摩挲，又拿过算盘噼里啪啦拨出三笔亏空，随后抄起桌上那杯凉茶一口气喝干，站起身一把拉开了紧闭的铁门，迎面正撞上带人赶来堵门的账房大管家，两人视线在门框下硬生生撞在一起。"
        self.assertGreater(len(brick), 120)
        rep_brick = self.checker.scan(brick, scene=_make_tomato_scene(chapter=2), project_meta=self.meta)
        self.assertFalse(rep_brick.passed)
        self.assertIn("RHYTHM_BRICK", {v.rule_id for v in rep_brick.violations})
        self.assertIn(0, rep_brick.diagnostics["brick_paragraph_indices"])

        # 2. >320 char continuous pure narration across multiple short paragraphs -> CRITICAL
        short_narr_paras = [
            "陈默低头核对账本最后一页，红笔圈出的数字整整少了三百万，墨迹在台灯下泛着刺眼的光，每一行签字都透着古怪。",
            "他拉开柜台最底层的铁抽屉，摸出一把带锈的铜钥匙，又顺手把那张泛黄的借条折进西装内袋，指腹摩挲过折角。",
            "外头走廊响起杂乱的皮鞋声，几个壮汉扛着铁撬棍停在卷闸门外，铁锁被砸得哐当直响，门框灰尘簌簌往下掉。",
            "陈默没有退后半步，反而抄起墙角的紫檀木镇尺，反手拔掉了走廊里的总电闸，整栋楼瞬间黑下来，只剩窗外路灯。",
            "他顺着货架背面的暗梯摸上二楼天台，铁门上的铜挂锁早就锈死了，一脚踹开便能直通隔壁老巷的青石板路。",
            "雨点砸在铁皮棚顶上啪啪作响，陈默把账本塞进防水袋里，贴着墙根快步绕过街角的监控探头，钻进了巷口。",
            "巷口尽头停着一辆没有挂牌的黑色面包车，车灯熄灭，驾驶座上的烟头一明一暗，车门正从里面缓缓推开一条缝。",
        ]
        self.assertTrue(all(len(p) <= 120 for p in short_narr_paras))
        self.assertGreater(sum(len(p) for p in short_narr_paras), 320)
        rep_wall = self.checker.scan(
            "\n\n".join(short_narr_paras),
            scene=_make_tomato_scene(chapter=2),
            project_meta=self.meta,
        )
        self.assertFalse(rep_wall.passed)
        self.assertIn("RHYTHM_NARRATION_WALL", {v.rule_id for v in rep_wall.violations})

        # 3. >4 consecutive turns of bare dialogue without action/expression tags -> CRITICAL
        bare_turns = "\n\n".join(
            [
                "“你确定要退婚？”",
                "“当然确定。”",
                "“一分钱彩礼都不退？”",
                "“一分不退。”",
                "“那咱们就当场算清。”",
            ]
        )
        rep_bare = self.checker.scan(bare_turns, scene=_make_tomato_scene(chapter=2), project_meta=self.meta)
        self.assertFalse(rep_bare.passed)
        self.assertIn("RHYTHM_BARE_DIALOGUE", {v.rule_id for v in rep_bare.violations})
        self.assertEqual(rep_bare.diagnostics["bare_dialogue_runs"][0]["turn_count"], 5)

    def test_rhythm_warning_burstiness_and_bystander_shock_diagnostics(self) -> None:
        # Uniform 18-char sentences -> Burstiness sigma < 10 (WARNING, still passed=True)
        # Plus 2 bystander shock clichés -> RHYTHM_BYSTANDER_SHOCK (WARNING)
        uniform_text = "\n\n".join(
            [
                "陈默左手捏着鉴定书大步走到拍卖台前。陈默抬起右手把支票重重拍在红木桌上。",
                "全场众人瞬间鸦雀无声，齐齐瞪大了双眼。拍卖师看着签字盖章的印泥彻底呆住了。",
                "王总捧着那块残缺的玉佩站在原地发愣。围观人群全都张大了嘴巴，现场一片哗然。",
                "陈默拎起手提箱转身走出拍卖厅大门。门外停着的黑色轿车立刻按响了双闪灯。",
            ]
        )
        rep = self.checker.scan(uniform_text, scene=_make_tomato_scene(chapter=2, min_chars=20), project_meta=self.meta)
        self.assertTrue(rep.passed, f"Soft WARNINGs must not fail CRITICAL passed flag: {rep.violations}")
        warning_ids = {v.rule_id for v in rep.violations if v.severity == "WARNING"}
        self.assertIn("RHYTHM_BURSTINESS", warning_ids)
        self.assertIn("RHYTHM_BYSTANDER_SHOCK", warning_ids)
        self.assertLess(rep.diagnostics["burstiness_sigma"], 10.0)
        self.assertEqual(rep.diagnostics["bystander_shock_paragraphs"], [1, 2])
        self.assertTrue(set([1, 2]).issubset(set(rep.diagnostics["warning_paragraph_indices"])))

        # Verify checker.diagnose() returns matching structured diagnostics dict
        diag = self.checker.diagnose(uniform_text, scene=_make_tomato_scene(chapter=2, min_chars=20), project_meta=self.meta)
        self.assertEqual(diag["burstiness_sigma"], rep.diagnostics["burstiness_sigma"])
        self.assertEqual(diag["bystander_shock_paragraphs"], [1, 2])

    def test_state_overdraft_zero_balance_benign_prose_and_explicit_amounts(self) -> None:
        benign_text = (
            "陈默从兜里拿出一张纸条，低头看清上面写的地址。\n\n"
            "他靠在巷口墙边掏出一根烟点上，花了十分钟才赶到医院门口。\n\n"
            "等手里宽裕了，他迟早要买下一套房，顺手掏出一块手帕擦了擦汗，又花了一点时间核对账目。"
        )
        for balances in ({"cash": 0.0}, {"spirit_stones": 0.0}):
            ws = WorldStateSnapshot(current_chapter=1, numeric_balances=balances)
            rep = self.checker.scan(
                benign_text,
                scene=_make_tomato_scene(chapter=2, min_chars=10),
                world_state=ws,
                project_meta=self.meta,
            )
            self.assertTrue(
                rep.passed,
                f"Zero-balance {balances} triggered false-positive STATE_OVERDRAFT: {rep.violations}",
            )

        # Explicit monetary/resource expenditures still trigger STATE_OVERDRAFT on 0.0 balance
        cash_zero = WorldStateSnapshot(current_chapter=1, numeric_balances={"cash": 0.0})
        for expr in (
            "陈默当场花了500元买下旧木盒。",
            "陈默毫不犹豫刷了十万买下原石。",
            "陈默直接花掉五百万盘下铺面。",
            "陈默付了两万块定金。",
            "陈默砸出三千五百万拍下压轴古玉。",
        ):
            rep_over = self.checker.scan(
                expr,
                scene=_make_tomato_scene(chapter=2, min_chars=5),
                world_state=cash_zero,
                project_meta=self.meta,
            )
            self.assertFalse(rep_over.passed, f"Expected STATE_OVERDRAFT for '{expr}'")
            self.assertIn("STATE_OVERDRAFT", {v.rule_id for v in rep_over.violations})

        silver_zero = WorldStateSnapshot(current_chapter=1, numeric_balances={"silver": 0.0})
        rep_silver = self.checker.scan(
            "陈默拍桌花了一百两银子买下消息。",
            scene=_make_tomato_scene(chapter=2, min_chars=5),
            world_state=silver_zero,
            project_meta=self.meta,
        )
        self.assertFalse(rep_silver.passed)
        self.assertIn("STATE_OVERDRAFT", {v.rule_id for v in rep_silver.violations})

        stone_zero = WorldStateSnapshot(current_chapter=1, numeric_balances={"spirit_stones": 0.0})
        rep_stone = self.checker.scan(
            "陈默运转功法，一口气消耗了十块灵石。",
            scene=_make_tomato_scene(chapter=2, min_chars=5),
            world_state=stone_zero,
            project_meta=self.meta,
        )
        self.assertFalse(rep_stone.passed)
        self.assertIn("STATE_OVERDRAFT", {v.rule_id for v in rep_stone.violations})

    def test_state_forbidden_title_pronoun_rejection_exemption(self) -> None:
        ws = WorldStateSnapshot(
            current_chapter=3,
            character_states={
                "su_qing": CharacterWorldState(
                    character_id="su_qing",
                    name="苏晴",
                    status="alive",
                    forbidden_titles=["老公", "夫君"],
                ),
            },
            forbidden_titles={"su_qing": ["老公", "夫君"]},
        )
        exempt_text = (
            "“别叫我老公，我们已经离婚了。”陈默推开她的手腕。\n\n"
            "“也别再叫他夫君，宗门契约早就作废。”长老冷声开口。"
        )
        rep_ok = self.checker.scan(
            exempt_text,
            scene=_make_tomato_scene(chapter=4, min_chars=10),
            world_state=ws,
            project_meta=self.meta,
        )
        self.assertTrue(rep_ok.passed, f"Unexpected STATE_FORBIDDEN_TITLE: {rep_ok.violations}")

        # Direct forbidden usage without rejection prefix still fails
        rep_bad = self.checker.scan(
            "苏晴眼眶通红，抓着他的衣角低声喊道：“老公，我错了。”",
            scene=_make_tomato_scene(chapter=4, min_chars=10),
            world_state=ws,
            project_meta=self.meta,
        )
        self.assertFalse(rep_bad.passed)
        self.assertIn("STATE_FORBIDDEN_TITLE", {v.rule_id for v in rep_bad.violations})


if __name__ == "__main__":
    unittest.main()
