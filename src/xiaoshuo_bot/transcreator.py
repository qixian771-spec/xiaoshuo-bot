from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pypinyin import Style, lazy_pinyin

from .llm.base import LLMError, LLMProvider, LLMRequest
from .llm.router import RoleModelRouter
from .models import ChapterArtifact, CharacterAnchor, NovelProject, ProjectMeta, format_chapter_filename

CULTURAL_TRACKS: Tuple[str, ...] = ("eastern_xuanhuan", "modern_urban")

_COMPOUND_SURNAMES: Tuple[str, ...] = (
    "欧阳",
    "司马",
    "上官",
    "诸葛",
    "夏侯",
    "东方",
    "皇甫",
    "尉迟",
    "公孙",
    "慕容",
    "宇文",
    "长孙",
    "司徒",
    "司空",
    "独孤",
    "令狐",
)

EASTERN_XUANHUAN_TERMS: Dict[str, str] = {
    "炼气期": "Qi Condensation",
    "炼气": "Qi Condensation",
    "筑基期": "Foundation Establishment",
    "筑基": "Foundation Establishment",
    "金丹期": "Golden Core",
    "金丹": "Golden Core",
    "元婴期": "Nascent Soul",
    "元婴": "Nascent Soul",
    "化神期": "Soul Formation",
    "化神": "Soul Formation",
    "下品灵石": "Low-Grade Spirit Stones",
    "中品灵石": "Mid-Grade Spirit Stones",
    "上品灵石": "High-Grade Spirit Stones",
    "极品灵石": "Supreme Spirit Stones",
    "灵石": "Spirit Stones",
    "筑基丹": "Foundation Establishment Pill",
    "外门": "Outer Sect",
    "内门": "Inner Sect",
    "真传弟子": "Core Disciple",
    "杂役": "Menial Disciple",
    "宗门": "Sect",
    "护山大阵": "Sect-Protecting Array",
    "灵脉": "Spirit Vein",
    "剑意": "Sword Intent",
    "丹田": "Dantian",
    "飞升": "Ascension",
}

MODERN_URBAN_TERMS: Dict[str, str] = {
    "督察署": "Inspectorate Bureau",
    "巡捕房": "Municipal Precinct",
    "市局巡捕": "Metro Detective Division",
    "审判庭": "High Court",
    "风投": "venture capital",
    "写字楼": "office tower",
    "工位": "workstation",
    "公关": "public relations",
}

_WESTERN_NAME_POOL: Tuple[str, ...] = (
    "Ethan Brooks",
    "Claire Vance",
    "Marcus Sterling",
    "Lucas Reed",
    "Evelyn Hart",
    "Julian Mercer",
    "Nora Bennett",
    "Victor Hayes",
    "Sophia Lane",
    "Adrian Cole",
    "Hannah Cross",
    "Owen Pierce",
    "Rachel Quinn",
    "Daniel Foster",
    "Maya Linwood",
    "Grant Sullivan",
)

_MEASUREMENT_FACTORS: Dict[str, Tuple[float, str]] = {
    "li": (0.31, "miles"),
    "zhang": (10.0, "feet"),
    "chi": (1.1, "feet"),
    "cun": (1.3, "inches"),
    "jin": (1.1, "pounds"),
    "liang": (1.76, "ounces"),
    "mu": (0.16, "acres"),
    "qing": (16.5, "acres"),
}

_NUMBER_WORDS: Dict[str, float] = {
    "half": 0.5,
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "dozen": 12.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "fifty": 50.0,
    "hundred": 100.0,
    "thousand": 1000.0,
}

_BARE_PINYIN_UNIT_RE = re.compile(
    r"\b(?P<num>\d+(?:\.\d+)?|half|an?|one|two|three|four|five|six|seven|eight|nine|ten|dozen|twenty|thirty|fifty|hundred|thousand)\s+"
    r"(?P<unit>li|zhang|cun|chi|liang|jin|mu|qing)\b",
    re.IGNORECASE,
)

_CHINGLISH_TITLE_RE = re.compile(
    r"\b(?P<title>Director|Vice\s+Director|Deputy\s+Director|Section\s+Chief|Bureau\s+Chief|"
    r"Department\s+Head|Teacher|Boss|Senior\s+Brother|Junior\s+Brother|Senior\s+Sister|Junior\s+Sister)\s+"
    r"(?P<surname>[A-Z][a-z]{1,18})\b"
)

