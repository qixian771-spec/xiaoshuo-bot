from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .namer import is_common_simplified_hanzi


class SanitizerError(ValueError):
    """Base exception for content compliance & sanitizer validation failures."""


class InvalidTweetAliasError(SanitizerError):
    """Raised when a Tomato influencer tweet alias violates the 4-8 pure common simplified hanzi rule."""


@dataclass(frozen=True)
class ComplianceViolation:
    rule_id: str
    category: str  # MODERN_REAL_LOCATION | MODERN_REAL_ORG | SYMBOL_EVASION | RED_LINE | TWEET_ALIAS
    severity: str  # CRITICAL | WARNING
    matched_text: str
    suggestion: str
    start: int = 0
    end: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Compliant fictionalization replacement suggestions for modern settings
FICTIONALIZATION_MAP: Dict[str, str] = {
    "中华人民共和国": "大夏",
    "中国": "大夏",
    "北京市": "京城",
    "北京": "京城",
    "上海市": "海城",
    "上海": "海城",
    "广州市": "南城",
    "广州": "南城",
    "深圳市": "深港市",
    "深圳": "深港市",
    "杭州市": "江城",
    "杭州": "江城",
    "南京市": "金陵",
    "南京": "金陵",
    "成都市": "锦城",
    "成都": "锦城",
    "武汉市": "汉江市",
    "武汉": "汉江市",
    "重庆市": "山城",
    "重庆": "山城",
    "西安市": "长安",
    "西安": "长安",
    "清华大学": "海城大学",
    "北京大学": "江城大学",
    "复旦大学": "东海大学",
    "浙江大学": "江南大学",
    "上海交通大学": "海城交通大学",
    "南京大学": "金陵大学",
    "武汉大学": "汉江大学",
    "中山大学": "南城大学",
    "中国人民大学": "京城人文大学",
    "中国科学技术大学": "大夏理工大学",
    "公安局": "市局巡捕",
    "派出所": "巡捕房",
    "警察局": "巡捕署",
    "人民警察": "巡捕",
    "公安干警": "巡捕",
    "刑警大队": "重案巡捕队",
    "刑警": "重案巡捕",
    "警察": "巡捕",
    "检察院": "督察署",
    "人民检察院": "督察署",
    "人民法院": "审判庭",
    "法院": "审判庭",
    "中纪委": "督察组",
    "纪委": "督察组",
    "巡视组": "督察组",
    "国务院": "内阁",
    "市委": "市政厅",
    "省委": "行省议会",
    "解放军": "镇边军",
    "武警": "城防卫",
}


# 1. Real modern countries / provinces / cities / universities
MODERN_REAL_LOCATION_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    (
        "LOC_COUNTRY",
        r"中华人民共和国|(?<![古新旧])中国(?![茶风结画红传统古代式餐医字结])|美利坚合众国",
        "现代架空背景请替换为「大夏」等架空国名",
    ),
    (
        "LOC_PROVINCE",
        r"(?:北京|上海|天津|重庆)市|(?:广东|江苏|浙江|山东|四川|湖北|湖南|河南|河北|福建|安徽|辽宁|陕西|山西|江西|云南|贵州|海南|吉林|黑龙江|甘肃|青海|台湾)省|(?:广西壮族|内蒙古|新疆维吾尔|西藏|宁夏回族)自治区|(?:香港|澳门)特别行政区",
        "现代背景禁止出现真实省区直辖市名称，请替换为「江南行省 / 海城 / 江城」等架空地名",
    ),
    (
        "LOC_CITY",
        r"北京(?!时间)|上海(?!牌)|广州|深圳|杭州|南京|成都|武汉|重庆|西安|苏州|天津|长沙|郑州|青岛|大连|宁波|厦门|福州|合肥|济南|沈阳|哈尔滨|昆明|长春|石家庄|南昌|太原|南宁|贵阳|海口|兰州|乌鲁木齐|拉萨|银川|西宁|呼和浩特",
        "现代背景禁止出现真实城市名，请替换为「海城 / 江城 / 南城 / 京城」等架空城市名",
    ),
    (
        "LOC_UNIV",
        r"(?:清华|北京|复旦|上海交通|浙江|南京|中国科学技术|武汉|中山|华中科技|西安交通|哈尔滨工业|中国人民|北京师范|同济|南开|四川|吉林|厦门|中南)大学",
        "禁止出现现实高校全称，请替换为「江城大学 / 海城大学」等架空校名",
    ),
)

