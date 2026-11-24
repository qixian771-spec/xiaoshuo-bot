"""Golden-three-chapters (黄金三章) multi-writer pilot runner and scorecard engine.

Runs each candidate writer provider through an engine factory on the requested chapters,
evaluates deterministic L1 quality rules, golden-three-chapters opening rubric checks, and
cross-family blind pairwise comparisons against a benchmark text, and atomically writes
structured JSON + Chinese Markdown scorecards (preserving completed results in
``scorecard.partial.json`` if any stage raises an exception).
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC, Sequence as SequenceABC
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .blueprint import BookBlueprint, load_blueprint
from .engine import NovelGenerationEngine, _resolve_snapshot_path
from .judge import PairwiseJudge, PairwiseResult
from .llm.base import LLMProvider
from .loader import load_project
from .models import NovelProject, SHANGHAI_TZ, resolve_chapter_path
from .opening_rules import OpeningReviewer, OpeningVerdict
from .quality_rules import RuleHit, run_l1_checks

_CHAPTER_HEADER_RE = re.compile(r"^第(\d+)章(?:[ \t]+.*)?$")
_CHAPTER_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")
_CHAPTER_INT_RE = re.compile(r"^\d+$")
_SNAPSHOT_AFTER_RE = re.compile(r"^chapter_(\d+)_after\.json$")
_MISSING_MARKER_PREFIX = "【缺失："
_MIN_NOT_LOSE_COUNT = 2


def _now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def _is_strict_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_chapters_sequence(chapters: Sequence[int]) -> List[int]:
    if (
        chapters is None
        or isinstance(chapters, (str, bytes, bytearray))
        or not isinstance(chapters, SequenceABC)
    ):
        raise ValueError("chapters must be a non-empty sequence of positive integers.")
    if len(chapters) == 0:
        raise ValueError("chapters must not be empty.")

    validated: List[int] = []
    seen: set[int] = set()
    for idx, ch in enumerate(chapters):
        if not _is_strict_positive_int(ch):
            raise ValueError(
                f"chapters[{idx}] must be a strict int >= 1, got {ch!r} ({type(ch).__name__})."
            )
        if ch in seen:
            raise ValueError(f"Duplicate chapter number {ch} in chapters.")
        seen.add(ch)
        validated.append(int(ch))
    return validated


def parse_chapters_arg(raw: str) -> List[int]:
    """Strictly parse a CLI chapters argument such as ``'1-3'`` or ``'1,2,3'``."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"chapters argument must be a non-empty string, got {raw!r}.")

    parts = raw.strip().split(",")
    chapters: List[int] = []
    seen: set[int] = set()
    for part in parts:
        token = part.strip()
        if not token:
            raise ValueError(f"Empty chapter segment in chapters argument {raw!r}.")
        range_match = _CHAPTER_RANGE_RE.match(token)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start < 1 or end < 1 or start > end:
                raise ValueError(
                    f"Invalid chapter range {token!r} in {raw!r}: start and end must satisfy 1 <= start <= end."
                )
            for ch in range(start, end + 1):
                if ch in seen:
                    raise ValueError(f"Duplicate chapter number {ch} in {raw!r}.")
                seen.add(ch)
                chapters.append(ch)
            continue

        if not _CHAPTER_INT_RE.match(token):
            raise ValueError(
                f"Invalid chapter token {token!r} in {raw!r}: expected positive integer or 'start-end' range."
            )
        ch = int(token)
        if ch < 1:
            raise ValueError(f"Chapter number must be >= 1, got {ch} in {raw!r}.")
        if ch in seen:
            raise ValueError(f"Duplicate chapter number {ch} in {raw!r}.")
        seen.add(ch)
        chapters.append(ch)

    if not chapters:
        raise ValueError(f"No valid chapters parsed from {raw!r}.")
    return chapters


