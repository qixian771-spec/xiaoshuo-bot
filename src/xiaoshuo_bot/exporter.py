from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import zipfile

from .genres import UnknownGenreError, get_genre
from .models import (
    ChapterArtifact,
    CharacterAnchor,
    NovelProject,
    ProjectMeta,
    SceneOutline,
    format_chapter_filename,
)
from .namer import is_common_simplified_hanzi
from .sanitizers import InvalidTweetAliasError, validate_tweet_alias


class ExportError(ValueError):
    """Base exception for multi-platform export failures."""


class ExportValidationError(ExportError):
    """Raised when exported metadata, chapter structure, or platform rules fail validation."""


_CHAPTER_PREFIX_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*第\s*[0-9零一二两三四五六七八九十百千万亿]+\s*[章回节卷]\s*[：:\-—.\s]*"),
    re.compile(r"^\s*Chapter\s+\d+\s*[:：\-—.\s]*", re.IGNORECASE),
    re.compile(r"^\s*\d+\s*[.、:：\-—]\s+"),
)

_AUTHOR_NOTE_INLINE_RE = re.compile(
    r"(?:^|\n)\s*(?:【?\s*作者有话说\s*】?|PS\s*[：:]|作者注\s*[：:])[^\n]*",
    re.IGNORECASE,
)


def strip_chapter_title_prefix(raw_title: str, chapter_no: Optional[int] = None) -> str:
    """Strip any residual '第X章'/'第N章'/'Chapter N' prefix for Tomato dual-box title compliance."""
    if not isinstance(raw_title, str):
        raise ExportValidationError(f"Chapter {chapter_no}: title must be str, got {type(raw_title).__name__}")
    cleaned = raw_title.strip()
    changed = True
    while changed and cleaned:
        changed = False
        for pat in _CHAPTER_PREFIX_PATTERNS:
            nxt = pat.sub("", cleaned, count=1).strip()
            if nxt != cleaned:
                cleaned = nxt
                changed = True
    cleaned = cleaned.strip("《》")
    if not cleaned:
        raise ExportValidationError(
            f"Chapter {chapter_no}: public_title {raw_title!r} is empty after stripping chapter prefix."
        )
    return cleaned


def _normalize_artifacts(
    chapters: Sequence[ChapterArtifact],
    scenes: Optional[Sequence[SceneOutline]] = None,
) -> List[ChapterArtifact]:
    if not chapters:
        raise ExportValidationError("At least 1 ChapterArtifact is required for export.")
    scene_map: Dict[int, SceneOutline] = {s.chapter: s for s in (scenes or ())}
    seen: set[int] = set()
    normalized: List[ChapterArtifact] = []
    for art in sorted(chapters, key=lambda c: c.chapter):
        if art.chapter <= 0:
            raise ExportValidationError(f"Chapter number must be positive, got {art.chapter}")
        if art.chapter in seen:
            raise ExportValidationError(f"Duplicate ChapterArtifact for chapter {art.chapter}")
        seen.add(art.chapter)
        raw_title = art.public_title.strip()
        if not raw_title and art.chapter in scene_map:
            sc = scene_map[art.chapter]
            raw_title = (sc.public_title or sc.internal_title).strip()
        if not raw_title:
            raise ExportValidationError(f"Chapter {art.chapter}: missing public_title.")
        clean_title = strip_chapter_title_prefix(raw_title, chapter_no=art.chapter)
        body = art.body.strip()
        if not body:
            raise ExportValidationError(f"Chapter {art.chapter}: body cannot be empty.")
        note = art.author_note.strip()
        if len(note) > 200:
            raise ExportValidationError(
                f"Chapter {art.chapter}: author_note length {len(note)} exceeds 200 chars."
            )
        if note and note in body:
            raise ExportValidationError(
                f"Chapter {art.chapter}: author_note is not physically separated from body."
            )
        if _AUTHOR_NOTE_INLINE_RE.search(body):
            raise ExportValidationError(
                f"Chapter {art.chapter}: body contains inline author-note marker; must be separated."
            )
        normalized.append(
            ChapterArtifact(
                chapter=art.chapter,
                public_title=clean_title,
                body=body,
                author_note=note,
                sha256=art.sha256 or ChapterArtifact(chapter=art.chapter, body=body).compute_sha256(),
                locked=art.locked,
                scene_id=art.scene_id,
            )
        )
    return normalized


