"""End-of-volume 6-point hard health check and volume lock state machine.

Combines deterministic L1/state/blueprint hard checks with cross-model strong-judge semantic
review (``PairwiseJudge.volume_health_review``) and atomically persists both per-volume health
reports (``audit/volume_health_vXXXX.json``) and the lock registry (``audit/volume_locks.json``).
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC, Sequence as SequenceABC
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .judge import PairwiseJudge, VALID_VOLUME_HEALTH_CHECK_IDS
from .models import SHANGHAI_TZ, WorldStateSnapshot
from .quality_rules import RuleHit, run_l1_checks


VOLUME_HEALTH_CHECK_IDS: Tuple[str, ...] = VALID_VOLUME_HEALTH_CHECK_IDS

VOLUME_HEALTH_CHECK_TITLES: Dict[str, str] = {
    "VH_01_NO_ROUTINE_REPEAT": "无套路重复与同质化",
    "VH_02_FORESHADOW_AND_REWARD": "伏笔闭环与阶段回报",
    "VH_03_CHARACTER_AND_ARC": "人物弧光与退场边界",
    "VH_04_CORE_FLAVOR_QUOTA": "核心卖点风味与配额",
    "VH_05_THREE_ACT_AND_HOOK": "三幕结构与卷末钩子",
    "VH_06_LEDGER_AND_DEBT_MATH": "数值账本与代价逻辑",
}

LOCK_STATE_PASSED: str = "UNLOCKED_PASSED"
LOCK_STATE_LOCKED: str = "LOCKED_NEEDS_REPAIR"

_VALID_VERDICTS = frozenset({"PASS", "FAIL"})
_VALID_LOCK_STATES = frozenset({LOCK_STATE_PASSED, LOCK_STATE_LOCKED})
_VOLUME_LOCKS_FILENAME = "volume_locks.json"


class VolumeHealthLockError(RuntimeError):
    """Raised when advancing into a volume whose preceding volume is not unlocked and passed."""

    def __init__(
        self,
        message: str,
        *,
        volume_index: int,
        failed_checks: Optional[Sequence[str]] = None,
        lock_file: str | Path = "",
        report_file: str | Path = "",
        repair_actions: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.volume_index: int = int(volume_index)
        self.failed_checks: List[str] = [str(item) for item in (failed_checks or [])]
        self.lock_file: str = str(lock_file)
        self.report_file: str = str(report_file)
        self.repair_actions: List[Dict[str, Any]] = [
            dict(action) for action in (repair_actions or [])
        ]


@dataclass
class VolumeHealthCheckItem:
    """Single merged (deterministic + semantic) check result within a volume health report."""

    id: str
    title: str
    status: str  # "PASS" | "FAIL"
    deterministic_passed: bool
    semantic_passed: bool
    chapters: List[int]
    evidence: str
    repair_hint: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VolumeHealthReport:
    """Complete end-of-volume 6-point health report persisted to ``volume_health_vXXXX.json``."""

    volume_index: int
    volume_title: str
    chapter_start: int
    chapter_end: int
    verdict: str  # "PASS" | "FAIL"
    lock_state: str  # LOCK_STATE_PASSED | LOCK_STATE_LOCKED
    checks: List[VolumeHealthCheckItem]
    repair_actions: List[Dict[str, Any]]
    summary: str
    judge_model: str
    writer_model: str
    report_file: str
    lock_file: str
    recorded_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "volume_index": self.volume_index,
            "volume_title": self.volume_title,
            "chapter_start": self.chapter_start,
            "chapter_end": self.chapter_end,
            "verdict": self.verdict,
            "lock_state": self.lock_state,
            "checks": [item.to_dict() for item in self.checks],
            "repair_actions": [dict(a) for a in self.repair_actions],
            "summary": self.summary,
            "judge_model": self.judge_model,
            "writer_model": self.writer_model,
            "report_file": self.report_file,
            "lock_file": self.lock_file,
            "recorded_at": self.recorded_at,
        }


def _now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _fmt_number(val: float) -> str:
    if float(val).is_integer():
        return f"{int(val):,}"
    return f"{val:,.4f}".rstrip("0").rstrip(".")


def _validate_audit_dir(audit_dir: str | Path) -> Path:
    if audit_dir is None:
        raise ValueError("audit_dir must not be None.")
    if isinstance(audit_dir, str) and not audit_dir.strip():
        raise ValueError("audit_dir must be a non-empty path.")
    return Path(audit_dir)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # Round-trip sanity check before touching disk.
    json.loads(serialized)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
            tmp_path = fh.name
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _volume_report_filename(volume_index: int) -> str:
    if not _is_strict_int(volume_index) or volume_index < 1:
        raise ValueError(f"volume_index must be an int >= 1, got {volume_index!r}.")
    return f"volume_health_v{volume_index:04d}.json"


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, MappingABC):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_mapping(obj: Any, *, label: str) -> Dict[str, Any]:
    if isinstance(obj, MappingABC):
        return dict(obj)
    to_dict_fn = getattr(obj, "to_dict", None)
    if callable(to_dict_fn):
        res = to_dict_fn()
        if isinstance(res, MappingABC):
            return dict(res)
    raise ValueError(f"{label} must be a Mapping or expose to_dict(), got {type(obj).__name__}.")


def _validate_chapters_pairs(
    chapters: Sequence[Tuple[int, str]],
    *,
    param_name: str,
    allow_empty: bool = False,
) -> List[Tuple[int, str]]:
    if (
        chapters is None
        or isinstance(chapters, (str, bytes, bytearray))
        or not isinstance(chapters, SequenceABC)
    ):
        raise ValueError(f"{param_name} must be a sequence of (chapter_num, text) pairs.")
    if not allow_empty and len(chapters) == 0:
        raise ValueError(f"{param_name} must not be empty.")

    seen: set[int] = set()
    normalized: List[Tuple[int, str]] = []
    for idx, item in enumerate(chapters):
        if (
            isinstance(item, (str, bytes, bytearray))
            or not isinstance(item, SequenceABC)
            or len(item) != 2
        ):
            raise ValueError(
                f"{param_name}[{idx}] must be a (chapter_num, text) pair, got {item!r}."
            )
        ch_num, text = item[0], item[1]
        if not _is_strict_int(ch_num) or ch_num < 1:
            raise ValueError(
                f"{param_name}[{idx}] chapter_num must be an int >= 1, got {ch_num!r}."
            )
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"{param_name}[{idx}] text must be a non-empty string."
            )
        if ch_num in seen:
            raise ValueError(f"Duplicate chapter number {ch_num} in {param_name}.")
        seen.add(ch_num)
        normalized.append((int(ch_num), text))
    normalized.sort(key=lambda pair: pair[0])
    return normalized


def _normalize_world_state_snapshot(snapshot: Any) -> Dict[str, Any]:
    if isinstance(snapshot, WorldStateSnapshot):
        data = snapshot.to_dict()
    elif isinstance(snapshot, MappingABC):
        data = dict(snapshot)
    else:
        raise ValueError(
            "world_state_snapshot must be a Mapping or WorldStateSnapshot, "
            f"got {type(snapshot).__name__}."
        )
    if "foreshadows" not in data or not isinstance(data["foreshadows"], MappingABC):
        raise ValueError("world_state_snapshot must contain a 'foreshadows' mapping.")
    if "numeric_balances" not in data or not isinstance(data["numeric_balances"], MappingABC):
        raise ValueError("world_state_snapshot must contain a 'numeric_balances' mapping.")
    return data


def _extract_volume_card(volume_spec: Any, *, label: str) -> Dict[str, Any]:
    if volume_spec is None:
        raise ValueError(f"{label} must not be None.")
    card = _to_mapping(volume_spec, label=label)
    # Safely enrich with U1 optional fields if present on the object and not already serialized.
    for opt_str in ("antagonist_motive", "new_gameplay", "mainline_step"):
        if opt_str not in card:
            val = getattr(volume_spec, opt_str, None)
            if isinstance(val, str) and val.strip():
                card[opt_str] = val
    for opt_int in ("daily_case_quota", "fugitive_case_quota"):
        if opt_int not in card:
            val = getattr(volume_spec, opt_int, None)
            if val is not None:
                card[opt_int] = val
    if "forbidden_from_prev_volume" not in card:
        val = getattr(volume_spec, "forbidden_from_prev_volume", None)
        if val:
            card["forbidden_from_prev_volume"] = list(val)
    return card


def _extract_cast_card(member: Any) -> Dict[str, Any]:
    card = _to_mapping(member, label="cast_member")
    for opt_str in ("independent_goal", "flaw_or_setback"):
        if opt_str not in card:
            val = getattr(member, opt_str, None)
            if isinstance(val, str) and val.strip():
                card[opt_str] = val
    if "stage_arcs" not in card:
        val = getattr(member, "stage_arcs", None)
        if val:
            card["stage_arcs"] = list(val)
    return card


def _collect_volume_cap_locks(
    snap: Mapping[str, Any],
    project: Any,
    *,
    volume_index: int,
) -> List[Dict[str, Any]]:
    """Merge volume_cap_locks from world_state_snapshot (authoritative runtime) and project."""
    merged: Dict[Tuple[int, str], Dict[str, Any]] = {}

    def _ingest(source: Any, *, source_name: str) -> None:
        if source is None:
            return
        if isinstance(source, MappingABC):
            items = list(source.values())
        elif isinstance(source, SequenceABC) and not isinstance(source, (str, bytes, bytearray)):
            items = list(source)
        else:
            raise ValueError(
                f"{source_name} must be a mapping or sequence of volume cap locks, "
                f"got {type(source).__name__}."
            )
        for idx, item in enumerate(items):
            ledger_key = _field(item, "ledger_key")
            vol = _field(item, "volume")
            max_val = _field(item, "max_value")
            unlocked = _field(item, "unlocked", False)
            unit = _field(item, "unit", "")
            if not isinstance(ledger_key, str) or not ledger_key.strip():
                raise ValueError(
                    f"{source_name}[{idx}].ledger_key must be a non-empty string, got {ledger_key!r}."
                )
            if not _is_strict_int(vol) or vol < 1:
                raise ValueError(
                    f"{source_name}[{idx}].volume must be an int >= 1, got {vol!r}."
                )
            if not _is_finite_number(max_val):
                raise ValueError(
                    f"{source_name}[{idx}].max_value must be a finite number, got {max_val!r}."
                )
            if not isinstance(unlocked, bool):
                raise ValueError(
                    f"{source_name}[{idx}].unlocked must be a bool, got {unlocked!r}."
                )
            key_tuple = (int(vol), ledger_key.strip())
            if key_tuple not in merged:
                merged[key_tuple] = {
                    "ledger_key": ledger_key.strip(),
                    "volume": int(vol),
                    "max_value": float(max_val),
                    "unlocked": unlocked,
                    "unit": str(unit or ""),
                }

    _ingest(snap.get("volume_cap_locks"), source_name="world_state_snapshot.volume_cap_locks")
    _ingest(getattr(project, "volume_cap_locks", None), source_name="project.volume_cap_locks")
    return [entry for entry in merged.values() if entry["volume"] == volume_index]


def load_volume_locks(audit_dir: str | Path) -> Dict[str, Any]:
    """Load ``audit_dir/volume_locks.json`` strictly.

    Returns ``{"volumes": {}}`` when the file does not exist yet.
    Raises ``ValueError`` whenever the file exists but is unreadable, corrupted, invalid JSON,
    or violates the expected ``{"volumes": {...}}`` schema (NEVER silently falls back).
    """
    audit_path = _validate_audit_dir(audit_dir)
    lock_path = audit_path / _VOLUME_LOCKS_FILENAME
    if not lock_path.exists():
        return {"volumes": {}}
    if not lock_path.is_file():
        raise ValueError(f"Expected regular file for volume locks at '{lock_path}'.")
    try:
        raw_text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Failed to read volume_locks.json at '{lock_path}': {exc}") from exc
    if not raw_text.strip():
        raise ValueError(f"Corrupted volume_locks.json at '{lock_path}': file is empty.")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Corrupted volume_locks.json at '{lock_path}': invalid JSON ({exc})."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid volume_locks.json at '{lock_path}': root must be a JSON object, "
            f"got {type(data).__name__}."
        )
    if "volumes" not in data or not isinstance(data["volumes"], dict):
        raise ValueError(
            f"Invalid volume_locks.json at '{lock_path}': missing or non-object 'volumes' field."
        )
    for vol_key, entry in data["volumes"].items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"Invalid volume_locks.json at '{lock_path}': volumes[{vol_key!r}] must be an object, "
                f"got {type(entry).__name__}."
            )
    return data


def save_volume_health_result(
    audit_dir: str | Path,
    report: VolumeHealthReport,
) -> Tuple[Path, Path]:
    """Atomically write ``volume_health_vXXXX.json`` and update ``volume_locks.json``."""
    audit_path = _validate_audit_dir(audit_dir)
    if not isinstance(report, VolumeHealthReport):
        raise TypeError(
            f"report must be a VolumeHealthReport instance, got {type(report).__name__}."
        )
    if not _is_strict_int(report.volume_index) or report.volume_index < 1:
        raise ValueError(
            f"report.volume_index must be an int >= 1, got {report.volume_index!r}."
        )
    if report.verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"report.verdict must be one of {sorted(_VALID_VERDICTS)}, got {report.verdict!r}."
        )
    if report.lock_state not in _VALID_LOCK_STATES:
        raise ValueError(
            f"report.lock_state must be one of {sorted(_VALID_LOCK_STATES)}, "
            f"got {report.lock_state!r}."
        )
    if report.verdict == "PASS" and report.lock_state != LOCK_STATE_PASSED:
        raise ValueError(
            f"Inconsistent report state: verdict='PASS' requires lock_state={LOCK_STATE_PASSED!r}, "
            f"got {report.lock_state!r}."
        )
    if report.verdict == "FAIL" and report.lock_state != LOCK_STATE_LOCKED:
        raise ValueError(
            f"Inconsistent report state: verdict='FAIL' requires lock_state={LOCK_STATE_LOCKED!r}, "
            f"got {report.lock_state!r}."
        )

    # Validate existing lock registry strictly before modifying anything.
    locks = load_volume_locks(audit_path)

    audit_path.mkdir(parents=True, exist_ok=True)
    report_path = audit_path / _volume_report_filename(report.volume_index)
    lock_path = audit_path / _VOLUME_LOCKS_FILENAME

    if not report.recorded_at:
        report.recorded_at = _now_shanghai_iso()
    report.report_file = str(report_path)
    report.lock_file = str(lock_path)

    _write_json_atomic(report_path, report.to_dict())

    failed_check_ids = [item.id for item in report.checks if item.status != "PASS"]
    locks["volumes"][str(report.volume_index)] = {
        "volume_index": report.volume_index,
        "volume_title": report.volume_title,
        "chapter_start": report.chapter_start,
        "chapter_end": report.chapter_end,
        "verdict": report.verdict,
        "lock_state": report.lock_state,
        "failed_checks": failed_check_ids,
        "repair_actions": [dict(a) for a in report.repair_actions],
        "summary": report.summary,
        "judge_model": report.judge_model,
        "writer_model": report.writer_model,
        "report_file": str(report_path),
        "recorded_at": report.recorded_at,
    }
    locks["updated_at"] = report.recorded_at
    _write_json_atomic(lock_path, locks)
    return report_path, lock_path


def assert_previous_volume_unlocked(audit_dir: str | Path, target_volume: int) -> None:
    """Assert that ``target_volume - 1`` passed end-of-volume health check and is unlocked.

    - ``target_volume <= 1`` returns immediately (Volume 1 has no preceding volume).
    - Corrupted ``volume_locks.json`` raises ``ValueError`` (via ``load_volume_locks``).
    - Missing prior lock record, non-PASS verdict, locked state, or missing/non-PASS report file
      immediately raises ``VolumeHealthLockError``.
    """
    if not _is_strict_int(target_volume):
        raise ValueError(f"target_volume must be an int, got {target_volume!r}.")
    if target_volume <= 1:
        return

    audit_path = _validate_audit_dir(audit_dir)
    lock_path = audit_path / _VOLUME_LOCKS_FILENAME
    prev_vol = target_volume - 1
    default_report_path = audit_path / _volume_report_filename(prev_vol)

    locks = load_volume_locks(audit_path)
    entry = locks["volumes"].get(str(prev_vol))
    if entry is None:
        raise VolumeHealthLockError(
            f"禁止进入第 {target_volume} 卷：第 {prev_vol} 卷缺少卷末六项体检通过记录（'{lock_path}'）。",
            volume_index=prev_vol,
            failed_checks=["MISSING_VOLUME_HEALTH_RECORD"],
            lock_file=lock_path,
            report_file=default_report_path,
            repair_actions=[
                {
                    "check_id": "MISSING_VOLUME_HEALTH_RECORD",
                    "repair_hint": f"请先对第 {prev_vol} 卷执行卷末六项体检并通过解锁后，再进入第 {target_volume} 卷。",
                }
            ],
        )

    recorded_lock_state = entry.get("lock_state")
    recorded_verdict = entry.get("verdict")
    recorded_failed = list(entry.get("failed_checks") or [])
    recorded_repairs = list(entry.get("repair_actions") or [])
    report_file_str = entry.get("report_file")

    if recorded_lock_state != LOCK_STATE_PASSED or recorded_verdict != "PASS":
        raise VolumeHealthLockError(
            f"禁止进入第 {target_volume} 卷：第 {prev_vol} 卷处于锁卷状态 "
            f"(lock_state={recorded_lock_state!r}, verdict={recorded_verdict!r}, "
            f"failed_checks={recorded_failed})。",
            volume_index=prev_vol,
            failed_checks=recorded_failed or ["VOLUME_LOCKED"],
            lock_file=lock_path,
            report_file=str(report_file_str or default_report_path),
            repair_actions=recorded_repairs,
        )

    if not isinstance(report_file_str, str) or not report_file_str.strip():
        raise VolumeHealthLockError(
            f"禁止进入第 {target_volume} 卷：第 {prev_vol} 卷锁表记录缺少有效 report_file 路径。",
            volume_index=prev_vol,
            failed_checks=["MISSING_REPORT_FILE"],
            lock_file=lock_path,
            report_file=default_report_path,
            repair_actions=recorded_repairs,
        )

    report_path = Path(report_file_str)
    if not report_path.is_file() and not report_path.is_absolute():
        candidate = audit_path / report_path
        if candidate.is_file():
            report_path = candidate
    if not report_path.is_file():
        raise VolumeHealthLockError(
            f"禁止进入第 {target_volume} 卷：第 {prev_vol} 卷体检报告文件不存在（'{report_path}'）。",
            volume_index=prev_vol,
            failed_checks=["MISSING_REPORT_FILE"],
            lock_file=lock_path,
            report_file=report_path,
            repair_actions=[
                {
                    "check_id": "MISSING_REPORT_FILE",
                    "repair_hint": f"第 {prev_vol} 卷体检报告缺失，请重新执行第 {prev_vol} 卷体检以生成有效 PASS 报告。",
                }
            ],
        )

    try:
        report_data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolumeHealthLockError(
            f"禁止进入第 {target_volume} 卷：第 {prev_vol} 卷体检报告损坏或无法解析（'{report_path}': {exc}）。",
            volume_index=prev_vol,
            failed_checks=["CORRUPTED_REPORT_FILE"],
            lock_file=lock_path,
            report_file=report_path,
            repair_actions=recorded_repairs,
        ) from exc

    if not isinstance(report_data, dict):
        raise VolumeHealthLockError(
            f"禁止进入第 {target_volume} 卷：第 {prev_vol} 卷体检报告格式非法（非 JSON 对象）。",
            volume_index=prev_vol,
            failed_checks=["CORRUPTED_REPORT_FILE"],
            lock_file=lock_path,
            report_file=report_path,
            repair_actions=recorded_repairs,
        )

    rep_verdict = report_data.get("verdict")
    rep_lock_state = report_data.get("lock_state")
    rep_vol_idx = report_data.get("volume_index")
    rep_checks = report_data.get("checks")
    checks_valid = (
        isinstance(rep_checks, list)
        and {c.get("id") for c in rep_checks if isinstance(c, dict)} == set(VOLUME_HEALTH_CHECK_IDS)
        and all(isinstance(c, dict) and c.get("status") == "PASS" for c in rep_checks)
    )
    if (
        rep_verdict != "PASS"
        or rep_lock_state != LOCK_STATE_PASSED
        or rep_vol_idx != prev_vol
        or not checks_valid
    ):
        rep_failed = (
            [
                str(c.get("id"))
                for c in rep_checks
                if isinstance(c, dict) and c.get("status") != "PASS"
            ]
            if isinstance(rep_checks, list)
            else []
        )
        raise VolumeHealthLockError(
            f"禁止进入第 {target_volume} 卷：第 {prev_vol} 卷体检报告未通过校验 "
            f"(report_verdict={rep_verdict!r}, report_lock_state={rep_lock_state!r}, "
            f"report_volume={rep_vol_idx!r}, checks_valid={checks_valid})。",
            volume_index=prev_vol,
            failed_checks=rep_failed or ["REPORT_VERDICT_NOT_PASS"],
            lock_file=lock_path,
            report_file=report_path,
            repair_actions=list(report_data.get("repair_actions") or recorded_repairs),
        )


def run_volume_health_check(
    *,
    volume_spec: Any,
    chapters: Sequence[Tuple[int, str]],
    world_state_snapshot: Mapping[str, Any],
    project: Any,
    blueprint: Any,
    judge: PairwiseJudge,
    audit_dir: str | Path,
    prev_volume_spec: Any = None,
    core_selling_point_keywords: Optional[Sequence[str]] = None,
    prior_volume_chapters: Optional[Sequence[Tuple[int, str]]] = None,
) -> VolumeHealthReport:
    """Execute deterministic + cross-model semantic 6-point volume health audit and persist lock state."""
    if volume_spec is None:
        raise ValueError("volume_spec must not be None.")
    if project is None:
        raise ValueError("project must not be None.")
    if blueprint is None:
        raise ValueError("blueprint must not be None.")
    if not isinstance(judge, PairwiseJudge):
        raise TypeError(
            f"judge must be a PairwiseJudge instance (enforcing cross-family isolation), "
            f"got {type(judge).__name__}."
        )
    audit_path = _validate_audit_dir(audit_dir)

    vol_index = _field(volume_spec, "index")
    vol_title = _field(volume_spec, "title")
    ch_start = _field(volume_spec, "chapter_start")
    ch_end = _field(volume_spec, "chapter_end")
    if not _is_strict_int(vol_index) or vol_index < 1:
        raise ValueError(f"volume_spec.index must be an int >= 1, got {vol_index!r}.")
    if not isinstance(vol_title, str) or not vol_title.strip():
        raise ValueError(f"volume_spec.title must be a non-empty string, got {vol_title!r}.")
    if not _is_strict_int(ch_start) or ch_start < 1:
        raise ValueError(f"volume_spec.chapter_start must be an int >= 1, got {ch_start!r}.")
    if not _is_strict_int(ch_end) or ch_end < ch_start:
        raise ValueError(
            f"volume_spec.chapter_end ({ch_end!r}) must be an int >= chapter_start ({ch_start!r})."
        )

    sorted_chapters = _validate_chapters_pairs(chapters, param_name="chapters", allow_empty=False)
    sorted_prior: List[Tuple[int, str]] = (
        _validate_chapters_pairs(
            prior_volume_chapters, param_name="prior_volume_chapters", allow_empty=True
        )
        if prior_volume_chapters is not None
        else []
    )
    overlap_chs = sorted({c for c, _ in sorted_prior} & {c for c, _ in sorted_chapters})
    if overlap_chs:
        raise ValueError(
            f"prior_volume_chapters and chapters contain overlapping chapter numbers: {overlap_chs}."
        )

    snap = _normalize_world_state_snapshot(world_state_snapshot)
    project_characters = getattr(project, "characters", None)
    if not isinstance(project_characters, MappingABC):
        raise ValueError("project.characters must be a mapping of CharacterAnchor.")
    project_scenes = getattr(project, "scenes", None)
    if (
        project_scenes is None
        or isinstance(project_scenes, (str, bytes, bytearray))
        or not isinstance(project_scenes, SequenceABC)
    ):
        raise ValueError("project.scenes must be a sequence of SceneOutline.")

    bp_premise = getattr(blueprint, "premise", None)
    if bp_premise is None:
        raise ValueError("blueprint.premise is required.")
    selling_point_keywords = getattr(bp_premise, "selling_point_keywords", None)
    bp_scale_rules_raw = getattr(blueprint, "scale_rules", None)
    bp_volumes = getattr(blueprint, "volumes", None)
    bp_cast = getattr(blueprint, "cast", None)
    if bp_scale_rules_raw is None or bp_volumes is None or bp_cast is None:
        raise ValueError("blueprint must provide scale_rules, volumes, and cast.")

    scale_rules_dicts = [
        _to_mapping(r, label=f"blueprint.scale_rules[{idx}]")
        for idx, r in enumerate(bp_scale_rules_raw)
    ]

    # Validate numeric_balances strictly before passing anywhere.
    numeric_balances: Dict[str, float] = {}
    for k, v in snap["numeric_balances"].items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError(f"numeric_balances key must be non-empty str, got {k!r}.")
        if not _is_finite_number(v):
            raise ValueError(f"numeric_balances[{k!r}] must be a finite number, got {v!r}.")
        numeric_balances[k] = float(v)

    # ------------------------------------------------------------------
    # Stage 1: Deterministic Hard Checks (VH_01, VH_02, VH_03, VH_04, VH_06)
    # ------------------------------------------------------------------
    det_issues: Dict[str, List[str]] = {cid: [] for cid in VOLUME_HEALTH_CHECK_IDS}
    det_chapters: Dict[str, set[int]] = {cid: set() for cid in VOLUME_HEALTH_CHECK_IDS}
    det_hints: Dict[str, List[str]] = {cid: [] for cid in VOLUME_HEALTH_CHECK_IDS}
    fallback_chapter = sorted_chapters[-1][0]

    # 1A. L1 cross-chapter checks across merged (prior_volume_chapters + chapters) window.
    merged_window = sorted(sorted_prior + sorted_chapters, key=lambda pair: pair[0])
    l1_hits: List[RuleHit] = run_l1_checks(
        merged_window,
        characters=project_characters,
        blueprint_scale_rules=scale_rules_dicts,
        selling_point_keywords=selling_point_keywords,
        ledger_balances=numeric_balances,
        core_selling_point_keywords=core_selling_point_keywords,
    )

    for hit in l1_hits:
        rid = hit.rule_id
        hit_chs = [c for c in hit.chapters if _is_strict_int(c) and c >= 1] or [fallback_chapter]
        if rid in ("QR_ANCHOR_REPEAT", "QR_VERBATIM_REPEAT"):
            det_issues["VH_01_NO_ROUTINE_REPEAT"].append(f"[{rid}] {hit.evidence}")
            det_chapters["VH_01_NO_ROUTINE_REPEAT"].update(hit_chs)
            det_hints["VH_01_NO_ROUTINE_REPEAT"].append(
                f"返修第 {sorted(set(hit_chs))} 章，删除或改写触发 {rid} 的复读段落与高频口癖/动作锚点。"
            )
        elif rid in (
            "QR_SELLING_POINT_DRIFT",
            "QR_CORE_SELLING_POINT_DRIFT",
            "QR_SELLING_POINT_MONOPOLY",
        ):
            det_issues["VH_04_CORE_FLAVOR_QUOTA"].append(f"[{rid}] {hit.evidence}")
            det_chapters["VH_04_CORE_FLAVOR_QUOTA"].update(hit_chs)
            det_hints["VH_04_CORE_FLAVOR_QUOTA"].append(
                f"返修第 {sorted(set(hit_chs))} 章，补强核心卖点关键词覆盖并消除单一词垄断（{rid}）。"
            )
        elif rid == "QR_SCALE":
            # Ledger exceedances are also verified directly below in VH_06 with full unit/cap info;
            # only add prose_scale_yi here so evidence is never duplicated.
            kind = hit.value.get("kind") if isinstance(hit.value, MappingABC) else None
            if kind != "ledger_exceeded":
                det_issues["VH_06_LEDGER_AND_DEBT_MATH"].append(f"[{rid}] {hit.evidence}")
                det_chapters["VH_06_LEDGER_AND_DEBT_MATH"].update(hit_chs)
                det_hints["VH_06_LEDGER_AND_DEBT_MATH"].append(
                    f"返修第 {sorted(set(hit_chs))} 章，修正违背现实量级的夸张数值描写（{rid}）。"
                )
        elif str(hit.severity).upper() == "CRITICAL":
            det_issues["VH_01_NO_ROUTINE_REPEAT"].append(f"[{rid}] {hit.evidence}")
            det_chapters["VH_01_NO_ROUTINE_REPEAT"].update(hit_chs)
            det_hints["VH_01_NO_ROUTINE_REPEAT"].append(
                f"返修第 {sorted(set(hit_chs))} 章，修复 L1 硬规违规项 {rid}。"
            )

    # 1B. VH_02: Foreshadow plant & payoff closure verification.
    snap_foreshadows = snap["foreshadows"]
    project_foreshadows = getattr(project, "foreshadows", {}) or {}
    fs_payoff_ids = list(_field(volume_spec, "foreshadows_payoff", []) or [])
    fs_plant_ids = list(_field(volume_spec, "foreshadows_plant", []) or [])

    def _fs_chapter_hint(fs_id: str, attr_name: str) -> int:
        raw_snap = snap_foreshadows.get(fs_id)
        ch_val = _field(raw_snap, attr_name) if raw_snap is not None else None
        if not _is_strict_int(ch_val) or not (ch_start <= ch_val <= ch_end):
            raw_proj = project_foreshadows.get(fs_id) if isinstance(project_foreshadows, MappingABC) else None
            ch_val = _field(raw_proj, attr_name) if raw_proj is not None else None
        if _is_strict_int(ch_val) and ch_start <= ch_val <= ch_end:
            return int(ch_val)
        return fallback_chapter

    for fs_id in fs_payoff_ids:
        if fs_id not in snap_foreshadows:
            ch_hint = _fs_chapter_hint(fs_id, "due_chapter")
            det_issues["VH_02_FORESHADOW_AND_REWARD"].append(
                f"本卷应回收伏笔 {fs_id!r} 在 world_state_snapshot['foreshadows'] 中缺失"
            )
            det_chapters["VH_02_FORESHADOW_AND_REWARD"].add(ch_hint)
            det_hints["VH_02_FORESHADOW_AND_REWARD"].append(
                f"补全并兑现伏笔 {fs_id!r}，确保世界状态快照中 status=='resolved'。"
            )
        else:
            status = _field(snap_foreshadows[fs_id], "status")
            if status != "resolved":
                ch_hint = _fs_chapter_hint(fs_id, "due_chapter")
                det_issues["VH_02_FORESHADOW_AND_REWARD"].append(
                    f"本卷应回收伏笔 {fs_id!r} 当前状态为 {status!r}（必须为 'resolved'）"
                )
                det_chapters["VH_02_FORESHADOW_AND_REWARD"].add(ch_hint)
                det_hints["VH_02_FORESHADOW_AND_REWARD"].append(
                    f"在第 {ch_hint} 章或卷尾前完成伏笔 {fs_id!r} 的物理证据兑现，使 status 推进为 'resolved'。"
                )

    for fs_id in fs_plant_ids:
        if fs_id not in snap_foreshadows:
            ch_hint = _fs_chapter_hint(fs_id, "plant_chapter")
            det_issues["VH_02_FORESHADOW_AND_REWARD"].append(
                f"本卷应埋设伏笔 {fs_id!r} 在 world_state_snapshot['foreshadows'] 中缺失"
            )
            det_chapters["VH_02_FORESHADOW_AND_REWARD"].add(ch_hint)
            det_hints["VH_02_FORESHADOW_AND_REWARD"].append(
                f"补全并埋设伏笔 {fs_id!r}，确保世界状态快照中 status 为 'planted' 或 'resolved'。"
            )
        else:
            status = _field(snap_foreshadows[fs_id], "status")
            if status not in ("planted", "resolved"):
                ch_hint = _fs_chapter_hint(fs_id, "plant_chapter")
                det_issues["VH_02_FORESHADOW_AND_REWARD"].append(
                    f"本卷应埋设伏笔 {fs_id!r} 当前状态为 {status!r}（必须为 'planted' 或 'resolved'）"
                )
                det_chapters["VH_02_FORESHADOW_AND_REWARD"].add(ch_hint)
                det_hints["VH_02_FORESHADOW_AND_REWARD"].append(
                    f"在第 {ch_hint} 章补写伏笔 {fs_id!r} 的物理埋设证据，使 status 不再停留于 {status!r}。"
                )

    # 1C. VH_03: Exited prior-volume characters must have 0 appearances in current volume scenes.
    cast_name_by_id: Dict[str, str] = {}
    for member in bp_cast:
        mid = _field(member, "id")
        mname = _field(member, "name", "")
        if isinstance(mid, str) and mid.strip():
            cast_name_by_id[mid] = str(mname or mid)
    for cid, char in project_characters.items():
        if cid not in cast_name_by_id:
            cast_name_by_id[cid] = str(getattr(char, "name", cid) or cid)

    exited_before_vol: Dict[str, int] = {}
    for vol in bp_volumes:
        v_idx = _field(vol, "index")
        if _is_strict_int(v_idx) and v_idx < vol_index:
            for ex in (_field(vol, "exits", []) or []):
                ex_id = _field(ex, "id")
                if isinstance(ex_id, str) and ex_id.strip():
                    prev_recorded = exited_before_vol.get(ex_id)
                    if prev_recorded is None or v_idx < prev_recorded:
                        exited_before_vol[ex_id] = int(v_idx)
    for member in bp_cast:
        mid = _field(member, "id")
        ev = _field(member, "exit_volume")
        if isinstance(mid, str) and mid.strip() and _is_strict_int(ev) and ev < vol_index:
            prev_recorded = exited_before_vol.get(mid)
            if prev_recorded is None or ev < prev_recorded:
                exited_before_vol[mid] = int(ev)

    volume_scene_char_ids: set[str] = set()
    exited_violations: Dict[str, List[int]] = {}
    for scene in project_scenes:
        sc_ch = _field(scene, "chapter")
        if not _is_strict_int(sc_ch) or not (ch_start <= sc_ch <= ch_end):
            continue
        active_list = list(_field(scene, "active_characters", []) or [])
        pov_cid = _field(scene, "pov_character", "")
        scene_cids: List[str] = []
        for cid in active_list:
            if isinstance(cid, str) and cid and cid not in scene_cids:
                scene_cids.append(cid)
        if isinstance(pov_cid, str) and pov_cid and pov_cid not in scene_cids:
            scene_cids.append(pov_cid)
        volume_scene_char_ids.update(scene_cids)
        for cid in scene_cids:
            if cid in exited_before_vol:
                exited_violations.setdefault(cid, []).append(int(sc_ch))

    for cid, viol_chs in sorted(exited_violations.items()):
        sorted_vchs = sorted(set(viol_chs))
        cname = cast_name_by_id.get(cid, cid)
        exit_v = exited_before_vol[cid]
        det_issues["VH_03_CHARACTER_AND_ARC"].append(
            f"前卷已退场角色「{cname}」({cid}，第 {exit_v} 卷已退场) 越界出现在本卷章纲 active_characters 中（章号 {sorted_vchs}）"
        )
        det_chapters["VH_03_CHARACTER_AND_ARC"].update(sorted_vchs)
        det_hints["VH_03_CHARACTER_AND_ARC"].append(
            f"从第 {sorted_vchs} 章的 active_characters 与正文中移除已退场角色「{cname}」({cid})，改由本卷在场角色承接戏份。"
        )

    # 1D. VH_06: Numeric ledger balances (no negatives, <= scale_rules hard_max, <= volume_cap_locks).
    for ledger_key, bal in sorted(numeric_balances.items()):
        if bal < 0.0:
            det_issues["VH_06_LEDGER_AND_DEBT_MATH"].append(
                f"数值台账 {ledger_key!r} 余额为负值 {_fmt_number(bal)}（严禁小于 0）"
            )
            det_chapters["VH_06_LEDGER_AND_DEBT_MATH"].add(fallback_chapter)
            det_hints["VH_06_LEDGER_AND_DEBT_MATH"].append(
                f"核对并修正台账 {ledger_key!r} 的收支扣减（当前 {_fmt_number(bal)}），确保全卷余额非负。"
            )

    for rule in scale_rules_dicts:
        key = str(rule.get("ledger_key") or "").strip()
        policy = str(rule.get("policy") or "")
        hard_max = rule.get("hard_max")
        unit = str(rule.get("unit") or "")
        if not key or hard_max is None:
            continue
        if not _is_finite_number(hard_max):
            raise ValueError(
                f"blueprint.scale_rules[{key!r}].hard_max must be finite numeric or None, got {hard_max!r}."
            )
        bal = numeric_balances.get(key, 0.0)
        if bal > float(hard_max):
            det_issues["VH_06_LEDGER_AND_DEBT_MATH"].append(
                f"数值台账 {key!r} 当前值 {_fmt_number(bal)}{unit} 超过全书 scale_rules({policy}) "
                f"硬上限 {_fmt_number(float(hard_max))}{unit}"
            )
            det_chapters["VH_06_LEDGER_AND_DEBT_MATH"].add(fallback_chapter)
            det_hints["VH_06_LEDGER_AND_DEBT_MATH"].append(
                f"回调本卷台账 {key!r} 增量，使当前值 {_fmt_number(bal)}{unit} 不超过硬上限 "
                f"{_fmt_number(float(hard_max))}{unit}。"
            )

    vol_locks = _collect_volume_cap_locks(snap, project, volume_index=vol_index)
    for lock in vol_locks:
        if lock["unlocked"]:
            continue
        key = lock["ledger_key"]
        cap = lock["max_value"]
        unit = lock["unit"]
        bal = numeric_balances.get(key, 0.0)
        if bal > cap:
            det_issues["VH_06_LEDGER_AND_DEBT_MATH"].append(
                f"数值台账 {key!r} 当前值 {_fmt_number(bal)}{unit} 超过第 {vol_index} 卷 "
                f"volume_cap_locks 上限 {_fmt_number(cap)}{unit}（未触发突破解锁）"
            )
            det_chapters["VH_06_LEDGER_AND_DEBT_MATH"].add(fallback_chapter)
            det_hints["VH_06_LEDGER_AND_DEBT_MATH"].append(
                f"下调第 {vol_index} 卷台账 {key!r} 数值至 <= {_fmt_number(cap)}{unit}，或按设定补齐突破解锁节点。"
            )

    # ------------------------------------------------------------------
    # Stage 2: Build Context & Invoke Cross-Model Strong Judge
    # ------------------------------------------------------------------
    volume_card = _extract_volume_card(volume_spec, label="volume_spec")
    prev_volume_card = (
        _extract_volume_card(prev_volume_spec, label="prev_volume_spec")
        if prev_volume_spec is not None
        else None
    )

    vol_enters = set(_field(volume_spec, "enters", []) or [])
    vol_exits = {
        _field(ex, "id")
        for ex in (_field(volume_spec, "exits", []) or [])
        if _field(ex, "id")
    }
    vol_antagonist = _field(volume_spec, "antagonist_id", "")
    relevant_cast_cards: List[Dict[str, Any]] = []
    for member in bp_cast:
        mid = _field(member, "id", "")
        first_v = _field(member, "first_volume", 1)
        exit_v = _field(member, "exit_volume", None)
        in_span = (
            _is_strict_int(first_v)
            and first_v <= vol_index
            and (exit_v is None or (_is_strict_int(exit_v) and exit_v >= vol_index))
        )
        referenced = (
            mid in vol_enters
            or mid in vol_exits
            or mid == vol_antagonist
            or mid in volume_scene_char_ids
        )
        if in_span or referenced:
            relevant_cast_cards.append(_extract_cast_card(member))

    deterministic_summary: Dict[str, Any] = {
        "volume_index": vol_index,
        "chapter_start": ch_start,
        "chapter_end": ch_end,
        "evaluated_chapters": [ch for ch, _ in sorted_chapters],
        "prior_window_chapters": [ch for ch, _ in sorted_prior],
        "u1_volume_hints": {
            "antagonist_motive": str(getattr(volume_spec, "antagonist_motive", "") or ""),
            "new_gameplay": str(getattr(volume_spec, "new_gameplay", "") or ""),
            "mainline_step": str(getattr(volume_spec, "mainline_step", "") or ""),
            "daily_case_quota": getattr(volume_spec, "daily_case_quota", None),
            "fugitive_case_quota": getattr(volume_spec, "fugitive_case_quota", None),
            "forbidden_from_prev_volume": list(
                getattr(volume_spec, "forbidden_from_prev_volume", []) or []
            ),
        },
        "checks": {
            cid: {
                "passed": len(det_issues[cid]) == 0,
                "issues": list(det_issues[cid]),
                "chapters": sorted(det_chapters[cid]),
            }
            for cid in VOLUME_HEALTH_CHECK_IDS
        },
        "l1_hits": [h.to_dict() for h in l1_hits],
        "numeric_balances": dict(numeric_balances),
    }

    judge_result = judge.volume_health_review(
        [text for _, text in sorted_chapters],
        volume_card,
        prev_volume_card=prev_volume_card,
        cast_cards=relevant_cast_cards,
        deterministic_summary=deterministic_summary,
    )

    # ------------------------------------------------------------------
    # Stage 3: Merge Deterministic & Semantic Verdicts + Atomic Persist
    # ------------------------------------------------------------------
    sem_by_id: Dict[str, Dict[str, Any]] = {
        str(item["id"]): item for item in judge_result.checks
    }

    merged_checks: List[VolumeHealthCheckItem] = []
    repair_actions: List[Dict[str, Any]] = []
    det_failed_ids: List[str] = []

    for cid in VOLUME_HEALTH_CHECK_IDS:
        sem_item = sem_by_id[cid]
        det_passed = len(det_issues[cid]) == 0
        sem_passed = sem_item["status"] == "PASS"
        if not det_passed:
            det_failed_ids.append(cid)

        final_status = "PASS" if (det_passed and sem_passed) else "FAIL"
        sem_chs = [c for c in sem_item["chapters"] if _is_strict_int(c) and c >= 1]
        combined_chs = sorted(det_chapters[cid] | set(sem_chs))
        if final_status == "FAIL" and not combined_chs:
            combined_chs = [fallback_chapter]

        sem_ev = str(sem_item["evidence"]).strip()
        if not det_passed and not sem_passed:
            evidence = f"[确定性失败] {'；'.join(det_issues[cid])} | [语义复核失败] {sem_ev}"
        elif not det_passed:
            evidence = f"[确定性失败] {'；'.join(det_issues[cid])} | [语义复核] {sem_ev}"
        elif not sem_passed:
            evidence = f"[语义复核失败] {sem_ev}"
        else:
            evidence = sem_ev

        if final_status == "PASS":
            repair_hint = ""
        else:
            hint_parts: List[str] = []
            if not det_passed:
                for h in det_hints[cid]:
                    if h and h not in hint_parts:
                        hint_parts.append(h)
            if not sem_passed:
                sem_hint = str(sem_item.get("repair_hint") or "").strip()
                if sem_hint and sem_hint not in hint_parts:
                    hint_parts.append(sem_hint)
            repair_hint = "；".join(hint_parts)

        check_item = VolumeHealthCheckItem(
            id=cid,
            title=VOLUME_HEALTH_CHECK_TITLES[cid],
            status=final_status,
            deterministic_passed=det_passed,
            semantic_passed=sem_passed,
            chapters=combined_chs,
            evidence=evidence,
            repair_hint=repair_hint,
        )
        merged_checks.append(check_item)

        if final_status == "FAIL":
            source = (
                "both"
                if (not det_passed and not sem_passed)
                else ("deterministic" if not det_passed else "semantic")
            )
            repair_actions.append(
                {
                    "check_id": cid,
                    "title": check_item.title,
                    "source": source,
                    "chapters": list(combined_chs),
                    "evidence": evidence,
                    "repair_hint": repair_hint,
                }
            )

    if len(repair_actions) == 0:
        verdict = "PASS"
        lock_state = LOCK_STATE_PASSED
        summary = judge_result.summary
    else:
        verdict = "FAIL"
        lock_state = LOCK_STATE_LOCKED
        if det_failed_ids and judge_result.verdict == "PASS":
            summary = f"[确定性硬核验未通过: {', '.join(det_failed_ids)}] {judge_result.summary}"
        else:
            summary = judge_result.summary

    report = VolumeHealthReport(
        volume_index=vol_index,
        volume_title=vol_title,
        chapter_start=ch_start,
        chapter_end=ch_end,
        verdict=verdict,
        lock_state=lock_state,
        checks=merged_checks,
        repair_actions=repair_actions,
        summary=summary,
        judge_model=judge.judge_model,
        writer_model=judge.writer_model,
        report_file="",
        lock_file="",
        recorded_at=_now_shanghai_iso(),
    )
    save_volume_health_result(audit_path, report)
    return report


__all__ = [
    "LOCK_STATE_LOCKED",
    "LOCK_STATE_PASSED",
    "VOLUME_HEALTH_CHECK_IDS",
    "VOLUME_HEALTH_CHECK_TITLES",
    "VolumeHealthCheckItem",
    "VolumeHealthLockError",
    "VolumeHealthReport",
    "assert_previous_volume_unlocked",
    "load_volume_locks",
    "run_volume_health_check",
    "save_volume_health_result",
]
