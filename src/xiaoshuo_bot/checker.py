from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    CONTRACT_TRACKS,
    CharacterAnchor,
    ForeshadowItem,
    PANEL_POLICIES,
    POV_MODES,
    ProjectMeta,
    ProjectValidationError,
    ScanReport,
    SceneOutline,
    Violation,
    WorldStateSnapshot,
)
from .sanitizers import scan_modern_identifiers, scan_redlines, scan_symbol_evasion


# 1. Summary / elevation clichés (总结腔 / 升华词)
SUMMARY_PATTERNS: List[Tuple[str, str, str]] = [
    ("SUM_001", r"命运的齿轮", "删除宿命化套话，改为具体器物动作或环境物理变化"),
    ("SUM_002", r"历史的车轮|时代的洪流|宿命的安排", "删除宏大升华词，只写眼前可见的物理细节"),
    ("SUM_003", r"总而言之|综上所述|由此可见|归根结底|换言之", "删除议论文总结连接词，直接呈现动作或对话"),
    ("SUM_004", r"某种意义上(?:来说|讲)?|不得不(?:说|承认)", "删除叙述者评议腔，保持冷峻客观白描"),
    ("SUM_005", r"(?:仿佛|似乎|像是在)(?:无声地)?(?:诉说着|宣告着|预示着|见证着)", "禁止拟人化抒情升华，直接描写器物外观或声响"),
    ("SUM_006", r"这一刻[，,]?(?:时间仿佛|一切都|她终于明白|他终于明白|所有的)", "删除‘这一刻’升华句式，用具体感官动作收束"),
    ("SUM_007", r"一切才刚刚开始|暴风雨前的宁静|序幕才刚刚拉开", "删除预告式悬念套话，事件写尽即止"),
]

# 2. Body-reaction clichés (身体反应套路) — scanned on non-narration-masked prose
BODY_TIC_PATTERNS: List[Tuple[str, str, str]] = [
    ("TIC_001", r"(?:不禁|微微|猛地|顿时)?一愣|(?:猛地|微微)一怔", "禁止‘一愣/一怔’套路，改用角色专属微动作锚点"),
    ("TIC_002", r"心(?:中|头|里)(?:猛地)?一(?:紧|凛|震|沉|颤|惊)", "禁止‘心中一紧’等抽象心理套词，改写外部物理动作"),
    ("TIC_003", r"心里咯噔一下|心脏(?:猛地一缩|漏跳(?:了)?(?:一|半)拍|狂跳不已)", "禁止套路化心跳描写，用呼吸、手部动作或器物接触替代"),
    ("TIC_004", r"眸(?:光|色)(?:微闪|一闪|一暗|一沉|微凝|微动|闪烁)", "绝对禁止‘眸光微闪’类网文套词，改写具体的视线落点"),
    ("TIC_005", r"瞳孔(?:微缩|猛地一缩|骤缩|骤然收缩|地震)", "绝对禁止‘瞳孔微缩’套路，改写可见的肢体停顿或器物操作"),
    ("TIC_006", r"眼(?:底|中|神中|眸中)(?:闪过|掠过|划过|浮现出)一(?:丝|抹)", "禁止‘眼底闪过一丝’套路，通过对话停顿或动作展现"),
    ("TIC_007", r"(?:嘴角|唇角)(?:微微)?(?:勾起|扬起|扯出)一抹|勾唇一笑|邪魅一笑", "禁止‘嘴角勾起一抹’套路，如实描写面部或手部细节"),
    ("TIC_008", r"倒吸一口(?:凉气|冷气)", "禁止‘倒吸一口凉气’套路"),
    ("TIC_009", r"(?:苦笑|自嘲)(?:了)?一声|自嘲地笑笑|喃喃自语(?:道)?", "禁止‘苦笑一声/喃喃自语’套话，直接写台词或沉默动作"),
    ("TIC_010", r"(?<!情)不禁", "删除‘不禁’惯性副词，直接陈述动作本身"),
]

TIC_010_EXEMPT_RE = re.compile(r"忍俊不禁|弱不禁风|夜里不禁行")

# 3. Structural symmetry patterns (结构化对称) — scanned on non-narration-masked prose
SYMMETRY_PATTERNS: List[Tuple[str, str, str]] = [
    (
        "SYM_001",
        r"(?:不是|并非)[^。！？；\n]{1,40}[，,]\s*而是[^。！？；\n]{1,40}",
        "禁止‘不是A，而是B’对称修辞，直接写实际发生的动作或事实",
    ),
    (
        "SYM_002",
        r"与其说[^。！？；\n]{1,35}[，,]\s*不如说[^。！？；\n]{1,35}",
        "禁止‘与其说是A，不如说是B’对比句式，直接陈述准确状态",
    ),
    (
        "SYM_003",
        r"是[^，。！？；\n]{1,28}[，、]\s*是[^，。！？；\n]{1,28}[，、]\s*(?:更是|也是|还是|是)[^，。！？；\n]{1,28}",
        "绝对禁止‘是……，是……，是……’三连排比句式，拆解为具体叙事句",
    ),
    (
        "SYM_004",
        r"没有[^，。！？；\n]{1,25}[，、]\s*没有[^，。！？；\n]{1,25}[，、]\s*(?:只有|唯有)[^，。！？；\n]{1,25}",
        "禁止‘没有A，没有B，只有C’三段式对称排比，直接描写C本身",
    ),
]

# 4. Format & meta-output contamination (格式与元信息污染)
_COMMON_FORMAT_PATTERNS: List[Tuple[str, str, str]] = [
    ("FMT_002", r"(?m)^\s*#{1,6}\s+.*$", "严禁输出 Markdown 标题或小标题"),
    ("FMT_003", r"```", "严禁输出代码围栏"),
    (
        "FMT_004",
        r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,}|={3,}|\+{3,}|~{3,})\s*$",
        "严禁输出 ASCII/Markdown 分割线（会污染有声听书朗读），请使用自然段落过渡",
    ),
    (
        "FMT_005",
        r"(?m)^(?!.{36,}\s*$)\s*第[一二三四五六七八九十百千零两\d]{1,10}[章节回部](?:[ \t:：—\-]+[^\n]{1,28})?(?<![，。；、…])\s*$",
        "严禁在正文内输出独立章节标题行",
    ),
]

FMT_006_FORBID_PANEL_PATTERN: Tuple[str, str, str] = (
    "FMT_006",
    r"(?:创作手记|作者(?:注|按|有话说)|【(?:注|提示|作者注|阅读提示|本章完|完)(?:[:：，,\s][^】\n]{0,60})?】|\(本章完\)|（本章完）|\[完\]|字数统计[:：])",
    "严禁输出元信息、创作手记、完结标记或未授权提示框",
)

FMT_006_ALLOW_PANEL_PATTERN: Tuple[str, str, str] = (
    "FMT_006",
    r"(?:创作手记|作者(?:注|按|有话说)|【(?:本章完|作者注|阅读提示|完)(?:[:：，,\s][^】\n]{0,60})?】|\(本章完\)|（本章完）|\[完\]|字数统计[:：])",
    "严禁输出元信息、作者注、阅读提示或完结标记（受控系统提示框除外）",
)

# Backwards-compatible default FORMAT_PATTERNS constant
FORMAT_PATTERNS: List[Tuple[str, str, str]] = [
    *_COMMON_FORMAT_PATTERNS,
    FMT_006_FORBID_PANEL_PATTERN,
]

# 5. Omniscient narrator leaks (上帝视角越界词)
OMNISCIENT_PATTERNS: List[Tuple[str, str, str]] = [
    ("POV_002", r"殊不知|却不知|浑然不知的是|不为人知的是|(?:没有|无)人(?:知道|知晓)", "严禁全知上帝视角剧透或评述，严格限制在视点角色当前感知范围内"),
    ("POV_003", r"与此同时[，,]?\s*(?:在|远在|另一边)", "严禁平行剪辑视点角色不在场的另一空间事件"),
    ("POV_004", r"多年以后[^。！？\n]*回想", "严禁全知预叙未来视角"),
]

# Idioms exempted from third-person POV_001 first-person leak check
THIRD_PERSON_ME_IDIOMS: Tuple[str, ...] = (
    "唯我独尊",
    "时不我待",
    "尔虞我诈",
    "舍我其谁",
    "我行我素",
    "卿卿我我",
    "非我族类",
    "物我两忘",
)
_THIRD_PERSON_ME_IDIOM_RE = re.compile("|".join(re.escape(i) for i in THIRD_PERSON_ME_IDIOMS))

# External observation / speculation cues that exempt POV_005 mind-hopping when preceding inner verb
_POV_EXTERNAL_OBSERVATION_RE = re.compile(r"似乎|大概|显然|猜到")

