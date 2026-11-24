from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import StyleProfile

VALID_CHANNELS = frozenset({"male", "female", "general"})
VALID_POV_MODES = frozenset({"third_limited", "first_person"})
VALID_PANEL_POLICIES = frozenset({"forbid", "allow_system_tag"})


class UnknownGenreError(ValueError):
    """Raised when querying an unregistered genre_id."""


@dataclass(frozen=True)
class GoldenChapterBeat:
    chapter: int
    role: str
    opening_hook: str
    core_conflict: str
    payoff_or_twist: str
    chapter_end_hook: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommonSenseCard:
    domain: str
    rules: List[str]
    reference_table: Dict[str, str] = field(default_factory=dict)
    forbidden_anachronisms: List[str] = field(default_factory=list)
    disclaimer: str = ""

    def format_prompt_block(self) -> str:
        lines: List[str] = [f"### 题材硬常识防穿帮卡（{self.domain}）"]
        for r in self.rules:
            lines.append(f"- {r}")
        if self.reference_table:
            lines.append("- 硬核参照基准：")
            for k, v in self.reference_table.items():
                lines.append(f"  - {k}：{v}")
        if self.forbidden_anachronisms:
            lines.append(f"- 严禁穿帮项：{'；'.join(self.forbidden_anachronisms)}")
        if self.disclaimer:
            lines.append(f"- 合规声明：{self.disclaimer}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GenreRecipe:
    genre_id: str
    display_name: str
    channel: str  # "male" | "female" | "general"
    tomato_primary_category: str
    tomato_tags: List[str]
    pov_mode: str  # "third_limited" | "first_person"
    panel_policy: str  # "forbid" | "allow_system_tag"
    golden_word_count_range: Tuple[int, int]
    title_templates: List[str]
    title_examples: List[str]
    blurb_templates: List[str]
    opening_300w_directive: str
    golden_three_beats: List[GoldenChapterBeat]
    style_profile: StyleProfile
    common_sense_card: CommonSenseCard
    rhythm_burstiness_check: bool
    allow_pov_inner_monologue: bool

    @property
    def min_chapter_words(self) -> int:
        return self.golden_word_count_range[0]

    @property
    def max_chapter_words(self) -> int:
        return self.golden_word_count_range[1]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "genre_id": self.genre_id,
            "display_name": self.display_name,
            "channel": self.channel,
            "tomato_primary_category": self.tomato_primary_category,
            "tomato_tags": list(self.tomato_tags),
            "pov_mode": self.pov_mode,
            "panel_policy": self.panel_policy,
            "golden_word_count_range": list(self.golden_word_count_range),
            "title_templates": list(self.title_templates),
            "title_examples": list(self.title_examples),
            "blurb_templates": list(self.blurb_templates),
            "opening_300w_directive": self.opening_300w_directive,
            "golden_three_beats": [b.to_dict() for b in self.golden_three_beats],
            "style_profile": self.style_profile.to_dict(),
            "common_sense_card": self.common_sense_card.to_dict(),
            "rhythm_burstiness_check": self.rhythm_burstiness_check,
            "allow_pov_inner_monologue": self.allow_pov_inner_monologue,
        }