def load_case_summaries(path: str | Path) -> Dict[int, str]:
    """Load chapter-to-case-summary mapping from a YAML file without silent defaults."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Case summaries file does not exist: '{p}'")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in case summaries file '{p}': {exc}") from exc

    if not isinstance(raw, MappingABC) or len(raw) == 0:
        raise ValueError(
            f"Case summaries file '{p}' must contain a non-empty YAML mapping of chapter int -> summary str."
        )

    summaries: Dict[int, str] = {}
    for raw_key, raw_val in raw.items():
        if isinstance(raw_key, bool):
            raise ValueError(f"Case summaries key must be a positive int, got bool {raw_key!r}.")
        if isinstance(raw_key, int):
            ch_num = raw_key
        elif isinstance(raw_key, str) and _CHAPTER_INT_RE.match(raw_key.strip()):
            ch_num = int(raw_key.strip())
        else:
            raise ValueError(
                f"Case summaries key must be a positive int, got {raw_key!r} ({type(raw_key).__name__})."
            )
        if ch_num < 1:
            raise ValueError(f"Case summaries chapter key must be >= 1, got {ch_num}.")
        if ch_num in summaries:
            raise ValueError(f"Duplicate chapter key {ch_num} in case summaries file '{p}'.")
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise ValueError(
                f"Case summaries[{ch_num}] in '{p}' must be a non-empty string, got {raw_val!r}."
            )
        summaries[ch_num] = raw_val.strip()
    return summaries


def load_benchmark_chapters(path: str | Path, chapters: Sequence[int]) -> Dict[int, str]:
    """Parse benchmark text (e.g. ``book1_ch1_3.txt``) into ``{chapter_num: body}``.

    Skips leading ``#`` comment lines before the first chapter header, splits on
    ``^第(\\d+)章`` headers (excluding the header line from body prose), drops lines
    starting with ``【缺失：``, and raises ``ValueError`` if any requested chapter is
    missing or has an empty body.
    """
    requested = _validate_chapters_sequence(chapters)
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Benchmark file does not exist: '{p}'")

    raw_text = p.read_text(encoding="utf-8-sig")
    parsed_lines: Dict[int, List[str]] = {}
    current_chapter: Optional[int] = None

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if current_chapter is None:
            if not stripped or stripped.startswith("#"):
                continue
            header_match = _CHAPTER_HEADER_RE.match(stripped)
            if not header_match:
                raise ValueError(
                    f"Benchmark file '{p}' line {line_no}: expected leading '#' comment or "
                    f"'第N章 ...' header before chapter body, got {stripped[:80]!r}."
                )
            ch_num = int(header_match.group(1))
            if ch_num < 1:
                raise ValueError(
                    f"Benchmark file '{p}' line {line_no}: chapter number must be >= 1, got {ch_num}."
                )
            parsed_lines[ch_num] = []
            current_chapter = ch_num
            continue

        header_match = _CHAPTER_HEADER_RE.match(stripped)
        if header_match:
            ch_num = int(header_match.group(1))
            if ch_num < 1:
                raise ValueError(
                    f"Benchmark file '{p}' line {line_no}: chapter number must be >= 1, got {ch_num}."
                )
            if ch_num in parsed_lines:
                raise ValueError(
                    f"Benchmark file '{p}' line {line_no}: duplicate chapter {ch_num}."
                )
            parsed_lines[ch_num] = []
            current_chapter = ch_num
            continue

        if stripped.startswith(_MISSING_MARKER_PREFIX):
            continue

        parsed_lines[current_chapter].append(raw_line.rstrip())

    result: Dict[int, str] = {}
    for ch in requested:
        if ch not in parsed_lines:
            raise ValueError(
                f"Benchmark file '{p}' is missing requested chapter {ch} "
                f"(available chapters: {sorted(parsed_lines)})."
            )
        body = "\n".join(parsed_lines[ch]).strip()
        if not body:
            raise ValueError(
                f"Benchmark file '{p}' has empty body for requested chapter {ch}."
            )
        result[ch] = body
    return result


def _write_text_atomic(path: Path, content: str) -> None:
    if not content.strip():
        raise ValueError(f"Refusing to write empty artifact to '{path}'.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
        tmp_path = fh.name
    os.replace(tmp_path, path)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    json.loads(serialized)
    _write_text_atomic(path, serialized + "\n")


@dataclass
class PilotScore:
    """Aggregated L1, golden-three-chapters opening, and pairwise blind-judge scorecard."""

    l1_hits: List[RuleHit]
    l1_critical_count: int
    opening_verdicts: Dict[int, OpeningVerdict]
    pairwise_results: Dict[int, PairwiseResult]
    not_lose_count: int
    opening_all_passed: bool
    passed: bool

    @property
    def win_count(self) -> int:
        return sum(1 for r in self.pairwise_results.values() if r.winner == "candidate")

    @property
    def tie_count(self) -> int:
        return sum(1 for r in self.pairwise_results.values() if r.winner == "tie")

    @property
    def lose_count(self) -> int:
        return sum(1 for r in self.pairwise_results.values() if r.winner == "reference")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "l1_critical_count": self.l1_critical_count,
            "l1_hits": [h.to_dict() for h in self.l1_hits],
            "opening_all_passed": self.opening_all_passed,
            "opening_verdicts": {
                str(ch): verdict.to_dict()
                for ch, verdict in sorted(self.opening_verdicts.items())
            },
            "not_lose_count": self.not_lose_count,
            "win_count": self.win_count,
            "tie_count": self.tie_count,
            "lose_count": self.lose_count,
            "pairwise_results": {
                str(ch): res.to_dict()
                for ch, res in sorted(self.pairwise_results.items())
            },
        }


def _validate_chapter_text_mapping(
    mapping: Mapping[int, str],
    *,
    label: str,
) -> Dict[int, str]:
    if mapping is None or not isinstance(mapping, MappingABC) or len(mapping) == 0:
        raise ValueError(f"{label} must be a non-empty Mapping[int, str].")
    normalized: Dict[int, str] = {}
    for ch, text in mapping.items():
        if not _is_strict_positive_int(ch):
            raise ValueError(
                f"{label} chapter key must be a strict int >= 1, got {ch!r} ({type(ch).__name__})."
            )
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{label}[{ch}] must be a non-empty string.")
        normalized[int(ch)] = text
    return normalized


def _validate_case_summaries_mapping(
    case_summaries: Mapping[int, str],
    chapters: Sequence[int],
) -> Dict[int, str]:
    if case_summaries is None or not isinstance(case_summaries, MappingABC):
        raise ValueError("case_summaries must be a Mapping[int, str] (not None).")
    normalized: Dict[int, str] = {}
    for ch, summary in case_summaries.items():
        if not _is_strict_positive_int(ch):
            raise ValueError(
                f"case_summaries key must be a strict int >= 1, got {ch!r} ({type(ch).__name__})."
            )
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(
                f"case_summaries[{ch}] must be a non-empty string, got {summary!r}."
            )
        normalized[int(ch)] = summary.strip()

    if 2 in chapters and 1 not in normalized:
        raise ValueError(
            "Chapter 2 opening review requires a non-empty case_summaries[1] entry."
        )
    return normalized


def score_chapters(
    *,
    chapter_texts: Mapping[int, str],
    benchmark: Mapping[int, str],
    project: NovelProject,
    blueprint: BookBlueprint,
    ledger_balances: Mapping[str, float],
    reviewer: OpeningReviewer,
    judge: PairwiseJudge,
    case_summaries: Mapping[int, str],
) -> PilotScore:
    """Evaluate generated chapters via L1 rules, opening rubric review, and blind pairwise judge."""
    norm_chapters = _validate_chapter_text_mapping(chapter_texts, label="chapter_texts")
    if benchmark is None or not isinstance(benchmark, MappingABC):
        raise ValueError("benchmark must be a Mapping[int, str].")
    if not isinstance(project, NovelProject):
        raise ValueError(f"project must be a NovelProject, got {type(project).__name__}.")
    if not isinstance(blueprint, BookBlueprint):
        raise ValueError(f"blueprint must be a BookBlueprint, got {type(blueprint).__name__}.")
    if reviewer is None or not callable(getattr(reviewer, "review", None)):
        raise ValueError("reviewer must implement review(text, chapter, previous_case_summary=...).")
    if judge is None or not callable(getattr(judge, "compare", None)):
        raise ValueError("judge must implement compare(candidate, reference, context=...).")

    sorted_chs = sorted(norm_chapters.keys())
    validated_benchmark = _validate_chapter_text_mapping(benchmark, label="benchmark")
    norm_benchmark: Dict[int, str] = {}
    for ch in sorted_chs:
        if ch not in validated_benchmark:
            raise ValueError(f"benchmark is missing chapter {ch} required by chapter_texts.")
        norm_benchmark[ch] = validated_benchmark[ch]

    norm_summaries = _validate_case_summaries_mapping(case_summaries, sorted_chs)

    # 1. Deterministic L1 quality rules (ScaleRule.to_dict() keys match ledger_key/policy/hard_max/unit)
    l1_hits = run_l1_checks(
        [(ch, norm_chapters[ch]) for ch in sorted_chs],
        characters=project.characters,
        blueprint_scale_rules=[rule.to_dict() for rule in blueprint.scale_rules],
        selling_point_keywords=blueprint.premise.selling_point_keywords,
        ledger_balances=ledger_balances,
    )
    l1_critical_count = sum(1 for hit in l1_hits if hit.severity == "CRITICAL")

    # 2. Golden-three-chapters opening review
    opening_verdicts: Dict[int, OpeningVerdict] = {}
    for ch in sorted_chs:
        if ch == 2:
            prev_summary: Optional[str] = norm_summaries[1]
        elif ch > 2:
            prev_summary = norm_summaries.get(ch - 1)
        else:
            prev_summary = None
        opening_verdicts[ch] = reviewer.review(
            norm_chapters[ch],
            ch,
            previous_case_summary=prev_summary,
        )

    # 3. Cross-family blind pairwise comparison against benchmark
    pairwise_results: Dict[int, PairwiseResult] = {}
    for ch in sorted_chs:
        pairwise_results[ch] = judge.compare(
            candidate=norm_chapters[ch],
            reference=norm_benchmark[ch],
            context={
                "chapter": ch,
                "title": project.meta.title,
                "genre_id": blueprint.style.genre_id,
            },
        )

    not_lose_count = sum(
        1 for res in pairwise_results.values() if res.winner in ("candidate", "tie")
    )
    opening_all_passed = bool(opening_verdicts) and all(
        verdict.passed_all for verdict in opening_verdicts.values()
    )
    passed = (
        (l1_critical_count == 0)
        and opening_all_passed
        and (not_lose_count >= _MIN_NOT_LOSE_COUNT)
    )

    return PilotScore(
        l1_hits=l1_hits,
        l1_critical_count=l1_critical_count,
        opening_verdicts=opening_verdicts,
        pairwise_results=pairwise_results,
        not_lose_count=not_lose_count,
        opening_all_passed=opening_all_passed,
        passed=passed,
    )


def _validate_numeric_balances(
    raw: Mapping[str, Any],
    *,
    source: str,
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{source}: ledger balance key must be a non-empty string, got {key!r}.")
        if not _is_finite_number(val):
            raise ValueError(
                f"{source}: ledger balance for {key!r} must be a finite number, got {val!r}."
            )
        out[key] = float(val)
    return out


def _read_writer_ledger_balances(
    writer_out: Path,
    ordered_chapters: Sequence[int],
    engine: Any,
    project: NovelProject,
) -> Dict[str, float]:
    """Explicitly read numeric_balances from latest snapshot, engine.state, or project.initial_balances."""
    snapshots_dir = writer_out / "snapshots"
    if snapshots_dir.is_dir():
        snap_path: Optional[Path] = None
        for ch in reversed(ordered_chapters):
            candidate = _resolve_snapshot_path(snapshots_dir, ch)
            if candidate is not None and candidate.is_file():
                snap_path = candidate
                break

        if snap_path is None:
            after_chapters: List[int] = []
            for item in snapshots_dir.iterdir():
                if not item.is_file():
                    continue
                m = _SNAPSHOT_AFTER_RE.match(item.name)
                if m:
                    after_chapters.append(int(m.group(1)))
            if after_chapters:
                snap_path = _resolve_snapshot_path(snapshots_dir, max(after_chapters))

        if snap_path is None:
            init_snap = _resolve_snapshot_path(snapshots_dir, 0)
            if init_snap is not None and init_snap.is_file():
                snap_path = init_snap

        if snap_path is not None:
            raw_data = json.loads(snap_path.read_text(encoding="utf-8"))
            if not isinstance(raw_data, MappingABC):
                raise ValueError(f"Snapshot '{snap_path}' must contain a JSON object.")
            if "numeric_balances" not in raw_data or not isinstance(
                raw_data["numeric_balances"], MappingABC
            ):
                raise ValueError(
                    f"Snapshot '{snap_path}' is missing a valid 'numeric_balances' mapping."
                )
            return _validate_numeric_balances(
                raw_data["numeric_balances"],
                source=str(snap_path),
            )

    engine_state = getattr(engine, "state", None)
    state_balances = getattr(engine_state, "numeric_balances", None)
    if isinstance(state_balances, MappingABC):
        return _validate_numeric_balances(state_balances, source="engine.state.numeric_balances")

    return _validate_numeric_balances(
        project.initial_balances,
        source="project.initial_balances",
    )


def _validate_writer_name(writer_name: Any) -> str:
    if not isinstance(writer_name, str) or not writer_name.strip():
        raise ValueError(f"Writer name must be a non-empty string, got {writer_name!r}.")
    cleaned = writer_name.strip()
    if "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."} or ".." in cleaned:
        raise ValueError(
            f"Writer name {writer_name!r} must be a safe single directory component."
        )
    return cleaned


def _default_engine_factory(
    project: NovelProject,
    provider: LLMProvider,
    output_dir: Path,
) -> NovelGenerationEngine:
    return NovelGenerationEngine(project, provider, output_dir)


def _format_winner_cn(winner: str) -> str:
    if winner == "candidate":
        return "胜 (candidate)"
    if winner == "tie":
        return "平 (tie)"
    if winner == "reference":
        return "负 (reference)"
    return winner


def render_scorecard_markdown(payload: Mapping[str, Any]) -> str:
    """Render a Chinese Markdown scorecard report starting with the direct pass/fail conclusion."""
    title = str(payload.get("project_title") or payload.get("project_id") or "")
    project_id = str(payload.get("project_id") or "")
    genre_id = str(payload.get("genre_id") or "")
    judge_model = str(payload.get("judge_model") or "")
    generated_at = str(payload.get("generated_at") or "")
    chapters = list(payload.get("chapters") or [])
    passed_writers = list(payload.get("passed_writers") or [])
    failed_writers = list(payload.get("failed_writers") or [])
    writer_order = list(payload.get("writer_order") or [])
    writers_map: Mapping[str, Any] = payload.get("writers") or {}

    lines: List[str] = [
        f"# 黄金三章试跑打分卡：《{title}》",
        "",
        "## 结论",
        "",
        f"- **过线写手（✅）**：{'、'.join(passed_writers) if passed_writers else '无'}",
        f"- **未过线写手（❌）**：{'、'.join(failed_writers) if failed_writers else '无'}",
        f"- **过线门槛**：`L1 严重违规 = 0` 且 `开篇黄金三章考核全部通过` 且 `跨模型盲比不败（胜+平）≥ {_MIN_NOT_LOSE_COUNT} 章`",
        f"- **项目信息**：`{project_id}` | 题材 `{genre_id}` | 试跑章节 `{chapters}` | 裁判模型 `{judge_model}` | 生成时间 `{generated_at}`",
        "",
        "## 写手总览",
        "",
        "| 写手 | 总评 | L1 严重违规 | 开篇考核通过率 | 跨模型盲比（胜/平/负，不败数） |",
        "| --- | --- | --- | --- | --- |",
    ]

    for w_name in writer_order:
        w_data = writers_map[w_name]
        w_passed = bool(w_data["passed"])
        verdict_badge = "✅ 过线" if w_passed else "❌ 未过线"

        l1_crit = int(w_data["l1_critical_count"])
        l1_total = len(w_data.get("l1_hits") or [])
        l1_cell = f"✅ 0（总命中 {l1_total}）" if l1_crit == 0 else f"❌ {l1_crit}（总命中 {l1_total}）"

        verdicts = w_data.get("opening_verdicts") or {}
        total_items = 0
        passed_items = 0
        for v_data in verdicts.values():
            items = v_data.get("items") or []
            total_items += len(items)
            passed_items += sum(1 for item in items if item.get("passed"))
        rate_pct = (passed_items / total_items * 100.0) if total_items > 0 else 0.0
        open_badge = "✅" if w_data.get("opening_all_passed") else "❌"
        open_cell = f"{open_badge} {passed_items}/{total_items} ({rate_pct:.0f}%)"

        wins = int(w_data.get("win_count", 0))
        ties = int(w_data.get("tie_count", 0))
        loses = int(w_data.get("lose_count", 0))
        not_lose = int(w_data.get("not_lose_count", 0))
        pw_badge = "✅" if not_lose >= _MIN_NOT_LOSE_COUNT else "❌"
        pw_cell = f"{pw_badge} {wins}胜 / {ties}平 / {loses}负（不败 {not_lose}）"

        lines.append(f"| {w_name} | {verdict_badge} | {l1_cell} | {open_cell} | {pw_cell} |")

    lines.append("")
    lines.append("## 分写手明细")

    for w_name in writer_order:
        w_data = writers_map[w_name]
        badge = "✅ 过线" if w_data["passed"] else "❌ 未过线"
        lines.extend(["", f"### {w_name}（{badge}）", ""])

        # L1 section
        l1_hits = w_data.get("l1_hits") or []
        l1_crit = int(w_data["l1_critical_count"])
        lines.append(f"#### 1. L1 规则检查（严重违规 {l1_crit} / 总命中 {len(l1_hits)}）")
        lines.append("")
        if not l1_hits:
            lines.append("- ✅ 0 违规命中。")
        else:
            for hit in l1_hits:
                sev = hit.get("severity", "")
                mark = "❌" if sev == "CRITICAL" else "⚠️"
                hit_chs = hit.get("chapters") or []
                lines.append(
                    f"- {mark} `[{sev}]` `{hit.get('rule_id')}`（章节 {hit_chs}）：{hit.get('evidence')}"
                )
        lines.append("")

        # Opening rubric section
        verdicts = w_data.get("opening_verdicts") or {}
        lines.append("#### 2. 开篇黄金三章考核")
        lines.append("")
        lines.append("| 章节 | 条目 | 结果 | 定位（段 / 字占比） | 评语 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for ch_key in sorted(verdicts.keys(), key=lambda x: int(x)):
            v_data = verdicts[ch_key]
            for item in v_data.get("items") or []:
                mark = "✅" if item.get("passed") else "❌"
                para_idx = item.get("paragraph_index")
                char_ratio = item.get("char_ratio")
                if para_idx is not None and char_ratio is not None:
                    loc = f"第 {para_idx} 段 / {float(char_ratio):.1%}"
                elif para_idx is not None:
                    loc = f"第 {para_idx} 段"
                else:
                    loc = "-"
                reason_clean = str(item.get("reason") or "").replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| 第 {ch_key} 章 | `{item.get('id')}` | {mark} | {loc} | {reason_clean} |"
                )
        lines.append("")

        # Pairwise blind comparison section
        pw_map = w_data.get("pairwise_results") or {}
        lines.append("#### 3. 跨模型两两盲比明细")
        lines.append("")
        lines.append("| 章节 | 结论 | 轮1 / 轮2 | 弃书风险点 | 对比理由 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for ch_key in sorted(pw_map.keys(), key=lambda x: int(x)):
            res = pw_map[ch_key]
            winner = str(res.get("winner") or "")
            mark = "✅" if winner in ("candidate", "tie") else "❌"
            r1 = str(res.get("round_1_winner") or "")
            r2 = str(res.get("round_2_winner") or "")
            drop_risk = str(res.get("drop_risk_point") or "").replace("|", "\\|").replace("\n", " ")
            reason = str(res.get("reason") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| 第 {ch_key} 章 | {mark} {_format_winner_cn(winner)} | {r1} / {r2} | {drop_risk} | {reason} |"
            )

    return "\n".join(lines).rstrip() + "\n"


def run_pilot(
    *,
    project_dir: str | Path,
    chapters: Sequence[int],
    writers: Mapping[str, LLMProvider],
    judge_provider: LLMProvider,
    judge_model: str,
    benchmark_path: str | Path,
    case_summaries: Mapping[int, str],
    out_dir: str | Path,
    engine_factory: Callable[[NovelProject, LLMProvider, Path], Any] = _default_engine_factory,
    blueprint_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Run golden-three-chapters pilot across writers and emit scorecard JSON/Markdown artifacts."""
    if writers is None or not isinstance(writers, MappingABC) or len(writers) == 0:
        raise ValueError("writers must be a non-empty Mapping[str, LLMProvider].")
    if not isinstance(judge_model, str) or not judge_model.strip():
        raise ValueError("judge_model must be a non-empty string.")
    if not callable(engine_factory):
        raise ValueError("engine_factory must be callable.")

    ordered_chapters = sorted(_validate_chapters_sequence(chapters))
    validated_writers: List[Tuple[str, LLMProvider]] = []
    seen_writer_names: set[str] = set()
    for raw_name, provider in writers.items():
        clean_name = _validate_writer_name(raw_name)
        if clean_name in seen_writer_names:
            raise ValueError(f"Duplicate writer name {clean_name!r}.")
        seen_writer_names.add(clean_name)
        validated_writers.append((clean_name, provider))

    project_root = Path(project_dir)
    project = load_project(project_root)
    bp_path = Path(blueprint_path) if blueprint_path is not None else project_root / "blueprint.yaml"
    blueprint = load_blueprint(bp_path)
    benchmark = load_benchmark_chapters(benchmark_path, ordered_chapters)
    norm_summaries = _validate_case_summaries_mapping(case_summaries, ordered_chapters)

    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    writer_order = [name for name, _ in validated_writers]
    completed_scores: Dict[str, PilotScore] = {}
    writer_dirs: Dict[str, Path] = {}
    writer_balances: Dict[str, Dict[str, float]] = {}

    current_writer: Optional[str] = None
    current_stage: str = "init"

    try:
        for writer_name, writer_provider in validated_writers:
            current_writer = writer_name
            current_stage = "init_judge"

            # Enforce cross-family writer/judge isolation BEFORE generating prose.
            reviewer = OpeningReviewer(
                provider=judge_provider,
                judge_model=judge_model,
                writer_model=writer_name,
            )
            judge = PairwiseJudge(
                provider=judge_provider,
                judge_model=judge_model,
                writer_model=writer_name,
            )

            current_stage = "generate"
            writer_out = out_root / writer_name
            writer_out.mkdir(parents=True, exist_ok=True)
            writer_dirs[writer_name] = writer_out

            engine = engine_factory(project, writer_provider, writer_out)
            engine.run(target_chapters=list(ordered_chapters))

            current_stage = "read_outputs"
            chapters_dir = writer_out / "chapters"
            chapter_texts: Dict[int, str] = {}
            for ch in ordered_chapters:
                ch_path = resolve_chapter_path(chapters_dir, ch)
                if ch_path is None or not ch_path.is_file():
                    raise FileNotFoundError(
                        f"Writer {writer_name!r} did not generate chapter {ch} in '{chapters_dir}'."
                    )
                text = ch_path.read_text(encoding="utf-8").strip()
                if not text:
                    raise ValueError(
                        f"Writer {writer_name!r} generated empty chapter {ch} at '{ch_path}'."
                    )
                chapter_texts[ch] = text

            ledger_balances = _read_writer_ledger_balances(
                writer_out,
                ordered_chapters,
                engine,
                project,
            )
            writer_balances[writer_name] = ledger_balances

            current_stage = "score"
            score = score_chapters(
                chapter_texts=chapter_texts,
                benchmark=benchmark,
                project=project,
                blueprint=blueprint,
                ledger_balances=ledger_balances,
                reviewer=reviewer,
                judge=judge,
                case_summaries=norm_summaries,
            )
            completed_scores[writer_name] = score
    except Exception as exc:
        partial_payload: Dict[str, Any] = {
            "status": "partial_error",
            "generated_at": _now_shanghai_iso(),
            "project_id": project.meta.project_id,
            "project_title": project.meta.title,
            "genre_id": blueprint.style.genre_id,
            "chapters": ordered_chapters,
            "judge_model": judge_model.strip(),
            "benchmark_path": str(Path(benchmark_path)),
            "writer_order": writer_order,
            "completed_writers": list(completed_scores.keys()),
            "passed_writers": [w for w, s in completed_scores.items() if s.passed],
            "failed_writers": [w for w, s in completed_scores.items() if not s.passed],
            "error": {
                "writer": current_writer,
                "stage": current_stage,
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "writers": {
                w: {
                    "writer_name": w,
                    "output_dir": str(writer_dirs[w]),
                    "ledger_balances": writer_balances[w],
                    **s.to_dict(),
                }
                for w, s in completed_scores.items()
            },
        }
        _write_json_atomic(out_root / "scorecard.partial.json", partial_payload)
        raise

    scorecard_json_path = out_root / "scorecard.json"
    scorecard_md_path = out_root / "scorecard.md"

    payload: Dict[str, Any] = {
        "status": "completed",
        "generated_at": _now_shanghai_iso(),
        "project_id": project.meta.project_id,
        "project_title": project.meta.title,
        "genre_id": blueprint.style.genre_id,
        "chapters": ordered_chapters,
        "judge_model": judge_model.strip(),
        "benchmark_path": str(Path(benchmark_path)),
        "writer_order": writer_order,
        "passed_writers": [w for w, s in completed_scores.items() if s.passed],
        "failed_writers": [w for w, s in completed_scores.items() if not s.passed],
        "writers": {
            w: {
                "writer_name": w,
                "output_dir": str(writer_dirs[w]),
                "ledger_balances": writer_balances[w],
                **s.to_dict(),
            }
            for w, s in completed_scores.items()
        },
        "artifacts": {
            "scorecard_json": str(scorecard_json_path),
            "scorecard_md": str(scorecard_md_path),
        },
    }

    _write_json_atomic(scorecard_json_path, payload)
    _write_text_atomic(scorecard_md_path, render_scorecard_markdown(payload))
    return payload


__all__ = [
    "PilotScore",
    "load_benchmark_chapters",
    "load_case_summaries",
    "parse_chapters_arg",
    "render_scorecard_markdown",
    "run_pilot",
    "score_chapters",
]