# 6. Tomato First-Screen Hook (HOOK_001) & Deflating Ending (HOOK_002)
HOOK_OPENING_WEATHER_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (
        r"^\s*(?:十[一二]?月|九月|八月|七月|六月|五月|四月|三月|二月|正月|那年|深秋|初春|盛夏|隆冬|"
        r"清晨|傍晚|黄昏|深夜|雨夜|风雪|大雪|暴雨|狂风|乌云|阳光|微风|天空|月色|江雾|雾气|夕阳|朝阳|夜风|寒风|秋风)"
        r"[^。！？\n]{0,35}(?:雨|雪|风|雾|云|阳光|月光|星光|天色|气温|树叶|街灯)",
        "番茄首屏黄金 200 字严禁天气景物起兴开篇，请直接以冲突、动作、对话或系统绑定事件开局",
    ),
    (
        r"万里无云|风和日丽|晴空万里|阳光明媚|倾盆大雨|大雪纷飞|鹅毛大雪|乌云密布|"
        r"阴雨连绵|电闪雷鸣|鸟语花香|微风习习|落叶纷飞|残阳如血|暮色四合|夜色如墨|天色阴沉",
        "番茄第 1 章前 3 段严禁空泛天气景物描写拖节奏，请立刻切入强冲突与核心事件",
    ),
)

HOOK_OPENING_RESUME_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (
        r"(?:他|她|我)?名叫[\u4e00-\u9fa5]{2,4}[，,]今年[\u4e00-\u9fa5\d]{1,3}岁",
        "严禁‘名叫XX，今年XX岁’户口本式简历介绍，请直接在行动与冲突中带出身份",
    ),
    (
        r"(?:出生于|毕业于[\u4e00-\u9fa5]{2,12}大学|家住[\u4e00-\u9fa5]{2,12}|祖籍[\u4e00-\u9fa5]{2,8}|身高\d{2,3}|体重\d{2,3})",
        "严禁第 1 章开篇罗列学历/籍贯/身高等简历式设定，请改用场景事件展示人物处境",
    ),
    (
        r"(?:是一个|是一名|是一位)(?:普通的|平凡的|穷困潦倒的|落魄的|年轻的)?(?:外卖员|大学生|程序员|社畜|上班族|实习生|孤儿|废柴|赘婿)",
        "严禁旁白直白贴‘是一名普通外卖员/社畜’说明书式开局，用具体动作事件体现窘境",
    ),
    (
        r"(?:从小|自幼)(?:父母双亡|在孤儿院长大|体弱多病|性格孤僻)|在这个以武为尊的世界|故事发生在[\u4e00-\u9fa5]{2,8}",
        "严禁开篇前 3 段背景说明书式世界观科普，请先丢出核心钩子事件",
    ),
)

HOOK_DEFLATING_ENDING_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (
        r"一夜无话|一觉睡到天亮|沉沉(?:睡去|入睡|睡着)|很快进入了梦乡|安然入睡",
        "章末严禁‘一夜无话/沉沉睡去’泄气收尾，必须在悬念最高点或新危机露头时断章",
    ),
    (
        r"圆满解决|皆大欢喜|完美落幕|度过了平静的一(?:天|夜)|期待明天|明天会更好|各自散去|回家休息了",
        "章末严禁‘圆满解决/期待明天’等平淡闭环废句，请保留未解悬念或下一拍冲突钩子",
    ),
)

# 7. AI Overused Clichés (SAFE_004)
AI_OVERUSED_CLICHES: Tuple[Tuple[str, str], ...] = (
    (r"(?:几分|三分|七分)薄凉|(?:几分|三分|七分)漫不经心|眼底尽是薄凉", "删除网文 AI 虚词套话‘三分薄凉/漫不经心’，改为具体动作或冷硬台词"),
    (r"空气仿佛凝固|空气瞬间凝固|周围的空气仿佛都凝固了", "删除 AI 套话‘空气仿佛凝固’，改为环境声响或人物肢体停顿"),
    (r"(?:嘴角|唇角)微不可察地", "删除 AI 套话‘微不可察地’，直接写可见的表情变化或对话"),
    (r"(?:犹如|宛如)实质般的(?:杀意|威压|冷意|目光)", "删除 AI 套话‘犹如实质般的杀意’，改写为压迫性的具体物理动作"),
    (r"声音(?:仿佛|像是)?淬了冰|大提琴般的(?:低沉)?嗓音", "删除 AI 腔调修辞‘淬了冰/大提琴般嗓音’，如实写语气与说话节奏"),
    (r"神色晦暗不明|眼神(?:变得)?晦暗不明|深不见底的幽潭", "删除 AI 虚词‘晦暗不明/深不见底的幽潭’，写清视线落点或面部动作"),
    (r"女人[，,]你成功引起了我的注意|像极了一只受惊的小鹿", "删除陈旧霸总 AI 套话，写贴合当下场景的真实反应"),
)

# 8. Anti-RLHF Saint-mother Drift (OOC_SAINT)
OOC_SAINT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (
        r"(?:算了|罢了)[，,]?(?:就当没发生过|放(?:他|她|他们)一马(?:吧)?|饶(?:他|她|他们)一条生路|不跟(?:他|她|他们)计较了)",
        "触发反圣母门禁：严禁对害人者无代价妥协放马，必须让反派付出对等代价或反杀",
    ),
    (
        r"(?:我|他|她)决定原谅(?:他|她|他们|凶手|仇人|背叛者)|选择(?:无条件)?原谅(?:了)?(?:那个|曾经)?(?:害死|背叛|欺辱|陷害|杀害|虐待)",
        "触发反圣母门禁：严禁无代价原谅死敌或加害者，保持爽点与因果清算",
    ),
    (
        r"冤冤相报何时了|得饶人处且饶人|以德报怨|用爱感化",
        "触发反圣母门禁：严禁圣母说教劝善套话，遵循有仇必报、快意恩仇逻辑",
    ),
    (
        r"(?:跪下|哭着|苦苦)?(?:替|为)(?:那个)?(?:凶手|仇人|罪犯|恶霸|反派|加害者)求情",
        "触发反圣母门禁：严禁主角或己方角色替加害者求情背锅",
    ),
    (
        r"(?:求求你|拜托了)[，,]?(?:放过|别杀|不要为难)(?:他|她|他们)吧[，,]?(?:他|她)一定不是故意的",
        "触发反圣母门禁：严禁替加害者辩解求饶",
    ),
    (
        r"(?:其实他也是受害者|他也有苦衷|错的是我)[^。！？\n]{0,20}(?:放过他|别追究了|原谅他)",
        "触发反圣母门禁：严禁替施害者找苦衷并放弃追究",
    ),
    (
        r"每个人都有活下去的权利|暴力不能解决任何问题|我们应该心怀善良|(?:不管怎么样|无论如何)[，,]他终究是你的(?:亲人|父母|血脉)|血浓于水[，,]怎么能这么冷血",
        "触发反圣母门禁：严禁大段 RLHF 道德说教或亲情绑架",
    ),
)

# 9. Bystander-shock clichés (RHYTHM_BYSTANDER_SHOCK -> WARNING)
BYSTANDER_SHOCK_PATTERNS: Tuple[str, ...] = (
    r"(?:全场|四周|周围|在场|众人|围观(?:的)?(?:人群|群众|人|者)|吃瓜群众|所有人都)(?:瞬间|顿时|一下子|全都|齐齐|纷纷)?(?:鸦雀无声|沸腾了|炸开了锅|一片哗然|倒抽一口|呆若木鸡|傻眼了|瞪大(?:了)?双眼|张大(?:了)?嘴巴|面面相觑|议论纷纷)",
    r"(?:全场死寂|一片哗然|落针可闻|针落可闻|下巴都要掉在地上|眼珠子都要瞪出来|怀疑人生|恐怖如斯|竟恐怖如斯)",
)

DIALOGUE_SPAN_RE = re.compile(r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\"")
PANEL_SPAN_RE = re.compile(r"【[^】\n]*】")
_QUOTE_CHAR_CHECK_RE = re.compile(r"[“”‘’「」『』\"]")

# Expenditure & Chinese numeral parsing for STATE_OVERDRAFT
_EXPENDITURE_VERB_RE = re.compile(
    r"(?P<verb>花了|花掉|花费|支出|支付|付了|刷卡|刷了|转账|转了|买下|拍下|砸出|砸下|掏出|拿出|赔偿|赔了|亏损|消耗|消耗了|扣除|扣除了|打赏|充值)"
    r"[^。！？；\n]{0,12}?"
    r"(?P<num>\d+(?:\.\d+)?|(?:[零一二三四五六七八九十百千万亿]|两(?!银子))+)"
    r"\s*(?P<scale>亿|千万|百万|十万|万|千)?"
    r"\s*(?:(?:枚|颗|块)(?=[^\S\n]*(?:下品|中品|上品|极品)?(?:灵石|金币|银两|铜钱)))?"
    r"\s*(?P<unit>下品灵石|中品灵石|上品灵石|极品灵石|灵石|金币|银两|两银子|铜钱|积分|元|块钱|块|点)?"
)

