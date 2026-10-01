"""Runs pre-filters in cost order and turns their results into one outcome."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .base import PreFilter
from .config import FilterMode, PipelineConfig
from .interaction import Interaction
from .result import PreFilterResult, Verdict

logger = logging.getLogger(__name__)


class PreflightFailed(RuntimeError):
    """An enabled filter cannot run. Raised at startup, never per submission."""


class MetricsSink(Protocol):
    """Anything that wants to see every stage outcome, e.g. StageMetrics (#24)."""

    def observe(self, result: PipelineResult) -> None: ...


@dataclass(frozen=True)
class FilterRecord:
    """What one filter did for one submission."""

    filter_name: str
    mode: FilterMode
    result: PreFilterResult | None
    error: str | None = None
    #: Time spent in `check` and `commit`, for the latency column in metrics (#24).
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class PipelineResult:
    """The pre-filter stage's decision for one submission.

    verdict is REJECT only when an enforced filter rejected; otherwise PASS and
    the submission goes on to the judge together with `flags`.
    """

    verdict: Verdict
    reason_code: str | None
    decided_by: str | None
    flags: tuple[FilterRecord, ...]
    shadow_rejections: tuple[FilterRecord, ...]
    records: tuple[FilterRecord, ...]

    @property
    def proceeds_to_judge(self) -> bool:
        return self.verdict is Verdict.PASS


class PreFilterPipeline:
    def __init__(
        self,
        filters: Iterable[PreFilter],
        config: PipelineConfig | None = None,
        metrics: MetricsSink | None = None,
    ):
        # sorted() is stable, so filters with equal cost keep registration order.
        self._filters = sorted(filters, key=lambda f: f.cost_rank)
        names = [f.name for f in self._filters]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"Duplicate filter names: {sorted(duplicates)}")
        self._by_name = {f.name: f for f in self._filters}
        self._config = config or PipelineConfig()
        self._metrics = metrics

    @property
    def filter_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self._filters)

    def preflight(self) -> None:
        """Check every enabled filter can run. Call once before serving.

        Filters that are off are skipped: a filter nobody runs may lack its
        model. Raises PreflightFailed naming the first filter that cannot run.
        """
        for prefilter in self._filters:
            if self._config.mode_for(prefilter.name) is FilterMode.OFF:
                continue
            try:
                prefilter.preflight()
            except Exception as exc:  # noqa: BLE001 - reported, with the filter's name
                raise PreflightFailed(
                    f"{prefilter.name} cannot run: {type(exc).__name__}: {exc}"
                ) from exc

    def run_pre_filters(self, interaction: Interaction) -> PipelineResult:
        """Run every enabled filter, then let the survivors claim their state."""
        result = self._run(interaction)
        if self._metrics is not None:
            try:
                self._metrics.observe(result)
            except Exception:  # noqa: BLE001 - observability must never block the stage
                # By now every filter has committed its claims. Losing the
                # result here would strand them, and the contributor's retry
                # would meet their own photo.
                logger.exception("prefilter_metrics_error submission=%s", interaction.submission_id)
        return result

    def _run(self, interaction: Interaction) -> PipelineResult:
        records: list[FilterRecord] = []

        for prefilter in self._filters:
            mode = self._config.mode_for(prefilter.name)
            if mode is FilterMode.OFF:
                continue

            record = self._run_one(prefilter, mode, interaction)
            records.append(record)

            if self._is_enforced_reject(record):
                # Fail fast: later (more expensive) filters never run, and
                # nothing is committed — a rejected submission must not claim a
                # photo it may want to resubmit after a fix.
                return self._build_result(records, decided_by=record)

        return self._commit(interaction, records)

    def _commit(self, interaction: Interaction, records: list[FilterRecord]) -> PipelineResult:
        """Let each filter that ran claim what it needs, now the stage has passed.

        A filter may return a revised verdict here: two submissions can both pass
        `check` before either commits, and only one can win a contested claim.
        When that revised verdict is an enforced REJECT, every filter that
        already committed is rolled back, so "a rejected submission claims
        nothing" holds here exactly as it does for a REJECT during `check`.
        """
        committed: list[FilterRecord] = []
        done: list[PreFilter] = []
        for index, record in enumerate(records):
            prefilter = self._by_name[record.filter_name]
            # Listed before the call, not after: a commit that raised halfway
            # may still have claimed something, and rollback is safe on a filter
            # that committed nothing.
            done.append(prefilter)
            started = time.perf_counter()
            try:
                revision = prefilter.commit(interaction)
            except Exception as exc:  # noqa: BLE001 - same fail-open rule as check()
                logger.exception(
                    "prefilter_commit_error filter=%s submission=%s",
                    prefilter.name,
                    interaction.submission_id,
                )
                commit_error = f"{type(exc).__name__}: {exc}"
                committed.append(
                    FilterRecord(
                        record.filter_name,
                        record.mode,
                        record.result,
                        # Keep a crash from check() too, rather than overwrite it.
                        error=commit_error
                        if record.error is None
                        else f"{record.error}; commit: {commit_error}",
                        duration_seconds=record.duration_seconds + (time.perf_counter() - started),
                    )
                )
                continue

            elapsed = record.duration_seconds + (time.perf_counter() - started)
            if revision is None:
                # Keep any error from check(): a filter that crashed still
                # crashed, and metrics count it.
                committed.append(
                    FilterRecord(
                        record.filter_name, record.mode, record.result, record.error, elapsed
                    )
                )
                continue

            logger.info(
                "prefilter_commit_revision filter=%s submission=%s verdict=%s reason=%s",
                prefilter.name,
                interaction.submission_id,
                revision.verdict.value,
                revision.reason_code,
            )
            # A crash in check() is still a crash, whatever commit() revised.
            revised = FilterRecord(record.filter_name, record.mode, revision, record.error, elapsed)
            committed.append(revised)
            if self._is_enforced_reject(revised):
                self._rollback(done, interaction)
                # The later filters already ran their `check`; only their commit
                # is skipped. Keep those results, or shadow mode loses the
                # evidence it exists to collect.
                return self._build_result(committed + records[index + 1 :], decided_by=revised)

        return self._build_result(committed, decided_by=None)

    @staticmethod
    def _rollback(done: Iterable[PreFilter], interaction: Interaction) -> None:
        """Undo the commits made before a later filter rejected.

        Fail-open, like `check`: a filter that cannot undo its own claim must
        not turn a clean rejection into a crashed stage. The leftover claim is
        logged, since it is the one thing that could block a resubmission.
        """
        for prefilter in done:
            try:
                prefilter.rollback(interaction)
            except Exception:  # noqa: BLE001 - same fail-open rule as check()
                logger.exception(
                    "prefilter_rollback_error filter=%s submission=%s",
                    prefilter.name,
                    interaction.submission_id,
                )

    @staticmethod
    def _is_enforced_reject(record: FilterRecord) -> bool:
        return (
            record.mode is FilterMode.ENFORCE
            and record.result is not None
            and record.result.verdict is Verdict.REJECT
        )

    def _run_one(
        self, prefilter: PreFilter, mode: FilterMode, interaction: Interaction
    ) -> FilterRecord:
        started = time.perf_counter()
        try:
            result = prefilter.check(interaction)
        except Exception as exc:  # noqa: BLE001 - one broken filter must not block the stage
            # Fail open: a crashed filter never rejects a contributor. The
            # judge still sees the submission, and the error is recorded.
            logger.exception(
                "prefilter_error filter=%s submission=%s",
                prefilter.name,
                interaction.submission_id,
            )
            return FilterRecord(
                prefilter.name,
                mode,
                None,
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=time.perf_counter() - started,
            )

        if mode is FilterMode.SHADOW and result.verdict is Verdict.REJECT:
            logger.info(
                "prefilter_shadow_reject filter=%s submission=%s reason=%s",
                prefilter.name,
                interaction.submission_id,
                result.reason_code,
            )
        return FilterRecord(
            prefilter.name, mode, result, duration_seconds=time.perf_counter() - started
        )

    @staticmethod
    def _build_result(
        records: list[FilterRecord], decided_by: FilterRecord | None
    ) -> PipelineResult:
        def with_verdict(verdict: Verdict, mode: FilterMode | None = None) -> tuple[FilterRecord, ...]:
            return tuple(
                r
                for r in records
                if r.result is not None
                and r.result.verdict is verdict
                and (mode is None or r.mode is mode)
            )

        rejected = decided_by is not None
        return PipelineResult(
            verdict=Verdict.REJECT if rejected else Verdict.PASS,
            reason_code=decided_by.result.reason_code if rejected else None,
            decided_by=decided_by.filter_name if rejected else None,
            flags=with_verdict(Verdict.FLAG),
            shadow_rejections=with_verdict(Verdict.REJECT, FilterMode.SHADOW),
            records=tuple(records),
        )