def _build_genre_registry() -> Dict[str, GenreRecipe]:
    recipes: List[GenreRecipe] = [
        # =====================================================================
        # MALE CHANNEL (5)
        # =====================================================================
        GenreRecipe(
            genre_id="urban_brainhole",
            display_name="都市脑洞",
            channel="male",
            tomato_primary_category="都市脑洞",
            tomato_tags=["系统", "神豪", "职场逆袭"],
            pov_mode="third_limited",
            panel_policy="allow_system_tag",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "{规则}，{身份}全麻了",
                "分手后，{金手指}每天返现十倍",
                "让你{日常}，没让你买下大楼",
                "被裁后，{外挂}到账一千万",
            ],
            title_examples=[
                "被裁当天，我的工位能刷出百亿",
                "每天返现一万，前任跪求复合",
                "让你送外卖，没让你买下整条街",
            ],
            blurb_templates=[
                (
                    "实习转正前夜，陈牧野被主管抢走核心方案，连同租住的隔断间也被房东限时清空。\n\n"
                    "兜里只剩三百块时，他手机弹出一条【每日破局账单】：只要当面拆穿一次职场黑幕，"
                    "就能按对方损失的公关价值十倍返现。他反手把加密底稿投上了全公司大屏。\n\n"
                    "起初众人只当他是光脚不怕穿鞋的疯子，直到他陆续买下整栋写字楼、控股对家风投，"
                    "曾经高不可攀的总监在电梯门口替他按住下行键，额头全是冷汗。"
                ),
            ],
            opening_300w_directive=(
                "前200字禁止写雨夜感慨、禁止户口本式履历；第1句直接落入具体羞辱或倒计时危机"
                "（如被抢功劳/押金被扣/限期搬离），第300字前必须触发脑洞规则或金手指首笔实质收益。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="破局激活与首笔立威",
                    opening_hook="当场被夺功劳或被迫赔款，手机/账单倒计时只剩两小时",
                    core_conflict="反派当众拿捏主角生计底线，周围同事冷眼旁观",
                    payoff_or_twist="触发系统/脑洞机制完成首次反手打脸并拿到第一笔现金或物权证据",
                    chapter_end_hook="更大一级的利益方盯上现场异动，电话直接打进办公室内线",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="升级验证与规则边界试探",
                    opening_hook="紧接电话威胁，反派试图用人脉封杀主角退路",
                    core_conflict="主角必须用首笔收益撬动更高倍率规则，同时摸清金手指冷却条件",
                    payoff_or_twist="在高端消费/竞拍/行业现场反向砸盘，反派靠山当场翻车",
                    chapter_end_hook="系统解锁二级权限，暴露真正幕后操控者名单",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="人前显圣与新地图铺路",
                    opening_hook="核心圈层聚会或签约现场，旧敌携新靠山企图当众踩头",
                    core_conflict="身份信息差全面拉开，对方仍按旧认知羞辱主角",
                    payoff_or_twist="新资产或幕后控股权当场亮牌，完成黄金三章第一次大爽点收口",
                    chapter_end_hook="神秘买家或更高阶行业大佬递出邀约函，引出下一轮十章主线",
                ),
            ],
            style_profile=StyleProfile(
                id="urban_brainhole_human",
                name="都市脑洞快节奏白描样本",
                granularity_notes=(
                    "竖屏短段呼吸感，多用具体金额、发票、转账回执、门禁卡、楼层数字推进爽感；"
                    "对话干脆带刺，人物不废话讲道理，直接用动作与资产变更砸脸。"
                ),
                sample_text=(
                    "人事主管把辞退通知推过玻璃桌时，陈牧野兜里只剩三百二十块零钱。\n"
                    "“补偿金按最低标准走，下午五点前把工位清干净。”主管抬手看了眼腕表，"
                    "“你那份算法底稿，项目组已经移交给小张了。”\n"
                    "【叮！检测到恶性职场侵占，十倍公关反噬账单已激活，当前可结算额度：一百八十万。】\n"
                    "陈牧野把工牌摘下来，顺手按在桌上那份移交单的签名栏旁。他点开手机邮件，"
                    "把昨晚备份的原始提交时间戳，直接群发进了全集团两千人的公告群。"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="现代都市金融与职场常识",
                rules=[
                    "大额转账须符合银行风控逻辑，动用千万级资金应借助正规托管账户、股权质押或并购协议掩护，严禁无来源现金直接塞满行李箱进银行。",
                    "所有城市、高校、执法与监管机构必须使用合规架空名（如海城、江城大学、市局巡捕、市场稽查司），严禁出现现实地名与真实政法机关名称。",
                    "急救与医疗场景严禁违背基础常识：心电图成直线属于不可电击心律，不能靠除颤仪强行电活。",
                ],
                reference_table={
                    "一线架空都市普通白领月薪": "6,000～14,000 元",
                    "银行单日普通卡转账限额": "5万～50万元（大额需网银盾或柜台）",
                    "合租单间押一付三常见起步支出": "6,000～12,000 元",
                },
                forbidden_anachronisms=[
                    "现实省市高校全称",
                    "真实公安/法院/纪委番号",
                    "无抵押当天空手批下百亿纯信用贷",
                ],
                disclaimer="本故事发生于现代架空平行都市（如大夏海城），一切机构、地名与金融规则均为虚构。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="xuanhuan_brainhole",
            display_name="玄幻脑洞",
            channel="male",
            tomato_primary_category="玄幻脑洞",
            tomato_tags=["系统", "穿越", "迪化反套路"],
            pov_mode="third_limited",
            panel_policy="allow_system_tag",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "让你{行为}，你直接{突破}",
                "宗门都以为我是{误解身份}",
                "签到{地点}十年，出关即无敌",
                "别人都修仙，只有我靠{机制}",
            ],
            title_examples=[
                "让你劈柴十年，你劈出了剑意",
                "全宗门都脑补我是隐世老祖",
                "练气九千层，师尊求我别飞升",
            ],
            blurb_templates=[
                (
                    "杂役院弟子赵启明资质垫底，只配每天在后山劈满三百斤铁木。\n\n"
                    "他不知道自己绑定了【百倍劳作熟练度面板】，每次挥斧砍柴都在叠加剑道基础暴击。"
                    "外门大比前夜，他随手削了一根扁担去挑水，路过演武场时顺手挡下内门天骄的必杀一剑。\n\n"
                    "长老们连夜翻遍宗门古籍，一致认定后山藏着一尊压境万年的绝世剑尊；"
                    "而赵启明看着面板上刚刷新的砍柴任务，只关心明天食堂还留不留红烧肉。"
                ),
            ],
            opening_300w_directive=(
                "开篇禁止万字世界观境界科普！前200字直接丢出宗门退婚/抢夺灵田/杂役受罚现场，"
                "第300字内亮出反套路外挂效果，用外行动作碾压内行骄子制造反差。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="低位受压与反常露一手",
                    opening_hook="杂役/废柴被强征配额或当众抢走丹药灵石",
                    core_conflict="外门执法弟子倚强凌弱，限一炷香内交出保命物件",
                    payoff_or_twist="靠奇葩面板叠加效果一招破防，顺带引发周围人初次误解/震惊",
                    chapter_end_hook="执事长老亲临勘察现场碎裂的法器残片",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="信息差放大与首次收割",
                    opening_hook="长老暗中试探主角真实深浅，态度一百八十度转弯",
                    core_conflict="敌对师兄找来真传靠山报复，企图借门规扣帽子",
                    payoff_or_twist="主角按日常习惯出手却直接打穿高阶阵法，反手拿走对方赌注资源",
                    chapter_end_hook="宗门秘境异动或魔道探子潜入，点名要查主角来历",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="小高潮立稳地位",
                    opening_hook="宗门大殿问责与秘境名额分配冲突",
                    core_conflict="保守派质疑主角出身杂役不配占核心资源",
                    payoff_or_twist="当众触发异象或顺手治好护山老祖暗疾，坐实高人/怪才名号",
                    chapter_end_hook="秘境大门开启，更大地图的强敌与隐藏机缘浮出水面",
                ),
            ],
            style_profile=StyleProfile(
                id="xuanhuan_brainhole_human",
                name="玄幻脑洞反差爽感样本",
                granularity_notes=(
                    "重点刻画剑气劈开青石的脆响、丹药药渣气味、法衣裂口与配角心理落差；"
                    "境界名词紧贴剧情需要逐步甩出，不堆砌设定说明书。"
                ),
                sample_text=(
                    "铁斧落下去时，院墙根下的青石板裂开一道三寸深的细缝。\n"
                    "外门执事张虎踢翻水桶，浑浊的井水泼湿了赵启明的布鞋：“明天交不出十捆黑铁木，"
                    "你这间破柴房就给新来的师弟腾地方。”\n"
                    "【叮！累计挥斧一万次，基础劈砍进阶为‘断脉劲（圆满）’，灵力穿透力提升十倍。】\n"
                    "赵启明弯腰捡起半截扁担，对着张虎腰间那块号称能挡筑基一击的护身玉佩，轻轻点了一下。"
                    "清脆的裂响声里，玉佩碎成了两半。"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="玄幻修仙体系与战力自洽常识",
                rules=[
                    "全书境界阶位、灵石换算比（如 1 上品 = 100 中品 = 10,000 下品）必须在开篇锁死，不可前章一万灵石买破剑、后章十块灵石当巨款。",
                    "受分卷上限锁约束，前卷不可越级秒杀高出两个大境界且无合理克制链的敌人。",
                    "严禁使用现实低俗网络烂梗（如‘奥利给’、‘YYDS’）破坏古代玄幻语境，现代梗仅限穿越者内心吐槽。",
                ],
                reference_table={
                    "下品/中品/上品灵石常见换算": "100:1 进制",
                    "外门杂役月例": "3～10 块下品灵石",
                    "炼气期至筑基期过渡跨度": "需筑基丹或特殊灵脉破境",
                },
                forbidden_anachronisms=[
                    "同卷内战力通胀失控",
                    "丹药药效与炼丹耗时前后矛盾",
                ],
                disclaimer="纯属东方玄幻架空异界设定，境界与法术体系为虚构文学创作。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="apocalypse_hoarding",
            display_name="末世囤货",
            channel="male",
            tomato_primary_category="科幻末世",
            tomato_tags=["末世", "囤物资", "空间基建"],
            pov_mode="third_limited",
            panel_policy="allow_system_tag",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "冰封倒计时，囤满{容量}仓库",
                "重生末世前，花光{金额}囤货",
                "别人啃树皮，我在屋里{享受}",
                "寒潮降临，我打造了{等级}堡垒",
            ],
            title_examples=[
                "重生极寒前，我搬空三大批发城",
                "全球冰封，我在安全屋涮铜锅羊肉",
                "末世降临，先给仇人留空仓库",
            ],
            blurb_templates=[
                (
                    "上一世，宋怀安把最后半块压缩饼干分给邻居家，却在零下六十度的夜里被对方反锁在门外冻死。\n\n"
                    "再睁眼，他回到极寒暴雪降临前七天，祖传吊坠里的百亩保鲜空间刚刚激活。"
                    "他连夜抵押全部不动产，租下郊区冷库与重型钢材厂，按卡路里、净水滤芯和柴油发电机组清单疯狂扫货。\n\n"
                    "七天后，冰封锁城，整栋楼都在断水断电的寒风里哭爹喊娘；"
                    "宋怀安隔着三层防弹真空玻璃，往自热铜锅里下了一盘手切鲜羊肉。"
                ),
            ],
            opening_300w_directive=(
                "前200字必须在‘被背叛冻死/饥饿惨死’的重生痛感与‘倒计时天数’中直接开枪，"
                "第300字内明确空间容积/启动资金与第一笔硬核采购动作。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="血仇重生与空间验货",
                    opening_hook="死在极寒/丧尸潮背叛瞬间，猛然惊醒看到手机日历距末世仅剩7天",
                    core_conflict="时间紧、本金有限，还需甩开前世吸血亲戚或渣邻纠缠",
                    payoff_or_twist="变现全部资产，用真实装货动作验证空间时间静止保鲜属性",
                    chapter_end_hook="大宗采购引起黑心包工头或前世仇人上门讨债抢货",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="批量扫货与堡垒改造",
                    opening_hook="前世坑过自己的物业/熟人上门试图低价强买房产",
                    core_conflict="如何在不暴露空间秘密的前提下，完成千吨级粮油冷链与特种防盗门施工",
                    payoff_or_twist="反手坑对方付违约金，并搞定独立太阳能、蓄电池组与无烟壁炉改装",
                    chapter_end_hook="气象台发出反常红色寒潮/红雾预警，倒计时缩至最后24小时",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="灾难降临与首次守门反杀",
                    opening_hook="极寒暴雪/酸雨突降，全城停水断电网断信号",
                    core_conflict="自私邻居或前世仇人砸门强借暖气与食物，企图道德绑架",
                    payoff_or_twist="展示安全屋碾压级物理防御与热腾腾晚餐香气对比，狠绝立威不圣母",
                    chapter_end_hook="楼外雪地出现第一批变异或 armed 掠夺小队踪迹",
                ),
            ],
            style_profile=StyleProfile(
                id="apocalypse_hoarding_human",
                name="末世囤货硬核清单流样本",
                granularity_notes=(
                    "强调真实物理参数：冷库吨位、柴油标号（-35号防冻）、卡路里配比、双层断桥铝真空玻璃、"
                    "食物香气与室外零下气温的感官对冲；绝不圣母心软送物资。"
                ),
                sample_text=(
                    "手机屏幕上的日期停在十月十四日早晨七点零二分，距离那场连下四十天的黑雪还有七天。\n"
                    "宋怀安摸向胸口，那枚在冰窖里染过血的青铜挂坠还贴着锁骨发烫。他试着把床头那杯刚倒的热豆浆收进去，"
                    "三分钟后再取出来，杯壁上仍挂着刚出锅的白雾。\n"
                    "【空间容积：十万立方米，内部时间静止。】\n"
                    "他抓起车钥匙下楼，先去了建材市场订了六扇乙级防盗暗门和两百平米三层夹胶钢化玻璃，"
                    "随后直奔城南冻品批发库——离十月二十一号越近，一斤五花肉的价格越难谈。"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="末世生存与物理能量守恒常识",
                rules=[
                    "囤货重量与空间容积必须算得清账（1立方米水重1吨，卡车单趟载重需合理），严禁虚空变出不符合商业供应链的物资。",
                    "极寒环境下普通0号柴油会结蜡凝固，必须明确采购-35号防冻柴油或蓄电池保温系统；室内烧炭取暖必须交待排烟管道与一氧化碳报警。",
                    "食物保质期与净水滤芯损耗须受虚拟日历约束（无静止空间不可囤鲜奶半年不坏）。",
                ],
                reference_table={
                    "成年人每日基础饮水最低需求": "2～3 升",
                    "极寒防冻柴油标号": "-35# 或 -50#",
                    "常见 20 尺标准集装箱容积": "约 33 立方米",
                },
                forbidden_anachronisms=[
                    "断电断网半年后手机还能正常刷外卖/扫码支付",
                    "密闭房间直接烧煤炉不通风却不中毒",
                ],
                disclaimer="本故事为科幻末世灾变架空虚构创作，涉及防灾物资仅作小说推演。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="suspense_rules",
            display_name="悬疑脑洞（规则怪谈）",
            channel="male",
            tomato_primary_category="悬疑脑洞",
            tomato_tags=["规则怪谈", "惊悚推理", "无女主"],
            pov_mode="first_person",
            panel_policy="allow_system_tag",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "怪谈：{场所}守则第{数字}条",
                "别信{物件}！规则里藏着真话",
                "动物园第{数字}条守则写反了",
                "全员守规则，只有我{反操作}",
            ],
            title_examples=[
                "规则怪谈：病房第4条守则写反了",
                "午夜便利店，别卖蓝衣服客人水",
                "我在怪谈副本里靠找漏洞活着",
            ],
            blurb_templates=[
                (
                    "一觉醒来，我被投放到全城直播的【青山精神病院规则副本】，手里只有一张染血的值班守则。\n\n"
                    "第一条：凌晨两点后不要回应走廊里的叫门声。\n"
                    "第二条：如果看见穿蓝大褂的医生，立刻躲进床底——除非他手里拿着体温计。\n\n"
                    "全球观众都以为我会死在第一夜的逻辑陷阱里，但我盯着纸条背面被墨水涂掉的那行小字，"
                    "发现这八条守则里，有两条本身就是‘它们’伪造的。"
                ),
            ],
            opening_300w_directive=(
                "第一人称直接丢进诡异封闭现场，前250字内必须完整贴出3～5条具体可推理的纸面规则"
                "（注意单条`【】`面板严格`<=65`字且不含`|`和斜杠），并立即发生第一起与规则冲突的敲门/异常事件。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="入局读规与首条悖论",
                    opening_hook="睁眼置身封闭场所，面前贴着带血迹/涂改痕迹的纸质守则",
                    core_conflict="同伴盲从错误规则当场出事，或两条规则互相打架",
                    payoff_or_twist="用物理细节（钟表倒走/门牌号奇偶/反光倒影）识破第一条假规则",
                    chapter_end_hook="倒计时铃声响起，真正的夜班巡查者脚步停在门外",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="利用漏洞反制诡异",
                    opening_hook="巡查者按规则查房，其余幸存者吓得不敢呼吸",
                    core_conflict="对方逼问违反字面规则的破绽，稍有犹豫即触发抹杀",
                    payoff_or_twist="用两条真规则的交叉逻辑反杀/卡死怪物行动权限，拿到隐藏钥匙",
                    chapter_end_hook="解锁二楼禁区，发现副本真正背景日志与管理员残页",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="揭开底层机制并通关/升阶",
                    opening_hook="污染值逼近临界，副本主规则开始篡改字面文字",
                    core_conflict="在真假混杂的终局指令中找出唯一生门",
                    payoff_or_twist="完成首次高评分完美拆解，拿到特殊免死道具或天赋词条",
                    chapter_end_hook="现实直播间沸腾，下一轮更高危S级规则副本预告弹出",
                ),
            ],
            style_profile=StyleProfile(
                id="suspense_rules_human",
                name="规则怪谈第一人称冷悚样本",
                granularity_notes=(
                    "第一人称‘我’感官紧贴呼吸声、日光灯管频闪、门缝透光与纸张折痕；"
                    "规则提示框简洁干脆（<=65字，无竖线斜杠），推理过程靠物证闭环而非瞎猜。"
                ),
                sample_text=(
                    "走廊顶上的日光灯管滋啦闪了两下，我低头核对墙上那张泛黄的《夜班保安守则》。\n"
                    "【守则三：凌晨一点前若有人敲值班室门送夜宵，请收下并道谢。】\n"
                    "字迹是蓝黑圆珠笔写的，可纸面右下角有一块被橡皮擦破的毛边，隐约能看清底下"
                    "压着一个铅笔写的‘别’字。\n"
                    "我把挂在墙上的老式机械表摘下来，贴到耳边听了三秒——秒针走了，分针却一直卡在十二点五十五分。"
                    "外头的水磨石地面上，忽然响起推餐车的铁轮滚动声，停在了玻璃窗正下方。"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="规则推理自洽与合规唯物/异界声明",
                rules=[
                    "所有规则谜题必须满足可推理闭环（如笔迹颜色、钟表误差、镜像左右颠倒），严禁主角全靠‘突然灵光一闪’机械降神。",
                    "严禁出现现实真实宗教邪教仪式、真实招魂法阵教程或宣扬封建迷信。",
                    "听书友好约束：规则提示框【...】每次严格不超过65字，严禁使用 `|` 表格线或 `30/100` 斜杠进度条。",
                ],
                reference_table={
                    "单次【规则提示】硬限长": "<= 65 汉字",
                    "禁止听书污染符号": "| 、/ 、==== 、----",
                },
                forbidden_anachronisms=[
                    "真实医院/学校/地名作为怪谈副本场景",
                    "无法从前文线索推导出的后补规则",
                ],
                disclaimer="本故事为纯虚构架空规则推理创作，世间并无鬼神，请相信科学、反对迷信。",
            ),
            rhythm_burstiness_check=True,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="history_brainhole",
            display_name="历史脑洞",
            channel="male",
            tomato_primary_category="历史脑洞",
            tomato_tags=["穿越", "架空历史", "种田基建"],
            pov_mode="third_limited",
            panel_policy="forbid",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "穿成{身份}，我用{技术}救国",
                "大旱三年，我靠{工艺}养活一县",
                "陛下别慌，臣真只是个{职业}",
                "古代荒年，我造出{硬核发明}",
            ],
            title_examples=[
                "穿成落魄皇子，从晒精盐开始造反",
                "大旱荒年，我靠水车养活十万流民",
                "陛下，臣真只是个打铁的",
            ],
            blurb_templates=[
                (
                    "工科博士严子昂一睁眼，成了大胤朝被发配边陲苦寒县的废黜七皇子，手下只有三十个老弱衙役和两仓发霉陈粮。\n\n"
                    "京城权贵等着看他熬不过今冬冻饿而死，他却蹲在盐碱滩旁，用草木灰和黄泥垒起第一座粗盐提纯滤池。\n\n"
                    "三个月后，雪花般洁白的细盐卖穿了北境互市，随后是高炉炼焦铁、曲辕犁与黑火药。"
                    "当皇帝派钦差来摘桃子时，边城城墙上已经架起了三百门青铜臼炮。"
                ),
            ],
            opening_300w_directive=(
                "开篇直接切入‘饿殍/贬谪/逼税/死罪’的古代生存绝境，前300字必须给出主角依仗的现代专业技能切入口"
                "（如制盐、烧石灰水泥、曲辕犁结构或外科缝合），不写流水账穿越过程。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="死局开局与技术破冰",
                    opening_hook="被发配/抄家/逼缴军粮，距离掉脑袋或饿死只剩三天",
                    core_conflict="本地豪强或酷吏囤积居奇，故意卡死粮食或盐铁供应",
                    payoff_or_twist="就地取材用古代可得原料做出首件降维打击物资（雪花盐/烈酒/肥皂）",
                    chapter_end_hook="豪强带家丁上门强夺配方或县衙公堂发难",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="公堂反杀与立住钱袋子",
                    opening_hook="对方诬告私贩官盐或聚众谋反，企图当场拿下",
                    core_conflict="封建官僚体制下的身份与法统压制",
                    payoff_or_twist="用契约漏洞、高额税收分成绑住地方主官利益，反向抄没恶霸库房",
                    chapter_end_hook="第一批流民/工匠闻风投奔，或北方游牧游骑小股来犯",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="首战立威与势力雏形",
                    opening_hook="小股劫掠骑兵或土匪趁夜袭营抢粮",
                    core_conflict="冷兵器时代老弱兵卒面对精锐骑兵的绝望战力差",
                    payoff_or_twist="靠石灰粉、铁蒺藜、连弩阵型或土炸药完成零伤亡伏击全歼",
                    chapter_end_hook="京城密探或朝堂皇子注意到边陲异动，暗流开始涌向主角",
                ),
            ],
            style_profile=StyleProfile(
                id="history_brainhole_human",
                name="历史脑洞种田工科白描样本",
                granularity_notes=(
                    "讲究古代器物与工艺细节：草木灰碱水比例、陶缸结晶火候、生铁含碳脆度、麻布滤渣手感；"
                    "语言兼具古风分寸与现代工程逻辑，不用文绉绉晦涩文言。"
                ),
                sample_text=(
                    "北风刮过盐碱地，把严子昂冻裂的手背吹得生疼。他蹲在半人高的黄泥灶台前，"
                    "拿木瓢舀起一勺浑浊发苦的卤水，顺着铺满细河沙和木炭粉的竹筐慢慢淋下去。\n"
                    "老衙役凑在灶膛边添柴，嘴里直犯嘀咕：“殿下，官盐局的盐都是晒出来的黄疙瘩，"
                    "您这又是倒草木灰又是过滤的，能吃出白花花的银子？”\n"
                    "“黄疙瘩里头全是氯化镁，吃多了苦嗓子还坏肚子。”严子昂盯着大铁锅边缘析出的第一圈雪白晶粒，"
                    "抄起铁铲刮了一小撮拍在粗瓷碗沿上，“把北门那间空米铺的门板卸了，明早先卖十斤试试水。”"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="古代历史工艺与朝代时间线防穿帮",
                rules=[
                    "农作物传入硬红线：玉米、番薯、马铃薯、辣椒、烟草均为明代中后期（16世纪后）传入中国，唐宋及以前背景严禁直接‘路边烤红薯/吃麻辣火锅’（除非明确金手指兑换）。",
                    "古代制盐需知‘海水晒盐/井盐煮盐’，硝石制冰需交代硝石（硝酸钾）来源且不可直接误写为食用盐。",
                    "古代官职、度量衡（一两银子约合千文铜钱）与尊卑避讳（见尊长自称晚辈/下官，不可直接称‘老板/同志’）需保持语境统一。",
                ],
                reference_table={
                    "唐宋明清常见货币换算基准": "1 两白银 ≈ 1,000 文铜钱（随朝代波动）",
                    "美洲作物（玉米/番薯/辣椒）传入线": "明中后期（嘉靖万历后）",
                    "生铁炒钢工艺": "汉代已成熟，高炉焦炭炼铁晚至近代",
                },
                forbidden_anachronisms=[
                    "唐朝吃炒辣椒土豆丝",
                    "毫无铺垫手搓现代步枪和TNT",
                    "古代平民随便直呼皇帝名讳",
                ],
                disclaimer="架空历史平行时空创作，不影射真实历史人物与事件。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        # =====================================================================
        # FEMALE CHANNEL (5)
        # =====================================================================
        GenreRecipe(
            genre_id="modern_mind_reading",
            display_name="现言脑洞（读心术）",
            channel="female",
            tomato_primary_category="现言脑洞",
            tomato_tags=["读心术", "豪门打脸", "先婚后爱"],
            pov_mode="third_limited",
            panel_policy="forbid",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "偷听心声后，{人设}人设崩了",
                "全家都能听见我{吐槽内容}",
                "闪婚后，{对象}心里全是恋爱脑",
                "装乖三年，{真相}被他听见了",
            ],
            title_examples=[
                "全家偷听我心声，假千金连夜跑路",
                "领证当天，我听见高冷总裁在求夸",
                "心声泄露后，全豪门按我剧本走",
            ],
            blurb_templates=[
                (
                    "被接回许家的第一天，真千金许清禾看着跪在客厅哭诉的假千金，表面乖巧递茶，心里却在疯狂吐槽：\n"
                    "【哭哭哭，再哭我就把你偷偷转移公司账目的优盘甩大哥脸上！还有我那个冤大头二哥，下周就要被你未婚夫骗去担保三个亿了！】\n\n"
                    "她不知道，坐在沙发上的许家父母和三位哥哥，此刻一字不漏地听见了她的心声。\n\n"
                    "原本准备责备她的许母手一抖茶盏差点打翻；大哥直接叫停了原本要签字的合作案；"
                    "全家人表面不动声色，背地里连夜顺着她的吐槽把渣男贱女送进了督察署。"
                ),
            ],
            opening_300w_directive=(
                "开篇禁止人物简历铺垫！前200字直接放在豪门客厅/订婚宴/认亲饭局，"
                "必须形成‘表面温顺受气 vs 内心精准爆瓜吐槽’的反差，并让听见心声的家人/男主当场给出微动作反馈。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="认亲修罗场与心声初泄",
                    opening_hook="假千金茶言茶语陷害或渣男当众退婚羞辱",
                    core_conflict="女主懒得辩解只想躺平领分红，反派以为稳赢",
                    payoff_or_twist="女主内心剧透反派即将爆雷的黑料，被关键家人/男主完整听见并当场改写态度",
                    chapter_end_hook="二哥/男主忽然改变原定行程，反问女主一个细思极恐的问题",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="吃瓜破局与暗中护短",
                    opening_hook="反派不死心，设计酒会泼红酒/偷换合同陷阱",
                    core_conflict="周围宾客准备看真千金出丑，家人按原剧情本应偏心",
                    payoff_or_twist="女主心声精准预警陷阱位置，家人顺手反坑反派当众出洋相",
                    chapter_end_hook="男主（或联姻对象）现身，心声交锋或察觉男主同样有反差秘密",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="大瓜落地与反派首轮垮台",
                    opening_hook="家族核心项目签约或生日宴",
                    core_conflict="假千金联手外人企图吞并股份，证据藏在众人眼皮底下",
                    payoff_or_twist="女主心声点破密室/保险柜或转账流水线索，大哥当场报警抓人",
                    chapter_end_hook="更大牌面的幕后老狐狸（或男主隐秘身份）浮出水面",
                ),
            ],
            style_profile=StyleProfile(
                id="modern_mind_reading_human",
                name="现言读心喜剧反差样本",
                granularity_notes=(
                    "善用‘表面安静喝汤吃虾 vs 内心弹幕级吃瓜吐槽’的双线对比，重点抓听声者捏碎高脚杯、"
                    "筷子夹空、突然起身改口等喜剧微动作，轻快抓马不拖沓。"
                ),
                sample_text=(
                    "假千金跪在波斯地毯上红了眼眶，把那只裂了口的青花瓷瓶碎片往许清禾脚边推了推。\n"
                    "许清禾低头啃着手里最后半块蛋挞，嘴上软声认错：“抱歉，是我刚才端茶没端稳。”\n"
                    "可她脑子里的声音跟连珠炮似的在客厅顶上炸开：【演得真卖力，可惜这瓶子是她昨天在古玩街"
                    "花八十块买的高仿货。真品早被她顺手塞进后备箱，准备抵给城西赌场还债了。】\n"
                    "坐在主位上的许父端着茶杯的手猛地顿在半空，滚烫的茶水溅在手背上也没顾得上擦，"
                    "抬眼看向门口站着的司机：“老陈，去把二小姐车钥匙拿来，查查后备箱。”"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="现代豪门法务、医疗与读心视角规范",
                rules=[
                    "读心术只可暴露‘女主自己知道的内心吐槽’，严格遵守限知视点，不可让女主未卜先知她根本没接触过的陌生人隐秘（除非结合前世记忆或系统吃瓜词条）。",
                    "现代公司股权转让、离婚财产分割、刑侦抓捕须符合基本法务逻辑与架空地名机构规范（如督察署、市局巡捕、海城）。",
                    "禁止无脑雌竞辱女词汇，反派打脸聚焦做局侵占利益、造谣背刺等实质恶行。",
                ],
                reference_table={
                    "有限责任公司控股线": "51% 绝对控股 / 67% 重大事项表决权",
                    "合法架空替换词": "江城大学、海城集团、市局巡捕",
                },
                forbidden_anachronisms=[
                    "现实省市大学与政法部门全称",
                    "口头一句话就能把上市公司几十亿股权直接过户",
                ],
                disclaimer="现代都市架空浪漫幻想小说，地名、企业与法规均为虚构设定。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="ancient_rebirth",
            display_name="古言重生",
            channel="female",
            tomato_primary_category="古言脑洞",
            tomato_tags=["重生复仇", "宅斗", "清醒大女主"],
            pov_mode="third_limited",
            panel_policy="forbid",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "重生新婚夜，{渣男}我换人嫁",
                "和离后，{权臣}跪求我别走",
                "满门抄斩前，我先撕了{婚书}",
                "这一世，{恶毒亲戚}自己进火坑",
            ],
            title_examples=[
                "重生退婚夜，我转头嫁了权臣叔父",
                "抄家前夜，我搬空渣夫库房跑路",
                "和离后，前夫全家在冷风里哭",
            ],
            blurb_templates=[
                (
                    "上一世，蒋清禾耗尽嫁妆替夫家撑起爵位，临了却等来一杯夹竹桃毒酒——"
                    "夫君嫌她商户女出身粗鄙，早已在别院养好了名门贵女做平妻。\n\n"
                    "再睁眼，她回到大婚后第三日回门清点陪嫁的午前。看着婆婆理所当然递来的“贴补公中账册”，"
                    "她当着满堂宾客一把合上箱盖：嫁妆是我娘家带出来的，一根线头也不留。\n\n"
                    "她反手收回铺子田契、切断夫家命脉银根，拿着御赐牌匾另嫁皇城最冷硬的都察院左都御史；"
                    "昔日清高的侯府世子淋着大雨跪在朱门外求复合，门房却只递出一纸早已生效的和离书。"
                ),
            ],
            opening_300w_directive=(
                "前200字用毒酒咽喉灼痛或乱葬岗风雪直接拉开重生，第300字内必须切入具体宅斗交锋现场"
                "（如抢嫁妆、逼签卖身契、新婚夜换人算计），果断甩出反制杀招，绝不拖延受辱。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="惨死重生与立断嫁妆/婚约",
                    opening_hook="前世毒酒/火海惨死，惊醒在逼交嫁妆单或下药陷害的关键节点",
                    core_conflict="婆婆/继母/渣男当众道德绑架逼她让利或吃哑巴亏",
                    payoff_or_twist="用契约与官府律法直接锁死嫁妆归属，反手将计就计让恶人自食恶果",
                    chapter_end_hook="渣男或更高段位幕后反派起疑，暗中派人阻截她带走财物",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="反手做局与借势脱身",
                    opening_hook="黑夜院落围堵或宴席算计名节",
                    core_conflict="古代女子单枪匹马难抗宗族与爵位压迫，急需强力盟友或官面凭据",
                    payoff_or_twist="精准截胡前世权臣（男主）的救急物件或救命线索，达成互利交易",
                    chapter_end_hook="皇帝/太后或宗正寺介入，渣夫家丑当场半掩不住",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="撕破体面与第一阶段清算",
                    opening_hook="当众对质或府衙退婚/和离公堂",
                    core_conflict="对方企图倒打一耙污蔑女主不孝不贞",
                    payoff_or_twist="当场甩出前世记住的贪墨暗账或私通物证，彻底剥掉对方伪善人皮",
                    chapter_end_hook="皇城朝堂大局暗涌，更深的家族旧案线索指向新任合作权臣",
                ),
            ],
            style_profile=StyleProfile(
                id="ancient_rebirth_human",
                name="古言重生宅斗冷冽样本",
                granularity_notes=(
                    "注重古代服饰、红漆嫁妆箱、契约银票、宗法礼数与院落规矩；"
                    "女主清醒狠厉，说话留三分余地却句句戳在宗法与银钱痛点上。"
                ),
                sample_text=(
                    "咽喉里那股夹竹桃混着烧酒的腥苦味还没散尽，蒋清禾猛地睁开眼，鼻尖闻到的却是账房里上好的松烟墨香。\n"
                    "铜镜里还是她十九岁的模样，红漆樟木嫁妆箱敞着盖，正摆在正堂青砖地上。\n"
                    "婆母端着茶盏，慢条斯理地用盖子撇去浮沫：“既进了侯府门，前院修葺和二弟捐官的银子，"
                    "便先从你那十二间铺子的租子里支应吧，都是一家人，莫要小气。”\n"
                    "蒋清禾没接那本公中账册，只抬手让贴身丫鬟把红木箱盖啪地一声扣死，当场落了铜锁："
                    "“大胤律例写得明白，女子陪嫁归属本房私产。婆母若要借银子，按市面三分利写借据便是。”"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="古代宗法礼制、嫡庶妻妾称谓与婚姻律法",
                rules=[
                    "宗法称谓严禁错乱：正妻称‘夫人/正室/嫡母’，妾室属于半奴婢或良妾，绝不允许正妻对妾室或平妻自称‘奴婢’；庶出子女必须称正妻为‘母亲/嫡母’，称亲生妾室为‘姨娘’。",
                    "和离与休妻有别：和离需双方合意或官府判离，可带走全部嫁妆；犯‘七出’被休则嫁妆归属视朝代律法而定（宋明重护嫁妆，不可乱写净身出户）。",
                    "未出阁大家闺秀不可毫无遮掩深夜独闯外男书房如入无人之境，须通过屏风、丫鬟传话、偶遇茶楼或急救等合理礼制缝隙推进接触。",
                ],
                reference_table={
                    "嫡出/庶出称谓": "嫡母称‘母亲’，生母妾室称‘姨娘’",
                    "明清正妻嫁妆归属": "《大明律》规定妇人陪嫁原则上归妻家/本房私产",
                },
                forbidden_anachronisms=[
                    "正妻向小妾磕头自称奴婢",
                    "古代出现现代结婚证/离婚证名词",
                    "寒门小官嫁女动辄‘十里红妆抬进皇宫’",
                ],
                disclaimer="古代架空言情故事，礼制与律例参考传统文献做文学化虚构。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="era_7080_wealth",
            display_name="年代致富（七零八零）",
            channel="female",
            tomato_primary_category="年代",
            tomato_tags=["七零", "八零", "赶海摆摊致富"],
            pov_mode="third_limited",
            panel_policy="forbid",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "七零：{反差}后我带全家吃肉",
                "八零小媳妇，靠{手艺}成万元户",
                "重生七七年，我抢先考上{身份}",
                "亲戚抢工分，我转身去{致富}",
            ],
            title_examples=[
                "七零：退婚后我带全家天天吃肉",
                "八零摆摊卖盒饭，成全厂首富",
                "重生七零，先分家再考大学",
            ],
            blurb_templates=[
                (
                    "林红梅累死在生产队猪圈旁那天，才知道二婶一家靠克扣她亲爹的抚恤金，悄悄供出两个城里工人。\n\n"
                    "再睁眼，她回到一九七六年秋收刚完的当口，二婶正端着一碗掺了红薯糠的稀饭，"
                    "哄她把机械厂招工表让给堂姐。\n\n"
                    "她直接掀了稀饭碗，当着大队长的面要回亲爹留下的三十块月钱和两斤肉票，带着寡母幼弟连夜单过。"
                    "先做卤味下水换粮票，再借高考春风进省城摆摊做服装，等二婶家还在为半两油票打架时，她已经骑着嘉陵摩托回村盖起了两层红砖楼。"
                ),
            ],
            opening_300w_directive=(
                "前200字必须用精准的1970s/1980s生活细节定锚（如红薯稀饭、煤油灯、工分本、肉票/布票、搪瓷缸），"
                "直接爆发极品亲戚占工分/抢工作名额/克扣抚恤金冲突，300字内当场翻桌反击绝不忍气吞声。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="立锚年份与当场抢回权益",
                    opening_hook="197X年具体物资匮乏现场，亲戚正哄骗抢走招工表/抚恤金/口粮",
                    core_conflict="以孝顺/顾大局为名道德绑架女主放弃唯一翻身机会",
                    payoff_or_twist="当众算清工分与粮票账，在队长或邻里见证下抢回名额或彻底分家",
                    chapter_end_hook="拿到第一份稀缺资源（几斤肉票/招工体检表/海边退潮鱼获）",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="分家立户与第一桶票证",
                    opening_hook="分家单写好，坏心亲戚嘲笑孤儿寡母撑不过入冬",
                    core_conflict="无本钱无门路，怎么把有限食材/手艺变成现金和紧俏票证",
                    payoff_or_twist="靠一手卤下水/编草帽/修收音机在供销社或集市悄悄换回第一笔十块现金与粮票",
                    chapter_end_hook="供销社干事或部队军官/厂里技术员看中手艺，递出长单引子",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="首顿红烧肉立威与时代机遇",
                    opening_hook="飘出全村最香的红烧肉味，极品亲戚馋哭上门找茬",
                    core_conflict="对方企图扣‘投机倒把’帽子或强抢灶台吃食",
                    payoff_or_twist="亮出合法自留地/零散副业凭证（或厂里帮厨身份），顺带抓出对方偷盗把柄送去扫猪圈",
                    chapter_end_hook="公社广播响起恢复高考/承包到户通知，更大致富与读大学主线展开",
                ),
            ],
            style_profile=StyleProfile(
                id="era_7080_wealth_human",
                name="年代致富七零八零烟火气样本",
                granularity_notes=(
                    "紧抓年代质感：印着红字的‘中国人民银行’五元大团结、掉瓷的牡丹花搪瓷盆、大队大喇叭、"
                    "半肥半瘦五花肉下锅滋啦响、二八大杠车铃铛；爽点扎实在吃饱穿暖、凭票购物与分家扬眉吐气上。"
                ),
                sample_text=(
                    "大队长敲了敲手里的铜烟锅，屋里糊着旧报纸的土墙震下几粒白灰。\n"
                    "“红梅啊，你堂姐身子骨弱，扛不了锄头，机械厂那个临时工指标让给她，队里给你多记两个壮劳力工分。”"
                    "二婶站在门槛边，手里攥着那张印着红章的招工表，笑得脸上的褶子都堆到一块去了。\n"
                    "林红梅抓起桌上缺了口的搪瓷缸子，把里头照得见人影的红薯糊糊往猪食桶里一倒。\n"
                    "“二婶，去年我爹修水库落下的抚恤金一百八十块，还有厂里给的两张工业券，你先当着大队长的面结清。"
                    "这表谁亲爹换来的，就归谁去考。”"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="1970s–1980s 年代工资、票证与物价硬常识",
                rules=[
                    "1977年以前严禁出现大规模自由个体户摆摊（当时查‘投机倒把’极严），真正放开个体经济与‘万元户’在1979–1984年以后；70年代中前期致富主要靠供销社副业、手艺帮工、退伍津贴、恢复高考（1977年冬）或公社记工分。",
                    "1983年以前买粮食、布匹、猪肉、自行车、缝纫机不仅要钱，还必须凭对应地方/全国粮票、肉票、布票、工业券。",
                    "严禁1970年代工人月薪动辄好几百元，或花几十块钱就买走一整头大肥猪。",
                ],
                reference_table={
                    "七八十年代一级/二级正式工月薪": "一级工 33 元左右，二级工 38.5 元，老工人 50～70 元",
                    "猪肉统购价（70年代末）": "约 0.78～1.10 元/斤（需肉票）",
                    "大米统购价": "约 0.14～0.18 元/斤（需粮票）",
                    "鸡蛋价格": "约 0.05～0.08 元/个",
                    "三大件（自行车/缝纫机/手表）": "永久/凤凰二八大杠约 150～180 元（需工业券/自行车票）；上海牌手表约 120 元",
                    "1977年恢复高考时间": "1977年10月公布，12月考试，1978年春入学",
                },
                forbidden_anachronisms=[
                    "1976年手机微信转账/超市扫码/点外卖",
                    "1978年前随便个人跨省倒卖成车商品被叫‘万元户’",
                    "1980年前普通人随便去售楼处买商品房",
                ],
                disclaimer="年代背景文学创作，物价与时代风貌力求贴合历史生活记忆。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="quick_transmigration",
            display_name="快穿系统",
            channel="female",
            tomato_primary_category="快穿",
            tomato_tags=["快穿", "虐渣", "无CP大女主"],
            pov_mode="third_limited",
            panel_policy="allow_system_tag",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "快穿：炮灰女配她{掀桌动作}",
                "宿主她每天都在{崩人设}",
                "满级大佬穿成{悲惨炮灰}后",
                "系统求我做个人，我偏{爽点}",
            ],
            title_examples=[
                "快穿：恶毒女配掀桌不干了",
                "满级大佬专治各种恋爱脑女主",
                "宿主又把男主气进医院了",
            ],
            blurb_templates=[
                (
                    "绑定【炮灰逆袭绩效系统】时，系统苦口婆心叮嘱：宿主要忍辱负重走完原主苦情线，才好攒够积分反杀。\n\n"
                    "刚穿进第一个世界成为被真千金逼捐骨髓的替身女配，韩令仪看了一眼病床边举着手术同意书的亲生父母。\n\n"
                    "她当场撕碎同意书，顺手把渣男和偏心父母的私密录音投进全场记者话筒：\n"
                    "“按原剧情忍三年？抱歉，我只负责三章内把你们送进去踩缝纫机。”"
                ),
            ],
            opening_300w_directive=(
                "开篇直接跳过系统漫长新手教程！前150字直接醒在第一个快穿小世界的致命虐点现场"
                "（逼捐/退婚/沉塘/替罪），300字内快速接收原主记忆并瞬间掀桌反杀，效率拉满。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="首穿高危局与秒速立人设",
                    opening_hook="刚落地就面临原主被吸干血/顶罪坐牢/毁容的绝境",
                    core_conflict="系统提示原剧情压制与周围反派步步紧逼",
                    payoff_or_twist="一改原主软弱性子，当场拆招反踹反派并完成第一波积分入账",
                    chapter_end_hook="系统震惊提示‘爽点偏离度+40%’，反派慌乱摇人升级打压",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="精准拆解原男女主光环",
                    opening_hook="原世界男主或‘白莲花’主角以为女主只是欲擒故纵",
                    core_conflict="对方试图用资源封杀、舆论网暴强迫女主低头",
                    payoff_or_twist="女主利用现世/上个世界学到的硬技能与证据链，当场砸碎对方体面",
                    chapter_end_hook="暴露小世界幕后气运异常节点（如隐藏篡改器或系统漏洞）",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="首世界半局清账与爽感爆发",
                    opening_hook="反派彻底急眼，孤注一掷当众摊牌",
                    core_conflict="原主一生执念在此一举",
                    payoff_or_twist="用绝对实力碾压收尾，彻底夺回属于原主的人生与事业",
                    chapter_end_hook="小世界隐患初露或直接准备高分结算跳转下一个反差更大的位面",
                ),
            ],
            style_profile=StyleProfile(
                id="quick_transmigration_human",
                name="快穿系统爽利干脆样本",
                granularity_notes=(
                    "节奏极快、绝不内耗！少心理啰嗦，多直接利落的动作反击和系统简短提示框吐槽；"
                    "不同位面切换时用称谓、器物迅速换装立住时代感。"
                ),
                sample_text=(
                    "消毒水味道呛进鼻腔时，韩令仪刚把钢笔从签字栏上移开。\n"
                    "“姐姐，医生说只有你的血型跟我配得上，你就救我这一次吧。”病床上的养女拉住她的袖口，"
                    "旁边的生母立刻拧紧眉头，“签了字，下个月给你转五万块零花。”\n"
                    "【叮！当前位面怨气值：98%。原主最终死于术后感染与强行取血。】\n"
                    "韩令仪把那张手术同意书对折再对折，塞进养女温热的掌心里，顺手抽回袖口拍了拍灰：\n"
                    "“五万块买一条命？你自己留着买好点的骨灰盒吧。”"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="快穿位面隔离与积分规则一致性",
                rules=[
                    "每进入一个新位面，必须立即遵守该位面的时代法规、称谓与科技水平（如穿到古代不可随口掏现代热武器乱扫，除非显式从系统商城高价兑换并扣账）。",
                    "系统积分兑换与道具冷却须与账本同步，绝不免费白送破局神器。",
                    "严禁出现‘女配逆袭靠抢别人男人/靠更高级男性当靠山解决一切’的伪大女主套路，核心爽点必须由女主自己出手拿回。",
                ],
                reference_table={
                    "单副本常规节奏建议": "10～20 章干脆完结一个位面",
                    "系统提示框限制": "<= 65 字，禁含竖线与斜杠",
                },
                forbidden_anachronisms=[
                    "不同位面规则混串吃书",
                    "反派智商全员降为负数只靠无脑送人头",
                ],
                disclaimer="多世界架空幻想作品，位面设定均为虚构。",
            ),
            rhythm_burstiness_check=False,
            allow_pov_inner_monologue=True,
        ),
        GenreRecipe(
            genre_id="ancient_mystery",
            display_name="古言悬疑探案",
            channel="female",
            tomato_primary_category="宫斗宅斗",
            tomato_tags=["古言探案", "仵作验尸", "强强联手"],
            pov_mode="third_limited",
            panel_policy="forbid",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "新婚夜，{职业}她剖开棺材查案",
                "大案现场，{女角}拿起了银针",
                "别动凶器！那是{关键物证}",
                "大理寺少卿请我验一桩{奇案}",
            ],
            title_examples=[
                "仵作娘子：开棺验出十年前旧案",
                "红妆入刑部，一具无名尸破奇局",
                "别烧嫁衣！上面沾着砒霜灰",
            ],
            blurb_templates=[
                (
                    "大盛朝京城接连出现三起新娘出嫁夜暴毙的诡案，官府定为冲撞喜神，连夜草草封棺。\n\n"
                    "被继母赶去守义庄的沈家女孟知微，趁夜提着竹刀撬开第三口薄皮棺，"
                    "从死者指甲缝里挑出半粒未化尽的乌头碱碎屑和一小片织金线。\n\n"
                    "就在她刚把物证收进油纸包时，巡夜的大理寺少卿推门撞个正着；"
                    "冷面少卿没有拿她治罪，反倒解下腰间玄铁牌丢在案头：案子归你验，凶手归我抓。"
                ),
            ],
            opening_300w_directive=(
                "前200字直接上‘停尸房/案发现场/封棺前拦车’的物理现场描写，"
                "第300字内必须通过具体古代验尸工具（银针、皂角、醋洗、骨裂观察）找出第一处推翻官府草率结论的硬证据。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="案发现场验出铁证",
                    opening_hook="死因被强行定为自杀/急病暴毙，家属或贪官催着火速下葬毁证",
                    core_conflict="女子抛头露面碰死尸被视作晦气触犯礼教，县衙直接喝阻拿人",
                    payoff_or_twist="三句话点出致命物理破绽（如舌骨骨折、死后入水无呛泥沙），令在场主官变色",
                    chapter_end_hook="冷面探案男主（少卿/推官）现身接管现场，盯上女主的验尸手法",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="共查一线与试探交锋",
                    opening_hook="男主下令停棺复验，实则也在暗中试探女主来历",
                    core_conflict="凶手留下误导物证把嫌疑引向替死鬼，甚至威胁女主身家性命",
                    payoff_or_twist="靠物证颗粒度（药材产地、织物染料、鞋底泥土）排除伪证锁定真凶圈层",
                    chapter_end_hook="第二名关联证人突然暴毙，留下一句带暗号的遗物线索",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="堂审拆穿与首案落幕",
                    opening_hook="高门宅院/公堂对质，真凶仗着品级和不在场证明矢口否认",
                    core_conflict="古代无DNA无监控，全靠物理逻辑拼图一击毙命",
                    payoff_or_twist="当场用化学显色/时间差物证演示拆穿不在场证明，凶手伏诛",
                    chapter_end_hook="凶手临死前露出的徽记竟与女主生母（或男主家族）多年前冤案同源",
                ),
            ],
            style_profile=StyleProfile(
                id="ancient_mystery_human",
                name="古言悬疑仵作白描样本",
                granularity_notes=(
                    "紧贴古代刑侦物证细节：米醋洗青斑、皂角净骨、生前烧伤起泡 vs 死后焚尸无水泡、"
                    "溺死肺胃必有泥沙藻类；冷峻克制，男女主靠脑力过招拉扯。"
                ),
                sample_text=(
                    "义庄外头雨点砸在瓦片上，屋里两盏油灯被风吹得直晃。\n"
                    "孟知微戴上浸过烈酒的薄鹿皮手套，拿竹签挑开死者左手无名指的指甲缝。"
                    "油灯光底下，一点暗紫色的丝绒碎屑混在干血痂里，捻起来有点发滑。\n"
                    "“京兆府文书上写的是失足落水，可人死后再被扔进河里，肺里不会带出这么多黑泥吗？”"
                    "她刚把那截碎布收进青瓷小盒，门轴忽然嘎吱响了一声。\n"
                    "玄色官靴停在棺材板三步外，男人手里提着一盏防风羊角灯，目光先落在她袖口沾着的尸蜡油渍上。"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="古代法医《洗冤集录》与刑侦常识防穿帮",
                rules=[
                    "银针试毒仅对含硫化物毒物（如古代工艺不纯带硫的砒霜）发黑有效，对乌头、夹竹桃、断肠草等生物碱毒素无效，严禁银针万能试百毒。",
                    "判断生前溺水 vs 死后抛尸：看口鼻泡沫（蕈状泡沫）及腹腔胃内是否咽入水沙；判断生前缢死 vs 死后悬尸：看缢沟皮下出血、绳结勒痕是否闭合交错及舌骨状况。",
                    "古代无指纹数据库与DNA比对，血液滴骨验亲属于古代错误认知（不能当成现代绝对科学铁证，可用作剧情拆穿反派的骗局）。",
                ],
                reference_table={
                    "古代法医经典依据": "南宋宋慈《洗冤集录》",
                    "银针发黑原理": "硫化物与银生成硫化银",
                    "古代验伤常用辅助物": "米醋、糟、红油伞（日光滤光看淤伤）、葱白",
                },
                forbidden_anachronisms=[
                    "古代仵作直接说‘测一下DNA’或‘指纹比对一致’",
                    "银针一插测出曼陀罗/夹竹桃毒",
                    "滴血认亲当成真科学铁证定案",
                ],
                disclaimer="古代架空探案虚构作品，传统检验技法仅作文学创作呈现。",
            ),
            rhythm_burstiness_check=True,
            allow_pov_inner_monologue=True,
        ),
        # =====================================================================
        # COMPATIBLE GENERAL GENRE (1)
        # =====================================================================
        GenreRecipe(
            genre_id="literary_suspense",
            display_name="现实悬疑（兼容严肃长篇）",
            channel="general",
            tomato_primary_category="悬疑脑洞",
            tomato_tags=["现实悬疑", "冷峻白描", "物证推理"],
            pov_mode="third_limited",
            panel_policy="forbid",
            golden_word_count_range=(2050, 2300),
            title_templates=[
                "渡口{物证}：{时间}后的真相",
                "消逝在{地点}的旧物",
                "{职业}的第七本旧账",
                "大雾散去，{物件}回到原地",
            ],
            title_examples=[
                "白雾渡口",
                "消失在钟表铺里的三号档案",
                "老桥尽头没有回程船",
            ],
            blurb_templates=[
                (
                    "老码头修表匠拿到一只停在五点半的铜壳怀表，表壳内圈刻着一串与二十年前失踪案完全吻合的编号。\n\n"
                    "来修表的人没有留下姓名，只隔着柜台压低帽檐，要求天黑前必须交货。"
                    "当她挑开表芯积年的黑油泥，底下露出的却是一道平口螺丝刀滑脱留下的新伤。\n\n"
                    "渡口大雾漫上石阶时，销账单上那枚蓝黑墨水签字正一点点浮出水面；"
                    "每一件沉默的旧物，都在替早已无法开口的人保留证词。"
                ),
            ],
            opening_300w_directive=(
                "以器物肌理、光影切面与具体微动作推进开局，严格锁定单一视点人物耳目所及，"
                "前300字内埋入核心异常物理线索与克制张力，绝不抒情升华。"
            ),
            golden_three_beats=[
                GoldenChapterBeat(
                    chapter=1,
                    role="异常委托与物理锚点建立",
                    opening_hook="具体器物交付或旧档案拆封，立刻暴露一处不合常理的物理破绽",
                    core_conflict="来访者隐瞒真实来意并急于催促销毁或交货",
                    payoff_or_twist="在物件内层发现被刻意掩盖的印记，与视点人物过往旧伤产生关联",
                    chapter_end_hook="外界交通中断或旧人突然现身门口，退路收窄",
                ),
                GoldenChapterBeat(
                    chapter=2,
                    role="档案对照与暗流试探",
                    opening_hook="双方在看似体面的日常交谈下围绕物件归属暗暗较劲",
                    core_conflict="关键证明单据在官方流程上已被销账，证人口径严丝合缝",
                    payoff_or_twist="通过细节比对（墨迹、纸张折痕、时间差）戳穿谎言的第一道裂缝",
                    chapter_end_hook="暗处有人抢先一步清理关联现场，留下刚熄灭不久的痕迹",
                ),
                GoldenChapterBeat(
                    chapter=3,
                    role="夜行追踪与首层谜底浮现",
                    opening_hook="深入潮湿封闭旧址实地核查物证去向",
                    core_conflict="视点受黑暗与环境限制，外部威胁正在逼近同一房间",
                    payoff_or_twist="找到被藏匿多年的实物原件，确立后续追查主线",
                    chapter_end_hook="门外响起沉重脚步声或渡船汽笛拉响，第一卷悬念彻底锁定",
                ),
            ],
            style_profile=StyleProfile(
                id="literary_suspense_human",
                name="现实冷峻白描样本",
                granularity_notes=(
                    "以具象名词和动词推进，聚焦金属氧化、木器包浆、潮气水痕与手指操作细节；"
                    "句幅扎实，绝不用感叹号与抒情副词，情绪全部折叠进摆放物件的角度与停顿里。"
                ),
                sample_text=(
                    "台灯罩边缘掉了一块绿漆，白炽灯泡烤得铜灯座微微发烫。她戴上指套，用竹签挑开表壳内圈"
                    "积着的一层黑油泥。油泥底下露出一道斜向划痕，边缘翻起极细的黄铜毛刺，是平口螺丝刀滑脱时"
                    "留下的硬伤。窗外轮渡拉响一声低沉的汽笛，玻璃窗跟着震了半秒，震落了窗框槽里的一点干灰。"
                    "她没抬头，把换下来的断发条夹进玻璃皿，镊子尖碰在皿壁上，发出一声清脆的短响。"
                ),
            ),
            common_sense_card=CommonSenseCard(
                domain="现实法证、刑侦程序与物证物理常识",
                rules=[
                    "法医推断死亡时间依靠尸温下降（约每小时0.8～1°C）、尸僵发展与缓解、尸斑指压退色程度及胃内容物消化状态（一般4～6小时排空），严禁看一眼就精确到分秒。",
                    "所有机构、港口、城市、区县须使用架空合规命名，避免映射现实地名与现役单位。",
                    "严格维持第三人称限知视点，未亲眼所见或未经证言传入的信息严禁在叙述段提前剧透。",
                ],
                reference_table={
                    "胃内容物排空参考时间": "普通混合饮食约 4～6 小时",
                    "尸斑开始显现时间": "死后约 2～4 小时",
                    "尸僵全身发展高峰": "死后约 12～24 小时",
                },
                forbidden_anachronisms=[
                    "看一眼尸斑就精确断言死于昨天下午3点17分",
                    "无搜查合法手续或程序凭空调取绝密档案",
                ],
                disclaimer="现实主义悬疑虚构作品，人物、地名与案件均为艺术创作。",
            ),
            rhythm_burstiness_check=True,
            allow_pov_inner_monologue=False,
        ),
    ]
    return {r.genre_id: r for r in recipes}