_HYPHENATED_PINYIN_NAME_RE = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+-[a-z]+\b|\b[A-Z][a-z]+-[A-Z][a-z]+\b")
_HANZI_RE = re.compile(r"[\u4e00-\u9fff]")


class TranscreationError(RuntimeError):
    """Base exception for English Webnovel cultural transcreation failures."""


class GlossaryValidationError(TranscreationError, ValueError):
    """Raised when transcreated text or glossary violates consistency or localization rules."""


class TranscreationProviderError(TranscreationError):
    """Raised when no LLM router/provider is configured or upstream inference fails."""


def infer_cultural_track(genre: str, genre_id: str = "") -> str:
    combined = f"{genre} {genre_id}".lower()
    if any(k in combined for k in ("玄幻", "修仙", "仙侠", "古言", "历史", "xuanhuan", "ancient", "history")):
        return "eastern_xuanhuan"
    return "modern_urban"


def to_unhyphenated_pinyin_name(zh_name: str) -> str:
    """Convert a Chinese personal name into un-hyphenated Title-Case Pinyin (e.g. 陈牧野 -> Chen Muye)."""
    cleaned = zh_name.strip()
    if not cleaned:
        raise GlossaryValidationError("Character name cannot be empty.")
    if not _HANZI_RE.search(cleaned):
        return cleaned

    surname_len = 2 if any(cleaned.startswith(s) for s in _COMPOUND_SURNAMES) and len(cleaned) >= 3 else 1
    if len(cleaned) == 1:
        py = lazy_pinyin(cleaned, style=Style.NORMAL)
        return "".join(py).capitalize()

    surname_py = "".join(lazy_pinyin(cleaned[:surname_len], style=Style.NORMAL)).capitalize()
    given_py = "".join(lazy_pinyin(cleaned[surname_len:], style=Style.NORMAL)).capitalize()
    if not given_py:
        return surname_py
    return f"{surname_py} {given_py}"


def convert_cny_to_usd_purchasing_power(amount_cny: float) -> str:
    """Convert RMB/CNY amount to clean order-of-magnitude USD purchasing power string."""
    if amount_cny <= 0:
        return "$0"
    # Purchasing-power-adjusted clean conversion (~1 USD ≈ 6.7 RMB, rounded to crisp narrative tiers)
    raw_usd = amount_cny / 6.6667
    if raw_usd >= 1_000_000_000:
        val = round(raw_usd / 1_000_000_000, 1)
        return f"${val:g} billion"
    if raw_usd >= 1_000_000:
        val = round(raw_usd / 1_000_000, 1)
        return f"${val:g} million"
    if raw_usd >= 100_000:
        rounded = int(round(raw_usd / 50_000.0) * 50_000)
        return f"${rounded:,}"
    if raw_usd >= 10_000:
        rounded = int(round(raw_usd / 5_000.0) * 5_000)
        return f"${rounded:,}"
    if raw_usd >= 1_000:
        rounded = int(round(raw_usd / 500.0) * 500)
        return f"${rounded:,}"
    if raw_usd >= 100:
        rounded = int(round(raw_usd / 50.0) * 50)
        return f"${rounded:,}"
    rounded = max(5, int(round(raw_usd / 5.0) * 5))
    return f"${rounded}"


