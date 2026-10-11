"""What an outcome may say about the checks and the judge: a whitelist.

A filter's result carries `details` (a duplicate's details name the first
submission and when it arrived), a confidence and, when a filter crashed, the
exception text. A verdict carries the prompt and rubric versions. None of that
may reach the caller, who shows outcomes to visitors. So nothing here passes an
object through: every field that leaves is named.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..judge import JudgeVerdict
from ..prefilters.pipeline import FilterRecord

#: A check that crashed has no verdict of its own: the stage failed open.
VERDICT_ERROR = "ERROR"


@dataclass(frozen=True)
class FilterReport:
    """One check that ran, as the caller may see it."""

    name: str
    mode: str
    verdict: str
    reason_code: str | None

    @classmethod
    def of(cls, record: FilterRecord) -> FilterReport:
        result = record.result
        if result is None:
            return cls(record.filter_name, record.mode.value, VERDICT_ERROR, None)
        return cls(record.filter_name, record.mode.value, result.verdict.value, result.reason_code)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "mode": self.mode, "verdict": self.verdict, "reason_code": self.reason_code}


@dataclass(frozen=True)
class JudgeReport:
    """The judge, as the caller may see it: which model, and its ratings."""

    model: str
    #: (criterion, rating) pairs in the rubric's order; a held verdict has none.
    criteria: tuple[tuple[str, int], ...]

    @classmethod
    def of(cls, verdict: JudgeVerdict) -> JudgeReport:
        return cls(verdict.provenance.model, tuple(verdict.criteria.items()))

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "criteria": dict(self.criteria)}


def filter_reports(records: Iterable[FilterRecord]) -> tuple[FilterReport, ...]:
    """One report per check that ran, in the order they ran."""
    return tuple(FilterReport.of(record) for record in records)
