from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import AbstractSet, Any, Callable, Dict, List, Optional, Sequence, Tuple

from .blueprint import BookBlueprint, VolumeSpec
from .checker import AITicChecker
from .compiler import PromptCompiler
from .contracts import (
    SSE_CHAPTER_ATTEMPT,
    SSE_CHAPTER_STARTED,
    SSE_CHAPTER_STOCKPILED,
    SSE_CRITIC_SCORED,
    SSE_GATE_FAILED,
    SSE_GATE_PASSED,
    SSE_PIPELINE_GATE_REPORT,
    SSE_PIPELINE_STAGE,
    SSE_SURGICAL_POLISHED,
)
from .critic_polisher import (
    AuthorNoteViolationError,
    CriticEvaluation,
    CriticResponseParseError,
    SurgicalPolishGateError,
    SurgicalPolishResult,
    TomatoCriticPolisher,
)
from .judge import PairwiseJudge
from .llm.base import LLMError, LLMProvider, LLMRequest, LLMStreamTruncatedError
from .llm.router import RoleModelRouter
from .memory import MemoryMigrationError, MemoryPyramid, migrate_legacy_memory
from .models import (
    AmbiguousChapterArtifactError,
    ChapterArtifact,
    NovelProject,
    ProductionIncident,
    SHANGHAI_TZ,
    ScanReport,
    SceneOutline,
    Violation,
    format_chapter_filename,
    resolve_chapter_path,
)
from .quality_rules import RuleHit, _validate_keywords, run_l1_checks
from .reporter import generate_html_dashboard, generate_markdown_report
from .state import (
    ForeshadowEvidenceMissingError,
    LockedChapterTamperedError,
    WorldStateError,
    WorldStateManager,
)
from .volume_health import (
    LOCK_STATE_PASSED,
    VolumeHealthLockError,
    VolumeHealthReport,
    assert_previous_volume_unlocked,
    load_volume_locks,
    run_volume_health_check,
)

ProgressCallback = Callable[[str, Dict[str, Any]], None]

_CHAPTER_TXT_RE = re.compile(r"^chapter_(\d+)\.txt$")
_WALL_CHARS_RE = re.compile(r"(\d+)\s*chars\s*>\s*(\d+)")
_BURST_SIGMA_RE = re.compile(r"σ\s*=\s*([0-9]+(?:\.[0-9]+)?)")


def build_public_gate_rules(
    report: ScanReport,
    scene: SceneOutline,
    *,
    due_count: int = 0,
    plant_count: int = 0,
) -> List[Dict[str, Any]]:
    """Convert a ScanReport into strictly sanitized rule-by-rule check rows (ZERO draft prose)."""
    violations = list(report.violations or [])
    crit_violations = [v for v in violations if str(v.severity).upper() == "CRITICAL"]

    len1_fail = any(v.rule_id == "LEN_001" for v in crit_violations)
    len2_fail = any(v.rule_id == "LEN_002" for v in crit_violations)

    fs_fails = [
        v for v in crit_violations
        if str(v.rule_id).startswith(("FS_", "FORESHADOW")) or "FORESHADOW" in str(v.category).upper()
    ]
    fs_total = max(0, int(due_count) + int(plant_count))
    if fs_total > 0:
        fs_ok = max(0, fs_total - len(fs_fails))
        fs_metric = f"{fs_ok}/{fs_total} 达标"
    elif fs_fails:
        fs_metric = f"{len(fs_fails)} 项缺失"
    else:
        fs_metric = "0 缺失"

    wall_fails = [v for v in crit_violations if v.rule_id == "RHYTHM_NARRATION_WALL"]
    wall_max_chars = 0
    wall_limit = 320
    for wf in wall_fails:
        m = _WALL_CHARS_RE.search(str(wf.matched_text or ""))
        if m:
            wall_max_chars = max(wall_max_chars, int(m.group(1)))
            wall_limit = int(m.group(2))
    wall_metric = (
        f"{wall_max_chars} / ≤{wall_limit} 字"
        if wall_fails and wall_max_chars > 0
        else (f"{len(wall_fails)} 处超限" if wall_fails else f"≤{wall_limit} 字")
    )

    burst_warns = [v for v in violations if v.rule_id == "RHYTHM_BURSTINESS"]
    burst_metric = "达标"
    if burst_warns:
        m_sig = _BURST_SIGMA_RE.search(str(burst_warns[0].matched_text or ""))
        burst_metric = f"σ={m_sig.group(1)} < 10.0" if m_sig else "偏均匀"

    ai_tic_fails = [
        v for v in crit_violations
        if str(v.rule_id).startswith(("TIC_", "SUM_", "SYM_", "AI_TIC"))
    ]
    world_fails = [
        v for v in crit_violations
        if str(v.rule_id).startswith(("STATE_", "WORLD_", "ASSET_", "LEDGER_"))
        or "WORLD_STATE" in str(v.category).upper()
    ]
    handled_ids = {
        id(v)
        for v in (
            [x for x in crit_violations if x.rule_id in ("LEN_001", "LEN_002")]
            + fs_fails
            + wall_fails
            + ai_tic_fails
            + world_fails
        )
    }
    other_style_fails = [v for v in crit_violations if id(v) not in handled_ids]

    return [
        {
            "rule_id": "LEN_001",
            "label": "字数下限",
            "passed": not len1_fail,
            "metric": f"{int(report.char_count)} / ≥{int(scene.min_chars)} 字",
            "severity": "CRITICAL",
        },
        {
            "rule_id": "LEN_002",
            "label": "字数上限",
            "passed": not len2_fail,
            "metric": f"{int(report.char_count)} / ≤{int(scene.max_chars)} 字",
            "severity": "CRITICAL",
        },
        {
            "rule_id": "FORESHADOW",
            "label": "伏笔埋设/兑现",
            "passed": len(fs_fails) == 0,
            "metric": fs_metric,
            "severity": "CRITICAL",
        },
        {
            "rule_id": "RHYTHM_NARRATION_WALL",
            "label": "纯旁白墙",
            "passed": len(wall_fails) == 0,
            "metric": wall_metric,
            "severity": "CRITICAL",
        },
        {
            "rule_id": "RHYTHM_BURSTINESS",
            "label": "句长爆发度",
            "passed": len(burst_warns) == 0,
            "metric": burst_metric,
            "severity": "WARNING",
        },
        {
            "rule_id": "AI_TIC",
            "label": "AI 腔/套话词",
            "passed": len(ai_tic_fails) == 0,
            "metric": "0 违规" if not ai_tic_fails else f"{len(ai_tic_fails)} 处违规",
            "severity": "CRITICAL",
        },
        {
            "rule_id": "POV_STYLE",
            "label": "视角与格式红线",
            "passed": len(other_style_fails) == 0,
            "metric": "0 违规" if not other_style_fails else f"{len(other_style_fails)} 处违规",
            "severity": "CRITICAL",
        },
        {
            "rule_id": "WORLD_STATE",
            "label": "世界状态一致",
            "passed": len(world_fails) == 0,
            "metric": "0 违规" if not world_fails else f"{len(world_fails)} 处违规",
            "severity": "CRITICAL",
        },
    ]


class ProductionGateFailure(RuntimeError):
    """Raised when a chapter still fails AI-TIC-CHECK after exhausting rewrite retries."""


class ConsecutiveGateFailureBrakeError(ProductionGateFailure):
    """Raised when consecutive chapter gate failures reach max_consecutive_failures."""

    def __init__(
        self,
        message: str,
        *,
        failed_chapters: Optional[List[int]] = None,
        consecutive_failures: int = 0,
    ) -> None:
        super().__init__(message)
        self.failed_chapters: List[int] = list(failed_chapters or [])
        self.consecutive_failures: int = int(consecutive_failures)


class ArcReviewBrakeError(ProductionGateFailure):
    """Raised when cross-model volume arc review returns verdict == 'FAIL'."""

    def __init__(
        self,
        message: str,
        *,
        chapter: int = 0,
        verdict: Optional[Any] = None,
        audit_file: str = "",
    ) -> None:
        super().__init__(message)
        self.chapter: int = int(chapter)
        self.verdict = verdict
        self.audit_file: str = str(audit_file)


class MissingPredecessorSnapshotError(RuntimeError):
    """Raised when a partial --chapters N (N > 1) run lacks chapter N-1 world-state snapshot."""


class LockedChapterError(RuntimeError):
    """Raised when attempting to overwrite a locked chapter without explicit unlock=True."""


def _validate_positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an int >= 1, got {value!r}.")
    return int(value)


def _now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def _now_shanghai_stamp() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")


def _compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _archive_file(src: Path, output_dir: Path, stamp: str) -> Optional[Path]:
    if not src.exists():
        return None
    try:
        rel = src.resolve().relative_to(output_dir.resolve())
    except ValueError:
        rel = Path(src.name)
    dest = output_dir / "_archive" / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def _write_text_atomic(
    path: Path,
    content: str,
    *,
    output_dir: Optional[Path] = None,
    archive_stamp: Optional[str] = None,
    require_nonempty: bool = False,
) -> None:
    if require_nonempty and not content.strip():
        raise ValueError(f"Refusing to write empty text artifact to '{path}'")
    path.parent.mkdir(parents=True, exist_ok=True)
    if archive_stamp and output_dir is not None and path.exists():
        _archive_file(path, output_dir, archive_stamp)
    # Unique temp file in the same directory + fsync so a crash never leaves a torn target
    # and concurrent writers never collide on a shared '<name>.tmp'.
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


def _write_json_atomic(
    path: Path,
    payload: Any,
    *,
    output_dir: Optional[Path] = None,
    archive_stamp: Optional[str] = None,
) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # Validate round-trip JSON before replacing target
    json.loads(serialized)
    _write_text_atomic(
        path,
        serialized,
        output_dir=output_dir,
        archive_stamp=archive_stamp,
        require_nonempty=True,
    )


