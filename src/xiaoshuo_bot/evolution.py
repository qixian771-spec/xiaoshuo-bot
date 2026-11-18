from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import json
import math
import os
from pathlib import Path
import random
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .genres import get_genre
from .models import SHANGHAI_TZ, ProjectMeta, ProjectValidationError


_UNSETTLED_SENTINELS = frozenset({"", "NOT_SETTLED", "UNSETTLED", "PENDING", "N/A", "NA", "--", "-"})


class EvolutionError(Exception):
    """Base exception for T09 evolution, racing, and feedback optimization."""


class MetricsNotSettledError(ValueError, EvolutionError):
    """Raised when required book performance metrics are None, unset, or marked not settled."""


class PublishedChapterImmutableError(ValueError, EvolutionError):
    """Raised when attempting to target a chapter <= max_published_chapter for outline adjustment."""


class PackagingPoolExhaustedError(RuntimeError, EvolutionError):
    """Raised when packaging rotation is required but the alternate title pool is empty."""


class InvalidAlternateTitleError(ProjectValidationError, EvolutionError):
    """Raised when a candidate alternate title violates Tomato's 1..15 char / no-《》 constraint."""


class RacingAction(str, Enum):
    PROMOTE_DAILY_UPDATE = "PROMOTE_DAILY_UPDATE"
    ROTATE_PACKAGING = "ROTATE_PACKAGING"
    SUGGEST_CUT = "SUGGEST_CUT"
    HOLD_OBSERVE = "HOLD_OBSERVE"


def _now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def validate_tomato_title(title: str, label: str = "alternate_title") -> str:
    """Validate Tomato book title: 1..15 chars and strictly no '《》'."""
    if not isinstance(title, str):
        raise InvalidAlternateTitleError(
            f"Tomato {label} must be a string, got {type(title).__name__}"
        )
    if "《" in title or "》" in title:
        raise InvalidAlternateTitleError(
            f"Tomato {label} must not contain book title marks '《》': {title!r}"
        )
    stripped = title.strip()
    if not stripped or len(stripped) > 15 or len(title) > 15:
        raise InvalidAlternateTitleError(
            f"Tomato {label} length must be 1..15 characters (got {len(title)}): {title!r}"
        )
    return stripped


def _parse_settled_ratio(
    value: Any,
    field_name: str,
    *,
    required: bool,
) -> Optional[float]:
    if value is None:
        if required:
            raise MetricsNotSettledError(
                f"Key metric '{field_name}' is None (unsettled); never coerce to 0.0"
            )
        return None
    if isinstance(value, bool):
        raise MetricsNotSettledError(
            f"Key metric '{field_name}' cannot be boolean: {value!r}"
        )
    if isinstance(value, str):
        token = value.strip().upper()
        if token in _UNSETTLED_SENTINELS:
            raise MetricsNotSettledError(
                f"Key metric '{field_name}' is marked unsettled ({value!r}); never coerce to 0.0"
            )
        try:
            numeric = float(value.strip())
        except ValueError as exc:
            raise MetricsNotSettledError(
                f"Key metric '{field_name}' is not a valid number: {value!r}"
            ) from exc
    elif isinstance(value, (int, float)):
        numeric = float(value)
    else:
        raise MetricsNotSettledError(
            f"Key metric '{field_name}' has unsupported type {type(value).__name__}"
        )

    if math.isnan(numeric) or math.isinf(numeric):
        raise MetricsNotSettledError(
            f"Key metric '{field_name}' is non-finite ({numeric!r})"
        )
    if numeric < 0.0 or numeric > 1.0:
        raise ValueError(
            f"Metric '{field_name}' ratio must be within [0.0, 1.0], got {numeric}"
        )
    return numeric


def _parse_settled_int(
    value: Any,
    field_name: str,
    *,
    required: bool,
) -> Optional[int]:
    if value is None:
        if required:
            raise MetricsNotSettledError(
                f"Key metric '{field_name}' is None (unsettled); never coerce to 0"
            )
        return None
    if isinstance(value, bool):
        raise MetricsNotSettledError(
            f"Key metric '{field_name}' cannot be boolean: {value!r}"
        )
    if isinstance(value, str):
        token = value.strip().upper()
        if token in _UNSETTLED_SENTINELS:
            raise MetricsNotSettledError(
                f"Key metric '{field_name}' is marked unsettled ({value!r}); never coerce to 0"
            )
        try:
            numeric = int(value.strip())
        except ValueError as exc:
            raise MetricsNotSettledError(
                f"Key metric '{field_name}' is not a valid integer: {value!r}"
            ) from exc
    elif isinstance(value, int):
        numeric = value
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value) or not value.is_integer():
            raise ValueError(f"Metric '{field_name}' must be an integer, got {value!r}")
        numeric = int(value)
    else:
        raise MetricsNotSettledError(
            f"Key metric '{field_name}' has unsupported type {type(value).__name__}"
        )

    if numeric < 0:
        raise ValueError(f"Metric '{field_name}' must be >= 0, got {numeric}")
    return numeric


# =============================================================================
# 1. Deterministic Racing Promotion / Packaging Rescue / Cut-Loss Engine
# =============================================================================


@dataclass(frozen=True)
class RacingThresholds:
    promote_ch2_read_through: float = 0.60
    promote_bookshelf_rate: float = 0.20
    promote_word100k_completion: float = 0.25
    rotate_min_ch2_read_through: float = 0.58
    rotate_max_ctr_exclusive: float = 0.15
    rotate_max_bookshelf_exclusive: float = 0.15
    cut_min_impressions: int = 1000
    cut_max_ch2_exclusive: float = 0.45
    cut_max_word100k_exclusive: float = 0.10
    max_daily_chapters: int = 5


