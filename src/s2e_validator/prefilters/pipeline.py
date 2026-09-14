"""Runs pre-filters in cost order and turns their results into one outcome."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from .base import PreFilter
from .config import FilterMode, PipelineConfig
from .interaction import Interaction
from .result import PreFilterResult, Verdict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterRecord:
    """What one filter did for one submission."""

    filter_name: str
    mode: FilterMode
    result: PreFilterResult | None
    error: str | None = None


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
    def __init__(self, filters: Iterable[PreFilter], config: PipelineConfig | None = None):
        # sorted() is stable, so filters with equal cost keep registration order.
        self._filters = sorted(filters, key=lambda f: f.cost_rank)
        names = [f.name for f in self._filters]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"Duplicate filter names: {sorted(duplicates)}")
        self._config = config or PipelineConfig()

    @property
    def filter_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self._filters)

    def run_pre_filters(self, interaction: Interaction) -> PipelineResult:
        records: list[FilterRecord] = []

        for prefilter in self._filters:
            mode = self._config.mode_for(prefilter.name)
            if mode is FilterMode.OFF:
                continue

            record = self._run_one(prefilter, mode, interaction)
            records.append(record)

            if (
                mode is FilterMode.ENFORCE
                and record.result is not None
                and record.result.verdict is Verdict.REJECT
            ):
                # Fail fast: later (more expensive) filters never run.
                return self._build_result(records, decided_by=record)

        return self._build_result(records, decided_by=None)

    def _run_one(
        self, prefilter: PreFilter, mode: FilterMode, interaction: Interaction
    ) -> FilterRecord:
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
            return FilterRecord(prefilter.name, mode, None, error=f"{type(exc).__name__}: {exc}")

        if mode is FilterMode.SHADOW and result.verdict is Verdict.REJECT:
            logger.info(
                "prefilter_shadow_reject filter=%s submission=%s reason=%s",
                prefilter.name,
                interaction.submission_id,
                result.reason_code,
            )
        return FilterRecord(prefilter.name, mode, result)

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