def _resolve_snapshot_path(snapshots_dir: Path, chapter: int) -> Optional[Path]:
    if chapter == 0:
        p4 = snapshots_dir / "chapter_0000_initial.json"
        p2 = snapshots_dir / "chapter_00_initial.json"
        if p4.exists() and p2.exists():
            raise AmbiguousChapterArtifactError(
                f"Ambiguous initial snapshot: both '{p4}' and '{p2}' exist"
            )
        if p4.exists():
            return p4
        if p2.exists():
            return p2
        return None

    p4 = snapshots_dir / f"chapter_{chapter:04d}_after.json"
    p2 = snapshots_dir / f"chapter_{chapter:02d}_after.json"
    if p4 != p2 and p4.exists() and p2.exists():
        raise AmbiguousChapterArtifactError(
            f"Ambiguous snapshot artifacts for chapter {chapter}: both '{p4}' and '{p2}' exist"
        )
    if p4.exists():
        return p4
    if p2.exists():
        return p2
    return None


def _resolve_memory_path(snapshots_dir: Path, chapter: int) -> Path:
    if chapter == 0:
        return snapshots_dir / "memory_0000_initial.json"
    return snapshots_dir / f"memory_{chapter:04d}_after.json"


def _chapter_meta_path(chapters_dir: Path, chapter: int) -> Path:
    return chapters_dir / f"chapter_{chapter:04d}.meta.json"


def _scan_completed_chapters(chapters_dir: Path) -> List[Tuple[int, Path]]:
    if not chapters_dir.is_dir():
        return []
    chapter_nums: set[int] = set()
    for p in chapters_dir.iterdir():
        if not p.is_file():
            continue
        m = _CHAPTER_TXT_RE.match(p.name)
        if m:
            chapter_nums.add(int(m.group(1)))
    resolved: List[Tuple[int, Path]] = []
    for ch_no in sorted(chapter_nums):
        if ch_no <= 0:
            continue
        p = resolve_chapter_path(chapters_dir, ch_no)
        if p is not None:
            resolved.append((ch_no, p))
    return resolved