def _resolve_category_and_tags(meta: ProjectMeta) -> Tuple[str, List[str]]:
    category = (meta.primary_category or "").strip()
    tags = [str(t).strip() for t in meta.tags if str(t).strip()]

    recipe = None
    if meta.genre_id:
        try:
            recipe = get_genre(meta.genre_id)
        except UnknownGenreError:
            recipe = None

    if not category:
        if recipe is not None:
            category = recipe.tomato_primary_category
        elif meta.genre.strip():
            category = meta.genre.strip()
    if not category:
        raise ExportValidationError("Missing primary_category for Tomato project card.")

    if len(tags) < 3 and recipe is not None:
        for t in recipe.tomato_tags:
            if t not in tags:
                tags.append(t)
            if len(tags) == 3:
                break
    if len(tags) < 3:
        raise ExportValidationError(
            f"Tomato project card requires 3 tags, got {len(tags)}: {tags!r}"
        )
    return category, tags[:3]


def _resolve_tweet_aliases(
    meta: ProjectMeta,
    explicit_aliases: Optional[Sequence[str]],
) -> List[str]:
    if explicit_aliases is not None:
        raw = [str(a).strip() for a in explicit_aliases if str(a).strip()]
        if len(raw) < 3:
            raise ExportValidationError(
                f"Tomato tweet kit requires 3 validated aliases, got {len(raw)}."
            )
        return [validate_tweet_alias(a) for a in raw[:3]]

    if meta.tweet_aliases:
        raw = [str(a).strip() for a in meta.tweet_aliases if str(a).strip()]
        if len(raw) < 3:
            raise ExportValidationError(
                f"ProjectMeta.tweet_aliases must contain at least 3 aliases, got {len(raw)}."
            )
        return [validate_tweet_alias(a) for a in raw[:3]]

    # Deterministic generation of 3 pure common simplified Hanzi 4..8 char aliases from title/genre
    hanzi_core = "".join(ch for ch in meta.title if is_common_simplified_hanzi(ch))
    genre_hanzi = "".join(ch for ch in (meta.primary_category or meta.genre) if is_common_simplified_hanzi(ch))
    stem = (hanzi_core[:4] or genre_hanzi[:4] or "江城夜雨")
    if len(stem) < 2:
        stem = f"{stem}风起"
    candidates = [
        f"{stem[:4]}录",
        f"{stem[:3]}风云起",
        f"{stem[:2]}破局夜",
    ]
    resolved: List[str] = []
    for cand in candidates:
        # Ensure length 4..8
        if len(cand) < 4:
            cand = (cand + "风云录")[:4]
        elif len(cand) > 8:
            cand = cand[:8]
        resolved.append(validate_tweet_alias(cand))
    return resolved


def _extract_hook_sentence(text: str, max_chars: int = 85) -> str:
    cleaned = " ".join(line.strip() for line in text.splitlines() if line.strip())
    parts = re.split(r"(?<=[。！？!?])", cleaned)
    for part in parts:
        p = part.strip()
        if len(p) >= 12:
            return p[:max_chars]
    return cleaned[:max_chars]


def _extract_tail_sentence(text: str, max_chars: int = 85) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    return tail[-max_chars:] if len(tail) > max_chars else tail