# 2. Real modern party / government / military / police / procuratorate / court identifiers
MODERN_REAL_ORG_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    (
        "ORG_GOV_PARTY",
        r"国务院|中南海|人民大会堂|全国人大|全国政协|中纪委|中央纪委|纪委监委|中组部|中宣部|中央军委|(?:省|市|区|县)委书记|(?:省|市)委(?:常委会|办公室)?|(?:省|市|县)政府|发改委|公安部|国安部|司法部|国防部",
        "现代背景禁止出现真实党政机关与职务全称，请架空为「内阁 / 市政厅 / 督察组」",
    ),
    (
        "ORG_MILITARY",
        r"中国人民解放军|解放军|武警(?:部队|总队)?|(?:东部|南部|西部|北部|中部)战区",
        "禁止使用现役军队及战区番号，请架空为「镇边军 / 城防卫 / 第X防区」",
    ),
    (
        "ORG_LAW_ENFORCEMENT",
        r"最高人民(?:法院|检察院)|最高法|最高检|人民(?:法院|检察院)|公安局|派出所|警察局|人民警察|公安干警|刑警(?:大队|支队)?|特警(?:支队)?|交警(?:大队|支队)?|国安局|警察",
        "现代背景禁止出现现实公检法机构及称谓，请替换为「市局巡捕 / 巡捕房 / 督察组 / 审判庭」",
    ),
)

# 3. Zero-tolerance symbol-insertion evasion patterns (e.g. 警*察, 肉.体, 警_察, 杀-人)
_EVASION_SEP = r"[ \t\u00a0\u3000*._\-/|\\~·•@#$%^&+=`']{1,4}"

_EVASION_WORD_TUPLES: Tuple[Tuple[str, ...], ...] = (
    ("警", "察"),
    ("公", "安"),
    ("派", "出", "所"),
    ("法", "院"),
    ("检", "察"),
    ("纪", "委"),
    ("政", "府"),
    ("政", "治"),
    ("领", "导"),
    ("国", "家"),
    ("军", "队"),
    ("肉", "体"),
    ("裸", "体"),
    ("下", "体"),
    ("胸", "部"),
    ("杀", "人"),
    ("自", "杀"),
    ("死", "亡"),
    ("割", "腕"),
    ("跳", "楼"),
    ("强", "奸"),
    ("猥", "亵"),
    ("色", "情"),
    ("淫", "秽"),
    ("毒", "品"),
    ("冰", "毒"),
    ("大", "麻"),
    ("枪", "支"),
    ("爆", "炸"),
    ("血", "腥"),
    ("鲜", "血"),
    ("分", "尸"),
    ("肢", "解"),
    ("暴", "力"),
)


def _compile_evasion_patterns() -> Tuple[Tuple[str, re.Pattern[str], str], ...]:
    compiled: List[Tuple[str, re.Pattern[str], str]] = []
    for idx, chars in enumerate(_EVASION_WORD_TUPLES, start=1):
        joined = _EVASION_SEP.join(re.escape(c) for c in chars)
        word = "".join(chars)
        compiled.append(
            (
                f"EVADE_{idx:03d}",
                re.compile(joined),
                f"严禁通过插入星号/点号/下划线/空格等符号绕过敏感词检测（命中「{word}」变体）",
            )
        )
    return tuple(compiled)


SYMBOL_EVASION_PATTERNS: Tuple[Tuple[str, re.Pattern[str], str], ...] = _compile_evasion_patterns()

# 4. High-risk red-line patterns (NSFW / extreme gore / minors)
HIGH_RISK_REDLINE_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    (
        "RED_NSFW_001",
        r"强奸|轮奸|迷奸|强暴|性侵|猥亵|淫秽|淫亵|嫖娼|卖淫|乱伦|群交|裸体交欢|高潮呻吟",
        "触发涉黄/性暴力高危红线，严禁输出",
    ),
    (
        "RED_NSFW_002",
        r"阴茎|阴道|阴蒂|阳具|生殖器|射精|肉棒|花穴",
        "触发露骨色情描写红线，严禁输出",
    ),
    (
        "RED_MINOR_001",
        r"(?:幼女|未成年|儿童|女童|男童|小学生|初中生)[^。！？\n]{0,18}(?:裸体|性侵|猥亵|强奸|侵犯|调教|床戏|下体|抚摸胸部)",
        "触发未成年人高危保护红线，绝对零容忍",
    ),
    (
        "RED_MINOR_002",
        r"(?:猥亵|性侵|强奸|调教|侵犯)[^。！？\n]{0,12}(?:幼女|未成年|儿童|女童|男童|小学生|初中生)",
        "触发未成年人高危保护红线，绝对零容忍",
    ),
    (
        "RED_GORE_001",
        r"碎尸万段|凌迟处死|活体解剖|生吃人肉|分尸烹煮|掏出内脏|肠穿肚烂|脑浆迸裂|剥皮抽筋",
        "触发血腥虐杀高危红线，请改为克制留白描写",
    ),
    (
        "RED_ILLEGAL_001",
        r"自杀教程|教唆自杀|自杀指南|制毒配方|制造炸药教程",
        "触发违法违禁及自残诱导高危红线，绝对禁止",
    ),
)


def suggest_fictionalization(term: str) -> str:
    """Return compliant fictionalized replacement if known, else generic guidance."""
    if term in FICTIONALIZATION_MAP:
        return FICTIONALIZATION_MAP[term]
    for k, v in FICTIONALIZATION_MAP.items():
        if k in term:
            return v
    return "使用大夏/海城/江城大学/市局巡捕/督察组等合规架空词替换"