class NovelGenerationEngine:
    """End-to-end Asset-Driven Novel Generation, Gate, Surgical Polish, and State Ledger Engine 2.0."""

    def __init__(
        self,
        project: NovelProject,
        provider: LLMProvider | RoleModelRouter,
        output_dir: str | Path,
        checker: Optional[AITicChecker] = None,
        *,
        critic_polisher: Optional[TomatoCriticPolisher] = None,
        enable_critic_polish: Optional[bool] = None,
        re_evaluate_after_polish: bool = True,
        memory_pyramid: Optional[MemoryPyramid] = None,
        on_progress: Optional[ProgressCallback] = None,
        blueprint: Optional[BookBlueprint] = None,
        core_selling_point_keywords: Optional[Sequence[str]] = None,
        arc_judge: Optional[PairwiseJudge] = None,
        arc_review_interval: int = 10,
        trial_checkpoint_interval: Optional[int] = None,
        max_consecutive_failures: int = 2,
        volume_health_judge: Optional[PairwiseJudge] = None,
        enforce_volume_health_gate: Optional[bool] = None,
    ) -> None:
        if blueprint is not None and not isinstance(blueprint, BookBlueprint):
            raise ValueError(
                f"blueprint must be a BookBlueprint or None, got {type(blueprint).__name__}."
            )
        validated_core_keywords: Optional[List[str]] = (
            _validate_keywords(
                core_selling_point_keywords, param_name="core_selling_point_keywords"
            )
            if core_selling_point_keywords is not None
            else None
        )
        if arc_judge is not None and not callable(getattr(arc_judge, "arc_review", None)):
            raise ValueError(
                "arc_judge must implement arc_review(chapters, volume_card) -> ArcVerdict."
            )
        if volume_health_judge is not None and not isinstance(volume_health_judge, PairwiseJudge):
            raise ValueError(
                "volume_health_judge must be a PairwiseJudge instance or None, "
                f"got {type(volume_health_judge).__name__}."
            )
        if enforce_volume_health_gate is not None and not isinstance(
            enforce_volume_health_gate, bool
        ):
            raise ValueError(
                "enforce_volume_health_gate must be a bool or None, "
                f"got {type(enforce_volume_health_gate).__name__}."
            )
        if enforce_volume_health_gate is True:
            if blueprint is None:
                raise ValueError(
                    "enforce_volume_health_gate=True requires blueprint to be provided."
                )
            if volume_health_judge is None and arc_judge is None:
                raise ValueError(
                    "enforce_volume_health_gate=True requires volume_health_judge or arc_judge to be provided."
                )
            effective_enforce_volume_health_gate = True
        elif enforce_volume_health_gate is False:
            effective_enforce_volume_health_gate = False
        else:
            effective_enforce_volume_health_gate = bool(
                blueprint is not None and volume_health_judge is not None
            )

        resolved_volume_health_judge: Optional[PairwiseJudge] = (
            volume_health_judge
            if volume_health_judge is not None
            else (arc_judge if effective_enforce_volume_health_gate else None)
        )
        if effective_enforce_volume_health_gate and not isinstance(
            resolved_volume_health_judge, PairwiseJudge
        ):
            raise ValueError(
                "Volume health gate requires a PairwiseJudge instance for volume_health_judge/arc_judge, "
                f"got {type(resolved_volume_health_judge).__name__}."
            )

        validated_arc_interval = _validate_positive_int(
            arc_review_interval, name="arc_review_interval"
        )
        validated_trial_interval: Optional[int] = (
            _validate_positive_int(
                trial_checkpoint_interval, name="trial_checkpoint_interval"
            )
            if trial_checkpoint_interval is not None
            else None
        )
        validated_max_consecutive = _validate_positive_int(
            max_consecutive_failures, name="max_consecutive_failures"
        )

        self.project = project
        self.provider = provider
        if isinstance(provider, RoleModelRouter):
            self.router: Optional[RoleModelRouter] = provider
            self.writer_provider: LLMProvider = provider.as_provider("writer")
        else:
            self.router = None
            self.writer_provider = provider

        self.output_dir = Path(output_dir).resolve()
        self.checker = checker or AITicChecker(
            pov_mode=project.meta.pov_mode,
            panel_policy=project.meta.panel_policy,
            contract_track=project.meta.contract_track,
        )
        self.compiler = PromptCompiler(project)
        self.state = WorldStateManager(project)
        self.memory = memory_pyramid or MemoryPyramid()
        for cid, ch_obj in project.characters.items():
            self.memory.register_entity_name(cid, ch_obj.name, "character")
        for aid, asset_obj in project.assets.items():
            self.memory.register_entity_name(aid, asset_obj.name, "asset")

        if critic_polisher is not None:
            self.critic_polisher: Optional[TomatoCriticPolisher] = critic_polisher
        elif enable_critic_polish is True:
            self.critic_polisher = TomatoCriticPolisher(provider, checker=self.checker)
        elif (
            enable_critic_polish is None
            and self.router is not None
            and self.router.has_role("critic")
            and self.router.has_role("polisher")
        ):
            self.critic_polisher = TomatoCriticPolisher(self.router, checker=self.checker)
        else:
            self.critic_polisher = None

        self.re_evaluate_after_polish = bool(re_evaluate_after_polish)
        self._default_on_progress = on_progress
        self.blueprint: Optional[BookBlueprint] = blueprint
        self.core_selling_point_keywords: Optional[List[str]] = validated_core_keywords
        self.arc_judge: Optional[PairwiseJudge] = arc_judge
        self.arc_review_interval: int = validated_arc_interval
        self.trial_checkpoint_interval: Optional[int] = validated_trial_interval
        self.max_consecutive_failures: int = validated_max_consecutive
        self.enforce_volume_health_gate: bool = effective_enforce_volume_health_gate
        self.volume_health_judge: Optional[PairwiseJudge] = resolved_volume_health_judge
        self.incidents: List[ProductionIncident] = []

    def _emit(
        self,
        callback: Optional[ProgressCallback],
        event: str,
        payload: Dict[str, Any],
    ) -> None:
        cb = callback or self._default_on_progress
        if cb is not None:
            enriched = {
                "project_id": self.project.meta.project_id,
                "timestamp": _now_shanghai_iso(),
                **payload,
            }
            cb(event, enriched)

    def _entity_names_map(self) -> Dict[str, str]:
        names: Dict[str, str] = {cid: c.name for cid, c in self.project.characters.items()}
        for aid, a in self.project.assets.items():
            names[aid] = a.name
        return names

    @staticmethod
    def _writer_max_tokens(scene: SceneOutline) -> int:
        """Size the writer output budget from the scene char ceiling (CJK ~1+ token/char + headroom)."""
        max_chars = int(getattr(scene, "max_chars", 0) or 0)
        return max(6000, int(max_chars * 2.5) + 2000)

    def _validate_state_gate(self, scene: SceneOutline, text: str) -> Optional[Violation]:
        """Run WorldStateManager.validate_chapter without mutating state.

        Text-dependent failures (missing foreshadow/expectation evidence, locked-chapter hash
        mismatch) are returned as a CRITICAL Violation so the rewrite loop can fix them.
        Outline-level WorldStateErrors propagate (they can't be fixed by rewriting).
        """
        try:
            self.state.validate_chapter(scene, text)
        except (ForeshadowEvidenceMissingError, LockedChapterTamperedError) as exc:
            rule = (
                "STATE_LOCKED_CHAPTER_TAMPERED"
                if isinstance(exc, LockedChapterTamperedError)
                else "STATE_EVIDENCE_MISSING"
            )
            return Violation(
                rule_id=rule,
                category="STATE_CONSISTENCY_VIOLATION",
                severity="CRITICAL",
                matched_text=str(exc)[:300],
                line_number=1,
                context_snippet=str(exc)[:300],
                fix_hint=(
                    "世界状态校验未通过：本章必须在正文中自然写出要求的物理关键词作为伏笔/期待证据"
                    f"（{exc}）"
                ),
            )
        return None

    def _project_ledger_balances(self, scene: SceneOutline) -> Dict[str, float]:
        """Compute post-chapter ledger balances on a copied dict without mutating ``self.state``."""
        projected: Dict[str, float] = {
            str(k): float(v) for k, v in self.state.numeric_balances.items()
        }
        for lm in scene.ledger_mutations:
            projected[lm.ledger_key] = projected.get(lm.ledger_key, 0.0) + float(lm.delta)
        return projected

    def _evaluate_l1_gate(
        self,
        scene: SceneOutline,
        candidate_text: str,
        chapters_dir: Path,
    ) -> Tuple[List[Violation], List[RuleHit]]:
        """Run deterministic L1 cross-chapter checks without mutating state or writing files."""
        if self.blueprint is None:
            return [], []

        l1_chapters: List[Tuple[int, str]] = []
        for ch_no, ch_path in _scan_completed_chapters(chapters_dir):
            if ch_no < scene.chapter:
                l1_chapters.append((ch_no, ch_path.read_text(encoding="utf-8").rstrip()))
        l1_chapters.append((scene.chapter, candidate_text))

        projected_balances = self._project_ledger_balances(scene)
        hits = run_l1_checks(
            l1_chapters,
            characters=self.project.characters,
            blueprint_scale_rules=[r.to_dict() for r in self.blueprint.scale_rules],
            selling_point_keywords=list(self.blueprint.premise.selling_point_keywords),
            ledger_balances=projected_balances,
            core_selling_point_keywords=self.core_selling_point_keywords,
        )

        violations: List[Violation] = []
        for hit in hits:
            sev = str(hit.severity).upper()
            violations.append(
                Violation(
                    rule_id=hit.rule_id,
                    category="L1_QUALITY_RULE",
                    severity=sev,
                    matched_text=hit.evidence[:300],
                    line_number=1,
                    context_snippet=hit.evidence[:300],
                    fix_hint=(
                        f"L1 跨章质量红线未通过（{hit.rule_id}）：{hit.evidence}。"
                        "请消除跨章原样重复段/角色招牌动作堆砌/量级越界或补回核心卖点后重写。"
                        if sev == "CRITICAL"
                        else f"L1 跨章质量预警（{hit.rule_id}）：{hit.evidence}"
                    ),
                )
            )
        return violations, hits

    def _resolve_blueprint_volume(self, scene: SceneOutline) -> VolumeSpec:
        if self.blueprint is None:
            raise ValueError("Cannot resolve VolumeSpec when blueprint is None.")
        for vol in self.blueprint.volumes:
            if vol.chapter_start <= scene.chapter <= vol.chapter_end:
                return vol
        for vol in self.blueprint.volumes:
            if vol.index == scene.volume:
                return vol
        raise ValueError(
            f"BookBlueprint has no VolumeSpec covering chapter {scene.chapter} (volume={scene.volume})."
        )

    def _run_arc_review_checkpoint(
        self,
        *,
        scene: SceneOutline,
        chapters_dir: Path,
        audit_dir: Path,
    ) -> Dict[str, Any]:
        if self.arc_judge is None or self.blueprint is None:
            raise ValueError("arc_judge and blueprint are both required for arc_review.")
        vol_spec = self._resolve_blueprint_volume(scene)
        window_start = max(1, scene.chapter - self.arc_review_interval + 1)
        completed_pairs = [
            (ch_no, ch_path)
            for ch_no, ch_path in _scan_completed_chapters(chapters_dir)
            if window_start <= ch_no <= scene.chapter
        ]
        if not completed_pairs:
            raise ProductionGateFailure(
                f"Cannot run arc_review at chapter {scene.chapter:04d}: "
                f"no completed chapter files found in '{chapters_dir}'."
            )
        reviewed_chapters = [ch_no for ch_no, _ in completed_pairs]
        review_texts = [
            ch_path.read_text(encoding="utf-8").strip() for _, ch_path in completed_pairs
        ]
        verdict = self.arc_judge.arc_review(review_texts, vol_spec.to_dict())
        verdict_val = str(getattr(verdict, "verdict", "") or "").strip()
        if verdict_val not in ("PASS", "FAIL"):
            raise ValueError(
                f"arc_judge.arc_review returned unexpected verdict {verdict_val!r}; expected 'PASS' or 'FAIL'."
            )
        arc_audit_path = audit_dir / f"arc_review_ch{scene.chapter:04d}.json"
        arc_record: Dict[str, Any] = {
            "chapter": scene.chapter,
            "volume_index": vol_spec.index,
            "volume_title": vol_spec.title,
            "reviewed_chapters": reviewed_chapters,
            "judge_model": str(getattr(self.arc_judge, "judge_model", "") or ""),
            "writer_model": str(getattr(self.arc_judge, "writer_model", "") or ""),
            "verdict": verdict_val,
            "problems": list(getattr(verdict, "problems", [])),
            "summary": str(getattr(verdict, "summary", "")),
            "raw_response": str(getattr(verdict, "raw_response", "")),
            "audit_file": str(arc_audit_path),
            "recorded_at": _now_shanghai_iso(),
        }
        _write_json_atomic(arc_audit_path, arc_record)
        return arc_record

    def _run_volume_health_checkpoint(
        self,
        *,
        vol_spec: VolumeSpec,
        chapters_dir: Path,
        audit_dir: Path,
    ) -> VolumeHealthReport:
        if self.blueprint is None or self.volume_health_judge is None:
            raise ValueError(
                "blueprint and volume_health_judge are both required for volume health check."
            )
        missing_chapters: List[int] = []
        vol_chapters: List[Tuple[int, str]] = []
        for ch_no in range(vol_spec.chapter_start, vol_spec.chapter_end + 1):
            ch_path = resolve_chapter_path(chapters_dir, ch_no)
            if ch_path is None or not ch_path.is_file():
                missing_chapters.append(ch_no)
                continue
            text = ch_path.read_text(encoding="utf-8").strip()
            if not text:
                raise ProductionGateFailure(
                    f"Volume {vol_spec.index} health check aborted: chapter {ch_no:04d} file "
                    f"'{ch_path}' is empty."
                )
            vol_chapters.append((ch_no, text))
        if missing_chapters:
            raise ProductionGateFailure(
                f"Cannot run volume {vol_spec.index} health check: missing chapter file(s) "
                f"{missing_chapters} in '{chapters_dir}' "
                f"(expected complete range {vol_spec.chapter_start}..{vol_spec.chapter_end})."
            )

        prev_vol: Optional[VolumeSpec] = None
        prior_chapters: List[Tuple[int, str]] = []
        if vol_spec.index > 1:
            for candidate in self.blueprint.volumes:
                if candidate.index == vol_spec.index - 1:
                    prev_vol = candidate
                    break
            if prev_vol is not None:
                for ch_no in range(prev_vol.chapter_start, prev_vol.chapter_end + 1):
                    prev_path = resolve_chapter_path(chapters_dir, ch_no)
                    if prev_path is not None and prev_path.is_file():
                        prev_text = prev_path.read_text(encoding="utf-8").strip()
                        if prev_text:
                            prior_chapters.append((ch_no, prev_text))

        return run_volume_health_check(
            volume_spec=vol_spec,
            chapters=vol_chapters,
            world_state_snapshot=self.state.snapshot_dict(),
            project=self.project,
            blueprint=self.blueprint,
            judge=self.volume_health_judge,
            audit_dir=audit_dir,
            prev_volume_spec=prev_vol,
            core_selling_point_keywords=self.core_selling_point_keywords,
            prior_volume_chapters=prior_chapters or None,
        )

    def _try_load_passed_volume_health_report(
        self,
        audit_dir: Path,
        volume_index: int,
    ) -> Optional[Dict[str, Any]]:
        locks = load_volume_locks(audit_dir)
        entry = locks["volumes"].get(str(volume_index))
        if not isinstance(entry, dict):
            return None
        if entry.get("lock_state") != LOCK_STATE_PASSED or entry.get("verdict") != "PASS":
            return None
        report_file_str = entry.get("report_file")
        if not isinstance(report_file_str, str) or not report_file_str.strip():
            return None
        report_path = Path(report_file_str)
        if not report_path.is_file() and not report_path.is_absolute():
            candidate = audit_dir / report_path
            if candidate.is_file():
                report_path = candidate
        if not report_path.is_file():
            return None
        try:
            assert_previous_volume_unlocked(audit_dir, volume_index + 1)
        except VolumeHealthLockError:
            return None
        return dict(json.loads(report_path.read_text(encoding="utf-8")))

    def _execute_and_enforce_volume_health_gate(
        self,
        *,
        vol_spec: VolumeSpec,
        chapters_dir: Path,
        audit_dir: Path,
        volume_health_reports: List[Dict[str, Any]],
    ) -> VolumeHealthReport:
        report = self._run_volume_health_checkpoint(
            vol_spec=vol_spec,
            chapters_dir=chapters_dir,
            audit_dir=audit_dir,
        )
        volume_health_reports.append(report.to_dict())
        failed_checks = [c.id for c in report.checks if c.status != "PASS"]
        print(
            f"[Engine] Volume {vol_spec.index} ({vol_spec.title}) 6-point health check: "
            f"verdict={report.verdict}, lock_state={report.lock_state}, "
            f"failed_checks={failed_checks}, report={report.report_file}",
            flush=True,
        )
        if report.verdict != "PASS":
            raise VolumeHealthLockError(
                f"第 {vol_spec.index} 卷《{vol_spec.title}》卷末六项体检未通过"
                f"（verdict={report.verdict}, lock_state={report.lock_state}, "
                f"failed_checks={failed_checks}），已锁卷停机，禁止进入第 {vol_spec.index + 1} 卷。"
                f"体检报告：{report.report_file}",
                volume_index=vol_spec.index,
                failed_checks=failed_checks,
                lock_file=report.lock_file,
                report_file=report.report_file,
                repair_actions=report.repair_actions,
            )
        return report

    def _record_degradation(
        self,
        scene: SceneOutline,
        attempt: int,
        *,
        stage: str,
        exc: BaseException,
        action_taken: str,
        incidents_jsonl: Path,
    ) -> ProductionIncident:
        incident = ProductionIncident(
            incident_id=f"INC-CH{scene.chapter:04d}-A{attempt}-{stage}",
            chapter=scene.chapter,
            scene_id=scene.scene_id,
            attempt=attempt,
            timestamp=_now_shanghai_iso(),
            violations=[
                Violation(
                    rule_id=stage,
                    category="PIPELINE_DEGRADED",
                    severity="WARNING",
                    matched_text=f"{type(exc).__name__}: {str(exc)[:300]}",
                    line_number=0,
                    context_snippet="",
                    fix_hint=action_taken,
                )
            ],
            action_taken=action_taken,
        )
        self.incidents.append(incident)
        incidents_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with incidents_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(incident.to_dict(), ensure_ascii=False) + "\n")
        print(
            f"[Engine] DEGRADED {incident.incident_id}: {type(exc).__name__}: {str(exc)[:200]} -> {action_taken}",
            flush=True,
        )
        return incident

    def _load_chapter_meta(self, chapters_dir: Path, chapter: int) -> Dict[str, Any]:
        meta_path = _chapter_meta_path(chapters_dir, chapter)
        if not meta_path.is_file():
            return {}
        return dict(json.loads(meta_path.read_text(encoding="utf-8")))

    def _is_chapter_locked(
        self,
        scene: SceneOutline,
        chapters_dir: Path,
        existing_ch_file: Optional[Path],
    ) -> bool:
        meta = self._load_chapter_meta(chapters_dir, scene.chapter)
        if bool(meta.get("locked", False)):
            return True
        if scene.chapter in self.state.locked_chapters:
            return True
        if scene.locked and existing_ch_file is not None and existing_ch_file.exists():
            return True
        if (
            self.project.meta.max_published_chapter > 0
            and scene.chapter <= self.project.meta.max_published_chapter
            and existing_ch_file is not None
            and existing_ch_file.exists()
        ):
            return True
        return False

    def _restore_predecessor_state(
        self,
        predecessor_chapter: int,
        chapters_dir: Path,
        snapshots_dir: Path,
    ) -> None:
        snap_path = _resolve_snapshot_path(snapshots_dir, predecessor_chapter)
        if snap_path is None:
            raise MissingPredecessorSnapshotError(
                f"Cannot run chapter {predecessor_chapter + 1}: missing predecessor snapshot "
                f"for chapter {predecessor_chapter} in '{snapshots_dir}'."
            )
        self.state.load_snapshot(snap_path, chapters_dir=chapters_dir)
        # Normalize current_chapter for legacy v1 snapshots lacking current_chapter field
        if self.state.current_chapter == 0 and self.state.completed_chapters:
            self.state.current_chapter = max(self.state.completed_chapters)

        mem_path = _resolve_memory_path(snapshots_dir, predecessor_chapter)
        scene_map = {s.chapter: s for s in self.project.scenes}
        if mem_path.is_file():
            try:
                self.memory = MemoryPyramid.load(mem_path)
            except MemoryMigrationError:
                raw_mem = json.loads(mem_path.read_text(encoding="utf-8"))
                chapter_day_map: Dict[int, int] = {}
                running_day = 1
                day_chapters = sorted(
                    {c for c in self.state.completed_chapters if 1 <= c <= predecessor_chapter}
                    | {s.chapter for s in self.project.scenes if 1 <= s.chapter <= predecessor_chapter}
                )
                for ch_no in day_chapters:
                    sc = scene_map.get(ch_no)
                    running_day += int(sc.calendar_days_elapsed) if sc is not None else 1
                    chapter_day_map[ch_no] = running_day
                self.memory = migrate_legacy_memory(raw_mem, chapter_day_map)
            return

        # Reconstruct pre-chapter MemoryPyramid deterministically for legacy projects without memory_*.json
        rebuilt = MemoryPyramid()
        names_map = self._entity_names_map()
        for eid, ename in names_map.items():
            etype = "character" if eid in self.project.characters else "asset"
            rebuilt.register_entity_name(eid, ename, etype)
        running_day = 1
        for ch_no in sorted(c for c in self.state.completed_chapters if 1 <= c <= predecessor_chapter):
            sc = scene_map.get(ch_no)
            running_day += int(sc.calendar_days_elapsed) if sc is not None else 1
            ch_file = resolve_chapter_path(chapters_dir, ch_no)
            if ch_file is not None and ch_file.is_file():
                body = ch_file.read_text(encoding="utf-8").rstrip()
                rebuilt.record_chapter(
                    ch_no,
                    body,
                    virtual_day=running_day,
                    scene=sc,
                    entity_names=names_map,
                )
        self.memory = rebuilt

    def _sync_existing_chapter_into_state(
        self,
        scene: SceneOutline,
        existing_text: str,
        chapters_dir: Path,
        snapshots_dir: Path,
    ) -> Dict[str, Any]:
        """Advance in-memory state/memory for a resumed existing chapter without overwriting files."""
        meta = self._load_chapter_meta(chapters_dir, scene.chapter)
        if bool(meta.get("locked", False)) or scene.locked:
            expected_sha = str(meta.get("sha256") or "")
            if expected_sha and _compute_sha256(existing_text) != expected_sha:
                raise LockedChapterTamperedError(
                    f"Locked chapter {scene.chapter} content mismatches recorded SHA256 in '{_chapter_meta_path(chapters_dir, scene.chapter)}'"
                )
            if scene.chapter not in self.state.locked_chapters:
                self.state.chapter_hashes[scene.chapter] = _compute_sha256(existing_text)
                self.state.locked_chapters.append(scene.chapter)

        state_diff = self.state.advance_after_chapter(scene, existing_text)
        # virtual_day 采用 advance_after_chapter 推进后的天数（与 state.virtual_day 一致）
        self.memory.record_chapter(
            scene.chapter,
            existing_text,
            virtual_day=self.state.virtual_day,
            scene=scene,
            entity_names=self._entity_names_map(),
        )
        return state_diff

    def run(
        self,
        target_chapters: Optional[List[int]] = None,
        resume: bool = False,
        *,
        unlock: bool = False,
        on_progress: Optional[ProgressCallback] = None,
        trial_checkpoint_interval: Optional[int] = None,
        brake_on_consecutive_failures: bool = False,
        strict_arc_review: bool = True,
    ) -> Dict[str, Any]:
        effective_checkpoint_interval: Optional[int] = (
            _validate_positive_int(
                trial_checkpoint_interval, name="trial_checkpoint_interval"
            )
            if trial_checkpoint_interval is not None
            else self.trial_checkpoint_interval
        )

        chapters_dir = self.output_dir / "chapters"
        audit_dir = self.output_dir / "audit"
        snapshots_dir = self.output_dir / "snapshots"
        for d in (chapters_dir, audit_dir, snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)

        run_stamp = _now_shanghai_stamp()
        is_partial_run = target_chapters is not None

        incidents_jsonl = audit_dir / "incidents.jsonl"
        if not resume and not is_partial_run and incidents_jsonl.exists():
            _archive_file(incidents_jsonl, self.output_dir, run_stamp)

        # Save initial state snapshot (Chapter 0000 baseline) without clobbering on partial/resume runs
        initial_snap_path = _resolve_snapshot_path(snapshots_dir, 0)
        if not resume and not is_partial_run:
            if initial_snap_path is not None and initial_snap_path.exists():
                _archive_file(initial_snap_path, self.output_dir, run_stamp)
            target_init_snap = snapshots_dir / "chapter_0000_initial.json"
            self.state.save_snapshot(target_init_snap)
            self.memory.save(_resolve_memory_path(snapshots_dir, 0))
        elif initial_snap_path is None:
            target_init_snap = snapshots_dir / "chapter_0000_initial.json"
            self.state.save_snapshot(target_init_snap)
            mem0 = _resolve_memory_path(snapshots_dir, 0)
            if not mem0.exists():
                self.memory.save(mem0)

        target_set = set(target_chapters) if target_chapters is not None else None
        scenes = sorted(
            [s for s in self.project.scenes if target_set is None or s.chapter in target_set],
            key=lambda s: s.chapter,
        )
        if target_chapters is not None and not scenes:
            raise ValueError(
                f"No matching scenes found for target_chapters={target_chapters!r}"
            )

        chapter_summaries: List[Dict[str, Any]] = []
        consecutive_failures: int = 0
        failed_chapters: List[int] = []
        failed_chapter_set: set[int] = set()
        failed_chapter_errors: List[Dict[str, Any]] = []
        arc_reviews: List[Dict[str, Any]] = []
        volume_health_reports: List[Dict[str, Any]] = []
        paused_for_review: bool = False
        checkpoint_chapter: Optional[int] = None
        started_at = _now_shanghai_iso()
        scene_map_all = {s.chapter: s for s in self.project.scenes}

        try:
            for scene in scenes:
                scene_vol_spec: Optional[VolumeSpec] = None
                if self.enforce_volume_health_gate:
                    scene_vol_spec = self._resolve_blueprint_volume(scene)
                    if scene_vol_spec.index > 1:
                        assert_previous_volume_unlocked(audit_dir, scene_vol_spec.index)

                # Enforce N-1 world-state snapshot continuity whenever starting at or jumping to N > 1
                # (unless N-1 just failed in this run under brake_on_consecutive_failures=True).
                if (
                    scene.chapter > 1
                    and (
                        self.state.current_chapter < scene.chapter - 1
                        or (scene.chapter - 1) not in self.state.completed_chapters
                    )
                    and not (
                        brake_on_consecutive_failures
                        and (scene.chapter - 1) in failed_chapter_set
                    )
                ):
                    self._restore_predecessor_state(
                        scene.chapter - 1,
                        chapters_dir=chapters_dir,
                        snapshots_dir=snapshots_dir,
                    )

                existing_ch_file = resolve_chapter_path(chapters_dir, scene.chapter)
                is_locked = self._is_chapter_locked(scene, chapters_dir, existing_ch_file)

                if resume and existing_ch_file is not None and existing_ch_file.exists():
                    existing_text = existing_ch_file.read_text(encoding="utf-8").rstrip()
                    due_fs = self.state.get_due_foreshadows(scene.chapter)
                    plant_fs = self.state.get_plant_foreshadows(scene.chapter)
                    chk = self.checker.scan(
                        text=existing_text,
                        scene=scene,
                        characters=self.project.characters,
                        due_foreshadows=due_fs,
                        plant_foreshadows=plant_fs,
                        project_meta=self.project.meta,
                        world_state=self.state.to_snapshot(),
                        chapter=scene.chapter,
                    )
                    if chk.passed and self._validate_state_gate(scene, existing_text) is not None:
                        print(
                            f"[Engine] Chapter {scene.chapter:04d} checkpoint fails world-state evidence "
                            f"gate; regenerating instead of resuming",
                            flush=True,
                        )
                        chk.passed = False
                    if chk.passed:
                        state_diff = self._sync_existing_chapter_into_state(
                            scene, existing_text, chapters_dir, snapshots_dir
                        )
                        existing_meta = self._load_chapter_meta(chapters_dir, scene.chapter)
                        public_title = str(
                            existing_meta.get("public_title")
                            or scene.public_title
                            or scene.internal_title
                        )
                        author_note = str(existing_meta.get("author_note") or "")
                        sha256_val = str(
                            existing_meta.get("sha256") or _compute_sha256(existing_text)
                        )
                        print(
                            f"[Engine] Chapter {scene.chapter:04d} resumed from verified checkpoint "
                            f"({chk.char_count} chars)",
                            flush=True,
                        )
                        consecutive_failures = 0
                        chapter_summaries.append(
                            {
                                "chapter": scene.chapter,
                                "volume": scene.volume,
                                "scene_id": scene.scene_id,
                                "internal_title": scene.internal_title,
                                "public_title": public_title,
                                "pov_character": self.project.characters[scene.pov_character].name,
                                "attempts_used": 1,
                                "rewrites_triggered": 0,
                                "char_count": chk.char_count,
                                "paragraph_count": chk.paragraph_count,
                                "first_char": existing_text[0] if existing_text else "",
                                "opening_preview": existing_text[:68].replace("\n", " "),
                                "ending_preview": existing_text[-68:].replace("\n", " "),
                                "model": str(existing_meta.get("model") or "checkpoint"),
                                "latency_sec": 0.0,
                                "chapter_file": str(existing_ch_file),
                                "meta_file": str(_chapter_meta_path(chapters_dir, scene.chapter)),
                                "author_note": author_note,
                                "sha256": sha256_val,
                                "locked": is_locked,
                                "state_transitions": state_diff,
                            }
                        )
                        if (
                            self.enforce_volume_health_gate
                            and scene_vol_spec is not None
                            and scene.chapter == scene_vol_spec.chapter_end
                        ):
                            reused_report = self._try_load_passed_volume_health_report(
                                audit_dir, scene_vol_spec.index
                            )
                            if reused_report is not None:
                                volume_health_reports.append(reused_report)
                                print(
                                    f"[Engine] Volume {scene_vol_spec.index} ({scene_vol_spec.title}) "
                                    f"health check reused verified {LOCK_STATE_PASSED} report: "
                                    f"{reused_report.get('report_file')}",
                                    flush=True,
                                )
                            else:
                                self._execute_and_enforce_volume_health_gate(
                                    vol_spec=scene_vol_spec,
                                    chapters_dir=chapters_dir,
                                    audit_dir=audit_dir,
                                    volume_health_reports=volume_health_reports,
                                )
                        continue
                    if is_locked and not unlock:
                        raise LockedChapterError(
                            f"Chapter {scene.chapter} is locked and failed resume verification; "
                            f"pass unlock=True (--unlock) to overwrite."
                        )

                if is_locked and not unlock:
                    raise LockedChapterError(
                        f"Chapter {scene.chapter} is locked and cannot be overwritten without "
                        f"unlock=True (--unlock)."
                    )

                if unlock and is_locked:
                    if scene.chapter in self.state.locked_chapters:
                        self.state.locked_chapters = [
                            c for c in self.state.locked_chapters if c != scene.chapter
                        ]
                    self.state.chapter_hashes.pop(scene.chapter, None)

                try:
                    ch_result = self._run_single_chapter(
                        scene=scene,
                        chapters_dir=chapters_dir,
                        audit_dir=audit_dir,
                        snapshots_dir=snapshots_dir,
                        incidents_jsonl=incidents_jsonl,
                        run_stamp=run_stamp,
                        on_progress=on_progress,
                        skip_recent_chapters=(
                            failed_chapter_set if brake_on_consecutive_failures else None
                        ),
                    )
                except ProductionGateFailure as exc:
                    if not brake_on_consecutive_failures:
                        raise
                    consecutive_failures += 1
                    failed_chapters.append(scene.chapter)
                    failed_chapter_set.add(scene.chapter)
                    failed_chapter_errors.append(
                        {
                            "chapter": scene.chapter,
                            "scene_id": scene.scene_id,
                            "error": str(exc),
                        }
                    )
                    print(
                        f"[Engine] Chapter {scene.chapter:04d} FAILED gate "
                        f"(consecutive_failures={consecutive_failures}/{self.max_consecutive_failures})",
                        flush=True,
                    )
                    if consecutive_failures >= self.max_consecutive_failures:
                        raise ConsecutiveGateFailureBrakeError(
                            f"Consecutive gate failure brake triggered after {consecutive_failures} "
                            f"consecutive failed chapter(s) (limit={self.max_consecutive_failures}, "
                            f"failed_chapters={failed_chapters}). Last failure (Ch.{scene.chapter}): {exc}",
                            failed_chapters=failed_chapters,
                            consecutive_failures=consecutive_failures,
                        ) from exc
                    continue

                consecutive_failures = 0
                chapter_summaries.append(ch_result)

                if (
                    self.arc_judge is not None
                    and self.blueprint is not None
                    and scene.chapter % self.arc_review_interval == 0
                ):
                    arc_record = self._run_arc_review_checkpoint(
                        scene=scene,
                        chapters_dir=chapters_dir,
                        audit_dir=audit_dir,
                    )
                    arc_reviews.append(arc_record)
                    print(
                        f"[Engine] Arc review at chapter {scene.chapter:04d}: "
                        f"verdict={arc_record['verdict']} (problems={len(arc_record['problems'])})",
                        flush=True,
                    )
                    if arc_record["verdict"] == "FAIL" and strict_arc_review:
                        raise ArcReviewBrakeError(
                            f"Arc review brake triggered at chapter {scene.chapter:04d} "
                            f"(verdict=FAIL, problems={len(arc_record['problems'])}): "
                            f"{arc_record['summary']}",
                            chapter=scene.chapter,
                            verdict=arc_record,
                            audit_file=str(arc_record["audit_file"]),
                        )

                if (
                    self.enforce_volume_health_gate
                    and scene_vol_spec is not None
                    and scene.chapter == scene_vol_spec.chapter_end
                ):
                    self._execute_and_enforce_volume_health_gate(
                        vol_spec=scene_vol_spec,
                        chapters_dir=chapters_dir,
                        audit_dir=audit_dir,
                        volume_health_reports=volume_health_reports,
                    )

                if (
                    effective_checkpoint_interval is not None
                    and scene.chapter % effective_checkpoint_interval == 0
                ):
                    paused_for_review = True
                    checkpoint_chapter = scene.chapter
                    print(
                        f"[Engine] Reached trial checkpoint at chapter {scene.chapter:04d} "
                        f"(interval={effective_checkpoint_interval}); pausing cleanly for review.",
                        flush=True,
                    )
                    break
        finally:
            if hasattr(self.writer_provider, "close"):
                self.writer_provider.close()
            if self.provider is not self.writer_provider and hasattr(self.provider, "close"):
                self.provider.close()

        finished_at = _now_shanghai_iso()

        # Assemble full manuscript across ALL completed chapters on disk sorted by chapter number
        all_on_disk = _scan_completed_chapters(chapters_dir)
        pure_parts: List[str] = []
        reader_parts: List[str] = [
            f"# 《{self.project.meta.title}》\n\n> {self.project.meta.synopsis}\n"
        ]
        for ch_no, ch_file in all_on_disk:
            body = ch_file.read_text(encoding="utf-8")
            pure_parts.append(body)
            meta = self._load_chapter_meta(chapters_dir, ch_no)
            sc = scene_map_all.get(ch_no)
            title_part = str(
                meta.get("public_title")
                or (sc.public_title if sc else "")
                or (sc.internal_title if sc else "")
            ).strip()
            heading = f"## 第 {ch_no} 章 {title_part}".strip()
            reader_parts.append(f"{heading}\n\n{body}\n")

        manuscript_pure_path = self.output_dir / "manuscript_pure.txt"
        _write_text_atomic(
            manuscript_pure_path,
            ("\n\n".join(pure_parts) + "\n") if pure_parts else "",
        )

        manuscript_reader_path = self.output_dir / "manuscript_reader.md"
        _write_text_atomic(
            manuscript_reader_path,
            "\n\n".join(reader_parts),
        )

        total_chars = sum(c["char_count"] for c in chapter_summaries)
        all_gates_ok = (
            (len(failed_chapters) == 0)
            and all(r.get("verdict") == "PASS" for r in arc_reviews)
            and all(r.get("verdict") == "PASS" for r in volume_health_reports)
        )
        manifest: Dict[str, Any] = {
            "project": self.project.meta.to_dict(),
            "started_at": started_at,
            "finished_at": finished_at,
            "timezone": "Asia/Shanghai",
            "chapter_count": len(chapter_summaries),
            "all_completed_chapters_on_disk": [ch_no for ch_no, _ in all_on_disk],
            "total_chars": total_chars,
            "total_incidents_detected": len(self.incidents),
            "all_gates_passed": all_gates_ok,
            "paused_for_review": paused_for_review,
            "checkpoint_chapter": checkpoint_chapter,
            "failed_chapters": list(failed_chapters),
            "failed_chapter_errors": list(failed_chapter_errors),
            "arc_reviews": list(arc_reviews),
            "volume_health_reports": list(volume_health_reports),
            "chapters": chapter_summaries,
            "incidents": [inc.to_dict() for inc in self.incidents],
            "final_world_state": self.state.snapshot_dict(),
            "artifacts": {
                "manuscript_pure": str(manuscript_pure_path),
                "manuscript_reader": str(manuscript_reader_path),
            },
        }

        report_md_path = self.output_dir / "SMOKE_TEST_REPORT.md"
        _write_text_atomic(report_md_path, generate_markdown_report(manifest))

        dashboard_html_path = self.output_dir / "smoke_test_dashboard.html"
        _write_text_atomic(
            dashboard_html_path,
            generate_html_dashboard(manifest, chapters_dir, output_dir=self.output_dir),
        )

        manifest["artifacts"]["smoke_test_report"] = str(report_md_path)
        manifest["artifacts"]["dashboard_html"] = str(dashboard_html_path)
        manifest_path = self.output_dir / "run_manifest.json"
        _write_json_atomic(manifest_path, manifest)
        return manifest

    @staticmethod
    def _load_recent_chapter_texts(
        chapter: int,
        chapters_dir: Path,
        *,
        skip_chapters: Optional[AbstractSet[int]] = None,
    ) -> Tuple[str, ...]:
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise ValueError(f"chapter must be an int >= 1, got {chapter!r}")
        if chapter == 1:
            return ()
        texts: List[str] = []
        for prev_ch in range(max(1, chapter - 3), chapter):
            if skip_chapters is not None and prev_ch in skip_chapters:
                # Preserve positional slot count required by PromptCompiler (min(3, chapter - 1))
                # for a chapter explicitly skipped after failing the gate under brake mode.
                texts.append("")
                continue
            prev_path = resolve_chapter_path(chapters_dir, prev_ch)
            if prev_path is None or not prev_path.is_file():
                raise ProductionGateFailure(
                    f"Chapter {chapter:04d} requires preceding chapter {prev_ch:04d} text for "
                    f"anchor cooldown check, but no chapter file was found in '{chapters_dir}'."
                )
            texts.append(prev_path.read_text(encoding="utf-8"))
        return tuple(texts)

    def _run_single_chapter(
        self,
        scene: SceneOutline,
        chapters_dir: Path,
        audit_dir: Path,
        snapshots_dir: Path,
        incidents_jsonl: Path,
        *,
        run_stamp: str,
        on_progress: Optional[ProgressCallback] = None,
        skip_recent_chapters: Optional[AbstractSet[int]] = None,
    ) -> Dict[str, Any]:
        max_attempts = self.project.meta.max_rewrite_retries + 1
        due_fs = self.state.get_due_foreshadows(scene.chapter)
        plant_fs = self.state.get_plant_foreshadows(scene.chapter)

        # Outline-only preflight (deceased on stage, ledger underflow / volume cap, foreshadow
        # keyword config, asset refs): fail with a clear configuration error BEFORE spending tokens.
        try:
            self.state.preflight_scene(scene)
        except WorldStateError as exc:
            print(
                f"[Engine] Chapter {scene.chapter:04d} ({scene.scene_id}) outline preflight FAILED "
                f"before any LLM call: {exc}",
                flush=True,
            )
            raise

        self._emit(
            on_progress,
            SSE_CHAPTER_STARTED,
            {"chapter": scene.chapter, "scene_id": scene.scene_id, "volume": scene.volume},
        )
        scene_title = (scene.public_title or scene.internal_title or f"第 {scene.chapter} 章").strip()
        self._emit(
            on_progress,
            SSE_PIPELINE_STAGE,
            {
                "chapter": scene.chapter,
                "stage": "outline",
                "status": "done",
                "scene_title": scene_title,
                "min_chars": int(scene.min_chars),
                "max_chars": int(scene.max_chars),
                "detail": f"章纲就绪 · 第 {scene.chapter} 章《{scene_title}》",
            },
        )
        switch_idx_start = (
            len(self.provider.get_model_switch_history())
            if isinstance(self.provider, RoleModelRouter)
            else 0
        )
        if self.router is not None:
            # Bug 2 fix: before writing each chapter, check whether an earlier model in the
            # rotation chain has finished cooling down and switch back automatically.
            self.router.recover_preferred_models(
                roles=("writer", "critic", "polisher"),
                chapter=scene.chapter,
            )

        self._emit(
            on_progress,
            SSE_PIPELINE_STAGE,
            {
                "chapter": scene.chapter,
                "stage": "compile",
                "status": "running",
                "attempt": 1,
                "max_attempts": max_attempts,
                "detail": "组装世界观、记忆金字塔与章节约束",
            },
        )
        recent_chapter_texts = self._load_recent_chapter_texts(
            scene.chapter,
            chapters_dir,
            skip_chapters=skip_recent_chapters,
        )
        sys_prompt, user_prompt = self.compiler.compile_scene_prompt(
            scene,
            self.state,
            memory_pyramid=self.memory,
            recent_chapter_texts=recent_chapter_texts,
        )
        _write_text_atomic(
            audit_dir / f"ch{scene.chapter:04d}_compiled_prompt.md",
            f"{sys_prompt}\n\n{user_prompt}\n",
        )
        self._emit(
            on_progress,
            SSE_PIPELINE_STAGE,
            {
                "chapter": scene.chapter,
                "stage": "compile",
                "status": "done",
                "attempt": 1,
                "max_attempts": max_attempts,
                "detail": "提示词组装完成",
            },
        )

        last_report: Optional[ScanReport] = None
        last_draft: str = ""

        def _active_model(role: str) -> str:
            if self.router is not None and self.router.has_role(role):
                return self.router.get_active_model(role)
            return str(getattr(self.writer_provider, "model", "") or "")

        for attempt in range(1, max_attempts + 1):
            purpose = "draft" if attempt == 1 else "rewrite"
            if attempt > 1 and last_report is not None:
                self._emit(
                    on_progress,
                    SSE_PIPELINE_STAGE,
                    {
                        "chapter": scene.chapter,
                        "stage": "compile",
                        "status": "running",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "detail": f"组装第 {attempt}/{max_attempts} 次重写修复指引",
                    },
                )
                sys_prompt, user_prompt = self.compiler.compile_rewrite_prompt(
                    scene=scene,
                    state=self.state,
                    draft_text=last_draft,
                    report=last_report,
                    memory_pyramid=self.memory,
                    recent_chapter_texts=recent_chapter_texts,
                )
                _write_text_atomic(
                    audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_rewrite_prompt.md",
                    f"{sys_prompt}\n\n{user_prompt}\n",
                )
                self._emit(
                    on_progress,
                    SSE_PIPELINE_STAGE,
                    {
                        "chapter": scene.chapter,
                        "stage": "compile",
                        "status": "done",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "detail": f"第 {attempt}/{max_attempts} 次重写指引就绪",
                    },
                )

            req_id = f"ch{scene.chapter:04d}_attempt{attempt}_{purpose}"
            llm_req = LLMRequest(
                request_id=req_id,
                chapter=scene.chapter,
                attempt=attempt,
                purpose=purpose,
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                temperature=0.65 if attempt == 1 else 0.45,
                max_tokens=self._writer_max_tokens(scene),
            )

            print(
                f"[Engine] Chapter {scene.chapter:04d} ({scene.scene_id}) | "
                f"Attempt {attempt}/{max_attempts} ({purpose})",
                flush=True,
            )
            self._emit(
                on_progress,
                SSE_CHAPTER_ATTEMPT,
                {
                    "chapter": scene.chapter,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "purpose": purpose,
                },
            )
            writer_model_before = _active_model("writer")
            self._emit(
                on_progress,
                SSE_PIPELINE_STAGE,
                {
                    "chapter": scene.chapter,
                    "stage": "draft",
                    "status": "running",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "purpose": purpose,
                    "model": writer_model_before,
                    "thinking": True,
                    "detail": f"思考中 · 第 {attempt}/{max_attempts} 次（{purpose}）",
                },
            )
            try:
                llm_resp = self.writer_provider.generate(llm_req)
            except LLMStreamTruncatedError as exc:
                # One in-attempt retry with a larger output budget (usage already billed upstream).
                self._record_degradation(
                    scene,
                    attempt,
                    stage="WRITER_TRUNCATED",
                    exc=exc,
                    action_taken="Writer stream truncated -> single retry with larger max_tokens",
                    incidents_jsonl=incidents_jsonl,
                )
                llm_req = LLMRequest(
                    request_id=f"{req_id}_truncretry",
                    chapter=llm_req.chapter,
                    attempt=llm_req.attempt,
                    purpose=llm_req.purpose,
                    system_prompt=llm_req.system_prompt,
                    user_prompt=llm_req.user_prompt,
                    temperature=llm_req.temperature,
                    max_tokens=int(llm_req.max_tokens * 1.5),
                    metadata=dict(llm_req.metadata),
                )
                llm_resp = self.writer_provider.generate(llm_req)
            candidate_text = llm_resp.content.rstrip()
            self._emit(
                on_progress,
                SSE_PIPELINE_STAGE,
                {
                    "chapter": scene.chapter,
                    "stage": "draft",
                    "status": "done",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "purpose": purpose,
                    "model": str(llm_resp.model or writer_model_before).strip(),
                    "latency_sec": round(float(llm_resp.latency_sec or 0.0), 2),
                    "detail": f"初稿完成（{round(float(llm_resp.latency_sec or 0.0), 1)}s）",
                },
            )

            self._emit(
                on_progress,
                SSE_PIPELINE_STAGE,
                {
                    "chapter": scene.chapter,
                    "stage": "gate",
                    "status": "running",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "detail": "执行静态门禁与世界状态一致性核验",
                },
            )
            report = self.checker.scan(
                text=candidate_text,
                scene=scene,
                characters=self.project.characters,
                due_foreshadows=due_fs,
                plant_foreshadows=plant_fs,
                project_meta=self.project.meta,
                world_state=self.state.to_snapshot(),
                chapter=scene.chapter,
            )
            if report.passed:
                # World-state gate (stricter evidence rules than the checker) must pass BEFORE any
                # chapter artifact is written, otherwise we'd leave an orphan chapter on disk.
                state_violation = self._validate_state_gate(scene, candidate_text)
                if state_violation is not None:
                    report.violations.append(state_violation)
                    report.passed = False

            l1_hits: List[RuleHit] = []
            if report.passed and self.blueprint is not None:
                l1_violations, l1_hits = self._evaluate_l1_gate(
                    scene, candidate_text, chapters_dir
                )
                if l1_violations:
                    report.violations.extend(l1_violations)
                    if any(v.severity == "CRITICAL" for v in l1_violations):
                        report.passed = False
                _write_json_atomic(
                    audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_l1.json",
                    {
                        "chapter": scene.chapter,
                        "attempt": attempt,
                        "passed": not any(
                            str(h.severity).upper() == "CRITICAL" for h in l1_hits
                        ),
                        "hits": [h.to_dict() for h in l1_hits],
                    },
                )

            _write_json_atomic(
                audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_scan.json",
                report.to_dict(),
            )
            public_rules = build_public_gate_rules(
                report,
                scene,
                due_count=len(due_fs),
                plant_count=len(plant_fs),
            )
            will_rewrite = (not report.passed) and (attempt < max_attempts)
            self._emit(
                on_progress,
                SSE_PIPELINE_GATE_REPORT,
                {
                    "chapter": scene.chapter,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "passed": bool(report.passed),
                    "char_count": int(report.char_count),
                    "min_chars": int(scene.min_chars),
                    "max_chars": int(scene.max_chars),
                    "will_rewrite": will_rewrite,
                    "rules": public_rules,
                },
            )
            self._emit(
                on_progress,
                SSE_PIPELINE_STAGE,
                {
                    "chapter": scene.chapter,
                    "stage": "gate",
                    "status": "done" if report.passed else "failed",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "char_count": int(report.char_count),
                    "will_rewrite": will_rewrite,
                    "detail": (
                        f"全部门禁通过 · {int(report.char_count)} 字"
                        if report.passed
                        else f"门禁拦截 · 准备第 {attempt + 1}/{max_attempts} 次重写"
                        if will_rewrite
                        else "门禁重试耗尽"
                    ),
                },
            )
            if will_rewrite:
                self._emit(
                    on_progress,
                    SSE_PIPELINE_STAGE,
                    {
                        "chapter": scene.chapter,
                        "stage": "draft",
                        "status": "rewriting",
                        "from_stage": "gate",
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "model": _active_model("writer"),
                        "detail": f"打回重写 · 第 {attempt + 1}/{max_attempts} 次",
                    },
                )

            if report.passed:
                self._emit(
                    on_progress,
                    SSE_GATE_PASSED,
                    {
                        "chapter": scene.chapter,
                        "attempt": attempt,
                        "char_count": report.char_count,
                    },
                )

                critic_eval: Optional[CriticEvaluation] = None
                pre_polish_eval: Optional[CriticEvaluation] = None
                post_polish_eval: Optional[CriticEvaluation] = None
                polish_reverted_for_score = False
                polish_res: Optional[SurgicalPolishResult] = None
                author_note = ""

                if self.critic_polisher is not None:
                    self._emit(
                        on_progress,
                        SSE_PIPELINE_STAGE,
                        {
                            "chapter": scene.chapter,
                            "stage": "polish",
                            "status": "running",
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "model": _active_model("critic"),
                            "detail": "四维审校评分中",
                        },
                    )
                    # Critic / polish / author-note are post-gate enhancements: an upstream LLM error
                    # or malformed critic output must degrade gracefully, never discard a gated chapter.
                    try:
                        critic_eval = self.critic_polisher.evaluate_chapter(
                            candidate_text,
                            scene=scene,
                            project_meta=self.project.meta,
                            scan_report=report,
                            characters=self.project.characters,
                            world_state=self.state.to_snapshot(),
                            chapter=scene.chapter,
                        )
                    except (LLMError, CriticResponseParseError) as exc:
                        critic_eval = None
                        self._record_degradation(
                            scene,
                            attempt,
                            stage="CRITIC_FAILED",
                            exc=exc,
                            action_taken="Critic evaluation failed -> keeping gated text without critic score/polish",
                            incidents_jsonl=incidents_jsonl,
                        )
                if self.critic_polisher is not None and critic_eval is not None:
                    pre_polish_eval = critic_eval
                    _write_json_atomic(
                        audit_dir / f"ch{scene.chapter:04d}_critic_eval.json",
                        critic_eval.to_dict(),
                    )
                    self._emit(
                        on_progress,
                        SSE_CRITIC_SCORED,
                        {
                            "chapter": scene.chapter,
                            "overall_score": critic_eval.overall_score,
                            "weak_paragraphs": list(critic_eval.weak_paragraphs),
                        },
                    )
                    if critic_eval.weak_paragraphs:
                        self._emit(
                            on_progress,
                            SSE_PIPELINE_STAGE,
                            {
                                "chapter": scene.chapter,
                                "stage": "polish",
                                "status": "running",
                                "attempt": attempt,
                                "max_attempts": max_attempts,
                                "model": _active_model("polisher"),
                                "detail": f"四维评分 {critic_eval.overall_score:.1f} · 定点精修中",
                            },
                        )
                        try:
                            polish_res = self.critic_polisher.surgical_polish(
                                candidate_text,
                                critic_eval,
                                scene=scene,
                                project_meta=self.project.meta,
                                characters=self.project.characters,
                                due_foreshadows=due_fs,
                                plant_foreshadows=plant_fs,
                                world_state=self.state.to_snapshot(),
                                chapter=scene.chapter,
                            )
                        except SurgicalPolishGateError as exc:
                            # candidate_text already passed 100% of AITicChecker & WorldStateManager gates.
                            # Reject the broken polish and keep the already-gated draft (consistent with
                            # POLISH_FAILED and POLISH_STATE_GATE degradation) instead of discarding a valid chapter.
                            polish_res = None
                            if exc.polished_text:
                                _write_text_atomic(
                                    audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_polish_rejected.txt",
                                    exc.polished_text,
                                )
                            self._record_degradation(
                                scene,
                                attempt,
                                stage="POLISH_GATE_REJECTED",
                                exc=exc,
                                action_taken="Surgical polish broke gate -> keeping unpolished gated text",
                                incidents_jsonl=incidents_jsonl,
                            )
                        except (LLMError, CriticResponseParseError) as exc:
                            polish_res = None
                            self._record_degradation(
                                scene,
                                attempt,
                                stage="POLISH_FAILED",
                                exc=exc,
                                action_taken="Surgical polish failed -> keeping unpolished gated text",
                                incidents_jsonl=incidents_jsonl,
                            )

                        polish_l1_hits: Optional[List[RuleHit]] = None
                        if polish_res is not None:
                            polish_state_violation = self._validate_state_gate(
                                scene, polish_res.polished_text
                            )
                            if polish_state_violation is not None:
                                self._record_degradation(
                                    scene,
                                    attempt,
                                    stage="POLISH_STATE_GATE",
                                    exc=RuntimeError(polish_state_violation.matched_text),
                                    action_taken="Polished text failed world-state gate -> keeping unpolished gated text",
                                    incidents_jsonl=incidents_jsonl,
                                )
                                polish_res = None

                        if polish_res is not None and self.blueprint is not None:
                            polish_l1_violations, polish_l1_hits = self._evaluate_l1_gate(
                                scene, polish_res.polished_text, chapters_dir
                            )
                            polish_l1_critical = [
                                v for v in polish_l1_violations if v.severity == "CRITICAL"
                            ]
                            if polish_l1_critical:
                                self._record_degradation(
                                    scene,
                                    attempt,
                                    stage="POLISH_L1_GATE",
                                    exc=RuntimeError(polish_l1_critical[0].matched_text),
                                    action_taken="Polished text failed L1 cross-chapter gate -> keeping unpolished gated text",
                                    incidents_jsonl=incidents_jsonl,
                                )
                                polish_res = None
                                polish_l1_hits = None

                        if polish_res is not None and self.re_evaluate_after_polish:
                            self._emit(
                                on_progress,
                                SSE_PIPELINE_STAGE,
                                {
                                    "chapter": scene.chapter,
                                    "stage": "polish",
                                    "status": "running",
                                    "attempt": attempt,
                                    "max_attempts": max_attempts,
                                    "model": _active_model("critic"),
                                    "detail": f"精修已过门禁（{len(polish_res.replaced_indices)} 段）· 同口径复评验分中",
                                },
                            )
                            try:
                                post_polish_eval = self.critic_polisher.evaluate_chapter(
                                    polish_res.polished_text,
                                    scene=scene,
                                    project_meta=self.project.meta,
                                    scan_report=polish_res.scan_report,
                                    characters=self.project.characters,
                                    world_state=self.state.to_snapshot(),
                                    chapter=scene.chapter,
                                )
                            except (LLMError, CriticResponseParseError) as exc:
                                post_polish_eval = None
                                self._record_degradation(
                                    scene,
                                    attempt,
                                    stage="POST_POLISH_CRITIC_FAILED",
                                    exc=exc,
                                    action_taken="Post-polish critic re-eval failed -> keeping gated polished text",
                                    incidents_jsonl=incidents_jsonl,
                                )

                            if post_polish_eval is not None:
                                _write_json_atomic(
                                    audit_dir / f"ch{scene.chapter:04d}_critic_eval_post.json",
                                    post_polish_eval.to_dict(),
                                )
                                if post_polish_eval.overall_score < pre_polish_eval.overall_score:
                                    polish_reverted_for_score = True
                                    self._record_degradation(
                                        scene,
                                        attempt,
                                        stage="POLISH_SCORE_REGRESSION",
                                        exc=RuntimeError(
                                            f"Post-polish score {post_polish_eval.overall_score:.2f} < "
                                            f"pre-polish score {pre_polish_eval.overall_score:.2f}"
                                        ),
                                        action_taken="Post-polish score regressed -> keeping higher-scoring pre-polish draft",
                                        incidents_jsonl=incidents_jsonl,
                                    )
                                    polish_res = None
                                else:
                                    critic_eval = post_polish_eval

                        if polish_res is not None:
                            candidate_text = polish_res.polished_text
                            report = polish_res.scan_report
                            if polish_l1_hits is not None:
                                l1_hits = polish_l1_hits
                            polish_payload = polish_res.to_dict()
                            if pre_polish_eval is not None:
                                polish_payload["pre_polish_score"] = pre_polish_eval.overall_score
                            if post_polish_eval is not None:
                                polish_payload["post_polish_score"] = post_polish_eval.overall_score
                                polish_payload["score_delta"] = round(
                                    post_polish_eval.overall_score - pre_polish_eval.overall_score, 2
                                )
                            _write_json_atomic(
                                audit_dir / f"ch{scene.chapter:04d}_polish.json",
                                polish_payload,
                            )
                            self._emit(
                                on_progress,
                                SSE_SURGICAL_POLISHED,
                                {
                                    "chapter": scene.chapter,
                                    "replaced_indices": list(polish_res.replaced_indices),
                                    "pre_polish_score": pre_polish_eval.overall_score if pre_polish_eval else None,
                                    "post_polish_score": post_polish_eval.overall_score if post_polish_eval else None,
                                },
                            )

                if self.critic_polisher is not None:
                    if polish_res is not None and pre_polish_eval is not None and post_polish_eval is not None:
                        delta_val = post_polish_eval.overall_score - pre_polish_eval.overall_score
                        polish_detail = (
                            f"定点精修完成 · {pre_polish_eval.overall_score:.1f} → "
                            f"{post_polish_eval.overall_score:.1f} ({delta_val:+.1f}) · "
                            f"替换 {len(polish_res.replaced_indices)} 段"
                        )
                    elif polish_res is not None:
                        polish_detail = f"定点精修完成 · 替换 {len(polish_res.replaced_indices)} 段"
                    elif polish_reverted_for_score and pre_polish_eval is not None:
                        polish_detail = f"四维评分 {pre_polish_eval.overall_score:.1f} · 精修未提分，保留高分原稿"
                    elif critic_eval is not None:
                        polish_detail = f"四维评分 {critic_eval.overall_score:.1f} · 保留已过门禁原稿"
                    else:
                        polish_detail = "审校降级跳过 · 保留已过门禁原稿"
                    self._emit(
                        on_progress,
                        SSE_PIPELINE_STAGE,
                        {
                            "chapter": scene.chapter,
                            "stage": "polish",
                            "status": "done",
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "overall_score": round(float(critic_eval.overall_score), 2) if critic_eval is not None else None,
                            "replaced_count": len(polish_res.replaced_indices) if polish_res is not None else 0,
                            "detail": polish_detail,
                        },
                    )
                else:
                    self._emit(
                        on_progress,
                        SSE_PIPELINE_STAGE,
                        {
                            "chapter": scene.chapter,
                            "stage": "polish",
                            "status": "done",
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "detail": "直通成稿",
                        },
                    )

                if self.critic_polisher is not None:
                    try:
                        author_note = self.critic_polisher.generate_author_note(
                            candidate_text,
                            scene=scene,
                            project_meta=self.project.meta,
                            chapter=scene.chapter,
                        )
                    except (LLMError, CriticResponseParseError, AuthorNoteViolationError) as exc:
                        author_note = ""
                        self._record_degradation(
                            scene,
                            attempt,
                            stage="AUTHOR_NOTE_FAILED",
                            exc=exc,
                            action_taken="Author note generation failed -> empty author_note",
                            incidents_jsonl=incidents_jsonl,
                        )

                self._emit(
                    on_progress,
                    SSE_PIPELINE_STAGE,
                    {
                        "chapter": scene.chapter,
                        "stage": "lock",
                        "status": "running",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "char_count": int(report.char_count),
                        "detail": "写入章节哈希并推进世界状态快照",
                    },
                )

                print(
                    f"[Engine] Chapter {scene.chapter:04d} PASSED AI-TIC-CHECK "
                    f"({report.char_count} chars, {report.paragraph_count} paragraphs, attempt {attempt})",
                    flush=True,
                )

                ch_filename = format_chapter_filename(scene.chapter)
                ch_path = chapters_dir / ch_filename
                legacy_path = chapters_dir / f"chapter_{scene.chapter:02d}.txt"
                if legacy_path != ch_path and legacy_path.exists():
                    _archive_file(legacy_path, self.output_dir, run_stamp)

                _write_text_atomic(
                    ch_path,
                    candidate_text,
                    output_dir=self.output_dir,
                    archive_stamp=run_stamp,
                    require_nonempty=True,
                )

                public_title = (scene.public_title or scene.internal_title).strip()
                artifact = ChapterArtifact(
                    chapter=scene.chapter,
                    public_title=public_title,
                    body=candidate_text,
                    author_note=author_note,
                    sha256=_compute_sha256(candidate_text),
                    locked=scene.locked,
                    scene_id=scene.scene_id,
                )
                chapter_switches = (
                    [
                        ev.to_dict()
                        for ev in self.provider.get_model_switch_history()[switch_idx_start:]
                        if ev.chapter in (0, scene.chapter)
                    ]
                    if isinstance(self.provider, RoleModelRouter)
                    else []
                )
                actual_model = str(llm_resp.model or "").strip()
                critic_model = str(critic_eval.model or "").strip() if critic_eval is not None else ""
                polisher_model = str(polish_res.model or "").strip() if polish_res is not None else ""
                l1_warning_dicts = [
                    h.to_dict()
                    for h in l1_hits
                    if str(h.severity).upper() != "CRITICAL"
                ]
                if l1_warning_dicts:
                    _write_json_atomic(
                        audit_dir / f"ch{scene.chapter:04d}_l1_warnings.json",
                        {
                            "chapter": scene.chapter,
                            "attempt": attempt,
                            "warnings": l1_warning_dicts,
                        },
                    )
                meta_payload = {
                    **artifact.to_dict(),
                    "model": actual_model,
                    "writer_model": actual_model,
                    "critic_model": critic_model,
                    "polisher_model": polisher_model,
                    "model_switches": chapter_switches,
                    "l1_warnings": l1_warning_dicts,
                    "updated_at": _now_shanghai_iso(),
                }
                if critic_eval is not None:
                    meta_payload["critic_score"] = critic_eval.overall_score
                if pre_polish_eval is not None:
                    meta_payload["pre_polish_critic_score"] = pre_polish_eval.overall_score
                if polish_res is not None and pre_polish_eval is not None and post_polish_eval is not None:
                    meta_payload["polish_score_delta"] = round(
                        post_polish_eval.overall_score - pre_polish_eval.overall_score, 2
                    )
                meta_path = _chapter_meta_path(chapters_dir, scene.chapter)
                _write_json_atomic(
                    meta_path,
                    meta_payload,
                    output_dir=self.output_dir,
                    archive_stamp=run_stamp,
                )

                state_diff = self.state.advance_after_chapter(scene, candidate_text)
                # virtual_day 采用 advance_after_chapter 推进后的天数（与 state.virtual_day 一致）
                self.memory.record_chapter(
                    scene.chapter,
                    candidate_text,
                    virtual_day=self.state.virtual_day,
                    scene=scene,
                    entity_names=self._entity_names_map(),
                )

                snap_path = snapshots_dir / f"chapter_{scene.chapter:04d}_after.json"
                legacy_snap = snapshots_dir / f"chapter_{scene.chapter:02d}_after.json"
                if legacy_snap != snap_path and legacy_snap.exists():
                    _archive_file(legacy_snap, self.output_dir, run_stamp)
                if snap_path.exists():
                    _archive_file(snap_path, self.output_dir, run_stamp)
                self.state.save_snapshot(snap_path)

                mem_path = _resolve_memory_path(snapshots_dir, scene.chapter)
                if mem_path.exists():
                    _archive_file(mem_path, self.output_dir, run_stamp)
                self.memory.save(mem_path)

                print(
                    f"[Engine] Chapter {scene.chapter:04d} LOCKED sha256:{artifact.sha256[:12]} "
                    f"({report.char_count} chars, model={actual_model or 'unknown'})",
                    flush=True,
                )
                self._emit(
                    on_progress,
                    SSE_PIPELINE_STAGE,
                    {
                        "chapter": scene.chapter,
                        "stage": "lock",
                        "status": "done",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "char_count": int(report.char_count),
                        "sha256_prefix": artifact.sha256[:12],
                        "model": actual_model,
                        "detail": f"LOCKED sha256:{artifact.sha256[:10]}",
                    },
                )
                self._emit(
                    on_progress,
                    SSE_CHAPTER_STOCKPILED,
                    {
                        "chapter": scene.chapter,
                        "char_count": report.char_count,
                        "sha256": artifact.sha256,
                        "locked": artifact.locked,
                        "model": actual_model,
                    },
                )

                return {
                    "chapter": scene.chapter,
                    "volume": scene.volume,
                    "scene_id": scene.scene_id,
                    "internal_title": scene.internal_title,
                    "public_title": public_title,
                    "pov_character": self.project.characters[scene.pov_character].name,
                    "attempts_used": attempt,
                    "rewrites_triggered": attempt - 1,
                    "char_count": report.char_count,
                    "paragraph_count": report.paragraph_count,
                    "first_char": candidate_text[0],
                    "opening_preview": candidate_text[:68].replace("\n", " "),
                    "ending_preview": candidate_text[-68:].replace("\n", " "),
                    "model": actual_model,
                    "writer_model": actual_model,
                    "critic_model": critic_model,
                    "polisher_model": polisher_model,
                    "model_switches": chapter_switches,
                    "latency_sec": llm_resp.latency_sec,
                    "chapter_file": str(ch_path),
                    "meta_file": str(meta_path),
                    "author_note": author_note,
                    "sha256": artifact.sha256,
                    "locked": artifact.locked,
                    "critic_evaluation": critic_eval.to_dict() if critic_eval is not None else None,
                    "polish_summary": (
                        {
                            "replaced_indices": list(polish_res.replaced_indices),
                            "model": polish_res.model,
                            **(
                                {
                                    "pre_polish_score": pre_polish_eval.overall_score,
                                    "post_polish_score": post_polish_eval.overall_score,
                                    "score_delta": round(
                                        post_polish_eval.overall_score - pre_polish_eval.overall_score, 2
                                    ),
                                }
                                if pre_polish_eval is not None and post_polish_eval is not None
                                else {}
                            ),
                        }
                        if polish_res is not None
                        else None
                    ),
                    "l1_warnings": l1_warning_dicts,
                    "state_transitions": state_diff,
                }

            # Record Production Incident & trigger automatic rewrite
            incident = ProductionIncident(
                incident_id=f"INC-CH{scene.chapter:04d}-A{attempt}",
                chapter=scene.chapter,
                scene_id=scene.scene_id,
                attempt=attempt,
                timestamp=_now_shanghai_iso(),
                violations=report.violations,
                action_taken=(
                    f"Triggered automatic rewrite attempt {attempt + 1}"
                    if attempt < max_attempts
                    else "Exhausted retries — aborting pipeline"
                ),
            )
            self.incidents.append(incident)
            with incidents_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(incident.to_dict(), ensure_ascii=False) + "\n")

            _write_text_atomic(
                audit_dir / f"ch{scene.chapter:04d}_attempt{attempt}_rejected.txt",
                candidate_text,
            )
            print(
                f"[Engine] PRODUCTION INCIDENT {incident.incident_id}: "
                f"{len(report.violations)} violation(s) -> {incident.action_taken}",
                flush=True,
            )
            self._emit(
                on_progress,
                SSE_GATE_FAILED,
                {
                    "chapter": scene.chapter,
                    "attempt": attempt,
                    "incident_id": incident.incident_id,
                    "violation_count": len(report.violations),
                },
            )
            last_report = report
            last_draft = candidate_text

        raise ProductionGateFailure(
            f"Chapter {scene.chapter} failed AI-TIC-CHECK after {max_attempts} attempts. "
            f"Last violations: {[v.to_dict() for v in (last_report.violations if last_report else [])]}"
        )