def build_tomato_tweet_kit(
    meta: ProjectMeta,
    chapters: Sequence[ChapterArtifact],
    *,
    explicit_aliases: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build 3 validated 4-8 Hanzi tweet aliases + 4-act golden-3-second script stopping before ch.5."""
    aliases = _resolve_tweet_aliases(meta, explicit_aliases)
    ch_map = {c.chapter: c for c in chapters}
    first_four = [ch_map[n] for n in sorted(ch_map.keys()) if n <= 4] or list(chapters[:4])

    c1 = first_four[0]
    c2 = first_four[1] if len(first_four) >= 2 else c1
    c3 = first_four[2] if len(first_four) >= 3 else c2
    c4 = first_four[3] if len(first_four) >= 4 else c3

    act_1 = (
        f"【第一幕·黄金前3秒暴击】"
        f"开局就出大事——「{_extract_hook_sentence(c1.body, 70)}」"
        f"换作旁人当场认栽，可他偏要当众掀桌！"
    )
    act_2 = (
        f"【第二幕·第1-2章极限压迫】"
        f"{c1.public_title}之后危机不降反升，紧接着「{c2.public_title}」："
        f"{_extract_hook_sentence(c2.body, 75)}"
    )
    act_3 = (
        f"【第三幕·第3-4章初次反杀与翻盘前奏】"
        f"所有人都以为他必死无疑，结果「{c3.public_title}」直接撕开破绽，"
        f"一路杀到「{c4.public_title}」：{_extract_hook_sentence(c4.body, 75)}"
    )
    act_4 = (
        f"【第四幕·第5章爆发前一秒悬崖急刹】"
        f"就在最后关头——「{_extract_tail_sentence(c4.body, 75)}」"
        f"下一秒第5章全面反攻即将炸场！想知道后续直接搜书名别名「{aliases[0]}」（或「{aliases[1]}」「{aliases[2]}」）。"
    )

    script_text = "\n\n".join([act_1, act_2, act_3, act_4])
    return {
        "book_title": meta.title,
        "pen_name": meta.pen_name or meta.author_name,
        "aliases": aliases,
        "chapters_covered": [c.chapter for c in first_four],
        "stops_before_chapter": 5,
        "acts": {
            "act_1_golden_3_seconds": act_1,
            "act_2_pressure_build": act_2,
            "act_3_first_turn_setup": act_3,
            "act_4_climax_cliffhanger": act_4,
        },
        "script_text": script_text,
    }


def build_character_star_cards(
    characters: Mapping[str, CharacterAnchor] | Sequence[CharacterAnchor],
) -> List[Dict[str, Any]]:
    items = list(characters.values()) if isinstance(characters, Mapping) else list(characters)
    cards: List[Dict[str, Any]] = []
    for ch in items:
        tagline = (ch.personality or ch.role).strip()
        if len(tagline) > 36:
            tagline = tagline[:35] + "…"
        cards.append(
            {
                "character_id": ch.id,
                "name": ch.name,
                "role": ch.role,
                "tagline": tagline,
                "personality": ch.personality,
                "speech_style": ch.speech_style,
                "faction": ch.faction,
                "initial_status": ch.initial_status,
                "behavioral_anchors": dict(ch.behavioral_anchors),
            }
        )
    return cards


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class TomatoExportResult:
    output_dir: Path
    zip_path: Path
    chapters_dir: Path
    project_card_path: Path
    star_cards_path: Path
    tweet_kit_path: Path
    chapter_count: int
    validated_aliases: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "zip_path": str(self.zip_path),
            "chapters_dir": str(self.chapters_dir),
            "project_card_path": str(self.project_card_path),
            "star_cards_path": str(self.star_cards_path),
            "tweet_kit_path": str(self.tweet_kit_path),
            "chapter_count": self.chapter_count,
            "validated_aliases": list(self.validated_aliases),
        }


def export_tomato_bundle(
    *,
    meta: ProjectMeta,
    chapters: Sequence[ChapterArtifact],
    output_dir: str | Path,
    characters: Mapping[str, CharacterAnchor] | Sequence[CharacterAnchor] = (),
    scenes: Optional[Sequence[SceneOutline]] = None,
    tweet_aliases: Optional[Sequence[str]] = None,
    cover_path: Optional[str | Path] = None,
    zip_path: Optional[str | Path] = None,
) -> TomatoExportResult:
    """Export Tomato standard bundle: chapters_split (JSON+TXT), project card, star cards, tweet kit, ZIP."""
    title = (meta.title or "").strip()
    if not (1 <= len(title) <= 15) or "《" in title or "》" in title:
        raise ExportValidationError(
            f"Tomato project title must be 1..15 chars without 《》, got {meta.title!r}"
        )

    blurb = (meta.synopsis or (meta.alternate_blurbs[0] if meta.alternate_blurbs else "")).strip()
    if not (50 <= len(blurb) <= 500):
        raise ExportValidationError(
            f"Tomato project blurb must be 50..500 chars, got {len(blurb)} chars."
        )

    primary_category, tags = _resolve_category_and_tags(meta)
    normalized_chapters = _normalize_artifacts(chapters, scenes=scenes)

    out_root = Path(output_dir).resolve()
    chapters_split_dir = out_root / "chapters_split"
    chapters_split_dir.mkdir(parents=True, exist_ok=True)

    exported_files: List[Path] = []
    for art in normalized_chapters:
        txt_name = format_chapter_filename(art.chapter)
        json_name = txt_name[:-4] + ".json"
        txt_path = chapters_split_dir / txt_name
        json_path = chapters_split_dir / json_name

        # TXT stores pure body ONLY (100% physically isolated from public_title & author_note)
        _write_text_atomic(txt_path, art.body + "\n")
        exported_files.append(txt_path)

        if art.author_note:
            note_path = chapters_split_dir / f"chapter_{art.chapter:04d}_author_note.txt"
            _write_text_atomic(note_path, art.author_note + "\n")
            exported_files.append(note_path)

        payload = {
            "chapter_no": art.chapter,
            "public_title": art.public_title,
            "body": art.body,
            "author_note": art.author_note,
            "char_count": len(re.sub(r"\s+", "", art.body)),
            "sha256": art.sha256,
            "locked": art.locked,
            "scene_id": art.scene_id,
        }
        _write_json_atomic(json_path, payload)
        exported_files.append(json_path)

    project_card = {
        "project_id": meta.project_id,
        "title": title,
        "blurb": blurb,
        "primary_category": primary_category,
        "tags": tags,
        "pen_name": meta.pen_name or meta.author_name,
        "author_level": meta.author_level,
        "contract_track": meta.contract_track,
        "pov_mode": meta.pov_mode,
        "panel_policy": meta.panel_policy,
    }
    project_card_path = out_root / "project_card.json"
    _write_json_atomic(project_card_path, project_card)
    exported_files.append(project_card_path)

    star_cards = build_character_star_cards(characters)
    star_cards_path = out_root / "character_star_cards.json"
    _write_json_atomic(star_cards_path, {"book_title": title, "cards": star_cards})
    exported_files.append(star_cards_path)

    tweet_kit = build_tomato_tweet_kit(meta, normalized_chapters, explicit_aliases=tweet_aliases)
    tweet_kit_path = out_root / "tweet_kit.json"
    _write_json_atomic(tweet_kit_path, tweet_kit)
    exported_files.append(tweet_kit_path)

    tweet_script_path = out_root / "tweet_script.txt"
    _write_text_atomic(tweet_script_path, tweet_kit["script_text"] + "\n")
    exported_files.append(tweet_script_path)

    if cover_path is not None:
        src_cover = Path(cover_path).resolve()
        if not src_cover.is_file():
            raise ExportValidationError(f"Cover file does not exist: '{src_cover}'")
        dst_cover = out_root / f"cover{src_cover.suffix.lower()}"
        if src_cover != dst_cover:
            shutil.copy2(src_cover, dst_cover)
        exported_files.append(dst_cover)

    final_zip = (
        Path(zip_path).resolve()
        if zip_path is not None
        else out_root / f"{meta.project_id}_tomato_bundle.zip"
    )
    final_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = final_zip.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fp in exported_files:
            zf.write(fp, arcname=fp.relative_to(out_root).as_posix())
    os.replace(tmp_zip, final_zip)

    return TomatoExportResult(
        output_dir=out_root,
        zip_path=final_zip,
        chapters_dir=chapters_split_dir,
        project_card_path=project_card_path,
        star_cards_path=star_cards_path,
        tweet_kit_path=tweet_kit_path,
        chapter_count=len(normalized_chapters),
        validated_aliases=list(tweet_kit["aliases"]),
    )


@dataclass(frozen=True)
class QimaoExportResult:
    output_dir: Path
    manuscript_path: Path
    chapters_dir: Path
    metadata_path: Path
    chapter_count: int


def export_qimao_bundle(
    *,
    meta: ProjectMeta,
    chapters: Sequence[ChapterArtifact],
    output_dir: str | Path,
    scenes: Optional[Sequence[SceneOutline]] = None,
) -> QimaoExportResult:
    """Export Qimao bundle using merged '第N章 副标题' chapter headers."""
    normalized = _normalize_artifacts(chapters, scenes=scenes)
    out_root = Path(output_dir).resolve()
    chapters_dir = out_root / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    merged_blocks: List[str] = []
    index_items: List[Dict[str, Any]] = []
    for art in normalized:
        header = f"第{art.chapter}章 {art.public_title}"
        chapter_text = f"{header}\n\n{art.body}"
        ch_file = chapters_dir / format_chapter_filename(art.chapter)
        _write_text_atomic(ch_file, chapter_text + "\n")
        merged_blocks.append(chapter_text)
        index_items.append(
            {
                "chapter_no": art.chapter,
                "header": header,
                "public_title": art.public_title,
                "author_note": art.author_note,
                "file": ch_file.name,
            }
        )

    manuscript_path = out_root / "qimao_manuscript.txt"
    _write_text_atomic(manuscript_path, "\n\n\n".join(merged_blocks) + "\n")

    metadata_path = out_root / "qimao_bundle.json"
    _write_json_atomic(
        metadata_path,
        {
            "platform": "qimao",
            "title": meta.title,
            "pen_name": meta.pen_name or meta.author_name,
            "chapter_count": len(normalized),
            "chapters": index_items,
        },
    )
    return QimaoExportResult(
        output_dir=out_root,
        manuscript_path=manuscript_path,
        chapters_dir=chapters_dir,
        metadata_path=metadata_path,
        chapter_count=len(normalized),
    )


def convert_to_corner_quotes(text: str) -> str:
    """Convert Chinese/ASCII curly and straight quotes into Zhihu Yanxuan corner brackets 「」/『』."""
    out: List[str] = []
    double_open = True
    single_open = True
    n = len(text)
    for idx, ch in enumerate(text):
        if ch == "“":
            out.append("「")
        elif ch == "”":
            out.append("」")
        elif ch == "‘":
            out.append("『")
        elif ch == "’":
            # Keep English apostrophe inside words like don't intact
            prev_is_ascii = idx > 0 and text[idx - 1].isascii() and text[idx - 1].isalpha()
            next_is_ascii = idx + 1 < n and text[idx + 1].isascii() and text[idx + 1].isalpha()
            if prev_is_ascii and next_is_ascii:
                out.append(ch)
            else:
                out.append("』")
        elif ch == '"':
            out.append("「" if double_open else "」")
            double_open = not double_open
        elif ch == "'":
            prev_is_ascii = idx > 0 and text[idx - 1].isascii() and text[idx - 1].isalpha()
            next_is_ascii = idx + 1 < n and text[idx + 1].isascii() and text[idx + 1].isalpha()
            if prev_is_ascii and next_is_ascii:
                out.append(ch)
            else:
                out.append("『" if single_open else "』")
                single_open = not single_open
        else:
            out.append(ch)
    return "".join(out)


@dataclass(frozen=True)
class ZhihuExportResult:
    output_dir: Path
    manuscript_path: Path
    annotated_path: Path
    metadata_path: Path
    section_count: int
    paywall_after_section: int
    paywall_char_ratio: float
    paywall_hook_lead_in: str


def export_zhihu_yanxuan_bundle(
    *,
    meta: ProjectMeta,
    chapters: Sequence[ChapterArtifact],
    output_dir: str | Path,
    scenes: Optional[Sequence[SceneOutline]] = None,
    paywall_lead_in: Optional[str] = None,
) -> ZhihuExportResult:
    """Export Zhihu Yanxuan short story bundle with 「」/『』 quotes, pure digit headers, and 30% paywall hook."""
    normalized = _normalize_artifacts(chapters, scenes=scenes)
    converted_sections: List[Tuple[int, str]] = []
    for idx, art in enumerate(normalized, start=1):
        converted_body = convert_to_corner_quotes(art.body)
        converted_sections.append((idx, converted_body))

    lengths = [len(re.sub(r"\s+", "", body)) for _, body in converted_sections]
    total_chars = sum(lengths)
    if total_chars <= 0:
        raise ExportValidationError("Total character count cannot be zero for Zhihu export.")

    # Locate section boundary closest to 30% paywall mark (at least section 1 when >=2 sections)
    target_chars = total_chars * 0.30
    cumulative = 0
    best_section = 1
    best_diff = float("inf")
    best_ratio = 0.0
    for idx, sec_len in enumerate(lengths, start=1):
        cumulative += sec_len
        if len(lengths) > 1 and idx == len(lengths):
            continue
        diff = abs(cumulative - target_chars)
        if diff < best_diff:
            best_diff = diff
            best_section = idx
            best_ratio = round(cumulative / total_chars, 4)

    lead_in_text = (
        paywall_lead_in.strip()
        if paywall_lead_in and paywall_lead_in.strip()
        else (
            f"（30%付费卡点导语：第{best_section}节末悬念骤起，真相与反杀正式揭幕，"
            f"下文进入核心高能反转——）"
        )
    )

    clean_blocks: List[str] = [convert_to_corner_quotes(meta.title), ""]
    annotated_blocks: List[str] = [
        f"# {convert_to_corner_quotes(meta.title)}",
        "",
    ]
    for sec_no, sec_body in converted_sections:
        clean_blocks.extend([str(sec_no), "", sec_body, ""])
        annotated_blocks.extend([str(sec_no), "", sec_body, ""])
        if sec_no == best_section:
            annotated_blocks.extend(
                [
                    f"<!-- [PAYWALL_30_PERCENT] ratio={best_ratio:.2%} -->",
                    lead_in_text,
                    "",
                ]
            )

    out_root = Path(output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    manuscript_path = out_root / "zhihu_yanxuan.txt"
    annotated_path = out_root / "zhihu_yanxuan_annotated.md"
    metadata_path = out_root / "yanxuan_meta.json"

    _write_text_atomic(manuscript_path, "\n".join(clean_blocks).rstrip() + "\n")
    _write_text_atomic(annotated_path, "\n".join(annotated_blocks).rstrip() + "\n")
    _write_json_atomic(
        metadata_path,
        {
            "platform": "zhihu_yanxuan",
            "title": meta.title,
            "section_count": len(converted_sections),
            "total_chars": total_chars,
            "paywall_after_section": best_section,
            "paywall_char_ratio": best_ratio,
            "paywall_hook_lead_in": lead_in_text,
        },
    )

    return ZhihuExportResult(
        output_dir=out_root,
        manuscript_path=manuscript_path,
        annotated_path=annotated_path,
        metadata_path=metadata_path,
        section_count=len(converted_sections),
        paywall_after_section=best_section,
        paywall_char_ratio=best_ratio,
        paywall_hook_lead_in=lead_in_text,
    )


def export_project_bundle(
    project: NovelProject,
    chapters: Sequence[ChapterArtifact],
    output_dir: str | Path,
    *,
    platform: str = "tomato",
    cover_path: Optional[str | Path] = None,
) -> Any:
    """Unified dispatcher for exporting a NovelProject to 'tomato', 'qimao', or 'zhihu'."""
    plat = platform.strip().lower()
    if plat == "tomato":
        return export_tomato_bundle(
            meta=project.meta,
            chapters=chapters,
            output_dir=output_dir,
            characters=project.characters,
            scenes=project.scenes,
            cover_path=cover_path,
        )
    if plat == "qimao":
        return export_qimao_bundle(
            meta=project.meta,
            chapters=chapters,
            output_dir=output_dir,
            scenes=project.scenes,
        )
    if plat in {"zhihu", "zhihu_yanxuan", "yanxuan"}:
        return export_zhihu_yanxuan_bundle(
            meta=project.meta,
            chapters=chapters,
            output_dir=output_dir,
            scenes=project.scenes,
        )
    raise ExportValidationError(f"Unsupported export platform {platform!r}")
