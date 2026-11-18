from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
import yaml

from .loader import load_project
from .models import NovelProject, ProjectValidationError


class PlagiarismGuardError(ValueError):
    """Base error for anti-rewrite plagiarism and cross-book collision checks."""


class PlagiarismViolationError(PlagiarismGuardError):
    """Raised when generated text violates benchmark LCS or 4-gram Jaccard gates."""

    def __init__(
        self,
        message: str,
        *,
        max_lcs_hanzi: int = 0,
        matched_lcs_text: str = "",
        max_4gram_jaccard: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.max_lcs_hanzi = max_lcs_hanzi
        self.matched_lcs_text = matched_lcs_text
        self.max_4gram_jaccard = max_4gram_jaccard


class CrossBookCollisionError(PlagiarismGuardError):
    """Raised when a new book collides with an existing local matrix project."""

    def __init__(
        self,
        message: str,
        *,
        colliding_project_id: str = "",
        collision_type: str = "",
        similarity: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.colliding_project_id = colliding_project_id
        self.collision_type = collision_type
        self.similarity = similarity


_HANZI_RE = re.compile(r"[\u4e00-\u9fff]")

# Common 4-character Chinese fixed idioms stripped before computing verbatim Hanzi overlap.
COMMON_FOUR_CHAR_IDIOMS: frozenset[str] = frozenset(
    {
        "不可思议", "莫名其妙", "不动声色", "一模一样", "目瞪口呆", "鸦雀无声",
        "面面相觑", "咬牙切齿", "理所当然", "雪上加霜", "画蛇添足", "迫不及待",
        "一举两得", "欣喜若狂", "恍然大悟", "不言而喻", "异口同声", "众目睽睽",
        "不知所措", "大惊失色", "毫无疑问", "天衣无缝", "水落石出", "顺理成章",
        "拭目以待", "当机立断", "心照不宣", "不以为然", "半信半疑", "小心翼翼",
        "大摇大摆", "鬼鬼祟祟", "堂而皇之", "理直气壮", "居高临下", "嗤之以鼻",
        "不屑一顾", "脱口而出", "如释重负", "魂飞魄散", "胆战心惊", "千真万确",
        "一清二楚", "无济于事", "无能为力", "束手无策", "雪中送炭", "落井下石",
        "顺水推舟", "借花献佛", "偷梁换柱", "暗度陈仓", "兵不厌诈", "破釜沉舟",
        "背水一战", "卷土重来", "东山再起", "功亏一篑", "前功尽弃", "半途而废",
        "持之以恒", "坚持不懈", "精益求精", "全力以赴", "尽心尽力", "义不容辞",
        "责无旁贷", "当仁不让", "自相矛盾", "漏洞百出", "天翻地覆", "翻天覆地",
        "惊天动地", "震耳欲聋", "声嘶力竭", "痛心疾首", "咬紧牙关", "忍气吞声",
        "忍无可忍", "暴跳如雷", "怒不可遏", "勃然大怒", "气急败坏", "恼羞成怒",
        "哑口无言", "无话可说", "无可奈何", "无可奉告", "一针见血", "切中要害",
        "一语道破", "豁然开朗", "茅塞顿开", "匪夷所思", "前所未有", "闻所未闻",
        "叹为观止", "拍案叫绝", "喜出望外", "大喜过望", "喜笑颜开", "眉开眼笑",
        "心花怒放", "悲痛欲绝", "痛不欲生", "泣不成声", "泪流满面", "声泪俱下",
        "心如刀割", "肝肠寸断", "魂不守舍", "心神不宁", "坐立不安", "焦急万分",
        "心急如焚", "迫在眉睫", "千钧一发", "生死攸关", "危在旦夕", "间不容发",
        "刻不容缓", "争分夺秒", "分秒必争", "风驰电掣", "快马加鞭", "日新月异",
        "突飞猛进", "蒸蒸日上", "如日中天", "方兴未艾", "势不可挡", "锐不可当",
        "所向披靡", "战无不胜", "攻无不克", "百战百胜", "旗开得胜", "马到成功",
        "一蹴而就", "水到渠成", "瓜熟蒂落", "名副其实", "实至名归", "众望所归",
        "人心所向", "大势所趋", "顺应潮流", "与时俱进", "独树一帜", "别出心裁",
        "标新立异", "独具匠心", "巧夺天工", "鬼斧神工", "美轮美奂", "富丽堂皇",
        "金碧辉煌", "灯火辉煌", "车水马龙", "人山人海", "水泄不通", "摩肩接踵",
        "络绎不绝", "门庭若市", "门可罗雀", "冷冷清清", "荒无人烟", "寸草不生",
        "满目疮痍", "断壁残垣", "狼藉不堪", "一塌糊涂", "乱七八糟", "乌烟瘴气",
        "鸡飞狗跳", "鸡犬不宁", "天怒人怨", "怨声载道", "民不聊生", "居安思危",
        "未雨绸缪", "防微杜渐", "防患未然", "有备无患", "万无一失", "滴水不漏",
        "无懈可击", "稳操胜券", "胸有成竹", "成竹在胸", "胜券在握", "运筹帷幄",
        "决胜千里", "足智多谋", "神机妙算", "深谋远虑", "高瞻远瞩", "老谋深算",
        "老奸巨猾", "阴险狡诈", "笑里藏刀", "口蜜腹剑", "两面三刀", "阳奉阴违",
        "欺上瞒下", "弄虚作假", "以假乱真", "鱼目混珠", "滥竽充数", "浑水摸鱼",
        "趁火打劫", "趁人之危", "过河拆桥", "忘恩负义", "恩将仇报", "背信弃义",
        "见利忘义", "唯利是图", "贪得无厌", "贪婪成性", "得寸进尺", "变本加厉",
        "肆无忌惮", "横行霸道", "欺行霸市", "仗势欺人", "狗仗人势", "狐假虎威",
        "为虎作伥", "助纣为虐", "同流合污", "狼狈为奸", "沆瀣一气", "一丘之貉",
        "臭味相投", "物以类聚", "人以群分", "情投意合", "志同道合", "相见恨晚",
        "一见如故", "莫逆之交", "生死之交", "患难与共", "同甘共苦", "风雨同舟",
        "肝胆相照", "竭诚相待", "推心置腹", "坦诚相待", "开诚布公", "直截了当",
        "开门见山", "单刀直入", "直奔主题", "言归正传", "长话短说", "简而言之",
        "总而言之", "综上所述", "归根结底", "毋庸置疑", "显而易见", "有目共睹",
        "众所周知", "家喻户晓", "妇孺皆知", "名扬四海", "声名远扬", "举世闻名",
        "名垂青史", "流芳百世", "遗臭万年", "臭名昭著", "声名狼藉", "身败名裂",
        "一败涂地", "溃不成军", "抱头鼠窜", "落荒而逃", "仓皇逃窜", "灰头土脸",
        "垂头丧气", "无精打采", "萎靡不振", "一蹶不振", "自暴自弃", "破罐破摔",
        "孤注一掷", "铤而走险", "狗急跳墙", "困兽犹斗", "垂死挣扎", "负隅顽抗",
        "束手就擒", "甘拜下风", "自愧不如", "望尘莫及", "自惭形秽", "无地自容",
        "羞愧难当", "追悔莫及", "悔不当初", "痛改前非", "洗心革面", "改过自新",
        "重新做人", "脱胎换骨", "焕然一新", "面目全非", "判若两人", "截然不同",
        "大相径庭", "天壤之别", "云泥之别", "天差地别", "迥然不同", "毫无关系",
        "风马牛不相及", "千丝万缕", "息息相关", "密不可分", "休戚与共", "唇亡齿寒",
        "一荣俱荣", "一损俱损", "相辅相成", "相得益彰", "锦上添花", "火上浇油",
        "煽风点火", "推波助澜", "挑拨离间", "搬弄是非", "造谣生事", "无中生有",
        "空穴来风", "捕风捉影", "胡说八道", "胡言乱语", "一派胡言", "荒谬绝伦",
        "荒诞不经", "可笑至极", "哭笑不得", "啼笑皆非", "左右为难", "进退两难",
        "进退维谷", "骑虎难下", "走投无路", "山穷水尽", "绝处逢生", "柳暗花明",
        "峰回路转", "否极泰来", "因祸得福", "塞翁失马", "得不偿失", "因小失大",
        "舍本逐末", "本末倒置", "喧宾夺主", "反客为主", "弄巧成拙", "适得其反",
        "南辕北辙", "缘木求鱼", "刻舟求剑", "守株待兔", "掩耳盗铃", "自欺欺人",
        "自以为是", "夜郎自大", "井底之蛙", "目中无人", "目空一切", "不可一世",
        "趾高气扬", "得意忘形", "忘乎所以", "嚣张跋扈", "盛气凌人", "咄咄逼人",
        "步步紧逼", "得理不饶人", "斤斤计较", "耿耿于怀", "念念不忘", "铭记在心",
        "刻骨铭心", "终身难忘", "历历在目", "记忆犹新", "栩栩如生", "惟妙惟肖",
        "活灵活现", "绘声绘色", "有声有色", "头头是道", "井井有条", "有条不紊",
        "循序渐进", "按部就班", "脚踏实地", "兢兢业业", "勤勤恳恳", "任劳任怨",
        "默默无闻", "无私奉献", "大公无私", "光明磊落", "堂堂正正", "问心无愧",
        "泰然自若", "镇定自若", "从容不迫", "不慌不忙", "临危不乱", "处变不惊",
        "面不改色", "心平气和", "不动如山", "稳如泰山", "纹丝不动", "一动不动",
        "屏息凝神", "聚精会神", "全神贯注", "专心致志", "目不转睛", "目不斜视",
        "视而不见", "听而不闻", "充耳不闻", "置若罔闻", "置之不理", "袖手旁观",
        "冷眼旁观", "隔岸观火", "坐山观虎斗", "渔翁得利", "坐收渔利", "不劳而获",
        "投机取巧", "捷足先登", "先发制人", "后发制人", "出其不意", "攻其不备",
        "声东击西", "调虎离山", "欲擒故纵", "以退为进", "以逸待劳", "釜底抽薪",
        "斩草除根", "永绝后患", "后患无穷", "风平浪静", "波澜不惊", "波涛汹涌",
        "暗潮涌动", "剑拔弩张", "箭在弦上", "蓄势待发", "一触即发", "水火不容",
        "势不两立", "不共戴天", "冤家路窄", "狭路相逢", "死灰复燃", "重蹈覆辙",
        "前车之鉴", "吃一堑长一智", "旁观者清", "当局者迷", "水滴石穿", "铁杵磨针",
    }
)


@dataclass(frozen=True)
class PlagiarismCheckResult:
    passed: bool
    max_lcs_hanzi: int
    matched_lcs_text: str
    max_4gram_jaccard: float
    checked_against_count: int
    details: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CollisionCheckResult:
    passed: bool
    checked_projects: int
    collisions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _extract_hanzi_ngrams(hanzi: str, n: int) -> Set[str]:
    if len(hanzi) < n or n <= 0:
        return set()
    return {hanzi[i : i + n] for i in range(len(hanzi) - n + 1)}


def _jaccard_sets(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a) + len(b) - inter
    return inter / union if union > 0 else 0.0


def _build_windows(text: str, window_size: int, step: int) -> List[str]:
    if not text:
        return []
    if len(text) <= window_size:
        return [text]
    windows: List[str] = []
    pos = 0
    while pos + window_size <= len(text):
        windows.append(text[pos : pos + window_size])
        pos += step
    if (len(text) - window_size) % step != 0:
        windows.append(text[-window_size:])
    return windows


class PlagiarismAndCollisionGuard:
    """Dual hard gate: benchmark anti-rewrite plagiarism + local matrix cross-book collision."""

    def __init__(
        self,
        *,
        max_contiguous_hanzi: int = 7,
        max_window_4gram_jaccard: float = 0.05,
        window_size: int = 200,
        window_step: int = 20,
        extra_idioms: Iterable[str] = (),
        title_similarity_threshold: float = 0.70,
        golden_finger_similarity_threshold: float = 0.65,
        opening_similarity_threshold: float = 0.45,
    ) -> None:
        if max_contiguous_hanzi < 1:
            raise ValueError(f"max_contiguous_hanzi must be >= 1, got {max_contiguous_hanzi}")
        if not (0.0 < max_window_4gram_jaccard <= 1.0):
            raise ValueError(
                f"max_window_4gram_jaccard must be in (0, 1], got {max_window_4gram_jaccard}"
            )
        if window_size < 4 or window_step < 1:
            raise ValueError("window_size must be >= 4 and window_step >= 1")

        self.max_contiguous_hanzi = max_contiguous_hanzi
        self.max_window_4gram_jaccard = max_window_4gram_jaccard
        self.window_size = window_size
        self.window_step = window_step
        self.title_similarity_threshold = title_similarity_threshold
        self.golden_finger_similarity_threshold = golden_finger_similarity_threshold
        self.opening_similarity_threshold = opening_similarity_threshold

        idioms = sorted(
            {
                w.strip()
                for w in (*COMMON_FOUR_CHAR_IDIOMS, *extra_idioms)
                if isinstance(w, str) and len(w.strip()) == 4
            },
            reverse=True,
        )
        self._idioms = frozenset(idioms)
        if idioms:
            self._idiom_re: Optional[re.Pattern[str]] = re.compile(
                "|".join(re.escape(w) for w in idioms)
            )
        else:
            self._idiom_re = None

    def normalize_for_plagiarism(self, text: str) -> str:
        """Strip common 4-char fixed idioms, punctuation, and whitespace; keep contiguous Hanzi."""
        if not isinstance(text, str):
            raise TypeError(f"Expected str, got {type(text).__name__}")
        cleaned = self._idiom_re.sub("", text) if self._idiom_re is not None else text
        return "".join(_HANZI_RE.findall(cleaned))

    @staticmethod
    def _has_common_kgram(short_s: str, long_s: str, k: int) -> Optional[str]:
        if k <= 0 or len(short_s) < k or len(long_s) < k:
            return None
        seen = {short_s[i : i + k] for i in range(len(short_s) - k + 1)}
        for j in range(len(long_s) - k + 1):
            sub = long_s[j : j + k]
            if sub in seen:
                return sub
        return None

    def longest_common_contiguous_hanzi(self, text_a: str, text_b: str) -> Tuple[int, str]:
        """Compute exact longest common contiguous Hanzi substring length & sample after normalization."""
        norm_a = self.normalize_for_plagiarism(text_a)
        norm_b = self.normalize_for_plagiarism(text_b)
        if not norm_a or not norm_b:
            return 0, ""

        short_s, long_s = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
        max_possible = len(short_s)

        # Fast-path check at threshold + 1 (default 8)
        threshold = self.max_contiguous_hanzi + 1
        match_at_thresh = self._has_common_kgram(short_s, long_s, threshold)
        if match_at_thresh is None:
            for k in range(min(self.max_contiguous_hanzi, max_possible), 0, -1):
                hit = self._has_common_kgram(short_s, long_s, k)
                if hit is not None:
                    return k, hit
            return 0, ""

        # Binary search [threshold, max_possible] for exact longest contiguous match
        lo = threshold
        hi = max_possible
        best_len = threshold
        best_sub = match_at_thresh
        while lo <= hi:
            mid = (lo + hi) // 2
            hit = self._has_common_kgram(short_s, long_s, mid)
            if hit is not None:
                best_len = mid
                best_sub = hit
                lo = mid + 1
            else:
                hi = mid - 1
        return best_len, best_sub

    def max_sliding_4gram_jaccard(self, text_a: str, text_b: str) -> float:
        """Compute maximum 4-gram Jaccard similarity across 200-char sliding Hanzi windows."""
        norm_a = self.normalize_for_plagiarism(text_a)
        norm_b = self.normalize_for_plagiarism(text_b)
        if len(norm_a) < 4 or len(norm_b) < 4:
            return 0.0

        windows_a = _build_windows(norm_a, self.window_size, self.window_step)
        windows_b = _build_windows(norm_b, self.window_size, self.window_step)

        b_sets: List[Set[str]] = [_extract_hanzi_ngrams(w, 4) for w in windows_b]
        inverted: Dict[str, List[int]] = {}
        for idx, gram_set in enumerate(b_sets):
            for gram in gram_set:
                inverted.setdefault(gram, []).append(idx)

        max_sim = 0.0
        for wa in windows_a:
            set_a = _extract_hanzi_ngrams(wa, 4)
            if not set_a:
                continue
            overlap_counts: Dict[int, int] = {}
            for gram in set_a:
                for b_idx in inverted.get(gram, ()):
                    overlap_counts[b_idx] = overlap_counts.get(b_idx, 0) + 1
            for b_idx, inter in overlap_counts.items():
                set_b = b_sets[b_idx]
                union = len(set_a) + len(set_b) - inter
                if union > 0:
                    sim = inter / union
                    if sim > max_sim:
                        max_sim = sim
        return max_sim

    def check_against_benchmark(
        self,
        candidate_text: str,
        benchmark_chapters: Sequence[str],
        *,
        raise_on_violation: bool = True,
    ) -> PlagiarismCheckResult:
        """Enforce <= 7 contiguous Hanzi LCS and < 5% 200-char 4-gram Jaccard vs benchmark chapters."""
        if not isinstance(candidate_text, str) or not candidate_text.strip():
            raise PlagiarismGuardError("candidate_text must be a non-empty string")
        if not benchmark_chapters:
            raise PlagiarismGuardError("benchmark_chapters must contain at least one chapter")

        overall_lcs = 0
        overall_lcs_text = ""
        overall_jaccard = 0.0
        details: List[Dict[str, Any]] = []

        for idx, ref_text in enumerate(benchmark_chapters, start=1):
            if not isinstance(ref_text, str) or not ref_text.strip():
                continue
            lcs_len, lcs_sample = self.longest_common_contiguous_hanzi(candidate_text, ref_text)
            jacc = self.max_sliding_4gram_jaccard(candidate_text, ref_text)
            details.append(
                {
                    "benchmark_index": idx,
                    "lcs_hanzi": lcs_len,
                    "lcs_sample": lcs_sample,
                    "max_4gram_jaccard": round(jacc, 6),
                }
            )
            if lcs_len > overall_lcs:
                overall_lcs = lcs_len
                overall_lcs_text = lcs_sample
            if jacc > overall_jaccard:
                overall_jaccard = jacc

        if not details:
            raise PlagiarismGuardError(
                "benchmark_chapters must contain at least one non-empty chapter"
            )

        lcs_ok = overall_lcs <= self.max_contiguous_hanzi
        jaccard_ok = overall_jaccard < self.max_window_4gram_jaccard
        passed = lcs_ok and jaccard_ok

        result = PlagiarismCheckResult(
            passed=passed,
            max_lcs_hanzi=overall_lcs,
            matched_lcs_text=overall_lcs_text,
            max_4gram_jaccard=round(overall_jaccard, 6),
            checked_against_count=len(details),
            details=details,
        )
        if not passed and raise_on_violation:
            reasons: List[str] = []
            if not lcs_ok:
                reasons.append(
                    f"contiguous Hanzi LCS={overall_lcs} > {self.max_contiguous_hanzi} "
                    f"(matched: {overall_lcs_text!r})"
                )
            if not jaccard_ok:
                reasons.append(
                    f"200-char 4-gram Jaccard={overall_jaccard:.4f} >= {self.max_window_4gram_jaccard:.4f}"
                )
            raise PlagiarismViolationError(
                f"Benchmark anti-rewrite gate failed: {'; '.join(reasons)}",
                max_lcs_hanzi=overall_lcs,
                matched_lcs_text=overall_lcs_text,
                max_4gram_jaccard=overall_jaccard,
            )
        return result

    @staticmethod
    def _title_similarity(title_a: str, title_b: str) -> float:
        a = re.sub(r"[\s《》「」『』【】\-_:：,，.。!！?？]+", "", title_a.strip())
        b = re.sub(r"[\s《》「」『』【】\-_:：,，.。!！?？]+", "", title_b.strip())
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        seq_ratio = SequenceMatcher(None, a, b).ratio()
        bigrams_a = _extract_hanzi_ngrams(a, 2)
        bigrams_b = _extract_hanzi_ngrams(b, 2)
        gram_sim = _jaccard_sets(bigrams_a, bigrams_b)
        return max(seq_ratio, gram_sim)

    @staticmethod
    def _text_overlap_similarity(text_a: str, text_b: str) -> float:
        hanzi_a = "".join(_HANZI_RE.findall(text_a))
        hanzi_b = "".join(_HANZI_RE.findall(text_b))
        if len(hanzi_a) < 4 or len(hanzi_b) < 4:
            return 0.0
        bi_a = _extract_hanzi_ngrams(hanzi_a, 2)
        bi_b = _extract_hanzi_ngrams(hanzi_b, 2)
        tri_a = _extract_hanzi_ngrams(hanzi_a, 3)
        tri_b = _extract_hanzi_ngrams(hanzi_b, 3)

        bi_min = min(len(bi_a), len(bi_b))
        tri_min = min(len(tri_a), len(tri_b))
        bi_overlap = (len(bi_a & bi_b) / bi_min) if bi_min >= 4 else 0.0
        tri_overlap = (len(tri_a & tri_b) / tri_min) if tri_min >= 4 else 0.0
        return max(tri_overlap, bi_overlap * 0.85)

    @staticmethod
    def _extract_existing_project_fingerprint(
        proj_dir: Path, project: NovelProject
    ) -> Dict[str, Any]:
        meta_yaml_path = proj_dir / "project.yaml"
        raw_meta: Dict[str, Any] = {}
        if meta_yaml_path.is_file():
            with meta_yaml_path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
                if isinstance(loaded, dict):
                    raw_meta = loaded

        opening_parts: List[str] = [project.meta.synopsis]
        for scene in sorted(project.scenes, key=lambda s: s.chapter)[:3]:
            opening_parts.append(scene.public_title)
            opening_parts.append(scene.internal_title)
            opening_parts.append(scene.location_time)
            opening_parts.extend(scene.events)
            opening_parts.append(scene.stop_condition)

        lead_char = project.characters.get(project.meta.default_pov_character)
        if lead_char is not None:
            opening_parts.append(lead_char.role)
            opening_parts.append(lead_char.personality)

        gf_parts: List[str] = []
        for key in ("golden_finger_type", "golden_finger", "golden_finger_description", "opening_motif"):
            val = raw_meta.get(key)
            if isinstance(val, str) and val.strip():
                gf_parts.append(val.strip())
        for asset in project.assets.values():
            gf_parts.append(f"{asset.name} {asset.physical_details}")

        return {
            "project_id": project.meta.project_id,
            "genre_id": project.meta.genre_id,
            "titles": [project.meta.title, *project.meta.alternate_titles],
            "golden_finger_type": str(raw_meta.get("golden_finger_type", "")).strip(),
            "golden_finger_text": "\n".join(gf_parts),
            "opening_text": "\n".join(p for p in opening_parts if p),
        }

    def check_cross_book_collision(
        self,
        *,
        title: str,
        projects_dir: str | Path,
        genre_id: str = "",
        golden_finger: str = "",
        opening_summary: str = "",
        opening_chapters: Sequence[str] = (),
        protagonist_persona: str = "",
        alternate_titles: Sequence[str] = (),
        exclude_project_id: Optional[str] = None,
        raise_on_collision: bool = True,
    ) -> CollisionCheckResult:
        """Scan existing projects under projects_dir for title, golden-finger, and opening collisions."""
        root = Path(projects_dir)
        if not root.exists():
            return CollisionCheckResult(passed=True, checked_projects=0, collisions=[])
        if not root.is_dir():
            raise PlagiarismGuardError(f"projects_dir is not a directory: {root}")

        candidate_titles = [t for t in [title, *alternate_titles] if isinstance(t, str) and t.strip()]
        candidate_opening = "\n".join(
            p for p in [opening_summary, protagonist_persona, *opening_chapters] if isinstance(p, str) and p.strip()
        )
        candidate_gf = golden_finger.strip()

        checked_count = 0
        collisions: List[Dict[str, Any]] = []

        for subdir in sorted(root.iterdir()):
            if not subdir.is_dir() or subdir.name.startswith(("_", ".")):
                continue
            if not (subdir / "project.yaml").is_file():
                continue
            existing = load_project(subdir)

            if exclude_project_id and existing.meta.project_id == exclude_project_id:
                continue
            checked_count += 1
            fp = self._extract_existing_project_fingerprint(subdir, existing)

            # 1. Title duplicate / similarity check
            for cand_t in candidate_titles:
                for exist_t in fp["titles"]:
                    if not exist_t:
                        continue
                    sim = self._title_similarity(cand_t, exist_t)
                    if sim >= self.title_similarity_threshold:
                        collisions.append(
                            {
                                "project_id": fp["project_id"],
                                "type": "title_collision",
                                "candidate": cand_t,
                                "existing": exist_t,
                                "similarity": round(sim, 4),
                            }
                        )

            # 2. Golden-finger mechanism collision check
            if candidate_gf:
                same_gf_type = (
                    bool(fp["golden_finger_type"])
                    and candidate_gf == fp["golden_finger_type"]
                    and (not genre_id or not fp["genre_id"] or genre_id == fp["genre_id"])
                )
                gf_sim = (
                    1.0
                    if same_gf_type
                    else self._text_overlap_similarity(candidate_gf, fp["golden_finger_text"])
                )
                if gf_sim >= self.golden_finger_similarity_threshold:
                    collisions.append(
                        {
                            "project_id": fp["project_id"],
                            "type": "golden_finger_collision",
                            "candidate": candidate_gf,
                            "existing": fp["golden_finger_type"] or fp["project_id"],
                            "similarity": round(gf_sim, 4),
                        }
                    )

            # 3. Opening 3-chapter core conflict / persona collision check
            if candidate_opening and fp["opening_text"]:
                open_sim = self._text_overlap_similarity(candidate_opening, fp["opening_text"])
                if open_sim >= self.opening_similarity_threshold:
                    collisions.append(
                        {
                            "project_id": fp["project_id"],
                            "type": "opening_conflict_persona_collision",
                            "similarity": round(open_sim, 4),
                        }
                    )

        passed = len(collisions) == 0
        result = CollisionCheckResult(
            passed=passed,
            checked_projects=checked_count,
            collisions=collisions,
        )
        if not passed and raise_on_collision:
            top = max(collisions, key=lambda c: c["similarity"])
            raise CrossBookCollisionError(
                f"Cross-book collision with '{top['project_id']}' "
                f"({top['type']}, similarity={top['similarity']:.2f}); switch motif/title/golden-finger.",
                colliding_project_id=str(top["project_id"]),
                collision_type=str(top["type"]),
                similarity=float(top["similarity"]),
            )
        return result


__all__ = [
    "COMMON_FOUR_CHAR_IDIOMS",
    "PlagiarismGuardError",
    "PlagiarismViolationError",
    "CrossBookCollisionError",
    "PlagiarismCheckResult",
    "CollisionCheckResult",
    "PlagiarismAndCollisionGuard",
]
