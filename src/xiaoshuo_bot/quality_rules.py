"""Deterministic L1 cross-chapter quality-rule engine (no LLM calls, no silent defaults).

Threshold constants (module-level, explicit; never silently overridden by implicit defaults):
- ``ANCHOR_WINDOW = 10``: sliding chapter window width for ``QR_ANCHOR_REPEAT``.
- ``ANCHOR_MIN_FRAG_LEN = 10``: minimum character length of a punctuation-split anchor fragment.
- ``ANCHOR_MAX_CHAPTER_HITS = 2``: maximum chapters inside any ``ANCHOR_WINDOW`` where an anchor
  fragment may appear verbatim before triggering ``QR_ANCHOR_REPEAT``.
- ``VERBATIM_WINDOW = 30``: sliding chapter window width for ``QR_VERBATIM_REPEAT``.
- ``VERBATIM_MIN_PARA_LEN = 15``: minimum stripped paragraph length for ``QR_VERBATIM_REPEAT``.
- ``VERBATIM_MIN_OCCURRENCES = 2``: minimum occurrences within any ``VERBATIM_WINDOW`` that triggers
  ``QR_VERBATIM_REPEAT``.
- ``SELLING_POINT_WINDOW = 20``: contiguous chapter window width for ``QR_SELLING_POINT_DRIFT``.
  When the total number of chapters passed to ``run_l1_checks`` is strictly less than
  ``SELLING_POINT_WINDOW`` (20), ``QR_SELLING_POINT_DRIFT`` is skipped and documented as not
  applicable so early/short drafts are not falsely flagged.
- ``SELLING_POINT_MIN_RATIO = 0.40``: minimum fraction of chapters in any 20-chapter window that
  must contain at least one keyword from ``selling_point_keywords`` (and separately from
  ``core_selling_point_keywords`` when supplied, emitting ``QR_CORE_SELLING_POINT_DRIFT``).
- ``SELLING_POINT_MONOPOLY_MIN_KEYWORDS = 3``: minimum keyword count in ``selling_point_keywords``
  required to activate ``QR_SELLING_POINT_MONOPOLY`` (WARNING).
- ``SELLING_POINT_MONOPOLY_SOLE_SHARE = 0.85``: strict lower bound on the fraction of matched
  chapters in a 20-chapter window covered solely by a single dominant keyword before triggering
  ``QR_SELLING_POINT_MONOPOLY``.
- ``SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO = 0.15``: strict upper bound on the combined 20-chapter
  window coverage ratio of all non-dominant keywords required alongside ``SOLE_SHARE > 0.85`` to
  emit ``QR_SELLING_POINT_MONOPOLY``.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC, Sequence as SequenceABC
from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .models import CharacterAnchor

ANCHOR_WINDOW: int = 10
ANCHOR_MIN_FRAG_LEN: int = 10
ANCHOR_MAX_CHAPTER_HITS: int = 2

VERBATIM_WINDOW: int = 30
VERBATIM_MIN_PARA_LEN: int = 15
VERBATIM_MIN_OCCURRENCES: int = 2

SELLING_POINT_WINDOW: int = 20
SELLING_POINT_MIN_RATIO: float = 0.40
SELLING_POINT_MONOPOLY_MIN_KEYWORDS: int = 3
SELLING_POINT_MONOPOLY_SOLE_SHARE: float = 0.85
SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO: float = 0.15

VALID_SCALE_POLICIES = frozenset({"bounded_realistic", "unbounded_fantasy"})

_ANCHOR_SPLIT_RE = re.compile(r"[，。；：、！？,.!?;:…—\s]+")

# Live-stream / viewer / follower / subscriber impossible "亿"-level numbers in prose.
_PROSE_SCALE_YI_RE = re.compile(
    r"(?P<metric>在线|观看|观众|人气|关注|粉丝|订阅|涨粉)"
    r"(?P<gap>[^\n。！？，]{0,12}?)"
    r"(?P<num>[一二两三四五六七八九十百千万0-9\.]+亿)"
)
_REVERSE_SCALE_YI_RE = re.compile(
    r"(?P<num>[一二两三四五六七八九十百千万0-9\.]+亿)"
    r"(?P<gap>(?:多|余|左右)?(?:名|位|个)?(?:的)?)"
    r"(?P<metric>在线人数|观看人数|观众|粉丝|订阅人数|订阅者)"
)

# Gap phrases showing the keyword is a verb/subject rather than a follower/viewer counter
# (e.g. "关注着那家百亿市值", "观众看着他砸出十亿元", "观众都在议论那桩百亿大案").
_SCALE_GAP_EXEMPT_RE = re.compile(
    r"(?:着|过|看着|盯着|讨论|议论|围观|花|砸|买|卖|赚|赔|捐|抢|这[家桩起笔个项场]|那[家桩起笔个项场])"
)

# Trailing monetary, box-office, asset, or historical-time units immediately after "亿..."
# so normal finance/history mentions like "百亿市值", "十亿元", "十亿年前" are not flagged.
_SCALE_POST_EXEMPT_RE = re.compile(
    r"^[零一二两三四五六七八九十百千万0-9\.多余来上下左右级个位的名]*"
    r"(?:市值|资产|资金|身家|身价|估值|营收|利润|销售额|交易额|流水|票房|影帝|"
    r"红包|现金|支票|存款|欠款|债务|赔款|赔偿|违约金|奖金|预算|投资|融资|亏损|"
    r"工程|项目|大案|案件|遗产|房产|楼盘|地皮|公司|企业|集团|银行|财团|基金|"
    r"豪门|富豪|大佬|市场|产业|行业|规模|体量|"
    r"美元|美金|人民币|港元|港币|欧元|英镑|日元|元|块钱|块|"
    r"人口|光年|公里|米|吨|年前|年代|年历史|岁)"
)


@dataclass
class RuleHit:
    """Single deterministic L1 rule violation emitted by ``run_l1_checks``."""

    rule_id: str
    severity: str
    chapters: List[int]
    evidence: str
    value: Any

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _is_real_number(val: Any) -> bool:
    return (
        isinstance(val, (int, float))
        and not isinstance(val, bool)
        and math.isfinite(float(val))
    )


def _validate_and_sort_chapters(chapters: Sequence[Tuple[int, str]]) -> List[Tuple[int, str]]:
    if (
        chapters is None
        or isinstance(chapters, (str, bytes, bytearray))
        or not isinstance(chapters, SequenceABC)
    ):
        raise ValueError("chapters must be a non-empty sequence of (chapter_num, text) tuples.")
    if len(chapters) == 0:
        raise ValueError("chapters must not be empty.")

    seen_chapters: set[int] = set()
    normalized: List[Tuple[int, str]] = []
    for idx, item in enumerate(chapters):
        if (
            isinstance(item, (str, bytes, bytearray))
            or not isinstance(item, SequenceABC)
            or len(item) != 2
        ):
            raise ValueError(f"chapters[{idx}] must be a (chapter_num, text) pair, got {item!r}.")
        ch_num, text = item[0], item[1]
        if isinstance(ch_num, bool) or not isinstance(ch_num, int) or ch_num < 0:
            raise ValueError(
                f"chapters[{idx}] chapter_num must be a non-negative int, got {ch_num!r}."
            )
        if not isinstance(text, str):
            raise ValueError(
                f"chapters[{idx}] text must be a str, got {type(text).__name__}."
            )
        if ch_num in seen_chapters:
            raise ValueError(f"Duplicate chapter number {ch_num} in chapters.")
        seen_chapters.add(ch_num)
        normalized.append((ch_num, text))

    normalized.sort(key=lambda pair: pair[0])
    return normalized


def _validate_characters(characters: Mapping[str, CharacterAnchor]) -> Mapping[str, CharacterAnchor]:
    if characters is None or not isinstance(characters, MappingABC):
        raise ValueError("characters must be a Mapping[str, CharacterAnchor] (not None).")
    for cid, char in characters.items():
        if not isinstance(char, CharacterAnchor):
            raise ValueError(
                f"characters[{cid!r}] must be a CharacterAnchor instance, got {type(char).__name__}."
            )
        if not isinstance(char.behavioral_anchors, MappingABC):
            raise ValueError(
                f"characters[{cid!r}].behavioral_anchors must be a Mapping."
            )
    return characters


def _validate_scale_rules(
    blueprint_scale_rules: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if (
        blueprint_scale_rules is None
        or isinstance(blueprint_scale_rules, (str, bytes, bytearray))
        or not isinstance(blueprint_scale_rules, SequenceABC)
    ):
        raise ValueError("blueprint_scale_rules must be a sequence of rule mappings (not None).")

    validated: List[Dict[str, Any]] = []
    for idx, raw_rule in enumerate(blueprint_scale_rules):
        if not isinstance(raw_rule, MappingABC):
            raise ValueError(
                f"blueprint_scale_rules[{idx}] must be a mapping, got {type(raw_rule).__name__}."
            )
        ledger_key = raw_rule.get("ledger_key")
        if not isinstance(ledger_key, str) or not ledger_key.strip():
            raise ValueError(
                f"blueprint_scale_rules[{idx}].ledger_key must be a non-empty string."
            )
        policy = raw_rule.get("policy")
        if not isinstance(policy, str) or policy not in VALID_SCALE_POLICIES:
            raise ValueError(
                f"blueprint_scale_rules[{idx}].policy must be one of {sorted(VALID_SCALE_POLICIES)}, "
                f"got {policy!r}."
            )
        hard_max = raw_rule.get("hard_max")
        if policy == "bounded_realistic":
            if not _is_real_number(hard_max) or float(hard_max) <= 0.0:
                raise ValueError(
                    f"blueprint_scale_rules[{idx}] ({ledger_key!r}): bounded_realistic requires "
                    f"finite numeric hard_max > 0, got {hard_max!r}."
                )
        elif hard_max is not None and (not _is_real_number(hard_max) or float(hard_max) <= 0.0):
            raise ValueError(
                f"blueprint_scale_rules[{idx}] ({ledger_key!r}): unbounded_fantasy hard_max "
                f"must be None or finite numeric > 0, got {hard_max!r}."
            )
        validated.append(
            {
                "ledger_key": ledger_key.strip(),
                "policy": policy,
                "hard_max": float(hard_max) if hard_max is not None else None,
                "unit": str(raw_rule.get("unit") or ""),
            }
        )
    return validated


def _validate_keywords(
    selling_point_keywords: Sequence[str],
    *,
    param_name: str = "selling_point_keywords",
) -> List[str]:
    if (
        selling_point_keywords is None
        or isinstance(selling_point_keywords, (str, bytes, bytearray))
        or not isinstance(selling_point_keywords, SequenceABC)
    ):
        raise ValueError(f"{param_name} must be a sequence of non-empty strings.")
    cleaned: List[str] = []
    for idx, kw in enumerate(selling_point_keywords):
        if not isinstance(kw, str) or not kw.strip():
            raise ValueError(
                f"{param_name}[{idx}] must be a non-empty string, got {kw!r}."
            )
        token = kw.strip()
        if token not in cleaned:
            cleaned.append(token)
    if not cleaned:
        raise ValueError(f"{param_name} must contain at least 1 non-empty keyword.")
    return cleaned


def _validate_ledger_balances(ledger_balances: Mapping[str, float]) -> Dict[str, float]:
    if ledger_balances is None or not isinstance(ledger_balances, MappingABC):
        raise ValueError("ledger_balances must be a Mapping[str, float] (not None).")
    out: Dict[str, float] = {}
    for k, v in ledger_balances.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError(f"ledger_balances key must be a non-empty string, got {k!r}.")
        if not _is_real_number(v):
            raise ValueError(
                f"ledger_balances[{k!r}] must be a finite number, got {v!r}."
            )
        out[k] = float(v)
    return out


def _check_anchor_repeats(
    sorted_chapters: Sequence[Tuple[int, str]],
    characters: Mapping[str, CharacterAnchor],
) -> List[RuleHit]:
    hits: List[RuleHit] = []
    seen_char_frags: set[Tuple[str, str]] = set()

    for char_key, char in characters.items():
        char_label = char.name.strip() or char.id.strip() or str(char_key)
        for trigger, anchor_desc in char.behavioral_anchors.items():
            if not isinstance(anchor_desc, str) or not anchor_desc.strip():
                continue
            for raw_part in _ANCHOR_SPLIT_RE.split(anchor_desc):
                frag = raw_part.strip()
                if len(frag) < ANCHOR_MIN_FRAG_LEN:
                    continue
                dedup_key = (str(char_key), frag)
                if dedup_key in seen_char_frags:
                    continue
                seen_char_frags.add(dedup_key)

                matched_chs = [ch_num for ch_num, text in sorted_chapters if frag in text]
                if len(matched_chs) <= ANCHOR_MAX_CHAPTER_HITS:
                    continue

                violating_windows: List[Tuple[int, int, List[int]]] = []
                violating_chs_set: set[int] = set()
                for start_ch in matched_chs:
                    end_ch = start_ch + ANCHOR_WINDOW - 1
                    win_hits = [c for c in matched_chs if start_ch <= c <= end_ch]
                    if len(win_hits) > ANCHOR_MAX_CHAPTER_HITS:
                        violating_windows.append((start_ch, end_ch, win_hits))
                        violating_chs_set.update(win_hits)

                if not violating_windows:
                    continue

                worst_start, worst_end, worst_hits = max(
                    violating_windows, key=lambda item: (len(item[2]), -item[0])
                )
                violating_chs = sorted(violating_chs_set)
                evidence = (
                    f"角色「{char_label}」锚点({trigger})片段「{frag}」在连续 {ANCHOR_WINDOW} 章窗口 "
                    f"[{worst_start}..{worst_end}] 内命中 {len(worst_hits)} 章 {worst_hits} "
                    f"(上限 <= {ANCHOR_MAX_CHAPTER_HITS} 章；超标窗口共 {len(violating_windows)} 个)"
                )
                hits.append(
                    RuleHit(
                        rule_id="QR_ANCHOR_REPEAT",
                        severity="CRITICAL",
                        chapters=violating_chs,
                        evidence=evidence,
                        value={
                            "character_id": char.id,
                            "character_name": char_label,
                            "fragment": frag,
                            "worst_window": [worst_start, worst_end],
                            "worst_window_hits": worst_hits,
                            "max_window_hit_count": len(worst_hits),
                            "violating_window_count": len(violating_windows),
                        },
                    )
                )
    return hits


def _check_verbatim_repeats(sorted_chapters: Sequence[Tuple[int, str]]) -> List[RuleHit]:
    para_occurrences: Dict[str, List[int]] = {}
    for ch_num, text in sorted_chapters:
        for raw_line in text.splitlines():
            para = raw_line.strip()
            if len(para) >= VERBATIM_MIN_PARA_LEN:
                para_occurrences.setdefault(para, []).append(ch_num)

    hits: List[RuleHit] = []
    for para, occ_chapters in para_occurrences.items():
        if len(occ_chapters) < VERBATIM_MIN_OCCURRENCES:
            continue

        violating_windows: List[Tuple[int, int, List[int]]] = []
        violating_chs_set: set[int] = set()
        checked_starts: set[int] = set()
        for start_ch in occ_chapters:
            if start_ch in checked_starts:
                continue
            checked_starts.add(start_ch)
            end_ch = start_ch + VERBATIM_WINDOW - 1
            win_occ = [c for c in occ_chapters if start_ch <= c <= end_ch]
            if len(win_occ) >= VERBATIM_MIN_OCCURRENCES:
                violating_windows.append((start_ch, end_ch, win_occ))
                violating_chs_set.update(win_occ)

        if not violating_windows:
            continue

        worst_start, worst_end, worst_occ = max(
            violating_windows, key=lambda item: (len(item[2]), -item[0])
        )
        violating_chs = sorted(dict.fromkeys(violating_chs_set))
        preview = para if len(para) <= 80 else f"{para[:80]}..."
        evidence = (
            f"长度 {len(para)} 字的段落在 {VERBATIM_WINDOW} 章窗口 [{worst_start}..{worst_end}] 内"
            f"原样出现 {len(worst_occ)} 次（章号 {worst_occ}）：「{preview}」"
        )
        hits.append(
            RuleHit(
                rule_id="QR_VERBATIM_REPEAT",
                severity="CRITICAL",
                chapters=violating_chs,
                evidence=evidence,
                value={
                    "paragraph": para,
                    "char_len": len(para),
                    "worst_window": [worst_start, worst_end],
                    "worst_window_occurrences": worst_occ,
                    "max_occurrences": len(worst_occ),
                },
            )
        )
    return hits


def _is_scale_prose_exempt(match: re.Match[str], text: str) -> bool:
    gap = match.group("gap")
    if _SCALE_GAP_EXEMPT_RE.search(gap):
        return True
    trailing = text[match.end() : match.end() + 20]
    if _SCALE_POST_EXEMPT_RE.match(trailing):
        return True
    return False


def _fmt_number(val: float) -> str:
    if float(val).is_integer():
        return f"{int(val):,}"
    return f"{val:,.4f}".rstrip("0").rstrip(".")


def _check_scale_rules(
    sorted_chapters: Sequence[Tuple[int, str]],
    scale_rules: Sequence[Mapping[str, Any]],
    ledger_balances: Mapping[str, float],
) -> List[RuleHit]:
    hits: List[RuleHit] = []

    for rule in scale_rules:
        if rule["policy"] != "bounded_realistic":
            continue
        key = str(rule["ledger_key"])
        hard_max = float(rule["hard_max"])
        balance = float(ledger_balances.get(key, 0.0))
        if balance > hard_max:
            unit = str(rule.get("unit") or "")
            evidence = (
                f"数值台账 {key!r} 当前值 {_fmt_number(balance)}{unit} 超过 bounded_realistic "
                f"硬上限 {_fmt_number(hard_max)}{unit}"
            )
            hits.append(
                RuleHit(
                    rule_id="QR_SCALE",
                    severity="CRITICAL",
                    chapters=[],
                    evidence=evidence,
                    value={
                        "kind": "ledger_exceeded",
                        "ledger_key": key,
                        "balance": balance,
                        "hard_max": hard_max,
                        "unit": unit,
                    },
                )
            )

    for ch_num, text in sorted_chapters:
        matched_spans: List[Tuple[int, int, str]] = []
        for match in _PROSE_SCALE_YI_RE.finditer(text):
            if _is_scale_prose_exempt(match, text):
                continue
            matched_spans.append((match.start(), match.end(), match.group(0)))
        for match in _REVERSE_SCALE_YI_RE.finditer(text):
            if any(not (match.end() <= s or match.start() >= e) for s, e, _ in matched_spans):
                continue
            matched_spans.append((match.start(), match.end(), match.group(0)))
        matched_spans.sort(key=lambda item: item[0])

        for start, end, matched_text in matched_spans:
            snippet_start = max(0, start - 12)
            snippet_end = min(len(text), end + 12)
            snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
            evidence = (
                f"第 {ch_num} 章正文出现违背现实量级的亿级在线/粉丝/关注数值「{matched_text}」"
                f"（上下文：「{snippet}」）"
            )
            hits.append(
                RuleHit(
                    rule_id="QR_SCALE",
                    severity="CRITICAL",
                    chapters=[ch_num],
                    evidence=evidence,
                    value={
                        "kind": "prose_scale_yi",
                        "matched_text": matched_text,
                        "chapter": ch_num,
                    },
                )
            )

    return hits


def _check_selling_point_drift(
    sorted_chapters: Sequence[Tuple[int, str]],
    keywords: Sequence[str],
    *,
    rule_id: str = "QR_SELLING_POINT_DRIFT",
    label: str = "卖点",
) -> List[RuleHit]:
    """Check 20-chapter windows for selling-point keyword presence ratio >= 40%.

    When fewer than ``SELLING_POINT_WINDOW`` (20) chapters are provided, this check is skipped
    to avoid false positives on short samples. Only contiguous 20-chapter spans
    (``window[-1][0] - window[0][0] < SELLING_POINT_WINDOW``) are evaluated so disconnected
    chapter ranges (e.g. first 60 + last 60 passed together) are not bridged falsely.
    """
    if len(sorted_chapters) < SELLING_POINT_WINDOW:
        return []

    chapter_has_kw = [any(kw in text for kw in keywords) for _, text in sorted_chapters]

    drifting_windows: List[Tuple[List[int], int, float, int]] = []
    for idx in range(0, len(sorted_chapters) - SELLING_POINT_WINDOW + 1):
        win_pairs = sorted_chapters[idx : idx + SELLING_POINT_WINDOW]
        if win_pairs[-1][0] - win_pairs[0][0] >= SELLING_POINT_WINDOW:
            continue
        hit_count = sum(1 for flag in chapter_has_kw[idx : idx + SELLING_POINT_WINDOW] if flag)
        ratio = hit_count / float(SELLING_POINT_WINDOW)
        if ratio < SELLING_POINT_MIN_RATIO:
            win_chs = [ch_num for ch_num, _ in win_pairs]
            drifting_windows.append((win_chs, hit_count, ratio, idx))

    if not drifting_windows:
        return []

    # Merge overlapping drifting windows into contiguous spans so a long drift arc produces
    # one clear hit per drifted span rather than N nearly identical overlapping hits.
    merged_clusters: List[List[Tuple[List[int], int, float, int]]] = []
    for win_item in drifting_windows:
        if not merged_clusters:
            merged_clusters.append([win_item])
            continue
        prev_chs = merged_clusters[-1][-1][0]
        if win_item[0][0] <= prev_chs[-1]:
            merged_clusters[-1].append(win_item)
        else:
            merged_clusters.append([win_item])

    hits: List[RuleHit] = []
    for cluster in merged_clusters:
        span_chs = sorted({ch for win_chs, _, _, _ in cluster for ch in win_chs})
        worst_chs, worst_cnt, worst_ratio, worst_idx = min(
            cluster, key=lambda item: (item[2], item[0][0])
        )
        worst_slice = sorted_chapters[worst_idx : worst_idx + SELLING_POINT_WINDOW]
        per_kw_counts = {
            kw: sum(1 for _, text in worst_slice if kw in text) for kw in keywords
        }
        evidence = (
            f"连续 {SELLING_POINT_WINDOW} 章窗口 [{worst_chs[0]}..{worst_chs[-1]}] 中仅 "
            f"{worst_cnt}/{SELLING_POINT_WINDOW} 章 ({worst_ratio:.1%}) 包含{label}关键词 "
            f"{list(keywords)}，低于最低要求 {SELLING_POINT_MIN_RATIO:.0%}"
        )
        hits.append(
            RuleHit(
                rule_id=rule_id,
                severity="CRITICAL",
                chapters=span_chs,
                evidence=evidence,
                value={
                    "ratio": worst_ratio,
                    "matched_chapters": worst_cnt,
                    "window_size": SELLING_POINT_WINDOW,
                    "min_ratio": SELLING_POINT_MIN_RATIO,
                    "worst_window": [worst_chs[0], worst_chs[-1]],
                    "drifting_window_count": len(cluster),
                    "per_keyword_counts": per_kw_counts,
                },
            )
        )
    return hits


def _check_selling_point_monopoly(
    sorted_chapters: Sequence[Tuple[int, str]],
    keywords: Sequence[str],
) -> List[RuleHit]:
    """Emit WARNING when a single keyword monopolizes > 85% of matched chapters in a 20-ch window
    while all remaining keywords combined cover < 15% of the window.

    Active only when ``len(keywords) >= SELLING_POINT_MONOPOLY_MIN_KEYWORDS`` (3) and
    ``len(sorted_chapters) >= SELLING_POINT_WINDOW`` (20).
    """
    if (
        len(keywords) < SELLING_POINT_MONOPOLY_MIN_KEYWORDS
        or len(sorted_chapters) < SELLING_POINT_WINDOW
    ):
        return []

    chapter_kw_sets: List[frozenset[str]] = [
        frozenset(kw for kw in keywords if kw in text) for _, text in sorted_chapters
    ]

    monopoly_windows: List[
        Tuple[List[int], str, int, int, float, int, float, Dict[str, int]]
    ] = []
    for idx in range(0, len(sorted_chapters) - SELLING_POINT_WINDOW + 1):
        win_pairs = sorted_chapters[idx : idx + SELLING_POINT_WINDOW]
        if win_pairs[-1][0] - win_pairs[0][0] >= SELLING_POINT_WINDOW:
            continue
        win_sets = chapter_kw_sets[idx : idx + SELLING_POINT_WINDOW]
        any_hit_count = sum(1 for s in win_sets if s)
        if any_hit_count == 0:
            continue

        total_hits = {kw: sum(1 for s in win_sets if kw in s) for kw in keywords}
        sole_hits = {
            kw: sum(1 for s in win_sets if len(s) == 1 and kw in s) for kw in keywords
        }
        dom_kw = max(keywords, key=lambda k: (sole_hits[k], total_hits[k]))
        sole_count = sole_hits[dom_kw]
        other_count = any_hit_count - sole_count
        sole_share = sole_count / float(any_hit_count)
        other_ratio = other_count / float(SELLING_POINT_WINDOW)

        if (
            sole_share > SELLING_POINT_MONOPOLY_SOLE_SHARE
            and other_ratio < SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO
        ):
            win_chs = [ch_num for ch_num, _ in win_pairs]
            monopoly_windows.append(
                (
                    win_chs,
                    dom_kw,
                    any_hit_count,
                    sole_count,
                    sole_share,
                    other_count,
                    other_ratio,
                    total_hits,
                )
            )

    if not monopoly_windows:
        return []

    merged_clusters: List[
        List[Tuple[List[int], str, int, int, float, int, float, Dict[str, int]]]
    ] = []
    for win_item in monopoly_windows:
        if not merged_clusters:
            merged_clusters.append([win_item])
            continue
        prev_chs = merged_clusters[-1][-1][0]
        prev_dom = merged_clusters[-1][-1][1]
        if win_item[0][0] <= prev_chs[-1] and win_item[1] == prev_dom:
            merged_clusters[-1].append(win_item)
        else:
            merged_clusters.append([win_item])

    hits: List[RuleHit] = []
    for cluster in merged_clusters:
        span_chs = sorted({ch for item in cluster for ch in item[0]})
        (
            worst_chs,
            worst_dom,
            worst_any,
            worst_sole,
            worst_sole_share,
            worst_other,
            worst_other_ratio,
            worst_total_hits,
        ) = max(cluster, key=lambda item: (item[4], -item[6], -item[0][0]))

        per_kw_coverage = {
            kw: {
                "chapters": cnt,
                "ratio": round(cnt / float(SELLING_POINT_WINDOW), 4),
            }
            for kw, cnt in worst_total_hits.items()
        }
        stats_text = ", ".join(
            f"{kw}={cnt}/{SELLING_POINT_WINDOW} ({cnt / float(SELLING_POINT_WINDOW):.0%})"
            for kw, cnt in worst_total_hits.items()
        )
        evidence = (
            f"连续 {SELLING_POINT_WINDOW} 章窗口 [{worst_chs[0]}..{worst_chs[-1]}] 存在单一卖点词垄断："
            f"全部 {worst_any} 个命中章中有 {worst_sole} 章 ({worst_sole_share:.1%}) 仅靠「{worst_dom}」"
            f"单独支撑（>{SELLING_POINT_MONOPOLY_SOLE_SHARE:.0%}），其余关键词合计仅覆盖 "
            f"{worst_other}/{SELLING_POINT_WINDOW} 章 ({worst_other_ratio:.1%} < "
            f"{SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO:.0%}）；各词覆盖统计：{stats_text}"
        )
        hits.append(
            RuleHit(
                rule_id="QR_SELLING_POINT_MONOPOLY",
                severity="WARNING",
                chapters=span_chs,
                evidence=evidence,
                value={
                    "dominant_keyword": worst_dom,
                    "matched_chapters": worst_any,
                    "sole_dominant_chapters": worst_sole,
                    "sole_share": worst_sole_share,
                    "other_keywords_chapters": worst_other,
                    "other_keywords_ratio": worst_other_ratio,
                    "window_size": SELLING_POINT_WINDOW,
                    "max_sole_share": SELLING_POINT_MONOPOLY_SOLE_SHARE,
                    "min_other_ratio": SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO,
                    "worst_window": [worst_chs[0], worst_chs[-1]],
                    "monopoly_window_count": len(cluster),
                    "per_keyword_counts": worst_total_hits,
                    "per_keyword_coverage": per_kw_coverage,
                },
            )
        )
    return hits


def run_l1_checks(
    chapters: Sequence[Tuple[int, str]],
    *,
    characters: Mapping[str, CharacterAnchor],
    blueprint_scale_rules: Sequence[Mapping[str, Any]],
    selling_point_keywords: Sequence[str],
    ledger_balances: Mapping[str, float],
    core_selling_point_keywords: Optional[Sequence[str]] = None,
) -> List[RuleHit]:
    """Run deterministic L1 cross-chapter quality rules and return all triggered ``RuleHit``s.

    All required keyword-only inputs are strictly validated; missing or malformed arguments
    raise ``ValueError`` immediately without falling back to implicit defaults.
    When ``core_selling_point_keywords`` is provided (not ``None``), it is strictly validated as
    a non-empty sequence of non-empty strings and evaluated for ``QR_CORE_SELLING_POINT_DRIFT``.
    """
    sorted_chapters = _validate_and_sort_chapters(chapters)
    validated_chars = _validate_characters(characters)
    validated_rules = _validate_scale_rules(blueprint_scale_rules)
    validated_keywords = _validate_keywords(
        selling_point_keywords, param_name="selling_point_keywords"
    )
    validated_core_keywords: Optional[List[str]] = (
        _validate_keywords(
            core_selling_point_keywords, param_name="core_selling_point_keywords"
        )
        if core_selling_point_keywords is not None
        else None
    )
    validated_balances = _validate_ledger_balances(ledger_balances)

    hits: List[RuleHit] = []
    hits.extend(_check_anchor_repeats(sorted_chapters, validated_chars))
    hits.extend(_check_verbatim_repeats(sorted_chapters))
    hits.extend(_check_scale_rules(sorted_chapters, validated_rules, validated_balances))
    hits.extend(_check_selling_point_drift(sorted_chapters, validated_keywords))
    hits.extend(_check_selling_point_monopoly(sorted_chapters, validated_keywords))
    if validated_core_keywords is not None:
        hits.extend(
            _check_selling_point_drift(
                sorted_chapters,
                validated_core_keywords,
                rule_id="QR_CORE_SELLING_POINT_DRIFT",
                label="核心卖点",
            )
        )
    return hits


__all__ = [
    "ANCHOR_MAX_CHAPTER_HITS",
    "ANCHOR_MIN_FRAG_LEN",
    "ANCHOR_WINDOW",
    "RuleHit",
    "SELLING_POINT_MIN_RATIO",
    "SELLING_POINT_MONOPOLY_MIN_KEYWORDS",
    "SELLING_POINT_MONOPOLY_OTHER_MAX_RATIO",
    "SELLING_POINT_MONOPOLY_SOLE_SHARE",
    "SELLING_POINT_WINDOW",
    "VERBATIM_MIN_OCCURRENCES",
    "VERBATIM_MIN_PARA_LEN",
    "VERBATIM_WINDOW",
    "run_l1_checks",
]
