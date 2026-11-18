from __future__ import annotations

import random
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pypinyin import Style, lazy_pinyin


class NamerError(ValueError):
    """Base exception for character naming and validation errors."""


class BlacklistedNameError(NamerError):
    """Raised when a character name hits the Tomato AI overused name blacklist."""


class RareCharacterError(NamerError):
    """Raised when a character name contains non-Hanzi or rare/archaic characters outside common simplified Chinese."""


class HomophoneCollisionError(NamerError):
    """Raised when a character name shares toneless pinyin with an existing cast member."""


# Curated common simplified Chinese characters outside GB2312 that appear in 通用规范汉字表一/二级
_SUPPLEMENTAL_COMMON_HANZI: frozenset[str] = frozenset({"珺", "旻", "祎"})

# Ultra-obscure / archaic GB2312 Level-2 characters unsuitable for normal naming or tweet aliases
_OBSCURE_GB2312_EXCLUDE: frozenset[str] = frozenset(
    {
        "蘩", "颃", "暾", "祧", "狴", "犴", "蠛", "瓞",
        "魑", "魅", "魍", "魉", "饕", "餮", "龌", "龊",
        "鹾", "齑", "耄", "耋", "氆", "氇", "毹", "毵",
    }
)


def is_common_simplified_hanzi(ch: str) -> bool:
    """Check if a single character falls within common simplified Chinese characters (GB2312 + 通用规范汉字表一/二级 coverage)."""
    if not isinstance(ch, str) or len(ch) != 1:
        return False
    if not ("\u4e00" <= ch <= "\u9fff"):
        return False
    if ch in _OBSCURE_GB2312_EXCLUDE:
        return False
    if ch in _SUPPLEMENTAL_COMMON_HANZI:
        return True
    try:
        encoded = ch.encode("gb2312")
    except UnicodeEncodeError:
        return False
    if len(encoded) != 2:
        return False
    area = encoded[0] - 0xA0
    # Areas 16-55 = GB2312 Level 1 (3755 most common simplified chars)
    # Areas 56-87 = GB2312 Level 2 (3008 secondary simplified chars)
    return 16 <= area <= 87


def validate_common_simplified_hanzi(text: str, label: str = "文本") -> None:
    """Ensure every character in text is a common simplified Chinese character."""
    if not isinstance(text, str) or not text:
        raise RareCharacterError(f"{label}不能为空")
    for ch in text:
        if not is_common_simplified_hanzi(ch):
            raise RareCharacterError(
                f"{label} {text!r} 包含非通用简体常用汉字或生僻/繁体/非汉字字符 {ch!r}（U+{ord(ch):04X}）"
            )


def to_toneless_pinyin(name: str) -> Tuple[str, ...]:
    """Convert a Chinese name to a tuple of toneless lowercase pinyin syllables using pypinyin."""
    validate_common_simplified_hanzi(name, label="人名")
    syllables = lazy_pinyin(name, style=Style.NORMAL, errors="strict")
    return tuple(s.lower() for s in syllables)


# Tomato / AIGC high-frequency overused character name blacklist
AI_NAME_BLACKLIST: frozenset[str] = frozenset(
    {
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
        "顾夜白",
        "霍庭深",
        "陆北辰",
        "傅司寒",
        "叶凡",
        "萧炎",
        "林清雪",
        "苏晚晴",
        "沈清秋",
        "江澈",
        "墨渊",
        "楚尘",
        "战北爵",
        "薄夜",
        "温言",
        "苏念",
        "夏星辰",
        "慕容云海",
        "南宫羽",
        "上官婉",
        "顾言",
        "顾泽",
        "顾辞",
        "陆衍",
        "沈砚",
        "谢妄",
        "谢辞",
        "叶辰",
        "陈凡",
        "林浅",
        "沈夜",
        "顾沉",
        "傅沉",
        "陆妄",
        "裴晏",
        "谢知行",
        "苏晚",
        "林知意",
        "霍斯年",
        "厉景琛",
        "傅寒洲",
        "顾景深",
        "陆司寒",
        "沈清欢",
        "苏软软",
        "林星晚",
        "白月光",
    }
)