@dataclass
class TranscreationGlossary:
    """Book-locked English cultural transcreation glossary."""

    book_id: str
    title_zh: str
    title_en: str
    track: str  # "eastern_xuanhuan" | "modern_urban"
    character_names: Dict[str, str] = field(default_factory=dict)
    realm_terms: Dict[str, str] = field(default_factory=dict)
    measurement_rules: Dict[str, str] = field(default_factory=dict)
    chinglish_replacements: Dict[str, str] = field(default_factory=dict)
    custom_terms: Dict[str, str] = field(default_factory=dict)
    locked: bool = True

    def __post_init__(self) -> None:
        if self.track not in CULTURAL_TRACKS:
            raise GlossaryValidationError(
                f"Unknown cultural track {self.track!r}; expected one of {CULTURAL_TRACKS}"
            )
        for zh_name, en_name in self.character_names.items():
            if not zh_name.strip() or not en_name.strip():
                raise GlossaryValidationError("Glossary character_names cannot have empty keys/values.")
            if self.track == "eastern_xuanhuan" and "-" in en_name:
                raise GlossaryValidationError(
                    f"Eastern Xuanhuan name {en_name!r} for {zh_name!r} must be un-hyphenated pinyin."
                )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TranscreationGlossary":
        return cls(
            book_id=str(data["book_id"]),
            title_zh=str(data["title_zh"]),
            title_en=str(data["title_en"]),
            track=str(data["track"]),
            character_names=dict(data.get("character_names", {})),
            realm_terms=dict(data.get("realm_terms", {})),
            measurement_rules=dict(data.get("measurement_rules", {})),
            chinglish_replacements=dict(data.get("chinglish_replacements", {})),
            custom_terms=dict(data.get("custom_terms", {})),
            locked=bool(data.get("locked", True)),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f"{target.suffix}.tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "TranscreationGlossary":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    def format_prompt_table(self) -> str:
        lines: List[str] = [
            f"### Locked Transcreation Glossary (Book: {self.title_en} / {self.title_zh}, Track: {self.track})",
            "You MUST use these exact locked English renderings without hyphenating pinyin or inventing alternatives:",
            "- Character Names:",
        ]
        for zh, en in self.character_names.items():
            lines.append(f"  - {zh} -> {en}")
        if self.realm_terms:
            lines.append("- World / Realm / Institutional Terms:")
            for zh, en in self.realm_terms.items():
                lines.append(f"  - {zh} -> {en}")
        if self.custom_terms:
            lines.append("- Custom Book Terms:")
            for zh, en in self.custom_terms.items():
                lines.append(f"  - {zh} -> {en}")
        lines.extend(
            [
                "- Measurement Rule: NEVER output bare pinyin units (li, zhang, cun, chi, jin, liang, mu, qing); convert to natural imperial/metric equivalents (miles, feet, inches, pounds).",
                "- Anti-Chinglish Rule: NEVER output 'Director Lu', 'Vice Director X', 'Teacher X', 'Section Chief X', 'Senior Brother X', or hyphenated pinyin names like 'Mu-ye'. Use natural native English titles ('Mr. Lu', 'VP Lu', 'Professor Li', 'Master Lin').",
            ]
        )
        if self.track == "modern_urban":
            lines.append(
                "- Currency Rule: Convert RMB/yuan amounts into order-of-magnitude USD purchasing power (e.g., 100万 RMB -> $150,000; 300 RMB -> $50)."
            )
        return "\n".join(lines)


def _pick_western_name(zh_name: str, used_names: set[str]) -> str:
    digest = hashlib.sha256(zh_name.encode("utf-8")).digest()
    start_idx = int.from_bytes(digest[:4], "big") % len(_WESTERN_NAME_POOL)
    for offset in range(len(_WESTERN_NAME_POOL)):
        cand = _WESTERN_NAME_POOL[(start_idx + offset) % len(_WESTERN_NAME_POOL)]
        if cand not in used_names:
            used_names.add(cand)
            return cand
    suffix = len(used_names) + 1
    cand = f"{_WESTERN_NAME_POOL[start_idx]} {suffix}"
    used_names.add(cand)
    return cand


def build_glossary(
    project_or_meta: NovelProject | ProjectMeta,
    *,
    characters: Optional[Mapping[str, CharacterAnchor] | Sequence[CharacterAnchor]] = None,
    track: Optional[str] = None,
    title_en: Optional[str] = None,
    character_overrides: Optional[Mapping[str, str]] = None,
    custom_terms: Optional[Mapping[str, str]] = None,
) -> TranscreationGlossary:
    """Construct a book-locked TranscreationGlossary from a NovelProject or ProjectMeta."""
    if isinstance(project_or_meta, NovelProject):
        meta = project_or_meta.meta
        char_source = characters if characters is not None else project_or_meta.characters
    else:
        meta = project_or_meta
        char_source = characters or ()

    resolved_track = track or infer_cultural_track(meta.genre, meta.genre_id)
    if resolved_track not in CULTURAL_TRACKS:
        raise GlossaryValidationError(
            f"Unsupported track {resolved_track!r}; expected one of {CULTURAL_TRACKS}"
        )

    char_list = (
        list(char_source.values())
        if isinstance(char_source, Mapping)
        else list(char_source)
    )
    overrides = dict(character_overrides or {})
    char_map: Dict[str, str] = {}
    used_western: set[str] = set(overrides.values())

    for ch in char_list:
        zh = ch.name.strip()
        if not zh:
            continue
        if zh in overrides:
            char_map[zh] = overrides[zh]
        elif resolved_track == "eastern_xuanhuan":
            char_map[zh] = to_unhyphenated_pinyin_name(zh)
        else:
            char_map[zh] = _pick_western_name(zh, used_western)

    for zh, en in overrides.items():
        if zh not in char_map:
            char_map[zh] = en

    realm_terms = (
        dict(EASTERN_XUANHUAN_TERMS)
        if resolved_track == "eastern_xuanhuan"
        else dict(MODERN_URBAN_TERMS)
    )

    resolved_title_en = (
        title_en.strip()
        if title_en and title_en.strip()
        else (
            f"Chronicles of {to_unhyphenated_pinyin_name(meta.title)}"
            if resolved_track == "eastern_xuanhuan"
            else f"The {to_unhyphenated_pinyin_name(meta.title).replace(' ', '')} Ledger"
        )
    )

    return TranscreationGlossary(
        book_id=meta.book_id or meta.project_id,
        title_zh=meta.title,
        title_en=resolved_title_en,
        track=resolved_track,
        character_names=char_map,
        realm_terms=realm_terms,
        measurement_rules={
            unit: f"Convert '{unit}' to {target} (*{factor:g})"
            for unit, (factor, target) in _MEASUREMENT_FACTORS.items()
        },
        chinglish_replacements={
            "Director <Surname>": "Mr./Ms. <Surname> (or the director)",
            "Teacher <Surname>": "Professor/Mr./Ms. <Surname>",
            "Section Chief <Surname>": "Mr./Ms. <Surname>",
        },
        custom_terms=dict(custom_terms or {}),
        locked=True,
    )


def normalize_localization_text(text: str, glossary: TranscreationGlossary) -> str:
    """Deterministically eliminate bare-pinyin units, hyphenated pinyin names, and Chinglish titles."""
    result = text

    def _replace_unit(match: re.Match[str]) -> str:
        raw_num = match.group("num").lower()
        unit = match.group("unit").lower()
        factor, eng_unit = _MEASUREMENT_FACTORS[unit]
        num_val = _NUMBER_WORDS[raw_num] if raw_num in _NUMBER_WORDS else float(raw_num)
        converted = num_val * factor
        if converted >= 10:
            formatted = str(int(round(converted)))
        else:
            formatted = f"{converted:.1f}".rstrip("0").rstrip(".")
        if formatted == "1" and eng_unit.endswith("s"):
            eng_unit = eng_unit[:-1]
        return f"{formatted} {eng_unit}"

    result = _BARE_PINYIN_UNIT_RE.sub(_replace_unit, result)

    def _replace_chinglish(match: re.Match[str]) -> str:
        title = match.group("title").lower()
        surname = match.group("surname")
        if "teacher" in title:
            return f"Professor {surname}"
        if "sister" in title:
            return f"Ms. {surname}"
        return f"Mr. {surname}"

    result = _CHINGLISH_TITLE_RE.sub(_replace_chinglish, result)

    if glossary.track == "eastern_xuanhuan":
        for en_name in glossary.character_names.values():
            parts = en_name.split()
            if len(parts) == 2 and len(parts[1]) >= 4:
                # Repair hyphenated forms like "Chen Mu-ye" -> "Chen Muye"
                pat = re.compile(rf"\b{re.escape(parts[0])}\s+[A-Z][a-z]+-[a-z]+\b")
                # Only repair when stripped form matches canonical en_name ignoring hyphen/case
                def _unhyphen(m: re.Match[str]) -> str:
                    joined = m.group(0).replace("-", "")
                    return en_name if joined.lower() == en_name.lower() else m.group(0)
                result = pat.sub(_unhyphen, result)

    return result


def validate_english_chapter_text(
    text: str,
    glossary: TranscreationGlossary,
    *,
    required_names: Optional[Sequence[str]] = None,
) -> List[str]:
    """Validate English Webnovel prose for zero Hanzi leakage, zero bare-pinyin units, zero Chinglish, and glossary consistency."""
    errors: List[str] = []
    if not text or not text.strip():
        errors.append("English chapter body is empty.")
        return errors

    hanzi_hits = _HANZI_RE.findall(text)
    if hanzi_hits:
        sample = "".join(hanzi_hits[:8])
        errors.append(f"Untranslated Hanzi leaked into English output: {sample!r}")

    for m in _BARE_PINYIN_UNIT_RE.finditer(text):
        errors.append(
            f"Bare-pinyin measurement unit detected: {m.group(0)!r} (must convert to natural English units)."
        )

    for m in _CHINGLISH_TITLE_RE.finditer(text):
        errors.append(
            f"Chinglish title/rank detected: {m.group(0)!r} (avoid literal '<Title> <Surname>' calques)."
        )

    if glossary.track == "eastern_xuanhuan":
        for m in _HYPHENATED_PINYIN_NAME_RE.finditer(text):
            errors.append(
                f"Hyphenated pinyin name detected: {m.group(0)!r} (must use un-hyphenated pinyin)."
            )

    if required_names:
        for canonical in required_names:
            if canonical and canonical not in text:
                errors.append(
                    f"Locked glossary name {canonical!r} missing from transcreated chapter."
                )

    return errors


@dataclass(frozen=True)
class TranscreatedChapter:
    chapter: int
    title_en: str
    body_en: str
    model: str = ""

    def format_serialized(self) -> str:
        return f"Chapter {self.chapter}: {self.title_en}\n\n{self.body_en.strip()}\n"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _parse_transcreated_output(raw: str, chapter_no: int, fallback_title: Optional[str] = None) -> Tuple[str, str]:
    stripped = raw.strip()
    if not stripped:
        raise GlossaryValidationError(f"Chapter {chapter_no}: LLM returned empty transcreation output.")
    lines = stripped.splitlines()
    first = lines[0].strip()
    m = re.match(r"^Chapter\s+\d+\s*[:\-—]\s*(.+)$", first, flags=re.IGNORECASE)
    if m:
        title_en = m.group(1).strip()
        body_en = "\n".join(lines[1:]).strip()
    elif fallback_title and fallback_title.strip():
        title_en = fallback_title.strip()
        body_en = stripped
    else:
        raise GlossaryValidationError(
            f"Chapter {chapter_no}: output must start with 'Chapter {chapter_no}: <English Title>', got {first!r}"
        )
    if not title_en or not body_en:
        raise GlossaryValidationError(
            f"Chapter {chapter_no}: parsed English title or body is empty."
        )
    return title_en, body_en


class WebnovelTranscreator:
    """English Webnovel cultural transcreator powered by RoleModelRouter / LLMProvider and locked glossary."""

    def __init__(
        self,
        glossary: TranscreationGlossary,
        *,
        router: Optional[RoleModelRouter] = None,
        provider: Optional[LLMProvider] = None,
        role: str = "writer",
        auto_normalize: bool = False,
    ) -> None:
        self.glossary = glossary
        self.router = router
        self.provider = provider
        self.role = role
        self.auto_normalize = auto_normalize

    def _compile_prompts(self, artifact: ChapterArtifact, chapter_title_en: Optional[str]) -> Tuple[str, str]:
        sys_prompt = (
            "You are a senior native-English Webnovel cultural transcreator. "
            "Translate and culturally adapt the Chinese webnovel chapter into punchy, natural, idiomatic English Webnovel prose. "
            f"Preserve narrative pacing, suspense hooks, and formatting.\n\n{self.glossary.format_prompt_table()}"
        )
        title_hint = f"Suggested English chapter title: {chapter_title_en}\n" if chapter_title_en else ""
        user_prompt = (
            f"Transcreate Chapter {artifact.chapter} ({artifact.public_title}) into English.\n"
            f"{title_hint}"
            f"Output format MUST begin with line 1 exactly as:\n"
            f"Chapter {artifact.chapter}: <English Chapter Title>\n"
            "Followed by a blank line and the complete English chapter prose.\n\n"
            f"Source Chinese Text:\n{artifact.body}"
        )
        return sys_prompt, user_prompt

    def transcreate_chapter(
        self,
        artifact: ChapterArtifact,
        *,
        chapter_title_en: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 6000,
    ) -> TranscreatedChapter:
        if self.router is None and self.provider is None:
            raise TranscreationProviderError(
                "WebnovelTranscreator requires RoleModelRouter or LLMProvider; no silent offline fallback allowed."
            )

        sys_prompt, user_prompt = self._compile_prompts(artifact, chapter_title_en)
        req = LLMRequest(
            request_id=f"transcreate_ch{artifact.chapter:04d}",
            chapter=artifact.chapter,
            attempt=1,
            purpose="transcreate",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            if self.router is not None:
                resp = self.router.generate(self.role, req)
            else:
                assert self.provider is not None
                resp = self.provider.generate(req)
        except LLMError as exc:
            raise TranscreationProviderError(
                f"Transcreation LLM call failed for chapter {artifact.chapter}: {exc}"
            ) from exc

        title_en, body_en = _parse_transcreated_output(
            resp.content,
            chapter_no=artifact.chapter,
            fallback_title=chapter_title_en,
        )
        if self.auto_normalize:
            title_en = normalize_localization_text(title_en, self.glossary)
            body_en = normalize_localization_text(body_en, self.glossary)

        # Require canonical English names for any glossary character appearing in the Chinese source
        required_names = [
            en_name
            for zh_name, en_name in self.glossary.character_names.items()
            if zh_name in artifact.body
        ]
        violations = validate_english_chapter_text(
            f"Chapter {artifact.chapter}: {title_en}\n\n{body_en}",
            self.glossary,
            required_names=required_names,
        )
        if violations:
            raise GlossaryValidationError(
                f"Chapter {artifact.chapter} transcreation failed validation: {'; '.join(violations)}"
            )

        return TranscreatedChapter(
            chapter=artifact.chapter,
            title_en=title_en,
            body_en=body_en,
            model=resp.model,
        )


@dataclass(frozen=True)
class EnglishBundleResult:
    output_dir: Path
    glossary_path: Path
    manuscript_path: Path
    bundle_json_path: Path
    chapters_dir: Path
    chapter_count: int


def export_english_serialization_bundle(
    chapters: Sequence[TranscreatedChapter],
    glossary: TranscreationGlossary,
    output_dir: str | Path,
) -> EnglishBundleResult:
    """Export validated 'Chapter N: Title' English Webnovel serialization bundle and locked glossary."""
    if not chapters:
        raise GlossaryValidationError("At least 1 TranscreatedChapter is required.")

    out_root = Path(output_dir).resolve()
    chapters_dir = out_root / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    glossary_path = glossary.save(out_root / "glossary_locked.json")
    merged_blocks: List[str] = [f"# {glossary.title_en}", ""]
    chapter_entries: List[Dict[str, Any]] = []

    for ch in sorted(chapters, key=lambda item: item.chapter):
        violations = validate_english_chapter_text(ch.format_serialized(), glossary)
        if violations:
            raise GlossaryValidationError(
                f"Cannot export invalid English chapter {ch.chapter}: {'; '.join(violations)}"
            )
        ch_text = ch.format_serialized()
        ch_path = chapters_dir / format_chapter_filename(ch.chapter)
        tmp_ch = ch_path.with_suffix(".txt.tmp")
        tmp_ch.write_text(ch_text, encoding="utf-8")
        os.replace(tmp_ch, ch_path)

        merged_blocks.append(ch_text.strip())
        merged_blocks.append("")
        chapter_entries.append(
            {
                "chapter": ch.chapter,
                "header": f"Chapter {ch.chapter}: {ch.title_en}",
                "title_en": ch.title_en,
                "file": ch_path.name,
                "model": ch.model,
            }
        )

    manuscript_path = out_root / "manuscript_en.txt"
    tmp_ms = manuscript_path.with_suffix(".txt.tmp")
    tmp_ms.write_text("\n".join(merged_blocks).rstrip() + "\n", encoding="utf-8")
    os.replace(tmp_ms, manuscript_path)

    bundle_json_path = out_root / "webnovel_bundle.json"
    tmp_bj = bundle_json_path.with_suffix(".json.tmp")
    tmp_bj.write_text(
        json.dumps(
            {
                "platform": "webnovel_en",
                "title_en": glossary.title_en,
                "title_zh": glossary.title_zh,
                "track": glossary.track,
                "chapter_count": len(chapter_entries),
                "chapters": chapter_entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp_bj, bundle_json_path)

    return EnglishBundleResult(
        output_dir=out_root,
        glossary_path=glossary_path,
        manuscript_path=manuscript_path,
        bundle_json_path=bundle_json_path,
        chapters_dir=chapters_dir,
        chapter_count=len(chapter_entries),
    )
