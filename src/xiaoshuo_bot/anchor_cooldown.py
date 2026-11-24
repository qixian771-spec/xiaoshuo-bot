from __future__ import annotations

import re
from typing import Dict, List, Mapping, Sequence, Set

from .models import CharacterAnchor

DEFAULT_COOLDOWN_CHAPTERS = 3

# Split on common Chinese/English punctuation and line breaks while preserving internal text.
_ANCHOR_PUNCT_SPLIT_RE = re.compile(
    r"[，。！？；：、…—,.!?;:()（）\[\]【】《》「」『』“”‘’\"'/／\r\n]+"
)


def extract_anchor_fragments(anchor_text: str) -> List[str]:
    """Split a behavioral anchor description by punctuation, strip whitespace, and keep >= 6-char fragments."""
    if not isinstance(anchor_text, str):
        raise TypeError(f"anchor_text must be str, got {type(anchor_text).__name__}")
    if not anchor_text.strip():
        return []
    fragments: List[str] = []
    seen: Set[str] = set()
    for part in _ANCHOR_PUNCT_SPLIT_RE.split(anchor_text):
        frag = part.strip()
        if len(re.sub(r"\s+", "", frag)) >= 6 and frag not in seen:
            seen.add(frag)
            fragments.append(frag)
    return fragments


def anchors_used_in(
    text: str,
    characters: Mapping[str, CharacterAnchor],
) -> Dict[str, Set[str]]:
    """Return {cid: {anchor_key, ...}} for anchors whose >= 6-char fragment appears verbatim in text."""
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    if not text or not characters:
        return {}
    used_by_char: Dict[str, Set[str]] = {}
    for cid, char in characters.items():
        if not char.behavioral_anchors:
            continue
        used_keys: Set[str] = set()
        for anchor_key, anchor_text in char.behavioral_anchors.items():
            frags = extract_anchor_fragments(anchor_text)
            if any(frag in text for frag in frags):
                used_keys.add(anchor_key)
        if used_keys:
            used_by_char[cid] = used_keys
    return used_by_char


def cooling_anchors(
    recent_texts: Sequence[str],
    characters: Mapping[str, CharacterAnchor],
    *,
    cooldown_chapters: int = DEFAULT_COOLDOWN_CHAPTERS,
) -> Dict[str, List[str]]:
    """Identify anchors used in up to the last `cooldown_chapters` (oldest -> newest), deterministically deduplicated."""
    if isinstance(cooldown_chapters, bool) or not isinstance(cooldown_chapters, int) or cooldown_chapters <= 0:
        raise ValueError(f"cooldown_chapters must be a positive int, got {cooldown_chapters!r}")
    if not recent_texts or not characters:
        return {}
    window = recent_texts[-cooldown_chapters:]
    cooling_by_char: Dict[str, List[str]] = {}
    seen_by_char: Dict[str, Set[str]] = {}
    for chapter_text in window:
        used = anchors_used_in(chapter_text, characters)
        for cid, char in characters.items():
            used_keys = used.get(cid)
            if not used_keys:
                continue
            keys_out = cooling_by_char.setdefault(cid, [])
            keys_seen = seen_by_char.setdefault(cid, set())
            for anchor_key in char.behavioral_anchors:
                if anchor_key in used_keys and anchor_key not in keys_seen:
                    keys_seen.add(anchor_key)
                    keys_out.append(anchor_key)
    return cooling_by_char