# Regexes catching overused AI surname + cliché morpheme concatenations
AI_NAME_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:顾|傅|厉|霍|陆|沈|薄|战|墨|裴|谢)"
        r"(?:寒|斯|霆|景|夜|北|司|御|墨|晏|妄|辞|渊|尘|沉|衍|砚|寂|庭)"
        r"(?:洲|年|琛|深|白|辰|寒|爵|川|之|行|礼|舟)?$"
    ),
    re.compile(
        r"^(?:林|苏|沈|温|夏|叶|柳|楚|白|江)"
        r"(?:晚|清|知|浅|念|语|如|若|初|星|诗|婉|默)"
        r"(?:意|浅|晴|雪|烟|辰|安|溪|柠|禾|眠|夏|秋|欢)?$"
    ),
    re.compile(
        r"^(?:慕容|南宫|欧阳|皇甫|司徒|上官|独孤|百里)"
        r"(?:云|羽|婉|尘|墨|夜|寒|绝|风|雪|辰|轩){1,2}$"
    ),
    re.compile(r"^(?:叶|萧|林|陈|楚|秦)(?:凡|炎|动|尘|辰|天|风|玄)$"),
)


def is_blacklisted_name(name: str) -> bool:
    """Return True if name hits explicit AI blacklist or AI cliché surname+character patterns."""
    if name in AI_NAME_BLACKLIST:
        return True
    return any(p.match(name) is not None for p in AI_NAME_PATTERNS)


# Era / gender realistic surname + given-name pools (all vetted against common simplified hanzi & AI blacklist)
_ERA_POOLS: Dict[str, Dict[str, Tuple[Sequence[str], Sequence[str]]]] = {
    "modern": {
        "male": (
            ("周", "陈", "张", "王", "赵", "刘", "杨", "黄", "吴", "徐", "孙", "马", "朱", "胡", "郭", "何", "高", "梁", "郑", "宋", "唐", "许", "冯", "邓", "曹", "彭", "曾", "田", "董", "袁", "潘", "蒋"),
            ("牧野", "启明", "知恒", "思远", "怀安", "季同", "正阳", "长青", "振宇", "峻峰", "望山", "嘉言", "定邦", "少康", "润生", "叙", "临川", "承泽", "明哲", "家豪", "文涛", "俊杰", "天佑", "景明", "志远", "向阳", "伯文", "子墨", "立诚", "敬尧", "致远", "海涛"),
        ),
        "female": (
            ("许", "宋", "蒋", "韩", "冯", "邓", "曹", "罗", "唐", "董", "袁", "潘", "余", "杜", "程", "魏", "吕", "丁", "任", "姚", "卢", "姜", "崔", "钟", "谭", "汪", "范", "金", "石", "廖", "贾", "韦"),
            ("清禾", "念慈", "望舒", "斯咏", "繁星", "明月", "晓燕", "雨晴", "丽娟", "静怡", "佩琪", "文静", "思敏", "嘉敏", "若谷", "安澜", "采薇", "知微", "素心", "秋水", "映雪", "舒窈", "书瑶", "惠敏", "兰心", "景澄", "照夜", "海宁", "云舒", "月明", "宁远", "青青"),
        ),
    },
    "era_7080": {
        "male": (
            ("张", "王", "李", "赵", "刘", "陈", "杨", "周", "吴", "孙", "马", "朱", "胡", "郭", "郑", "高", "林", "罗", "梁", "宋", "田", "董", "袁", "曹"),
            ("建国", "建军", "卫国", "志刚", "国强", "胜利", "向前", "解放", "大勇", "铁柱", "永康", "德胜", "长顺", "双喜", "根生", "明远", "东升", "红兵", "保国", "富贵", "跃进", "成钢", "庆丰", "兴旺"),
        ),
        "female": (
            ("王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴", "徐", "孙", "马", "朱", "胡", "郭", "郑", "高", "林", "何", "田", "冯", "曹", "邓"),
            ("秀英", "桂兰", "春梅", "淑芬", "翠花", "红霞", "玉珍", "慧芳", "红梅", "秀兰", "桂英", "玉梅", "秋月", "丽华", "爱华", "素芬", "彩霞", "春燕", "巧云", "凤英", "月娥", "冬梅", "雪莲", "淑华"),
        ),
    },
    "ancient": {
        "male": (
            ("谢", "程", "崔", "杜", "孟", "冯", "贺", "薛", "严", "宋", "韦", "范", "曹", "许", "姜", "罗", "蒋", "蔡", "韩", "齐", "袁", "董", "周", "唐"),
            ("文昌", "明远", "敬尧", "衡", "知远", "敬之", "兰亭", "延龄", "子昂", "怀瑾", "承宗", "仲霖", "元礼", "攸宁", "伯庸", "晦明", "玄度", "元亮", "叔夜", "季常", "清晏", "彦博", "守仁", "长风"),
        ),
        "female": (
            ("蒋", "孟", "韩", "蔡", "罗", "齐", "袁", "董", "崔", "杜", "冯", "薛", "韦", "范", "许", "姜", "程", "周", "宋", "唐", "卢", "姚", "余", "严"),
            ("清禾", "知微", "令仪", "素心", "云裳", "静婉", "思齐", "月明", "幼微", "妙仪", "徽音", "锦书", "兰芝", "婉仪", "琼华", "清徽", "玉茗", "淑仪", "明昭", "若水", "嘉禾", "映月", "惜花", "疏影"),
        ),
    },
}