@dataclass
class BookMetrics:
    book_id: str
    ch2_read_through: Any = None
    bookshelf_rate: Any = None
    click_through_rate: Any = None
    word100k_completion: Any = None
    impressions: Any = None
    settled: bool = True
    settlement_status: str = "SETTLED"
    signed: bool = False
    current_title: str = ""
    current_daily_chapters: int = 2
    total_words: int = 0
    debut_reached: bool = False
    collected_at: str = field(default_factory=_now_shanghai_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionDirective:
    book_id: str
    previous_daily_chapters: int
    new_daily_chapters: int
    signal_long_serial_extension: bool
    reason: str
    created_at: str = field(default_factory=_now_shanghai_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PackagingRotationTask:
    book_id: str
    old_title: str
    new_title: str
    new_blurb: Optional[str]
    regenerate_cover: bool
    remaining_titles: List[str]
    remaining_blurbs: List[str]
    reason: str
    created_at: str = field(default_factory=_now_shanghai_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CutLossRecommendation:
    book_id: str
    requires_telegram_confirmation: bool
    suggested_mode: str
    ch2_read_through: float
    word100k_completion: Optional[float]
    impressions: Optional[int]
    reason: str
    created_at: str = field(default_factory=_now_shanghai_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RacingDecision:
    book_id: str
    action: RacingAction
    reason: str
    metrics: BookMetrics
    evaluated_at: str = field(default_factory=_now_shanghai_iso)
    promotion: Optional[PromotionDirective] = None
    packaging_task: Optional[PackagingRotationTask] = None
    cut_recommendation: Optional[CutLossRecommendation] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "book_id": self.book_id,
            "action": self.action.value,
            "reason": self.reason,
            "metrics": self.metrics.to_dict(),
            "evaluated_at": self.evaluated_at,
            "promotion": self.promotion.to_dict() if self.promotion else None,
            "packaging_task": self.packaging_task.to_dict() if self.packaging_task else None,
            "cut_recommendation": (
                self.cut_recommendation.to_dict() if self.cut_recommendation else None
            ),
        }


class RacingDecisionEngine:
    """100% deterministic Tomato racing promotion / packaging rescue / cut-loss engine."""

    def __init__(self, thresholds: Optional[RacingThresholds] = None) -> None:
        self.thresholds = thresholds or RacingThresholds()

    def evaluate(
        self,
        metrics: BookMetrics,
        *,
        alternate_titles: Optional[List[str]] = None,
        alternate_blurbs: Optional[List[str]] = None,
        mutate_pool: bool = True,
    ) -> RacingDecision:
        if not isinstance(metrics.book_id, str) or not metrics.book_id.strip():
            raise ValueError("BookMetrics.book_id must be a non-empty string")
        if not metrics.settled:
            raise MetricsNotSettledError(
                f"Book '{metrics.book_id}' metrics are marked settled=False; refusing evaluation"
            )
        status_norm = str(metrics.settlement_status).strip().upper()
        if status_norm != "SETTLED":
            raise MetricsNotSettledError(
                f"Book '{metrics.book_id}' settlement_status is {metrics.settlement_status!r}, "
                f"expected 'SETTLED'"
            )

        ch2 = _parse_settled_ratio(metrics.ch2_read_through, "ch2_read_through", required=True)
        bookshelf = _parse_settled_ratio(metrics.bookshelf_rate, "bookshelf_rate", required=True)
        ctr = _parse_settled_ratio(metrics.click_through_rate, "click_through_rate", required=False)
        word100k = _parse_settled_ratio(
            metrics.word100k_completion, "word100k_completion", required=False
        )
        impressions = _parse_settled_int(metrics.impressions, "impressions", required=False)

        assert ch2 is not None and bookshelf is not None
        th = self.thresholds
        now_iso = _now_shanghai_iso()

        # 1. Potential Promotion (PROMOTE_DAILY_UPDATE)
        ch2_bookshelf_promoted = (
            ch2 >= th.promote_ch2_read_through and bookshelf >= th.promote_bookshelf_rate
        )
        word100k_promoted = (
            word100k is not None and word100k >= th.promote_word100k_completion
        )
        if ch2_bookshelf_promoted or word100k_promoted:
            prev_daily = max(1, int(metrics.current_daily_chapters))
            new_daily = min(th.max_daily_chapters, max(3, prev_daily + 1))
            if prev_daily >= th.max_daily_chapters:
                new_daily = prev_daily
            trigger_desc = (
                f"ch2_read_through={ch2:.1%} >= {th.promote_ch2_read_through:.0%} and "
                f"bookshelf_rate={bookshelf:.1%} >= {th.promote_bookshelf_rate:.0%}"
                if ch2_bookshelf_promoted
                else f"word100k_completion={word100k:.1%} >= {th.promote_word100k_completion:.0%}"
            )
            reason = (
                f"Potential hit confirmed ({trigger_desc}); raising daily target "
                f"{prev_daily} -> {new_daily} chapters and signaling long-serial extension."
            )
            return RacingDecision(
                book_id=metrics.book_id,
                action=RacingAction.PROMOTE_DAILY_UPDATE,
                reason=reason,
                metrics=metrics,
                evaluated_at=now_iso,
                promotion=PromotionDirective(
                    book_id=metrics.book_id,
                    previous_daily_chapters=prev_daily,
                    new_daily_chapters=new_daily,
                    signal_long_serial_extension=True,
                    reason=reason,
                    created_at=now_iso,
                ),
            )

        # 2. Cold-start "Good prose, weak packaging" Rescue (ROTATE_PACKAGING)
        if ch2 >= th.rotate_min_ch2_read_through:
            weak_bookshelf = bookshelf < th.rotate_max_bookshelf_exclusive
            if not weak_bookshelf and ctr is None:
                raise MetricsNotSettledError(
                    f"Book '{metrics.book_id}' has ch2_read_through={ch2:.1%} >= "
                    f"{th.rotate_min_ch2_read_through:.0%} and bookshelf_rate={bookshelf:.1%} >= "
                    f"{th.rotate_max_bookshelf_exclusive:.0%}; click_through_rate is required "
                    f"(not None) to determine packaging health"
                )
            weak_ctr = ctr is not None and ctr < th.rotate_max_ctr_exclusive
            if weak_bookshelf or weak_ctr:
                if metrics.signed:
                    return RacingDecision(
                        book_id=metrics.book_id,
                        action=RacingAction.HOLD_OBSERVE,
                        reason=(
                            f"Strong retention (ch2_read_through={ch2:.1%}) with weak packaging "
                            f"(ctr={ctr!r}, bookshelf_rate={bookshelf:.1%}), but signed=True "
                            f"locks title/contract; skipping packaging rotation."
                        ),
                        metrics=metrics,
                        evaluated_at=now_iso,
                    )

                title_pool = list(alternate_titles) if alternate_titles is not None else []
                blurb_pool = list(alternate_blurbs) if alternate_blurbs is not None else []
                if not title_pool:
                    raise PackagingPoolExhaustedError(
                        f"Book '{metrics.book_id}' qualifies for ROTATE_PACKAGING "
                        f"(ch2={ch2:.1%}, ctr={ctr!r}, bookshelf={bookshelf:.1%}, signed=False) "
                        f"but alternate_titles pool is empty"
                    )

                raw_next_title = title_pool.pop(0)
                validated_title = validate_tomato_title(raw_next_title, "alternate_title")
                next_blurb = blurb_pool.pop(0) if blurb_pool else None

                if mutate_pool and alternate_titles is not None:
                    alternate_titles.pop(0)
                if mutate_pool and alternate_blurbs is not None and alternate_blurbs:
                    alternate_blurbs.pop(0)

                weak_parts: List[str] = []
                if weak_ctr and ctr is not None:
                    weak_parts.append(
                        f"click_through_rate={ctr:.1%} < {th.rotate_max_ctr_exclusive:.0%}"
                    )
                if weak_bookshelf:
                    weak_parts.append(
                        f"bookshelf_rate={bookshelf:.1%} < {th.rotate_max_bookshelf_exclusive:.0%}"
                    )
                reason = (
                    f"Strong content retention (ch2_read_through={ch2:.1%} >= "
                    f"{th.rotate_min_ch2_read_through:.0%}) with weak packaging "
                    f"({', '.join(weak_parts)}) and signed=False; rotating to alternate title "
                    f"'{validated_title}' and regenerating cover."
                )
                return RacingDecision(
                    book_id=metrics.book_id,
                    action=RacingAction.ROTATE_PACKAGING,
                    reason=reason,
                    metrics=metrics,
                    evaluated_at=now_iso,
                    packaging_task=PackagingRotationTask(
                        book_id=metrics.book_id,
                        old_title=metrics.current_title,
                        new_title=validated_title,
                        new_blurb=next_blurb,
                        regenerate_cover=True,
                        remaining_titles=title_pool,
                        remaining_blurbs=blurb_pool,
                        reason=reason,
                        created_at=now_iso,
                    ),
                )

        # 3. Cut-Loss Suggestion (SUGGEST_CUT)
        low_ch2 = ch2 < th.cut_max_ch2_exclusive
        low_100k = word100k is not None and word100k < th.cut_max_word100k_exclusive
        if low_ch2 or low_100k:
            if not metrics.debut_reached and impressions is None:
                raise MetricsNotSettledError(
                    f"Book '{metrics.book_id}' has low retention (ch2={ch2:.1%}, "
                    f"word100k={word100k!r}) while debut_reached=False; impressions is required "
                    f"to verify sample sufficiency"
                )
            sample_sufficient = metrics.debut_reached or (
                impressions is not None and impressions >= th.cut_min_impressions
            )
            if sample_sufficient:
                causes: List[str] = []
                if low_ch2:
                    causes.append(
                        f"ch2_read_through={ch2:.1%} < {th.cut_max_ch2_exclusive:.0%}"
                    )
                if low_100k and word100k is not None:
                    causes.append(
                        f"word100k_completion={word100k:.1%} < {th.cut_max_word100k_exclusive:.0%}"
                    )
                reason = (
                    f"Sufficient sample (impressions={impressions}, debut_reached={metrics.debut_reached}) "
                    f"with sub-threshold retention ({', '.join(causes)}); recommending cut-loss "
                    f"(pending Telegram 2-step confirmation or finale wrap-up)."
                )
                return RacingDecision(
                    book_id=metrics.book_id,
                    action=RacingAction.SUGGEST_CUT,
                    reason=reason,
                    metrics=metrics,
                    evaluated_at=now_iso,
                    cut_recommendation=CutLossRecommendation(
                        book_id=metrics.book_id,
                        requires_telegram_confirmation=True,
                        suggested_mode="cut_confirm_or_finale",
                        ch2_read_through=ch2,
                        word100k_completion=word100k,
                        impressions=impressions,
                        reason=reason,
                        created_at=now_iso,
                    ),
                )
            return RacingDecision(
                book_id=metrics.book_id,
                action=RacingAction.HOLD_OBSERVE,
                reason=(
                    f"Low retention (ch2={ch2:.1%}) but insufficient sample size "
                    f"(impressions={impressions} < {th.cut_min_impressions}, debut_reached=False); "
                    f"continuing observation."
                ),
                metrics=metrics,
                evaluated_at=now_iso,
            )

        # 4. Middle Band -> Hold & Observe
        return RacingDecision(
            book_id=metrics.book_id,
            action=RacingAction.HOLD_OBSERVE,
            reason=(
                f"Settled metrics in normal observation band (ch2_read_through={ch2:.1%}, "
                f"bookshelf_rate={bookshelf:.1%}, ctr={ctr!r}); holding current cadence."
            ),
            metrics=metrics,
            evaluated_at=now_iso,
        )


def evaluate_book_racing(
    metrics: BookMetrics,
    *,
    alternate_titles: Optional[List[str]] = None,
    alternate_blurbs: Optional[List[str]] = None,
    thresholds: Optional[RacingThresholds] = None,
    mutate_pool: bool = True,
) -> RacingDecision:
    """Convenience functional wrapper around RacingDecisionEngine.evaluate."""
    engine = RacingDecisionEngine(thresholds=thresholds)
    return engine.evaluate(
        metrics,
        alternate_titles=alternate_titles,
        alternate_blurbs=alternate_blurbs,
        mutate_pool=mutate_pool,
    )


def evaluate_project_racing(
    meta: ProjectMeta,
    metrics: BookMetrics,
    *,
    thresholds: Optional[RacingThresholds] = None,
    mutate_meta: bool = True,
) -> RacingDecision:
    """Evaluate racing decision using ProjectMeta's signed flag and alternate pools."""
    enriched = BookMetrics(
        book_id=metrics.book_id or meta.book_id or meta.project_id,
        ch2_read_through=metrics.ch2_read_through,
        bookshelf_rate=metrics.bookshelf_rate,
        click_through_rate=metrics.click_through_rate,
        word100k_completion=metrics.word100k_completion,
        impressions=metrics.impressions,
        settled=metrics.settled,
        settlement_status=metrics.settlement_status,
        signed=metrics.signed or meta.signed,
        current_title=metrics.current_title or meta.title,
        current_daily_chapters=metrics.current_daily_chapters,
        total_words=metrics.total_words,
        debut_reached=metrics.debut_reached,
        collected_at=metrics.collected_at,
    )
    decision = evaluate_book_racing(
        enriched,
        alternate_titles=meta.alternate_titles,
        alternate_blurbs=meta.alternate_blurbs,
        thresholds=thresholds,
        mutate_pool=mutate_meta,
    )
    if mutate_meta and decision.action == RacingAction.ROTATE_PACKAGING and decision.packaging_task:
        meta.title = decision.packaging_task.new_title
        if decision.packaging_task.new_blurb is not None:
            meta.synopsis = decision.packaging_task.new_blurb
    return decision


# =============================================================================
# 2. Bayesian Motif Weight Library (MotifWeightStore)
# =============================================================================


@dataclass
class MotifWeightEntry:
    genre_id: str
    opening_motif: str
    golden_finger_type: str
    alpha: float = 1.0
    beta: float = 1.0
    trials: int = 0
    successes: int = 0
    failures: int = 0
    avg_ch2_read_through: float = 0.0
    updated_at: str = field(default_factory=_now_shanghai_iso)

    @property
    def posterior_mean(self) -> float:
        total = self.alpha + self.beta
        if total <= 0.0:
            raise ValueError(f"Invalid Beta parameters alpha={self.alpha}, beta={self.beta}")
        return self.alpha / total

    @property
    def posterior_variance(self) -> float:
        a = self.alpha
        b = self.beta
        total = a + b
        return (a * b) / ((total * total) * (total + 1.0))

    @property
    def key(self) -> str:
        return f"{self.genre_id}::{self.opening_motif}::{self.golden_finger_type}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "genre_id": self.genre_id,
            "opening_motif": self.opening_motif,
            "golden_finger_type": self.golden_finger_type,
            "alpha": self.alpha,
            "beta": self.beta,
            "trials": self.trials,
            "successes": self.successes,
            "failures": self.failures,
            "avg_ch2_read_through": self.avg_ch2_read_through,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MotifWeightEntry:
        genre_id = str(data["genre_id"]).strip()
        get_genre(genre_id)
        return cls(
            genre_id=genre_id,
            opening_motif=str(data["opening_motif"]).strip(),
            golden_finger_type=str(data["golden_finger_type"]).strip(),
            alpha=float(data["alpha"]),
            beta=float(data["beta"]),
            trials=int(data["trials"]),
            successes=int(data["successes"]),
            failures=int(data["failures"]),
            avg_ch2_read_through=float(data.get("avg_ch2_read_through", 0.0)),
            updated_at=str(data.get("updated_at", _now_shanghai_iso())),
        )


def _validate_motif_key(
    genre_id: str, opening_motif: str, golden_finger_type: str
) -> Tuple[str, str, str]:
    get_genre(genre_id)
    gid = genre_id.strip()
    if not isinstance(opening_motif, str) or not opening_motif.strip():
        raise ValueError(f"opening_motif must be a non-empty string, got {opening_motif!r}")
    if not isinstance(golden_finger_type, str) or not golden_finger_type.strip():
        raise ValueError(
            f"golden_finger_type must be a non-empty string, got {golden_finger_type!r}"
        )
    return gid, opening_motif.strip(), golden_finger_type.strip()


class MotifWeightStore:
    """Persistent Bayesian Beta(alpha, beta) motif weight library per genre/motif/golden_finger."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
    ) -> None:
        if prior_alpha <= 0.0 or prior_beta <= 0.0:
            raise ValueError("prior_alpha and prior_beta must be > 0")
        self.store_path = Path(store_path)
        self.prior_alpha = float(prior_alpha)
        self.prior_beta = float(prior_beta)
        self._entries: Dict[str, MotifWeightEntry] = {}
        if self.store_path.exists():
            self._load()

    def _load(self) -> None:
        with self.store_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"MotifWeightStore JSON root must be an object: {self.store_path}")
        entries_raw = raw.get("entries", {})
        if not isinstance(entries_raw, dict):
            raise ValueError(f"MotifWeightStore 'entries' must be an object: {self.store_path}")
        loaded: Dict[str, MotifWeightEntry] = {}
        for key, item in entries_raw.items():
            entry = MotifWeightEntry.from_dict(item)
            loaded[entry.key if entry.key == key else str(key)] = entry
        self._entries = loaded

    def save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.store_path.with_name(f"{self.store_path.name}.tmp")
        payload = {
            "version": 1,
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
            "updated_at": _now_shanghai_iso(),
            "entries": {k: v.to_dict() for k, v in sorted(self._entries.items())},
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.store_path)

    def get_entry(
        self,
        genre_id: str,
        opening_motif: str,
        golden_finger_type: str,
    ) -> MotifWeightEntry:
        gid, motif, gf = _validate_motif_key(genre_id, opening_motif, golden_finger_type)
        key = f"{gid}::{motif}::{gf}"
        if key in self._entries:
            return self._entries[key]
        return MotifWeightEntry(
            genre_id=gid,
            opening_motif=motif,
            golden_finger_type=gf,
            alpha=self.prior_alpha,
            beta=self.prior_beta,
            trials=0,
            successes=0,
            failures=0,
            avg_ch2_read_through=0.0,
            updated_at=_now_shanghai_iso(),
        )

    def get_posterior_mean(
        self,
        genre_id: str,
        opening_motif: str,
        golden_finger_type: str,
    ) -> float:
        return self.get_entry(genre_id, opening_motif, golden_finger_type).posterior_mean

    def record_outcome(
        self,
        genre_id: str,
        opening_motif: str,
        golden_finger_type: str,
        *,
        success: Optional[bool] = None,
        ch2_read_through: Any = None,
        bookshelf_rate: Any = None,
        weight: float = 1.0,
        settled: bool = True,
    ) -> MotifWeightEntry:
        if not settled:
            raise MetricsNotSettledError(
                "Cannot update MotifWeightStore with unsettled metrics (settled=False)"
            )
        if weight <= 0.0 or math.isnan(weight) or math.isinf(weight):
            raise ValueError(f"Outcome weight must be finite and > 0, got {weight!r}")

        gid, motif, gf = _validate_motif_key(genre_id, opening_motif, golden_finger_type)
        ch2_val = _parse_settled_ratio(ch2_read_through, "ch2_read_through", required=False)
        bookshelf_val = _parse_settled_ratio(bookshelf_rate, "bookshelf_rate", required=False)

        if success is None:
            if ch2_val is None:
                raise MetricsNotSettledError(
                    "Either explicit 'success' bool or settled 'ch2_read_through' is required"
                )
            if bookshelf_val is not None:
                is_success = ch2_val >= 0.58 and bookshelf_val >= 0.15
            else:
                is_success = ch2_val >= 0.58
        else:
            if not isinstance(success, bool):
                raise ValueError(f"success must be a bool, got {type(success).__name__}")
            is_success = success

        key = f"{gid}::{motif}::{gf}"
        entry = self._entries.get(key)
        if entry is None:
            entry = MotifWeightEntry(
                genre_id=gid,
                opening_motif=motif,
                golden_finger_type=gf,
                alpha=self.prior_alpha,
                beta=self.prior_beta,
            )
            self._entries[key] = entry

        if is_success:
            entry.alpha += float(weight)
            entry.successes += 1
        else:
            entry.beta += float(weight)
            entry.failures += 1

        prev_trials = entry.trials
        entry.trials += 1
        if ch2_val is not None:
            entry.avg_ch2_read_through = (
                (entry.avg_ch2_read_through * prev_trials) + ch2_val
            ) / entry.trials
        entry.updated_at = _now_shanghai_iso()
        self.save()
        return entry

    def record_racing_decision(
        self,
        decision: RacingDecision,
        *,
        genre_id: str,
        opening_motif: str,
        golden_finger_type: str,
        weight: float = 1.0,
    ) -> Optional[MotifWeightEntry]:
        """Update motif weights from a settled RacingDecision (HOLD_OBSERVE makes no update)."""
        ch2 = _parse_settled_ratio(
            decision.metrics.ch2_read_through, "ch2_read_through", required=True
        )
        bookshelf = _parse_settled_ratio(
            decision.metrics.bookshelf_rate, "bookshelf_rate", required=True
        )
        if decision.action in (RacingAction.PROMOTE_DAILY_UPDATE, RacingAction.ROTATE_PACKAGING):
            return self.record_outcome(
                genre_id,
                opening_motif,
                golden_finger_type,
                success=True,
                ch2_read_through=ch2,
                bookshelf_rate=bookshelf,
                weight=weight,
                settled=True,
            )
        if decision.action == RacingAction.SUGGEST_CUT:
            return self.record_outcome(
                genre_id,
                opening_motif,
                golden_finger_type,
                success=False,
                ch2_read_through=ch2,
                bookshelf_rate=bookshelf,
                weight=weight,
                settled=True,
            )
        return None

    def rank_motifs(
        self,
        genre_id: Optional[str] = None,
        *,
        limit: Optional[int] = None,
    ) -> List[MotifWeightEntry]:
        if genre_id is not None:
            get_genre(genre_id)
            gid = genre_id.strip()
            pool = [e for e in self._entries.values() if e.genre_id == gid]
        else:
            pool = list(self._entries.values())

        ranked = sorted(
            pool,
            key=lambda e: (
                -e.posterior_mean,
                -e.trials,
                e.genre_id,
                e.opening_motif,
                e.golden_finger_type,
            ),
        )
        if limit is not None:
            if limit < 0:
                raise ValueError(f"limit must be >= 0, got {limit}")
            return ranked[:limit]
        return ranked

    def sample_top_motifs(
        self,
        genre_id: str,
        *,
        top_k: int = 3,
        candidates: Optional[Sequence[Tuple[str, str]]] = None,
        mode: str = "deterministic",
        seed: Optional[int] = None,
    ) -> List[MotifWeightEntry]:
        if top_k <= 0:
            raise ValueError(f"top_k must be > 0, got {top_k}")
        get_genre(genre_id)
        gid = genre_id.strip()

        if candidates is not None:
            pool = [self.get_entry(gid, m, gf) for m, gf in candidates]
        else:
            pool = [e for e in self._entries.values() if e.genre_id == gid]

        if not pool:
            return []

        if mode == "deterministic":
            return sorted(
                pool,
                key=lambda e: (
                    -e.posterior_mean,
                    -e.trials,
                    e.opening_motif,
                    e.golden_finger_type,
                ),
            )[:top_k]
        if mode == "thompson":
            rng = random.Random(seed)
            scored = [
                (rng.betavariate(e.alpha, e.beta), e)
                for e in pool
            ]
            scored.sort(
                key=lambda item: (
                    -item[0],
                    -item[1].posterior_mean,
                    item[1].opening_motif,
                    item[1].golden_finger_type,
                )
            )
            return [e for _, e in scored[:top_k]]
        raise ValueError(f"Unsupported mode {mode!r}; expected 'deterministic' or 'thompson'")


# =============================================================================
# 3. Reader Comment Sentiment & Immutability-Guarded Outline Optimizer
# =============================================================================


def assert_chapter_mutable(target_chapter: int, max_published_chapter: int) -> None:
    """Strictly forbid adjusting or touching any chapter <= max_published_chapter."""
    if isinstance(target_chapter, bool) or not isinstance(target_chapter, int):
        raise ValueError(f"target_chapter must be an int, got {target_chapter!r}")
    if isinstance(max_published_chapter, bool) or not isinstance(max_published_chapter, int):
        raise ValueError(
            f"max_published_chapter must be an int, got {max_published_chapter!r}"
        )
    if max_published_chapter < 0:
        raise ValueError(f"max_published_chapter must be >= 0, got {max_published_chapter}")
    if target_chapter < 1:
        raise ValueError(f"target_chapter must be >= 1, got {target_chapter}")
    if target_chapter <= max_published_chapter:
        raise PublishedChapterImmutableError(
            f"Target chapter {target_chapter} <= max_published_chapter ({max_published_chapter}): "
            f"published chapters are strictly immutable to protect Tomato recommendation pool weight."
        )


@dataclass(frozen=True)
class FeedbackSignalRule:
    signal_type: str
    default_priority: int
    sentiment: str  # "negative" | "positive" | "neutral"
    keywords: Tuple[str, ...]
    instruction_template: str


FEEDBACK_SIGNAL_RULES: Tuple[FeedbackSignalRule, ...] = (
    FeedbackSignalRule(
        signal_type="ACCELERATE_PACE",
        default_priority=1,
        sentiment="negative",
        keywords=(
            "节奏慢",
            "太慢",
            "拖沓",
            "水字数",
            "水了",
            "墨迹",
            "磨叽",
            "废话多",
            "推进慢",
            "铺垫太长",
        ),
        instruction_template=(
            "压缩过渡铺垫与重复心理描写，每章前300字必须切入实质冲突或资源推进，单事件严禁拖过2章。"
        ),
    ),
    FeedbackSignalRule(
        signal_type="RESOLVE_VILLAIN_SOONER",
        default_priority=2,
        sentiment="negative",
        keywords=(
            "反派蹦跶太久",
            "蹦跶太久",
            "反派蹦跶",
            "还没死",
            "还不杀",
            "打脸太慢",
            "反派戏太多",
            "忍太久",
            "憋屈",
            "太窝囊",
        ),
        instruction_template=(
            "加速现阶段压迫反派的清算与打脸兑现，严禁同一小反派连续挑衅超过3章不还手。"
        ),
    ),
    FeedbackSignalRule(
        signal_type="ENFORCE_DECISIVE_PROTAGONIST",
        default_priority=3,
        sentiment="negative",
        keywords=(
            "圣母",
            "太圣母",
            "心软",
            "白莲花",
            "降智",
            "烂好人",
            "放过他",
            "原谅",
        ),
        instruction_template=(
            "强化主角利益边界与果断止损原则，严禁无合理收益地心软放虎归山或强行降智妥协。"
        ),
    ),
    FeedbackSignalRule(
        signal_type="TIGHTEN_LOGIC_AND_CAPS",
        default_priority=4,
        sentiment="negative",
        keywords=(
            "战力崩",
            "前后矛盾",
            "逻辑不通",
            "穿帮",
            "物价崩",
            "数值崩",
            "bug",
            "BUG",
        ),
        instruction_template=(
            "严格核验前文分卷数值上限锁、资产账本与硬常识防穿帮卡，补齐前置因果兑现。"
        ),
    ),
    FeedbackSignalRule(
        signal_type="BOOST_SUPPORTING_CHARACTER",
        default_priority=5,
        sentiment="positive",
        keywords=(
            "配角讨喜",
            "好讨喜",
            "太讨喜",
            "真讨喜",
            "多点戏份",
            "加戏",
            "意难平",
            "神助攻",
            "太可爱",
            "很讨喜",
        ),
        instruction_template=(
            "适当增加高人气配角的有效互动与关键助攻高光戏份，保持角色记忆点不断线。"
        ),
    ),
)

_POSITIVE_LEXICON: Tuple[str, ...] = (
    "好看",
    "爽",
    "催更",
    "不够看",
    "带感",
    "上头",
    "期待",
    "讨喜",
    "神助攻",
    "给力",
)

_NEGATIVE_LEXICON: Tuple[str, ...] = (
    "节奏慢",
    "拖沓",
    "水字数",
    "水了",
    "蹦跶太久",
    "圣母",
    "降智",
    "憋屈",
    "穿帮",
    "逻辑不通",
    "弃书",
    "难看",
)

_ENTITY_CUE_PATTERN = re.compile(
    r"(?<!某)([\u4e00-\u9fff]{2,4})(?:好讨喜|太讨喜|真讨喜|很讨喜|太可爱|多点戏份|多写点|加戏|真帅)"
)
_GENERIC_ENTITY_STOPWORDS = frozenset({"这个配角", "那个配角", "某配角", "配角", "反派", "主角"})


@dataclass(frozen=True)
class ReaderComment:
    content: str
    chapter: int = 1
    comment_id: str = ""
    likes: int = 0
    sentiment: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommentFeedbackSummary:
    total_comments: int
    positive_count: int
    negative_count: int
    neutral_count: int
    sentiment_score: float
    signal_counts: Dict[str, int]
    signal_weights: Dict[str, float]
    matched_keywords: Dict[str, List[str]]
    mentioned_entities: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutlineAdjustmentDirective:
    directive_id: str
    book_id: str
    target_chapter: int
    max_published_chapter: int
    signal_type: str
    priority: int
    trigger_keywords: List[str]
    mention_count: int
    instruction: str
    mentioned_entities: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_shanghai_iso)

    def __post_init__(self) -> None:
        assert_chapter_mutable(self.target_chapter, self.max_published_chapter)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptimizationReport:
    book_id: str
    max_published_chapter: int
    target_start_chapter: int
    summary: CommentFeedbackSummary
    directives: List[OutlineAdjustmentDirective]
    generated_at: str = field(default_factory=_now_shanghai_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "book_id": self.book_id,
            "max_published_chapter": self.max_published_chapter,
            "target_start_chapter": self.target_start_chapter,
            "summary": self.summary.to_dict(),
            "directives": [d.to_dict() for d in self.directives],
            "generated_at": self.generated_at,
        }


def _coerce_comment(item: ReaderComment | Mapping[str, Any] | str, idx: int) -> ReaderComment:
    if isinstance(item, ReaderComment):
        return item
    if isinstance(item, str):
        return ReaderComment(content=item, chapter=1, comment_id=f"c_{idx}")
    if isinstance(item, Mapping):
        if "content" not in item:
            raise ValueError(f"Comment mapping at index {idx} missing 'content'")
        return ReaderComment(
            content=str(item["content"]),
            chapter=int(item.get("chapter", 1)),
            comment_id=str(item.get("comment_id", f"c_{idx}")),
            likes=int(item.get("likes", 0)),
            sentiment=item.get("sentiment"),
        )
    raise TypeError(f"Unsupported comment item type: {type(item).__name__}")


def _classify_sentiment(text: str, explicit: Optional[str]) -> str:
    if explicit is not None:
        norm = explicit.strip().lower()
        if norm not in ("positive", "negative", "neutral"):
            raise ValueError(
                f"Unknown comment sentiment {explicit!r}; expected positive/negative/neutral"
            )
        return norm
    pos_hits = sum(1 for kw in _POSITIVE_LEXICON if kw in text)
    neg_hits = sum(1 for kw in _NEGATIVE_LEXICON if kw in text)
    if neg_hits > pos_hits:
        return "negative"
    if pos_hits > neg_hits:
        return "positive"
    return "neutral"


class ReviewFeedbackOptimizer:
    """Aggregate reader comments into future outline adjustment directives (strictly > max_published_chapter)."""

    def __init__(self, rules: Optional[Sequence[FeedbackSignalRule]] = None) -> None:
        self.rules: Tuple[FeedbackSignalRule, ...] = (
            tuple(rules) if rules is not None else FEEDBACK_SIGNAL_RULES
        )

    def optimize(
        self,
        book_id: str,
        comments: Sequence[ReaderComment | Mapping[str, Any] | str],
        *,
        max_published_chapter: int,
        target_chapter: Optional[int] = None,
        target_chapters: Optional[Sequence[int]] = None,
        min_mentions: int = 1,
    ) -> OptimizationReport:
        if not isinstance(book_id, str) or not book_id.strip():
            raise ValueError("book_id must be a non-empty string")
        if min_mentions < 1:
            raise ValueError(f"min_mentions must be >= 1, got {min_mentions}")

        # Hard Immutability Guard BEFORE any processing
        resolved_targets: List[int]
        if target_chapters is not None:
            resolved_targets = list(target_chapters)
            if not resolved_targets:
                raise ValueError("target_chapters must not be empty when provided")
            for ch in resolved_targets:
                assert_chapter_mutable(ch, max_published_chapter)
        elif target_chapter is not None:
            assert_chapter_mutable(target_chapter, max_published_chapter)
            resolved_targets = [target_chapter]
        else:
            default_target = max_published_chapter + 1
            assert_chapter_mutable(default_target, max_published_chapter)
            resolved_targets = [default_target]

        coerced = [_coerce_comment(c, idx) for idx, c in enumerate(comments, start=1)]

        positive_count = 0
        negative_count = 0
        neutral_count = 0

        signal_counts: Dict[str, int] = {}
        signal_weights: Dict[str, float] = {}
        matched_keywords: Dict[str, List[str]] = {}
        mentioned_entities: List[str] = []

        rule_by_type = {r.signal_type: r for r in self.rules}

        for c in coerced:
            text = c.content.strip()
            if not text:
                continue
            sent = _classify_sentiment(text, c.sentiment)
            if sent == "positive":
                positive_count += 1
            elif sent == "negative":
                negative_count += 1
            else:
                neutral_count += 1

            weight = 1.0 + min(max(0, c.likes), 100) * 0.1

            for rule in self.rules:
                hits = [kw for kw in rule.keywords if kw in text]
                if hits:
                    signal_counts[rule.signal_type] = signal_counts.get(rule.signal_type, 0) + 1
                    signal_weights[rule.signal_type] = (
                        signal_weights.get(rule.signal_type, 0.0) + weight
                    )
                    existing = matched_keywords.setdefault(rule.signal_type, [])
                    for kw in hits:
                        if kw not in existing:
                            existing.append(kw)
                    if rule.signal_type == "BOOST_SUPPORTING_CHARACTER":
                        for m in _ENTITY_CUE_PATTERN.finditer(text):
                            ent = m.group(1)
                            if ent not in _GENERIC_ENTITY_STOPWORDS and ent not in mentioned_entities:
                                mentioned_entities.append(ent)

        total = len([c for c in coerced if c.content.strip()])
        sentiment_score = (positive_count - negative_count) / total if total > 0 else 0.0

        summary = CommentFeedbackSummary(
            total_comments=total,
            positive_count=positive_count,
            negative_count=negative_count,
            neutral_count=neutral_count,
            sentiment_score=round(sentiment_score, 4),
            signal_counts=signal_counts,
            signal_weights={k: round(v, 3) for k, v in signal_weights.items()},
            matched_keywords=matched_keywords,
            mentioned_entities=mentioned_entities,
        )

        triggered_signals = [
            (sig, cnt)
            for sig, cnt in signal_counts.items()
            if cnt >= min_mentions
        ]
        triggered_signals.sort(
            key=lambda item: (
                -signal_weights[item[0]],
                -item[1],
                rule_by_type[item[0]].default_priority,
                item[0],
            )
        )

        now_iso = _now_shanghai_iso()
        directives: List[OutlineAdjustmentDirective] = []
        for idx, (sig_type, cnt) in enumerate(triggered_signals, start=1):
            rule = rule_by_type[sig_type]
            target_ch = resolved_targets[(idx - 1) % len(resolved_targets)]
            instruction = rule.instruction_template
            entities_for_dir: List[str] = []
            if sig_type == "BOOST_SUPPORTING_CHARACTER" and mentioned_entities:
                entities_for_dir = list(mentioned_entities)
                instruction = f"{instruction}（重点关注呼声较高角色：{'、'.join(entities_for_dir)}）"

            directives.append(
                OutlineAdjustmentDirective(
                    directive_id=f"OPT-{book_id}-CH{target_ch:04d}-{idx:02d}",
                    book_id=book_id,
                    target_chapter=target_ch,
                    max_published_chapter=max_published_chapter,
                    signal_type=sig_type,
                    priority=idx,
                    trigger_keywords=list(matched_keywords.get(sig_type, [])),
                    mention_count=cnt,
                    instruction=instruction,
                    mentioned_entities=entities_for_dir,
                    created_at=now_iso,
                )
            )

        return OptimizationReport(
            book_id=book_id,
            max_published_chapter=max_published_chapter,
            target_start_chapter=min(resolved_targets),
            summary=summary,
            directives=directives,
            generated_at=now_iso,
        )


__all__ = [
    "EvolutionError",
    "MetricsNotSettledError",
    "PublishedChapterImmutableError",
    "PackagingPoolExhaustedError",
    "InvalidAlternateTitleError",
    "RacingAction",
    "RacingThresholds",
    "BookMetrics",
    "PromotionDirective",
    "PackagingRotationTask",
    "CutLossRecommendation",
    "RacingDecision",
    "RacingDecisionEngine",
    "evaluate_book_racing",
    "evaluate_project_racing",
    "validate_tomato_title",
    "MotifWeightEntry",
    "MotifWeightStore",
    "assert_chapter_mutable",
    "FeedbackSignalRule",
    "FEEDBACK_SIGNAL_RULES",
    "ReaderComment",
    "CommentFeedbackSummary",
    "OutlineAdjustmentDirective",
    "OptimizationReport",
    "ReviewFeedbackOptimizer",
]
