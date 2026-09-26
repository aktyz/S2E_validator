"""What the stage is actually doing, per filter and overall.

Shadow-mode filters are first-class here: "what would this filter have
rejected" is the number that decides whether it is safe to enforce, and it is
the only way to tune a threshold before it can hurt anyone.

Three pieces, each with one job: `StageMetrics` collects, `StageSnapshot` is a
frozen copy of what was collected, and `RateMonitor` watches one rate and warns
when it crosses a ceiling. A new kind of alert is a new monitor, not an edit
to the collector.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .config import FilterMode
from .pipeline import FilterRecord, PipelineResult
from .result import Verdict

logger = logging.getLogger(__name__)

#: An operations estimate for one judge call, used only to print "judge calls
#: saved" as a rough dollar figure in the metrics table. Not accounting, not a
#: price, and nothing reads it to make a decision: no budget, fee or reward
#: depends on it. A placeholder until a judge model is chosen (#43, J4).
DEFAULT_JUDGE_CALL_USD = 0.004

#: Outcomes a reason code is counted under.
REJECT, SHADOW, FLAG = "reject", "shadow", "flag"


@dataclass(frozen=True)
class FilterStats:
    """Counts for one filter at the moment of a snapshot. Never changes.

    A record can be both an error and a verdict: when `commit` crashes after
    `check` returned one, the verdict still counts, and so does the error. So
    `passed + flagged + rejected + would_reject + errors` can exceed
    `checked`, and it is not a bug when it does.
    """

    name: str
    checked: int = 0
    passed: int = 0
    flagged: int = 0
    #: Rejections that actually stopped a submission (enforce mode).
    rejected: int = 0
    #: Rejections a shadow-mode filter would have made. The number to read
    #: before promoting a filter to enforce.
    would_reject: int = 0
    errors: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0
    #: Keyed by (outcome, reason_code), outcome being "reject", "shadow" or
    #: "flag", so a code is attributed to what it actually did.
    reason_codes: Mapping[tuple[str, str], int] = field(default_factory=dict)

    @property
    def mean_ms(self) -> float:
        return (self.total_seconds / self.checked * 1000) if self.checked else 0.0

    @property
    def max_ms(self) -> float:
        return self.max_seconds * 1000

    @property
    def reject_rate(self) -> float:
        """Share of submissions this filter rejected, enforced or shadowed."""
        decisive = self.rejected + self.would_reject
        return decisive / self.checked if self.checked else 0.0


@dataclass
class _FilterCounters:
    """The mutable side of `FilterStats`, private to `StageMetrics` and only
    ever touched under its lock."""

    name: str
    checked: int = 0
    passed: int = 0
    flagged: int = 0
    rejected: int = 0
    would_reject: int = 0
    errors: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0
    reason_codes: Counter[tuple[str, str]] = field(default_factory=Counter)

    def freeze(self) -> FilterStats:
        return FilterStats(
            name=self.name,
            checked=self.checked,
            passed=self.passed,
            flagged=self.flagged,
            rejected=self.rejected,
            would_reject=self.would_reject,
            errors=self.errors,
            total_seconds=self.total_seconds,
            max_seconds=self.max_seconds,
            reason_codes=MappingProxyType(dict(self.reason_codes)),
        )


@dataclass(frozen=True)
class StageSnapshot:
    """Everything collected up to one moment. Owns its data: later submissions
    never change it, and reading it needs no lock."""

    submissions: int
    passed: int
    rejected: int
    flagged: int
    #: Filter errors, not submissions with an error: one submission on which
    #: two filters crashed counts twice, matching the per-filter `errors`.
    errors: int
    total_seconds: float
    filters: tuple[FilterStats, ...]
    judge_call_cost_usd: float

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.submissions if self.submissions else 0.0

    @property
    def judge_calls_saved(self) -> int:
        """Every rejected submission is a judge call not made."""
        return self.rejected

    @property
    def judge_cost_saved_usd(self) -> float:
        """A rough operations estimate; see DEFAULT_JUDGE_CALL_USD."""
        return self.judge_calls_saved * self.judge_call_cost_usd

    @property
    def mean_stage_ms(self) -> float:
        return (self.total_seconds / self.submissions * 1000) if self.submissions else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "submissions": self.submissions,
            "passed": self.passed,
            "rejected": self.rejected,
            "flagged": self.flagged,
            "errors": self.errors,
            "reject_rate": round(self.reject_rate, 4),
            "judge_calls_saved": self.judge_calls_saved,
            "judge_cost_saved_usd_estimate": round(self.judge_cost_saved_usd, 4),
            "mean_stage_ms": round(self.mean_stage_ms, 3),
            "filters": [_filter_dict(stats) for stats in self.filters],
        }


def _filter_dict(stats: FilterStats) -> dict[str, Any]:
    by_outcome: dict[str, dict[str, int]] = {REJECT: {}, SHADOW: {}, FLAG: {}}
    for (outcome, code), count in sorted(stats.reason_codes.items()):
        by_outcome[outcome][code] = count
    return {
        "name": stats.name,
        "checked": stats.checked,
        "passed": stats.passed,
        "flagged": stats.flagged,
        "rejected": stats.rejected,
        "would_reject": stats.would_reject,
        "errors": stats.errors,
        "mean_ms": round(stats.mean_ms, 3),
        "max_ms": round(stats.max_ms, 3),
        "reason_codes": by_outcome,
    }


def render_snapshot(snapshot: StageSnapshot) -> str:
    """A table for a log line or a terminal: the reviewable view.

    Per-filter `pass` excludes flagged submissions; the stage's `passed` counts
    every submission that went on to the judge, flagged or not.
    """
    header = (
        f"{'filter':<24}{'checked':>8}{'pass':>7}{'flag':>7}"
        f"{'reject':>8}{'shadow':>8}{'err':>5}{'mean ms':>9}{'max ms':>9}"
    )
    rows = [
        f"{s.name:<24}{s.checked:>8}{s.passed:>7}{s.flagged:>7}"
        f"{s.rejected:>8}{s.would_reject:>8}{s.errors:>5}{s.mean_ms:>9.2f}{s.max_ms:>9.2f}"
        for s in snapshot.filters
    ]
    summary = (
        f"submissions={snapshot.submissions} passed={snapshot.passed} "
        f"rejected={snapshot.rejected} flagged={snapshot.flagged} "
        f"reject_rate={snapshot.reject_rate:.1%} judge_calls_saved={snapshot.judge_calls_saved} "
        f"(~${snapshot.judge_cost_saved_usd:.2f} est.) mean_stage={snapshot.mean_stage_ms:.1f}ms"
    )
    return "\n".join([header, "-" * len(header), *rows, "", summary])


@dataclass(frozen=True)
class AnomalySettings:
    """When a rate is worth a warning.

    Over the last `window` submissions, warn if the share that matched is above
    `max_rate`, but only once at least `min_submissions` have been seen, so a
    handful of early submissions cannot trip it.
    """

    window: int = 200
    max_rate: float = 0.5
    min_submissions: int = 50

    def __post_init__(self) -> None:
        if isinstance(self.window, bool) or not isinstance(self.window, int) or self.window < 1:
            raise ValueError(f"window must be a whole number of at least 1, got {self.window!r}")
        # Exclusive at 1: the alert fires when the rate is *above* max_rate,
        # and no rate is above 1.
        if not 0.0 <= self.max_rate < 1.0:
            raise ValueError(f"max_rate must be at least 0 and below 1, got {self.max_rate!r}")
        if self.min_submissions < 1 or self.min_submissions > self.window:
            # The window never holds more than `window` entries, so a larger
            # minimum would silently disable the alert.
            raise ValueError(
                f"min_submissions must be 1 to window ({self.window}), got {self.min_submissions}"
            )


class RateMonitor:
    """Watches one rate over a rolling window. Warns once when it crosses the
    ceiling and once when it recovers, never once per submission.

    Thread-safe on its own lock, and it logs only after releasing it: a log
    handler is free to call back into the metrics (to attach the current table
    to an alert, say) without deadlocking.
    """

    def __init__(self, name: str, settings: AnomalySettings, hint: str = ""):
        self.name = name
        self._settings = settings
        self._hint = hint
        self._recent: deque[bool] = deque(maxlen=settings.window)
        self._lock = threading.Lock()
        self.alerting = False

    def observe(self, hit: bool) -> None:
        with self._lock:
            self._recent.append(hit)
            if len(self._recent) < self._settings.min_submissions:
                return
            rate = sum(self._recent) / len(self._recent)
            window = len(self._recent)
            crossed = rate > self._settings.max_rate and not self.alerting
            recovered = rate <= self._settings.max_rate and self.alerting
            if crossed:
                self.alerting = True
            elif recovered:
                self.alerting = False
        # Outside the lock, so a slow or re-entrant handler holds nothing up.
        if crossed:
            logger.warning(
                "prefilter_%s_spike rate=%.1f%% window=%d ceiling=%.1f%%%s",
                self.name,
                rate * 100,
                window,
                self._settings.max_rate * 100,
                f" - {self._hint}" if self._hint else "",
            )
        elif recovered:
            logger.info("prefilter_%s_recovered rate=%.1f%%", self.name, rate * 100)


def reject_rate_monitor(settings: AnomalySettings | None = None) -> RateMonitor:
    """Counts a submission a filter *would* have rejected, shadow included, so
    the alert works in the all-shadow default, which is exactly when a broken
    filter would otherwise go unnoticed."""
    return RateMonitor(
        "reject_rate",
        settings or AnomalySettings(),
        "check for a broken filter before assuming more farming",
    )


def error_rate_monitor(settings: AnomalySettings | None = None) -> RateMonitor:
    """Filter crashes fail open into a PASS, so they push the reject rate
    *down*; only a separate monitor on errors can see them."""
    return RateMonitor(
        "error_rate",
        settings or AnomalySettings(max_rate=0.05),
        "a filter is crashing and every affected submission is passing unchecked",
    )


class StageMetrics:
    """Collects per-filter and overall counts from pipeline results."""

    def __init__(
        self,
        reject_alert: RateMonitor | None = None,
        error_alert: RateMonitor | None = None,
        judge_call_cost_usd: float = DEFAULT_JUDGE_CALL_USD,
    ):
        self._reject_alert = reject_alert or reject_rate_monitor()
        self._error_alert = error_alert or error_rate_monitor()
        self._judge_call_cost_usd = judge_call_cost_usd
        self._filters: dict[str, _FilterCounters] = {}
        self._submissions = self._passed = self._rejected = self._flagged = self._errors = 0
        self._total_seconds = 0.0
        self._lock = threading.Lock()

    def observe(self, result: PipelineResult) -> None:
        with self._lock:
            self._record_stage(result)
            for record in result.records:
                self._record_filter(record)
        # After the collector's lock is released: the monitors may log, and a
        # log handler may call snapshot().
        self._reject_alert.observe(
            result.verdict is Verdict.REJECT or bool(result.shadow_rejections)
        )
        self._error_alert.observe(any(r.error is not None for r in result.records))

    def snapshot(self) -> StageSnapshot:
        with self._lock:
            return StageSnapshot(
                submissions=self._submissions,
                passed=self._passed,
                rejected=self._rejected,
                flagged=self._flagged,
                errors=self._errors,
                total_seconds=self._total_seconds,
                filters=tuple(
                    counters.freeze()
                    for counters in sorted(self._filters.values(), key=lambda c: c.name)
                ),
                judge_call_cost_usd=self._judge_call_cost_usd,
            )

    def _record_stage(self, result: PipelineResult) -> None:
        self._submissions += 1
        self._total_seconds += sum(r.duration_seconds for r in result.records)
        if result.verdict is Verdict.REJECT:
            self._rejected += 1
        else:
            self._passed += 1
        if result.flags:
            self._flagged += 1

    def _record_filter(self, record: FilterRecord) -> None:
        counters = self._filters.setdefault(record.filter_name, _FilterCounters(record.filter_name))
        counters.checked += 1
        counters.total_seconds += record.duration_seconds
        counters.max_seconds = max(counters.max_seconds, record.duration_seconds)
        if record.error is not None:
            counters.errors += 1
            self._errors += 1
        # A verdict survives a crashed commit, so it still counts.
        if record.result is None:
            return
        verdict = record.result.verdict
        if verdict is Verdict.PASS:
            counters.passed += 1
            return
        # Every non-PASS result carries a reason code (PreFilterResult enforces it).
        code = record.result.reason_code
        if verdict is Verdict.FLAG:
            counters.flagged += 1
            counters.reason_codes[(FLAG, code)] += 1
        elif record.mode is FilterMode.ENFORCE:
            counters.rejected += 1
            counters.reason_codes[(REJECT, code)] += 1
        else:
            counters.would_reject += 1
            counters.reason_codes[(SHADOW, code)] += 1