_GENRE_TO_ERA: Dict[str, str] = {
    "urban_brainhole": "modern",
    "xuanhuan_brainhole": "ancient",
    "apocalypse_hoarding": "modern",
    "suspense_rules": "modern",
    "history_brainhole": "ancient",
    "modern_mind_reading": "modern",
    "ancient_rebirth": "ancient",
    "era_7080_wealth": "era_7080",
    "quick_transmigration": "modern",
    "ancient_mystery": "ancient",
    "literary_suspense": "modern",
    "modern": "modern",
    "urban": "modern",
    "era_7080": "era_7080",
    "7080": "era_7080",
    "ancient": "ancient",
    "xuanhuan": "ancient",
    "history": "ancient",
}


def _resolve_era(era: str) -> str:
    key = era.strip().lower()
    if key not in _GENRE_TO_ERA:
        raise ValueError(f"Unsupported era/genre {era!r} for CharacterNamer. Supported: {sorted(_GENRE_TO_ERA.keys())}")
    return _GENRE_TO_ERA[key]


class CharacterNamer:
    """Era/channel-aware realistic Chinese character namer with AI blacklist, common-hanzi, and homophone guards."""

    def __init__(self, existing_names: Optional[Iterable[str]] = None) -> None:
        self._cast: List[str] = []
        self._pinyin_index: Dict[Tuple[str, ...], str] = {}
        if existing_names is not None:
            for name in existing_names:
                self.register_name(name)

    @property
    def registered_names(self) -> List[str]:
        return list(self._cast)

    @staticmethod
    def is_blacklisted(name: str) -> bool:
        return is_blacklisted_name(name)

    @staticmethod
    def has_homophone_collision(name: str, existing_names: Iterable[str]) -> bool:
        target_py = to_toneless_pinyin(name)
        for existing in existing_names:
            if to_toneless_pinyin(existing) == target_py:
                return True
        return False

    def validate_name(
        self,
        name: str,
        existing_names: Optional[Iterable[str]] = None,
    ) -> Tuple[str, ...]:
        """Validate name against length, common simplified hanzi, AI blacklist, and cast homophone collision."""
        if not isinstance(name, str) or not (2 <= len(name) <= 4):
            raise NamerError(f"人名长度必须为 2~4 个汉字，收到: {name!r}")

        target_py = to_toneless_pinyin(name)

        if is_blacklisted_name(name):
            raise BlacklistedNameError(
                f"人名 {name!r} 命中番茄 AI 高频重灾区黑名单，严禁使用"
            )

        pool_to_check: List[str] = list(self._cast)
        if existing_names is not None:
            pool_to_check.extend(existing_names)

        for existing in pool_to_check:
            existing_py = to_toneless_pinyin(existing)
            if existing_py == target_py:
                raise HomophoneCollisionError(
                    f"人名 {name!r} ({' '.join(target_py)}) 与已有角色 {existing!r} 无声调拼音撞音，易导致听书混淆"
                )
        return target_py

    def register_name(self, name: str) -> None:
        """Validate and register an existing character name into this namer's cast state."""
        py = self.validate_name(name)
        self._cast.append(name)
        self._pinyin_index[py] = name

    def generate_name(
        self,
        era: str = "modern",
        channel: str = "general",
        gender: str = "neutral",
        existing_names: Optional[Iterable[str]] = None,
        seed: Optional[int] = None,
        register: bool = False,
    ) -> str:
        """Generate a realistic, non-AI-cliché, common-hanzi name with zero homophone collision."""
        resolved_era = _resolve_era(era)
        if channel not in {"male", "female", "general"}:
            raise ValueError(f"Invalid channel {channel!r}")
        if gender not in {"male", "female", "neutral"}:
            raise ValueError(f"Invalid gender {gender!r}")

        effective_gender = gender
        if effective_gender == "neutral":
            effective_gender = "female" if channel == "female" else "male"

        surnames, given_names = _ERA_POOLS[resolved_era][effective_gender]
        rng = random.Random(seed)

        candidates = [f"{s}{g}" for s in surnames for g in given_names]
        rng.shuffle(candidates)

        # Track seen pinyin of existing_names + self._cast for O(1) collision check during search
        occupied_py: Set[Tuple[str, ...]] = set(self._pinyin_index.keys())
        if existing_names is not None:
            for ex in existing_names:
                occupied_py.add(to_toneless_pinyin(ex))

        for cand in candidates:
            if not (2 <= len(cand) <= 4):
                continue
            if is_blacklisted_name(cand):
                continue
            try:
                py = to_toneless_pinyin(cand)
            except RareCharacterError:
                continue
            if py in occupied_py:
                continue
            if register:
                self._cast.append(cand)
                self._pinyin_index[py] = cand
            return cand

        raise NamerError(
            f"无法在 era={era!r}, channel={channel!r}, gender={gender!r} 下找到不撞音的可用人名（已占 {len(occupied_py)} 个音节组）"
        )

    def generate_cast(
        self,
        count: int,
        era: str = "modern",
        channel: str = "general",
        existing_names: Optional[Iterable[str]] = None,
        seed: Optional[int] = None,
    ) -> List[str]:
        """Generate `count` distinct, zero-homophone-collision names for a book cast."""
        if count < 0:
            raise ValueError(f"count must be >= 0, got {count}")
        accumulated: List[str] = list(existing_names) if existing_names is not None else []
        generated: List[str] = []
        base_seed = seed if seed is not None else 42
        for idx in range(count):
            g = "female" if (channel == "female" and idx % 2 == 0) or (channel != "female" and idx % 3 == 1) else "male"
            name = self.generate_name(
                era=era,
                channel=channel,
                gender=g,
                existing_names=accumulated,
                seed=base_seed + idx * 131,
                register=False,
            )
            accumulated.append(name)
            generated.append(name)
        return generated