def scan_symbol_evasion(text: str) -> List[ComplianceViolation]:
    """Scan text for zero-tolerance symbol-inserted sensitive word evasion variants."""
    hits: List[ComplianceViolation] = []
    for rule_id, pattern, hint in SYMBOL_EVASION_PATTERNS:
        for m in pattern.finditer(text):
            hits.append(
                ComplianceViolation(
                    rule_id=rule_id,
                    category="SYMBOL_EVASION",
                    severity="CRITICAL",
                    matched_text=m.group(0),
                    suggestion=hint,
                    start=m.start(),
                    end=m.end(),
                )
            )
    return hits


def scan_redlines(text: str) -> List[ComplianceViolation]:
    """Scan text for explicit NSFW, gore, minor-safety, and illegal red-line violations."""
    hits: List[ComplianceViolation] = []
    for rule_id, pattern_str, hint in HIGH_RISK_REDLINE_PATTERNS:
        for m in re.finditer(pattern_str, text):
            hits.append(
                ComplianceViolation(
                    rule_id=rule_id,
                    category="RED_LINE",
                    severity="CRITICAL",
                    matched_text=m.group(0),
                    suggestion=hint,
                    start=m.start(),
                    end=m.end(),
                )
            )
    return hits


def scan_modern_identifiers(text: str) -> List[ComplianceViolation]:
    """Scan text for real modern locations, universities, and party/gov/military/law-enforcement terms."""
    hits: List[ComplianceViolation] = []
    for rule_id, pattern_str, hint in MODERN_REAL_LOCATION_PATTERNS:
        for m in re.finditer(pattern_str, text):
            matched = m.group(0)
            rep = suggest_fictionalization(matched)
            hits.append(
                ComplianceViolation(
                    rule_id=rule_id,
                    category="MODERN_REAL_LOCATION",
                    severity="CRITICAL",
                    matched_text=matched,
                    suggestion=f"{hint}（推荐替换为「{rep}」）",
                    start=m.start(),
                    end=m.end(),
                )
            )
    for rule_id, pattern_str, hint in MODERN_REAL_ORG_PATTERNS:
        for m in re.finditer(pattern_str, text):
            matched = m.group(0)
            rep = suggest_fictionalization(matched)
            hits.append(
                ComplianceViolation(
                    rule_id=rule_id,
                    category="MODERN_REAL_ORG",
                    severity="CRITICAL",
                    matched_text=matched,
                    suggestion=f"{hint}（推荐替换为「{rep}」）",
                    start=m.start(),
                    end=m.end(),
                )
            )
    return hits


def scan_compliance(
    text: str,
    check_modern_identifiers: bool = True,
) -> List[ComplianceViolation]:
    """Run all compliance scans (symbol evasion, redlines, and optionally modern real identifiers)."""
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    violations: List[ComplianceViolation] = []
    violations.extend(scan_symbol_evasion(text))
    violations.extend(scan_redlines(text))
    if check_modern_identifiers:
        violations.extend(scan_modern_identifiers(text))
    return violations


def fictionalize_modern_terms(text: str) -> str:
    """Deterministically replace known real modern terms using FICTIONALIZATION_MAP (longest keys first)."""
    result = text
    for src in sorted(FICTIONALIZATION_MAP.keys(), key=len, reverse=True):
        dst = FICTIONALIZATION_MAP[src]
        if src == "北京":
            result = re.sub(r"北京(?!时间)", dst, result)
        elif src == "上海":
            result = re.sub(r"上海(?!牌)", dst, result)
        elif src == "中国":
            result = re.sub(r"(?<![古新旧])中国(?![茶风结画红传统古代式餐医字])", dst, result)
        else:
            result = result.replace(src, dst)
    return result


def inspect_tweet_alias(alias: str) -> Optional[ComplianceViolation]:
    """Check Tomato influencer tweet alias and return structured ComplianceViolation if invalid, else None."""
    if not isinstance(alias, str):
        return ComplianceViolation(
            rule_id="ALIAS_TYPE",
            category="TWEET_ALIAS",
            severity="CRITICAL",
            matched_text=repr(alias),
            suggestion="推文别名必须是字符串",
        )
    if not (4 <= len(alias) <= 8):
        return ComplianceViolation(
            rule_id="ALIAS_LEN",
            category="TWEET_ALIAS",
            severity="CRITICAL",
            matched_text=alias,
            suggestion=f"番茄达人推文别名必须严格为 4~8 个汉字，当前长度 {len(alias)}",
        )
    for ch in alias:
        if not is_common_simplified_hanzi(ch):
            return ComplianceViolation(
                rule_id="ALIAS_CHAR",
                category="TWEET_ALIAS",
                severity="CRITICAL",
                matched_text=alias,
                suggestion=(
                    f"番茄达人推文别名仅允许纯简体常用汉字（零标点、零数字、零英文字母、零生僻繁体），"
                    f"发现非法字符 {ch!r}"
                ),
            )
    return None


def validate_tweet_alias(alias: str) -> str:
    """Validate Tomato influencer tweet alias (4~8 pure common simplified Chinese chars). Raises InvalidTweetAliasError."""
    violation = inspect_tweet_alias(alias)
    if violation is not None:
        raise InvalidTweetAliasError(violation.suggestion)
    return alias