GENRE_REGISTRY: Dict[str, GenreRecipe] = _build_genre_registry()


def get_genre(genre_id: str) -> GenreRecipe:
    """Return the GenreRecipe for genre_id or raise UnknownGenreError (no silent fallback)."""
    if not isinstance(genre_id, str) or not genre_id.strip():
        raise UnknownGenreError(f"Unknown or empty genre_id: {genre_id!r}")
    key = genre_id.strip()
    if key not in GENRE_REGISTRY:
        available = ", ".join(sorted(GENRE_REGISTRY.keys()))
        raise UnknownGenreError(f"Unknown genre_id {key!r}. Available genres: {available}")
    return GENRE_REGISTRY[key]


def list_genres(channel: Optional[str] = None) -> List[GenreRecipe]:
    """List registered GenreRecipes, optionally filtered by channel ('male'|'female'|'general')."""
    if channel is None:
        return list(GENRE_REGISTRY.values())
    if channel not in VALID_CHANNELS:
        raise ValueError(f"Invalid channel {channel!r}. Expected one of {sorted(VALID_CHANNELS)}")
    return [r for r in GENRE_REGISTRY.values() if r.channel == channel]


def get_genre_ids(channel: Optional[str] = None) -> List[str]:
    """Return list of registered genre_ids, optionally filtered by channel."""
    return [r.genre_id for r in list_genres(channel=channel)]