_NON_MONETARY_CLASSIFIER_RE = re.compile(
    r"^\s*(?:分钟|秒钟|秒|小时|钟头|天|日(?![元薪租])|周|星期|个月|月(?![薪租费供])|年(?![薪费租])|岁|载|"
    r"张|根|把|件|套|本(?![金息])|个|名|位|步|次|回|趟|遍|条|只|头|匹|辆|架|艘|栋|座|间|层|米|公里|千米|斤|公斤|克|吨|升|毫升|度|倍)"
)
_PHYSICAL_OBJECT_VERBS = frozenset({"掏出", "拿出", "买下", "拍下"})
_BLOCK_CURRENCY_FOLLOW_RE = re.compile(r"^\s*(?:钱|买|去|给|交|付|抵|赔|就|才|都|还|也|了|着|吧|呢|吗|啊)")

_CN_DIGITS: Dict[str, int] = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_SMALL_UNITS: Dict[str, int] = {"十": 10, "百": 100, "千": 1000}
_CN_LARGE_UNITS: Dict[str, int] = {"万": 10_000, "亿": 100_000_000}
_UNIT_MULTIPLIERS: Dict[str, float] = {
    "亿": 100_000_000.0,
    "千万": 10_000_000.0,
    "百万": 1_000_000.0,
    "十万": 100_000.0,
    "万": 10_000.0,
    "千": 1_000.0,
}

# Deceased character real-time action detection
_DECEASED_FLASHBACK_EXEMPT_RE = re.compile(
    r"生前|死前|死后|遗物|遗照|墓碑|灵位|牌位|棺木|棺材|尸体|遗体|"
    r"亡兄|亡姐|亡弟|亡妹|亡夫|亡妻|亡母|亡父|已故|回忆|当年|曾经|那年|"
    r"梦中|梦里|噩梦|幻觉|幻影|鬼魂|照片|画像|监控录像|录像里|追悼|祭拜|想起|想到|提起|提到|名字|字迹|签名|出事"
)
_DECEASED_ACTION_VERBS = (
    r"(?:说道|开口道|冷笑道|大笑道|喊道|问道|答道|笑着说|淡淡地说|怒喝|低声道|厉声道|"
    r"走进|走来|走了进来|推门|站起身|站在门口|出现在|现身|坐在|抬起头|抬手|伸手|转身|"
    r"拔出|拔刀|抽出|拿起|端起|接过|递过|拍案|冷哼|点头|摇头|挥手|冲上前|冲了过来|"
    r"一把抓住|瞪着|盯着|看着|微微一笑|：[“「]|「)"
)


def _parse_cn_integer(text: str) -> Optional[float]:
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    total = 0
    section = 0
    current = 0
    seen_digit = False
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
            seen_digit = True
        elif ch in _CN_SMALL_UNITS:
            unit = _CN_SMALL_UNITS[ch]
            if current == 0 and not seen_digit:
                current = 1
            section += current * unit
            current = 0
            seen_digit = False
        elif ch in _CN_LARGE_UNITS:
            unit = _CN_LARGE_UNITS[ch]
            section += current
            if section == 0:
                section = 1
            total += section * unit
            section = 0
            current = 0
            seen_digit = False
        else:
            return None
    return float(total + section + current)


def parse_chinese_amount(num_text: str, unit_text: str = "") -> Optional[float]:
    """Parse Arabic/Chinese expenditure amounts like '500'+'万' or '五百万' into numeric floats."""
    base = _parse_cn_integer(num_text)
    if base is None:
        return None
    if unit_text in _UNIT_MULTIPLIERS:
        # Avoid double multiplying when num_text already ended with the same magnitude (e.g. "五百万" + "")
        return base * _UNIT_MULTIPLIERS[unit_text]
    return base


def _mask_span_preserve_offsets(match: re.Match[str]) -> str:
    raw = match.group(0)
    if len(raw) <= 2:
        return raw
    inner = "".join("\n" if ch == "\n" else "□" for ch in raw[1:-1])
    return raw[0] + inner + raw[-1]


def mask_dialogue(text: str) -> str:
    """Replace quoted dialogue content with placeholder boxes while preserving exact offsets & newlines."""
    return DIALOGUE_SPAN_RE.sub(_mask_span_preserve_offsets, text)


def mask_non_narration(text: str, mask_panels: bool = True) -> str:
    """Mask quoted dialogue and optionally controlled 【...】 panels/danmaku while preserving offsets."""
    masked = DIALOGUE_SPAN_RE.sub(_mask_span_preserve_offsets, text)
    if mask_panels:
        masked = PANEL_SPAN_RE.sub(_mask_span_preserve_offsets, masked)
    return masked


def _line_number_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _snippet_around(text: str, start: int, end: int, radius: int = 24) -> str:
    s = max(0, start - radius)
    e = min(len(text), end + radius)
    return text[s:e].replace("\n", "⏎")


def _sentence_bounds_around(text: str, start: int, end: int) -> Tuple[int, int]:
    left = start
    while left > 0 and text[left - 1] not in "。！？\n":
        left -= 1
    right = end
    while right < len(text) and text[right] not in "。！？\n":
        right += 1
    return left, right


def _clause_start_before(text: str, pos: int) -> int:
    idx = pos
    while idx > 0 and text[idx - 1] not in "。！？；，、\n":
        idx -= 1
    return idx


def _extract_paragraphs_with_lines(text: str) -> List[Tuple[int, int, str]]:
    """Return [(0-based paragraph_index, 1-based line_number, stripped_text), ...]."""
    result: List[Tuple[int, int, str]] = []
    para_idx = 0
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped:
            result.append((para_idx, line_no, stripped))
            para_idx += 1
    return result


def _is_bare_dialogue_paragraph(para: str) -> bool:
    """Return True if a paragraph contains dialogue quotes and lacks meaningful action/expression narration."""
    if not _QUOTE_CHAR_CHECK_RE.search(para):
        return False
    without_quotes = DIALOGUE_SPAN_RE.sub("", para)
    # Remove punctuation and whitespace; if <= 3 chars remain (e.g. "他说：" or empty), it's bare dialogue
    residual = re.sub(r"[\s，。！？；：、…—\-·（）()《》【】\[\]]+", "", without_quotes)
    return len(residual) <= 3


def _match_balance_keys(unit: str, balances: Dict[str, float]) -> List[Tuple[str, float]]:
    if not balances:
        return []
    if unit:
        for key, val in balances.items():
            if unit in key or key in unit:
                return [(key, val)]
        unit_aliases: Dict[str, Tuple[str, ...]] = {
            "灵石": ("spirit_stone", "spirit_stones", "灵石"),
            "下品灵石": ("spirit_stone", "spirit_stones", "灵石"),
            "中品灵石": ("spirit_stone", "spirit_stones", "灵石"),
            "上品灵石": ("spirit_stone", "spirit_stones", "灵石"),
            "极品灵石": ("spirit_stone", "spirit_stones", "灵石"),
            "金币": ("gold", "coin", "coins", "金币"),
            "银两": ("silver", "tael", "银两", "银子"),
            "两银子": ("silver", "tael", "银两", "银子"),
            "铜钱": ("copper", "coin", "coins", "铜钱"),
            "积分": ("points", "score", "credit", "积分"),
            "点": ("points", "attribute", "点", "积分"),
        }
        if unit in unit_aliases:
            aliases = unit_aliases[unit]
            matched = [(k, v) for k, v in balances.items() if k.lower() in aliases or any(a in k for a in aliases)]
            if matched:
                return matched
    monetary_units = {"", "亿", "千万", "百万", "十万", "万", "千", "元", "块", "块钱"}
    if unit in monetary_units:
        money_keys = ("cash", "money", "balance", "rmb", "fund", "funds", "现金", "余额", "资金", "存款", "银行卡", "资产")
        matched = [(k, v) for k, v in balances.items() if k.lower() in money_keys or any(mk in k for mk in money_keys)]
        if matched:
            return matched
    if len(balances) == 1 and unit in {"", "亿", "千万", "百万", "十万", "万", "千"}:
        return list(balances.items())
    if len(balances) == 1 and unit in {"元", "块", "块钱"}:
        single_key = next(iter(balances)).lower()
        non_rmb_tokens = ("spirit_stone", "silver", "tael", "gold", "copper", "point", "score", "credit", "attribute", "灵石", "银", "铜", "积分")
        if not any(tok in single_key for tok in non_rmb_tokens):
            return list(balances.items())
    return []


