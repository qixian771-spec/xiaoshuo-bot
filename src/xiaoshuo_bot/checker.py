from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from .models import CharacterAnchor, ForeshadowItem, ScanReport, SceneOutline, Violation


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

# 2. Body-reaction clichés (身体反应套路)
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

# 3. Structural symmetry patterns (结构化对称)
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
FORMAT_PATTERNS: List[Tuple[str, str, str]] = [
    ("FMT_002", r"(?m)^\s*#{1,6}\s+.*$", "严禁输出 Markdown 标题或小标题"),
    ("FMT_003", r"```", "严禁输出代码围栏"),
    ("FMT_004", r"(?m)^\s*(?:---|\*\*\*|___)\s*$", "严禁输出分割线，使用自然段落过渡"),
    ("FMT_005", r"(?m)^\s*第[一二三四五六七八九十百零\d]+[章节回部]\b.*$", "严禁在正文内输出章节标题"),
    (
        "FMT_006",
        r"(?:创作手记|作者(?:注|按|有话说)|【(?:注|提示|本章完|完)】|\(本章完\)|（本章完）|\[完\]|字数统计[:：])",
        "严禁输出元信息、创作手记或完结标记",
    ),
]

# 5. Omniscient narrator leaks (上帝视角越界词)
OMNISCIENT_PATTERNS: List[Tuple[str, str, str]] = [
    ("POV_002", r"殊不知|却不知|浑然不知的是|不为人知的是|(?:没有|无)人(?:知道|知晓)", "严禁全知上帝视角剧透或评述，严格限制在视点角色当前感知范围内"),
    ("POV_003", r"与此同时[，,]?\s*(?:在|远在|另一边)", "严禁平行剪辑视点角色不在场的另一空间事件"),
    ("POV_004", r"多年以后[^。！？\n]*回想", "严禁全知预叙未来视角"),
]

DIALOGUE_SPAN_RE = re.compile(r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\"")


def mask_dialogue(text: str) -> str:
    """Replace quoted dialogue content with placeholder boxes while preserving exact offsets & newlines."""

    def _repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        if len(raw) <= 2:
            return raw
        inner = "".join("\n" if ch == "\n" else "□" for ch in raw[1:-1])
        return raw[0] + inner + raw[-1]

    return DIALOGUE_SPAN_RE.sub(_repl, text)


def _line_number_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _snippet_around(text: str, start: int, end: int, radius: int = 24) -> str:
    s = max(0, start - radius)
    e = min(len(text), end + radius)
    snippet = text[s:e].replace("\n", "⏎")
    return snippet


class AITicChecker:
    """Deterministic static analyzer enforcing System Constraints, AI-TIC-CHECK, POV, and Foreshadow recovery."""

    def __init__(self, extra_banned_phrases: Optional[List[str]] = None) -> None:
        self.extra_banned_phrases = extra_banned_phrases or []

    def scan(
        self,
        text: str,
        scene: Optional[SceneOutline] = None,
        characters: Optional[Dict[str, CharacterAnchor]] = None,
        due_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
        plant_foreshadows: Optional[Iterable[ForeshadowItem]] = None,
    ) -> ScanReport:
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
            return ScanReport(passed=False, char_count=0, paragraph_count=0, violations=violations)

        # Check 1: First character must be the very first word of prose (no leading whitespace/newlines/symbols)
        first_char = text[0]
        if first_char.isspace() or first_char in "#*`[-—=【[":
            violations.append(
                Violation(
                    rule_id="FMT_001",
                    category="FORMAT_VIOLATION",
                    severity="CRITICAL",
                    matched_text=repr(text[:20]),
                    line_number=1,
                    context_snippet=_snippet_around(text, 0, min(len(text), 20)),
                    fix_hint="第一个字符必须是正文的第一个汉字，严禁前置空行、空格、符号或标题",
                )
            )

        # Check first line is not a bare title (e.g. short unpunctuated header line < 20 chars without sentence punctuation)
        first_line = text.splitlines()[0].strip()
        if len(first_line) < 22 and not re.search(r"[，。！？；：、“”——…]", first_line):
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

        # Format patterns across full text
        self._apply_regex_group(text, FORMAT_PATTERNS, "FORMAT_VIOLATION", violations)
        # Summary clichés across full text
        self._apply_regex_group(text, SUMMARY_PATTERNS, "SUMMARY_CLICHE", violations)
        # Body-reaction clichés across full text
        self._apply_regex_group(text, BODY_TIC_PATTERNS, "BODY_TIC_CLICHE", violations)
        # Structural symmetry across full text
        self._apply_regex_group(text, SYMMETRY_PATTERNS, "STRUCTURAL_SYMMETRY", violations)

        # Extra project-specific banned phrases
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

        # POV checks on narrative prose (quoted dialogue masked out)
        masked = mask_dialogue(text)
        self._apply_regex_group(masked, OMNISCIENT_PATTERNS, "POV_VIOLATION", violations, source_text=text)

        # First-person narrator leak outside dialogue
        for m in re.finditer(r"(?<![自忘故敌你])我", masked):
            violations.append(
                Violation(
                    rule_id="POV_001",
                    category="POV_VIOLATION",
                    severity="CRITICAL",
                    matched_text=text[m.start() : m.end()],
                    line_number=_line_number_at(text, m.start()),
                    context_snippet=_snippet_around(text, m.start(), m.end()),
                    fix_hint="叙述语段出现第一人称‘我’（非引号内台词），必须严格保持第三人称限知视角",
                )
            )

        # Non-POV character direct inner-thought violation
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
                mind_hop_re = re.compile(rf"(?:{names_alt})[^。！？；\n“”「」]{{0,8}}{inner_verbs}")
                for m in mind_hop_re.finditer(masked):
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

        # Length & Foreshadow physical checks
        non_ws_chars = len(re.sub(r"\s+", "", text))
        paragraphs = [p for p in text.splitlines() if p.strip()]

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
                if fs.payoff_keywords and not any(kw in text for kw in fs.payoff_keywords):
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
                if fs.plant_keywords and not any(kw in text for kw in fs.plant_keywords):
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

        passed = len([v for v in violations if v.severity == "CRITICAL"]) == 0
        return ScanReport(
            passed=passed,
            char_count=non_ws_chars,
            paragraph_count=len(paragraphs),
            violations=violations,
        )

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