class AITicChecker:
    """Parameterized AI-TIC-CHECK 2.0 static analyzer for literary & Tomato web-fiction gates."""

    def __init__(
        self,
        extra_banned_phrases: Optional[List[str]] = None,
        *,
        pov_mode: str = "third_limited",
        panel_policy: str = "forbid",
        contract_track: str = "legacy",
        short_paragraph_mode: Optional[bool] = None,
        check_modern_identifiers: bool = True,
    ) -> None:
        self._validate_modes(pov_mode=pov_mode, panel_policy=panel_policy, contract_track=contract_track)
        self.extra_banned_phrases = extra_banned_phrases or []
        self.pov_mode = pov_mode
        self.panel_policy = panel_policy
        self.contract_track = contract_track
        self.short_paragraph_mode = short_paragraph_mode
        self.check_modern_identifiers = check_modern_identifiers

    @staticmethod
    def _validate_modes(*, pov_mode: str, panel_policy: str, contract_track: str) -> None:
        if pov_mode not in POV_MODES:
            raise ProjectValidationError(f"Unknown pov_mode {pov_mode!r}; expected one of {POV_MODES}")
        if panel_policy not in PANEL_POLICIES:
            raise ProjectValidationError(f"Unknown panel_policy {panel_policy!r}; expected one of {PANEL_POLICIES}")
        if contract_track not in CONTRACT_TRACKS:
            raise ProjectValidationError(
                f"Unknown contract_track {contract_track!r}; expected one of {CONTRACT_TRACKS}"
            )

    def scan(
        self,
        text: str,
        scene: Optional[SceneOutline] = None,
        characters: Optional[Dict[str, CharacterAnchor]] = None,
        due_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
        plant_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
        project_meta: Optional[ProjectMeta] = None,
        world_state: Optional[WorldStateSnapshot] = None,
        chapter: Optional[int] = None,
        pov_mode: Optional[str] = None,
        panel_policy: Optional[str] = None,
        contract_track: Optional[str] = None,
        short_paragraph_mode: Optional[bool] = None,
        check_modern_identifiers: Optional[bool] = None,
    ) -> ScanReport:
        eff_pov = pov_mode if pov_mode is not None else (project_meta.pov_mode if project_meta else self.pov_mode)
        eff_panel = (
            panel_policy
            if panel_policy is not None
            else (project_meta.panel_policy if project_meta else self.panel_policy)
        )
        eff_track = (
            contract_track
            if contract_track is not None
            else (project_meta.contract_track if project_meta else self.contract_track)
        )
        self._validate_modes(pov_mode=eff_pov, panel_policy=eff_panel, contract_track=eff_track)

        if short_paragraph_mode is not None:
            eff_short_para = short_paragraph_mode
        elif self.short_paragraph_mode is not None:
            eff_short_para = self.short_paragraph_mode
        else:
            eff_short_para = eff_track != "legacy"

        eff_check_modern = (
            check_modern_identifiers if check_modern_identifiers is not None else self.check_modern_identifiers
        )
        eff_chapter = scene.chapter if scene is not None else chapter

        violations: List[Violation] = []

        if not text or not text.strip():
            violations.append(
                Violation(
                    rule_id="FMT_000",
                    category="FORMAT_VIOLATION",
                    severity="CRITICAL",
                    matched_text="<EMPTY>",
                    line_number=1,
                    context_snippet="",
                    fix_hint="输出正文不能为空",
                )
            )
            empty_report = ScanReport(passed=False, char_count=0, paragraph_count=0, violations=violations)
            empty_report.diagnostics = {  # type: ignore[attr-defined]
                "char_count": 0,
                "paragraph_count": 0,
                "sentence_count": 0,
                "mean_sentence_length": 0.0,
                "burstiness_sigma": 0.0,
                "short_paragraph_mode": eff_short_para,
                "pov_mode": eff_pov,
                "panel_policy": eff_panel,
                "contract_track": eff_track,
                "brick_paragraph_indices": [],
                "pure_narration_runs": [],
                "bare_dialogue_runs": [],
                "bystander_shock_paragraphs": [],
                "burstiness_paragraph_indices": [],
                "critical_paragraph_indices": [],
                "warning_paragraph_indices": [],
                "flagged_paragraph_indices": [],
            }
            return empty_report

        # 1. FMT_001: First character check (allows legal left quotes “「『; allows 【 when panel_policy="allow_system_tag")
        first_char = text[0]
        forbidden_start_chars = "#*`[-—=" if eff_panel == "allow_system_tag" else "#*`[-—=【"
        if first_char.isspace() or first_char in forbidden_start_chars:
            violations.append(
                Violation(
                    rule_id="FMT_001",
                    category="FORMAT_VIOLATION",
                    severity="CRITICAL",
                    matched_text=repr(text[:20]),
                    line_number=1,
                    context_snippet=_snippet_around(text, 0, min(len(text), 20)),
                    fix_hint="第一个字符必须是正文的第一个汉字、合法左引号（“「『）或受控系统标签，严禁前置空行、空格或标题符号",
                )
            )

        # 2. FMT_007: First-line bare title check (exempt in Tomato vertical short-paragraph mode)
        first_line = text.splitlines()[0].strip()
        if not eff_short_para:
            if len(first_line) < 22 and not re.search(r"[，。！？；：、“”‘’「」『』【】《》——…]", first_line):
                violations.append(
                    Violation(
                        rule_id="FMT_007",
                        category="FORMAT_VIOLATION",
                        severity="CRITICAL",
                        matched_text=first_line,
                        line_number=1,
                        context_snippet=first_line,
                        fix_hint="首行疑似标题或小标题，正文首句必须是完整叙事句或对话",
                    )
                )

        # 3. Format patterns across unmasked text (FMT_002~FMT_006)
        fmt_patterns: List[Tuple[str, str, str]] = [
            *_COMMON_FORMAT_PATTERNS,
            FMT_006_ALLOW_PANEL_PATTERN if eff_panel == "allow_system_tag" else FMT_006_FORBID_PANEL_PATTERN,
        ]
        self._apply_regex_group(text, fmt_patterns, "FORMAT_VIOLATION", violations)

        # When panel_policy == "forbid", also reject any stray 【...】 panel tags not already flagged by FMT_006
        if eff_panel == "forbid":
            existing_fmt6_spans = {
                (m.start(), m.end())
                for m in re.finditer(FMT_006_FORBID_PANEL_PATTERN[1], text)
            }
            for m in PANEL_SPAN_RE.finditer(text):
                if (m.start(), m.end()) in existing_fmt6_spans:
                    continue
                violations.append(
                    Violation(
                        rule_id="FMT_006",
                        category="FORMAT_VIOLATION",
                        severity="CRITICAL",
                        matched_text=m.group(0),
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint="当前项目 panel_policy='forbid'，严禁输出【...】系统面板或提示框",
                    )
                )

        # 4. TTS_001: Audiobook panel length <= 65 chars & no '|' or '380/500' progress-bar slashes
        for m in PANEL_SPAN_RE.finditer(text):
            panel_str = m.group(0)
            panel_reasons: List[str] = []
            if len(panel_str) > 65:
                panel_reasons.append(f"长度 {len(panel_str)} 字超过 65 字上限")
            if "|" in panel_str:
                panel_reasons.append("包含破坏有声朗读的竖线符号 '|'")
            if re.search(r"\d+\s*/\s*\d+", panel_str):
                panel_reasons.append("包含破坏有声朗读的进度条斜杠（如 380/500）")
            if panel_reasons:
                violations.append(
                    Violation(
                        rule_id="TTS_001",
                        category="TTS_PANEL_VIOLATION",
                        severity="CRITICAL",
                        matched_text=panel_str,
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint=f"听书面板门禁拦截（{'；'.join(panel_reasons)}），请压缩为 <=65 字纯文本播报且去除 '|' 与数字斜杠",
                    )
                )

        # 5. Summary clichés across full text
        self._apply_regex_group(text, SUMMARY_PATTERNS, "SUMMARY_CLICHE", violations)

        # 6. Extra project-specific banned phrases
        for idx, phrase in enumerate(self.extra_banned_phrases, start=1):
            for m in re.finditer(re.escape(phrase), text):
                violations.append(
                    Violation(
                        rule_id=f"CUSTOM_{idx:03d}",
                        category="CUSTOM_BANNED",
                        severity="CRITICAL",
                        matched_text=m.group(0),
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint=f"禁用表达‘{phrase}’，请改写为具体动作细节",
                    )
                )

        # 7. Non-narration masking layer (masks quoted dialogue AND 【...】 panels/danmaku)
        masked = mask_non_narration(text, mask_panels=True)

        # Body-reaction clichés on masked prose (exempting 忍俊不禁 / 弱不禁风 / 夜里不禁行 for TIC_010)
        self._scan_body_tics(masked, text, violations)
        # Structural symmetry on masked prose (so dialogue & danmaku parallelism are NOT falsely flagged)
        self._apply_regex_group(masked, SYMMETRY_PATTERNS, "STRUCTURAL_SYMMETRY", violations, source_text=text)
        # Omniscient narrator leaks on masked prose
        self._apply_regex_group(masked, OMNISCIENT_PATTERNS, "POV_VIOLATION", violations, source_text=text)

        # 8. POV_001: First-person leak (only under third_limited; exempt idioms in THIRD_PERSON_ME_IDIOMS)
        if eff_pov != "first_person":
            idiom_masked = _THIRD_PERSON_ME_IDIOM_RE.sub(lambda m: "□" * len(m.group(0)), masked)
            for m in re.finditer(r"(?<![自忘故敌你])我", idiom_masked):
                violations.append(
                    Violation(
                        rule_id="POV_001",
                        category="POV_VIOLATION",
                        severity="CRITICAL",
                        matched_text=text[m.start() : m.end()],
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint="叙述语段出现第一人称‘我’（非引号内台词/面板/固定成语），必须严格保持第三人称限知视角",
                    )
                )

        # 9. POV_005: Non-POV character direct inner-thought violation (never crosses commas ，、; exempts 似乎/大概/显然/猜到)
        if scene and characters:
            pov_char = characters.get(scene.pov_character)
            other_names = [
                c.name for cid, c in characters.items() if cid != scene.pov_character and c.name
            ]
            if other_names:
                names_alt = "|".join(re.escape(n) for n in other_names)
                inner_verbs = (
                    r"(?:心想|暗想|心中暗道|心里清楚|心里明白|心里盘算|暗自思忖|"
                    r"暗自庆幸|暗自叫苦|心念电转|在心里说|心知肚明|感到一阵心虚|意识到自己)"
                )
                mind_hop_re = re.compile(rf"(?:{names_alt})[^，、。！？；\n“”‘’「」『』]{{0,8}}({inner_verbs})")
                for m in mind_hop_re.finditer(masked):
                    clause_start = _clause_start_before(masked, m.start())
                    clause_prefix = masked[clause_start : m.start(1)]
                    sent_start, _ = _sentence_bounds_around(masked, m.start(), m.end())
                    if _POV_EXTERNAL_OBSERVATION_RE.search(clause_prefix) or re.search(
                        r"猜到[，,]?\s*$", masked[sent_start : m.start()]
                    ):
                        continue
                    pov_label = pov_char.name if pov_char else scene.pov_character
                    violations.append(
                        Violation(
                            rule_id="POV_005",
                            category="POV_VIOLATION",
                            severity="CRITICAL",
                            matched_text=text[m.start() : m.end()],
                            line_number=_line_number_at(text, m.start()),
                            context_snippet=_snippet_around(text, m.start(), m.end()),
                            fix_hint=(
                                f"严禁切换角色内心！当前唯一限知视点为‘{pov_label}’，"
                                f"只能通过‘{pov_label}’观察到的对方神态、动作、台词来侧面呈现"
                            ),
                        )
                    )

        paragraphs_with_lines = _extract_paragraphs_with_lines(text)
        paragraphs = [p for _, _, p in paragraphs_with_lines]
        masked_paragraphs_with_lines = _extract_paragraphs_with_lines(masked)

        # 10. HOOK_001 & HOOK_002: First-screen 200-char gate & deflating ending gate
        if eff_track != "legacy" and eff_chapter == 1:
            self._scan_hook_001(
                text,
                masked_paragraphs_with_lines,
                violations,
                raw_paragraphs_with_lines=paragraphs_with_lines,
            )
        self._scan_hook_002(text, masked_paragraphs_with_lines, violations)

        # 11. SAFE_001~SAFE_004: Compliance, symbol evasion, redlines, and AI clichés
        self._scan_safety_gates(text, masked, eff_check_modern, violations)

        # 12. OOC_SAINT: Anti-RLHF Saint-mother drift
        self._scan_saint_ooc(text, violations)

        # 13. STATE_*: WorldStateSnapshot consistency cross-gates (without importing state.py)
        if world_state is not None:
            self._scan_world_state(
                text=text,
                masked=masked,
                scene=scene,
                world_state=world_state,
                eff_chapter=eff_chapter,
                out=violations,
            )

        # 14. Length & Foreshadow physical checks
        non_ws_chars = len(re.sub(r"\s+", "", text))
        if scene:
            if non_ws_chars < scene.min_chars:
                violations.append(
                    Violation(
                        rule_id="LEN_001",
                        category="LENGTH_VIOLATION",
                        severity="CRITICAL",
                        matched_text=f"{non_ws_chars} chars < min {scene.min_chars}",
                        line_number=len(text.splitlines()),
                        context_snippet=text[-60:].replace("\n", "⏎"),
                        fix_hint=f"当前正文字数（{non_ws_chars}）不足章纲最低要求（{scene.min_chars}），请充分展开物理动作与对话细节（严禁注水议论）",
                    )
                )
            elif non_ws_chars > scene.max_chars:
                violations.append(
                    Violation(
                        rule_id="LEN_002",
                        category="LENGTH_VIOLATION",
                        severity="CRITICAL",
                        matched_text=f"{non_ws_chars} chars > max {scene.max_chars}",
                        line_number=len(text.splitlines()),
                        context_snippet=text[-60:].replace("\n", "⏎"),
                        fix_hint=f"当前正文字数（{non_ws_chars}）超出章纲上限（{scene.max_chars}），事件写尽即止，请删减冗余铺陈",
                    )
                )

        if due_foreshadows:
            for fs in due_foreshadows:
                # Blank keywords never count as evidence (aligned with WorldStateManager.validate_chapter)
                payoff_kws = [kw for kw in fs.payoff_keywords if kw and str(kw).strip()]
                if payoff_kws and not any(kw in text for kw in payoff_kws):
                    violations.append(
                        Violation(
                            rule_id=f"FS_DUE_{fs.id}",
                            category="FORESHADOW_UNRECOVERED",
                            severity="CRITICAL",
                            matched_text=f"Missing payoff for {fs.id} ({fs.title})",
                            line_number=1,
                            context_snippet=f"Required physical cues: {', '.join(fs.payoff_keywords)}",
                            fix_hint=f"本章必须物理回收伏笔 [{fs.id}]：{fs.payoff_instruction}（需体现关键物理要素：{', '.join(fs.payoff_keywords)}）",
                        )
                    )

        if plant_foreshadows:
            for fs in plant_foreshadows:
                plant_kws = [kw for kw in fs.plant_keywords if kw and str(kw).strip()]
                if plant_kws and not any(kw in text for kw in plant_kws):
                    violations.append(
                        Violation(
                            rule_id=f"FS_PLANT_{fs.id}",
                            category="FORESHADOW_UNPLANTED",
                            severity="CRITICAL",
                            matched_text=f"Missing plant for {fs.id} ({fs.title})",
                            line_number=1,
                            context_snippet=f"Required physical cues: {', '.join(fs.plant_keywords)}",
                            fix_hint=f"本章需埋设物理伏笔 [{fs.id}]：{fs.plant_instruction}（需自然出现细节：{', '.join(fs.plant_keywords)}）",
                        )
                    )

        # 15. Rhythm extremes (CRITICAL) & soft diagnostics (WARNING) for T07 surgical polisher
        diagnostics = self._scan_rhythm_and_diagnostics(
            text=text,
            paragraphs_with_lines=paragraphs_with_lines,
            eff_short_para=eff_short_para,
            eff_pov=eff_pov,
            eff_panel=eff_panel,
            eff_track=eff_track,
            non_ws_chars=non_ws_chars,
            violations=violations,
        )

        passed = len([v for v in violations if v.severity == "CRITICAL"]) == 0
        report = ScanReport(
            passed=passed,
            char_count=non_ws_chars,
            paragraph_count=len(paragraphs),
            violations=violations,
        )
        report.diagnostics = diagnostics  # type: ignore[attr-defined]
        return report

    def diagnose(
        self,
        text: str,
        *,
        scene: Optional[SceneOutline] = None,
        characters: Optional[Dict[str, CharacterAnchor]] = None,
        project_meta: Optional[ProjectMeta] = None,
        world_state: Optional[WorldStateSnapshot] = None,
        chapter: Optional[int] = None,
        pov_mode: Optional[str] = None,
        panel_policy: Optional[str] = None,
        contract_track: Optional[str] = None,
        short_paragraph_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Run scan and return structured paragraph/rhythm diagnostics dictionary for T07 surgical polisher."""
        report = self.scan(
            text=text,
            scene=scene,
            characters=characters,
            project_meta=project_meta,
            world_state=world_state,
            chapter=chapter,
            pov_mode=pov_mode,
            panel_policy=panel_policy,
            contract_track=contract_track,
            short_paragraph_mode=short_paragraph_mode,
        )
        return dict(getattr(report, "diagnostics", {}))

    def _scan_body_tics(self, masked: str, source_text: str, out: List[Violation]) -> None:
        exempt_spans = [(m.start(), m.end()) for m in TIC_010_EXEMPT_RE.finditer(masked)]
        for rule_id, pattern, hint in BODY_TIC_PATTERNS:
            for m in re.finditer(pattern, masked):
                if rule_id == "TIC_010":
                    if any(es <= m.start() and m.end() <= ee for es, ee in exempt_spans):
                        continue
                out.append(
                    Violation(
                        rule_id=rule_id,
                        category="BODY_TIC_CLICHE",
                        severity="CRITICAL",
                        matched_text=source_text[m.start() : m.end()],
                        line_number=_line_number_at(source_text, m.start()),
                        context_snippet=_snippet_around(source_text, m.start(), m.end()),
                        fix_hint=hint,
                    )
                )

    def _scan_hook_001(
        self,
        text: str,
        paragraphs_with_lines: Sequence[Tuple[int, int, str]],
        out: List[Violation],
        *,
        raw_paragraphs_with_lines: Optional[Sequence[Tuple[int, int, str]]] = None,
    ) -> None:
        first_three = paragraphs_with_lines[:3]
        if not first_three:
            return
        raw_first_three = raw_paragraphs_with_lines[:3] if raw_paragraphs_with_lines is not None else first_three
        opening_block = "\n".join(p for _, _, p in first_three)
        raw_opening_block = "\n".join(p for _, _, p in raw_first_three)
        # Focus on the first 3 paragraphs (and first ~220 chars of opening)
        for pattern, hint in HOOK_OPENING_WEATHER_PATTERNS:
            for idx, (_, line_no, para) in enumerate(first_three):
                m = re.search(pattern, para)
                if m:
                    raw_para = raw_first_three[idx][2] if idx < len(raw_first_three) else para
                    # Only treat paragraph 1 or standalone scenery sentences in first 3 paragraphs as violation
                    if idx == 0 or len(para) <= 60 or not _QUOTE_CHAR_CHECK_RE.search(para):
                        out.append(
                            Violation(
                                rule_id="HOOK_001",
                                category="HOOK_VIOLATION",
                                severity="CRITICAL",
                                matched_text=m.group(0),
                                line_number=line_no,
                                context_snippet=raw_para[:60],
                                fix_hint=hint,
                            )
                        )
                        return

        for pattern, hint in HOOK_OPENING_RESUME_PATTERNS:
            m = re.search(pattern, opening_block)
            if m:
                line_no = _line_number_at(text, text.find(m.group(0))) if m.group(0) in text else 1
                out.append(
                    Violation(
                        rule_id="HOOK_001",
                        category="HOOK_VIOLATION",
                        severity="CRITICAL",
                        matched_text=m.group(0),
                        line_number=line_no,
                        context_snippet=_snippet_around(raw_opening_block, m.start(), m.end()),
                        fix_hint=hint,
                    )
                )
                return

    def _scan_hook_002(
        self,
        source_text: str,
        masked_paragraphs_with_lines: Sequence[Tuple[int, int, str]],
        out: List[Violation],
    ) -> None:
        if not masked_paragraphs_with_lines:
            return
        _, last_line_no, last_para = masked_paragraphs_with_lines[-1]
        for pattern, hint in HOOK_DEFLATING_ENDING_PATTERNS:
            m = re.search(pattern, last_para)
            if m:
                out.append(
                    Violation(
                        rule_id="HOOK_002",
                        category="HOOK_VIOLATION",
                        severity="CRITICAL",
                        matched_text=m.group(0),
                        line_number=last_line_no,
                        context_snippet=last_para[-60:],
                        fix_hint=hint,
                    )
                )
                break

    def _scan_safety_gates(
        self,
        text: str,
        masked: str,
        check_modern_identifiers: bool,
        out: List[Violation],
    ) -> None:
        # SAFE_001: Real modern locations & party/gov/military/law-enforcement identifiers
        if check_modern_identifiers:
            for hit in scan_modern_identifiers(text):
                out.append(
                    Violation(
                        rule_id="SAFE_001",
                        category="COMPLIANCE_VIOLATION",
                        severity="CRITICAL",
                        matched_text=hit.matched_text,
                        line_number=_line_number_at(text, hit.start),
                        context_snippet=_snippet_around(text, hit.start, hit.end),
                        fix_hint=f"[{hit.rule_id}] {hit.suggestion}",
                    )
                )

        # SAFE_002: Zero-tolerance symbol-insertion evasion (e.g. 警*察, 肉.体)
        for hit in scan_symbol_evasion(text):
            out.append(
                Violation(
                    rule_id="SAFE_002",
                    category="COMPLIANCE_VIOLATION",
                    severity="CRITICAL",
                    matched_text=hit.matched_text,
                    line_number=_line_number_at(text, hit.start),
                    context_snippet=_snippet_around(text, hit.start, hit.end),
                    fix_hint=f"[{hit.rule_id}] {hit.suggestion}",
                )
            )

        # SAFE_003: NSFW / extreme gore / minor safety redlines
        for hit in scan_redlines(text):
            out.append(
                Violation(
                    rule_id="SAFE_003",
                    category="COMPLIANCE_VIOLATION",
                    severity="CRITICAL",
                    matched_text=hit.matched_text,
                    line_number=_line_number_at(text, hit.start),
                    context_snippet=_snippet_around(text, hit.start, hit.end),
                    fix_hint=f"[{hit.rule_id}] {hit.suggestion}",
                )
            )

        # SAFE_004: Overused AI web-fiction clichés
        for pattern, hint in AI_OVERUSED_CLICHES:
            for m in re.finditer(pattern, masked):
                out.append(
                    Violation(
                        rule_id="SAFE_004",
                        category="AI_CLICHE_VIOLATION",
                        severity="CRITICAL",
                        matched_text=text[m.start() : m.end()],
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint=hint,
                    )
                )

    def _scan_saint_ooc(self, text: str, out: List[Violation]) -> None:
        for pattern, hint in OOC_SAINT_PATTERNS:
            for m in re.finditer(pattern, text):
                out.append(
                    Violation(
                        rule_id="OOC_SAINT",
                        category="OOC_SAINT_VIOLATION",
                        severity="CRITICAL",
                        matched_text=m.group(0),
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint=hint,
                    )
                )

    def _scan_world_state(
        self,
        *,
        text: str,
        masked: str,
        scene: Optional[SceneOutline],
        world_state: WorldStateSnapshot,
        eff_chapter: Optional[int],
        out: List[Violation],
    ) -> None:
        # 1. STATE_DECEASED: Deceased character acting/speaking in real time
        deceased_names = world_state.get_deceased_names()
        if deceased_names:
            names_alt = "|".join(re.escape(n) for n in deceased_names)
            deceased_re = re.compile(rf"(?:{names_alt})[^，、。！？；\n]{{0,6}}{_DECEASED_ACTION_VERBS}")
            for m in deceased_re.finditer(masked):
                s_start, s_end = _sentence_bounds_around(masked, m.start(), m.end())
                sentence_ctx = masked[s_start:s_end]
                if _DECEASED_FLASHBACK_EXEMPT_RE.search(sentence_ctx):
                    continue
                out.append(
                    Violation(
                        rule_id="STATE_DECEASED",
                        category="STATE_CONSISTENCY_VIOLATION",
                        severity="CRITICAL",
                        matched_text=text[m.start() : m.end()],
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint="世界状态机拦截：已死亡角色严禁在当前时间线实时出场施动或说话（仅限明确标记的回忆/遗物/生前提及）",
                    )
                )

        # 2. STATE_FORBIDDEN_TITLE: Obsolete relationship/faction titles
        forbidden_titles = world_state.get_all_forbidden_titles()
        for title in forbidden_titles:
            if not title:
                continue
            for m in re.finditer(re.escape(title), text):
                prefix = text[max(0, m.start() - 8) : m.start()]
                if re.search(r"(?:别叫|别再叫|不再叫|不要叫|别喊|别再喊|不再是|早就不是|不是你的|前任|昔日)(?:我|你|他|她)?$", prefix):
                    continue
                out.append(
                    Violation(
                        rule_id="STATE_FORBIDDEN_TITLE",
                        category="STATE_CONSISTENCY_VIOLATION",
                        severity="CRITICAL",
                        matched_text=m.group(0),
                        line_number=_line_number_at(text, m.start()),
                        context_snippet=_snippet_around(text, m.start(), m.end()),
                        fix_hint=f"世界状态机拦截：关系/阵营已变更，禁止继续使用已作废称谓‘{title}’",
                    )
                )

        # 3. STATE_OVERDRAFT: Single expenditure exceeding current balance
        balances = world_state.numeric_balances
        if balances:
            flagged_overdraft = False
            if scene and scene.ledger_mutations:
                for mut in scene.ledger_mutations:
                    if mut.delta < 0 and mut.ledger_key in balances:
                        current_bal = balances[mut.ledger_key]
                        if current_bal + mut.delta < 0:
                            flagged_overdraft = True
                            out.append(
                                Violation(
                                    rule_id="STATE_OVERDRAFT",
                                    category="STATE_CONSISTENCY_VIOLATION",
                                    severity="CRITICAL",
                                    matched_text=f"{mut.ledger_key}: delta={mut.delta} > balance={current_bal}",
                                    line_number=1,
                                    context_snippet=f"Ledger mutation '{mut.ledger_key}' causes negative balance",
                                    fix_hint=f"账本透支拦截：‘{mut.ledger_key}’当前余额 {current_bal}，单笔支出 {abs(mut.delta)} 超出余额",
                                )
                            )
            if not flagged_overdraft:
                for m in _EXPENDITURE_VERB_RE.finditer(text):
                    verb_raw = m.group("verb")
                    num_raw = m.group("num")
                    scale_raw = m.group("scale") or ""
                    unit_raw = m.group("unit") or ""
                    has_monetary_indicator = bool(
                        unit_raw
                        or scale_raw
                        or any(mag in num_raw for mag in ("千", "万", "亿"))
                    )
                    if not has_monetary_indicator:
                        continue
                    trailing = text[m.end() :]
                    if not unit_raw and _NON_MONETARY_CLASSIFIER_RE.match(trailing):
                        continue
                    amount = parse_chinese_amount(num_raw, scale_raw)
                    if amount is None or amount <= 0:
                        continue
                    if (
                        unit_raw == "块"
                        and not scale_raw
                        and amount == 1.0
                        and verb_raw in _PHYSICAL_OBJECT_VERBS
                        and re.match(r"^\s*[\u4e00-\u9fa5]", trailing)
                        and not _BLOCK_CURRENCY_FOLLOW_RE.match(trailing)
                    ):
                        continue
                    if (
                        unit_raw == "点"
                        and not scale_raw
                        and amount == 1.0
                        and verb_raw not in {"扣除", "扣除了", "消耗", "消耗了", "支出", "支付"}
                        and not re.match(r"^\s*(?:积分|点数|属性|能量|经验|战力|功德|声望|贡献|灵力|体力|生命|法力|气血)", trailing)
                    ):
                        continue
                    targets = _match_balance_keys(unit_raw, balances)
                    for key, bal in targets:
                        if amount > bal:
                            out.append(
                                Violation(
                                    rule_id="STATE_OVERDRAFT",
                                    category="STATE_CONSISTENCY_VIOLATION",
                                    severity="CRITICAL",
                                    matched_text=m.group(0),
                                    line_number=_line_number_at(text, m.start()),
                                    context_snippet=_snippet_around(text, m.start(), m.end()),
                                    fix_hint=f"账本透支拦截：单笔支出 {amount:g} 超出当前‘{key}’账面余额 {bal:g}",
                                )
                            )
                            break

        # 4. STATE_EXPECTATION_OVERDUE: Unfulfilled expectation past deadline
        check_ch = eff_chapter if eff_chapter is not None else (world_state.current_chapter + 1)
        scene_fulfill_directives = (
            {d.id: d for d in scene.expectation_directives if d.action == "fulfill"}
            if scene is not None
            else {}
        )
        for exp in world_state.active_expectations.values():
            if exp.status == "fulfilled":
                continue
            is_overdue = exp.status == "overdue" or exp.due_chapter < check_ch
            missing_evidence_kws: List[str] = []
            if not is_overdue and exp.due_chapter == check_ch and exp.id in scene_fulfill_directives:
                keywords = [
                    kw for kw in scene_fulfill_directives[exp.id].evidence_keywords if kw and str(kw).strip()
                ]
                if keywords and not any(kw in text for kw in keywords):
                    is_overdue = True
                    missing_evidence_kws = keywords
            if is_overdue:
                kw_hint = (
                    f"（本章正文必须自然包含以下至少 1 个证据词：{'、'.join(missing_evidence_kws)}）"
                    if missing_evidence_kws
                    else ""
                )
                out.append(
                    Violation(
                        rule_id="STATE_EXPECTATION_OVERDUE",
                        category="STATE_CONSISTENCY_VIOLATION",
                        severity="CRITICAL",
                        matched_text=f"{exp.id}: {exp.title} (due Ch.{exp.due_chapter})",
                        line_number=1,
                        context_snippet=f"Expectation '{exp.title}' overdue at chapter {check_ch}",
                        fix_hint=f"期待感兑现逾期：承诺 [{exp.id}]‘{exp.title}’应于第 {exp.due_chapter} 章前兑现，请立即在本章完成兑现核销{kw_hint}",
                    )
                )

        # 5. STATE_STALE_LOCATION: > 2 consecutive chapters in the same location_id
        recent = [loc for loc in world_state.recent_locations if loc]
        cur_loc = scene.location_id if (scene is not None and scene.location_id) else ""
        stale_loc: Optional[str] = None
        if cur_loc and len(recent) >= 2 and recent[-1] == cur_loc and recent[-2] == cur_loc:
            stale_loc = cur_loc
        elif not cur_loc and len(recent) >= 3 and len(set(recent[-3:])) == 1:
            stale_loc = recent[-1]
        if stale_loc:
            out.append(
                Violation(
                    rule_id="STATE_STALE_LOCATION",
                    category="STATE_CONSISTENCY_VIOLATION",
                    severity="CRITICAL",
                    matched_text=f"location_id={stale_loc} (>2 consecutive chapters)",
                    line_number=1,
                    context_snippet=f"recent_locations={recent[-3:]}, current={cur_loc or stale_loc}",
                    fix_hint=f"场景僵化拦截：已在同一场景‘{stale_loc}’连续驻留超过 2 章，必须切换地图或推进新空间冲突",
                )
            )

    def _scan_rhythm_and_diagnostics(
        self,
        *,
        text: str,
        paragraphs_with_lines: Sequence[Tuple[int, int, str]],
        eff_short_para: bool,
        eff_pov: str,
        eff_panel: str,
        eff_track: str,
        non_ws_chars: int,
        violations: List[Violation],
    ) -> Dict[str, Any]:
        brick_indices: List[int] = []
        pure_narration_runs: List[Dict[str, Any]] = []
        bare_dialogue_runs: List[Dict[str, Any]] = []
        bystander_shock_paragraphs: List[int] = []
        burstiness_paragraphs: List[int] = []

        # Map 1-based line_number -> 0-based paragraph_index
        line_to_para_idx: Dict[int, int] = {line_no: para_idx for para_idx, line_no, _ in paragraphs_with_lines}

        # A. Brick paragraphs (>120 chars) & Pure narration walls (>320 chars) in Tomato short-para mode
        narr_run_chars = 0
        narr_start_idx: Optional[int] = None
        narr_start_line: int = 1
        narr_end_idx: int = 0

        for para_idx, line_no, para in paragraphs_with_lines:
            para_non_ws = len(re.sub(r"\s+", "", para))
            if eff_short_para and para_non_ws > 120:
                brick_indices.append(para_idx)
                violations.append(
                    Violation(
                        rule_id="RHYTHM_BRICK",
                        category="RHYTHM_VIOLATION",
                        severity="CRITICAL",
                        matched_text=f"Paragraph #{para_idx + 1} ({para_non_ws} chars > 120)",
                        line_number=line_no,
                        context_snippet=para[:60],
                        fix_hint=f"第 {para_idx + 1} 段共 {para_non_ws} 字（超过番茄竖屏短段 120 字上限），请拆分为短句呼吸段落",
                    )
                )

            has_quote_or_panel = bool(_QUOTE_CHAR_CHECK_RE.search(para) or PANEL_SPAN_RE.search(para))
            if has_quote_or_panel:
                if eff_short_para and narr_run_chars > 320 and narr_start_idx is not None:
                    pure_narration_runs.append(
                        {
                            "start_para": narr_start_idx,
                            "end_para": narr_end_idx,
                            "char_count": narr_run_chars,
                        }
                    )
                    violations.append(
                        Violation(
                            rule_id="RHYTHM_NARRATION_WALL",
                            category="RHYTHM_VIOLATION",
                            severity="CRITICAL",
                            matched_text=f"Paragraphs #{narr_start_idx + 1}~#{narr_end_idx + 1} ({narr_run_chars} chars > 320)",
                            line_number=narr_start_line,
                            context_snippet=paragraphs_with_lines[narr_start_idx][2][:60],
                            fix_hint=(
                                f"第 {narr_start_idx + 1}~{narr_end_idx + 1} 段连续不含引号的纯旁白达 {narr_run_chars} 字（超过 320 字上限），"
                                "请在该区间中段穿插带引号“...”的人物对话、角色低声自语/复核或书面/短信引文（纯动作描写不打断纯旁白计数）"
                            ),
                        )
                    )
                narr_run_chars = 0
                narr_start_idx = None
            else:
                if narr_start_idx is None:
                    narr_start_idx = para_idx
                    narr_start_line = line_no
                narr_end_idx = para_idx
                narr_run_chars += para_non_ws

        if eff_short_para and narr_run_chars > 320 and narr_start_idx is not None:
            pure_narration_runs.append(
                {
                    "start_para": narr_start_idx,
                    "end_para": narr_end_idx,
                    "char_count": narr_run_chars,
                }
            )
            violations.append(
                Violation(
                    rule_id="RHYTHM_NARRATION_WALL",
                    category="RHYTHM_VIOLATION",
                    severity="CRITICAL",
                    matched_text=f"Paragraphs #{narr_start_idx + 1}~#{narr_end_idx + 1} ({narr_run_chars} chars > 320)",
                    line_number=narr_start_line,
                    context_snippet=paragraphs_with_lines[narr_start_idx][2][:60],
                    fix_hint=(
                        f"第 {narr_start_idx + 1}~{narr_end_idx + 1} 段连续不含引号的纯旁白达 {narr_run_chars} 字（超过 320 字上限），"
                        "请在该区间中段穿插带引号“...”的人物对话、角色低声自语/复核或书面/短信引文（纯动作描写不打断纯旁白计数）"
                    ),
                )
            )

        # B. Consecutive bare dialogue turns (>4 turns without action/expression tags)
        bare_start_idx: Optional[int] = None
        bare_start_line: int = 1
        bare_count = 0
        bare_end_idx = 0

        for para_idx, line_no, para in paragraphs_with_lines:
            if _is_bare_dialogue_paragraph(para):
                if bare_start_idx is None:
                    bare_start_idx = para_idx
                    bare_start_line = line_no
                bare_end_idx = para_idx
                bare_count += 1
            else:
                if bare_count > 4 and bare_start_idx is not None:
                    bare_dialogue_runs.append(
                        {"start_para": bare_start_idx, "end_para": bare_end_idx, "turn_count": bare_count}
                    )
                    violations.append(
                        Violation(
                            rule_id="RHYTHM_BARE_DIALOGUE",
                            category="RHYTHM_VIOLATION",
                            severity="CRITICAL",
                            matched_text=f"Paragraphs #{bare_start_idx + 1}~#{bare_end_idx + 1} ({bare_count} bare dialogue turns > 4)",
                            line_number=bare_start_line,
                            context_snippet=paragraphs_with_lines[bare_start_idx][2][:60],
                            fix_hint=(
                                f"第 {bare_start_idx + 1}~{bare_end_idx + 1} 段连续出现 {bare_count} 轮无动作神态描写的‘裸对话’，"
                                "请补入微动作、器物触碰或停顿神态"
                            ),
                        )
                    )
                bare_start_idx = None
                bare_count = 0

        if bare_count > 4 and bare_start_idx is not None:
            bare_dialogue_runs.append(
                {"start_para": bare_start_idx, "end_para": bare_end_idx, "turn_count": bare_count}
            )
            violations.append(
                Violation(
                    rule_id="RHYTHM_BARE_DIALOGUE",
                    category="RHYTHM_VIOLATION",
                    severity="CRITICAL",
                    matched_text=f"Paragraphs #{bare_start_idx + 1}~#{bare_end_idx + 1} ({bare_count} bare dialogue turns > 4)",
                    line_number=bare_start_line,
                    context_snippet=paragraphs_with_lines[bare_start_idx][2][:60],
                    fix_hint=(
                        f"第 {bare_start_idx + 1}~{bare_end_idx + 1} 段连续出现 {bare_count} 轮无动作神态描写的‘裸对话’，"
                        "请补入微动作、器物触碰或停顿神态"
                    ),
                )
            )

        # C. Sentence-length Burstiness sigma (< 10 -> WARNING)
        sentence_lengths: List[int] = []
        para_sent_lengths: Dict[int, List[int]] = {}
        for para_idx, _, para in paragraphs_with_lines:
            sents = [s.strip() for s in re.split(r"[。！？；]+", para) if s.strip()]
            lens = [len(re.sub(r"\s+", "", s)) for s in sents if len(re.sub(r"\s+", "", s)) > 0]
            if lens:
                para_sent_lengths[para_idx] = lens
                sentence_lengths.extend(lens)

        sent_count = len(sentence_lengths)
        if sent_count > 0:
            mean_len = sum(sentence_lengths) / sent_count
            variance = sum((x - mean_len) ** 2 for x in sentence_lengths) / sent_count
            sigma = math.sqrt(variance)
        else:
            mean_len = 0.0
            sigma = 0.0

        if sent_count >= 8 and sigma < 10.0:
            for para_idx, lens in para_sent_lengths.items():
                if all(abs(l - mean_len) < 8.0 for l in lens):
                    burstiness_paragraphs.append(para_idx)
            if not burstiness_paragraphs and paragraphs_with_lines:
                burstiness_paragraphs = [p_idx for p_idx, _, _ in paragraphs_with_lines[:3]]
            first_burst_line = (
                paragraphs_with_lines[burstiness_paragraphs[0]][1] if burstiness_paragraphs else 1
            )
            violations.append(
                Violation(
                    rule_id="RHYTHM_BURSTINESS",
                    category="RHYTHM_DIAGNOSTIC",
                    severity="WARNING",
                    matched_text=f"Burstiness σ={sigma:.2f} < 10.0 (mean={mean_len:.1f}, n={sent_count})",
                    line_number=first_burst_line,
                    context_snippet=f"Uniform sentence cadence in paragraphs {[i + 1 for i in burstiness_paragraphs[:8]]}",
                    fix_hint=(
                        f"句长节奏标准差 Burstiness σ={sigma:.2f} < 10（全篇句长过于均匀，疑似 AI 匀速节拍），"
                        "建议由局部精修器在长句之间穿插 3~8 字短促动作句拉开长短差"
                    ),
                )
            )

        # D. Excessive bystander-shock clichés (WARNING when >= 2 hits)
        shock_hits: List[Tuple[int, int, str, str]] = []
        for para_idx, line_no, para in paragraphs_with_lines:
            for pat in BYSTANDER_SHOCK_PATTERNS:
                m = re.search(pat, para)
                if m:
                    shock_hits.append((para_idx, line_no, m.group(0), para[:60]))
                    break
        if len(shock_hits) >= 2:
            for para_idx, line_no, matched_str, snippet in shock_hits:
                bystander_shock_paragraphs.append(para_idx)
                violations.append(
                    Violation(
                        rule_id="RHYTHM_BYSTANDER_SHOCK",
                        category="RHYTHM_DIAGNOSTIC",
                        severity="WARNING",
                        matched_text=matched_str,
                        line_number=line_no,
                        context_snippet=snippet,
                        fix_hint=f"第 {para_idx + 1} 段路人震惊套话偏多（全章累计 {len(shock_hits)} 处），建议改写为具体人物反常反应或动作失措",
                    )
                )

        # Build paragraph index sets for T07 surgical polisher
        critical_paras: set[int] = set(brick_indices)
        warning_paras: set[int] = set(burstiness_paragraphs) | set(bystander_shock_paragraphs)
        for run in pure_narration_runs:
            critical_paras.update(range(run["start_para"], run["end_para"] + 1))
        for run in bare_dialogue_runs:
            critical_paras.update(range(run["start_para"], run["end_para"] + 1))
        for v in violations:
            p_idx = line_to_para_idx.get(v.line_number)
            if p_idx is not None:
                if v.severity == "CRITICAL":
                    critical_paras.add(p_idx)
                else:
                    warning_paras.add(p_idx)

        critical_list = sorted(critical_paras)
        warning_list = sorted(warning_paras)
        flagged_list = sorted(critical_paras | warning_paras)

        return {
            "char_count": non_ws_chars,
            "paragraph_count": len(paragraphs_with_lines),
            "sentence_count": sent_count,
            "mean_sentence_length": round(mean_len, 2),
            "burstiness_sigma": round(sigma, 2),
            "short_paragraph_mode": eff_short_para,
            "pov_mode": eff_pov,
            "panel_policy": eff_panel,
            "contract_track": eff_track,
            "brick_paragraph_indices": brick_indices,
            "pure_narration_runs": pure_narration_runs,
            "bare_dialogue_runs": bare_dialogue_runs,
            "bystander_shock_paragraphs": bystander_shock_paragraphs,
            "burstiness_paragraph_indices": burstiness_paragraphs,
            "critical_paragraph_indices": critical_list,
            "warning_paragraph_indices": warning_list,
            "flagged_paragraph_indices": flagged_list,
        }

    @staticmethod
    def _apply_regex_group(
        scan_target: str,
        patterns: List[Tuple[str, str, str]],
        category: str,
        out: List[Violation],
        source_text: Optional[str] = None,
    ) -> None:
        orig = source_text if source_text is not None else scan_target
        for rule_id, pattern, hint in patterns:
            for m in re.finditer(pattern, scan_target):
                out.append(
                    Violation(
                        rule_id=rule_id,
                        category=category,
                        severity="CRITICAL",
                        matched_text=orig[m.start() : m.end()],
                        line_number=_line_number_at(orig, m.start()),
                        context_snippet=_snippet_around(orig, m.start(), m.end()),
                        fix_hint=hint,
                    )
                )
