"""What a single pre-filter returns."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_REASON_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Verdict(str, Enum):
    """Outcome of one filter.

    PASS   - nothing found.
    FLAG   - suspicious but not decisive; the submission continues and the flag
             is passed to the judge as context (language mismatch, stolen-image
             signals, elevated velocity).
    REJECT - obvious junk or farming; stops the pipeline when the filter is enforced.
    """

    PASS = "PASS"
    FLAG = "FLAG"
    REJECT = "REJECT"


@dataclass(frozen=True)
class PreFilterResult:
    verdict: Verdict
    reason_code: str | None = None
    confidence: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict is Verdict.PASS:
            if self.reason_code is not None:
                raise ValueError("A PASS result must not carry a reason_code")
        else:
            # Every REJECT and FLAG must say why: debugging, appeals and metrics
            # all depend on it.
            if not self.reason_code or not _REASON_CODE_PATTERN.match(self.reason_code):
                raise ValueError(
                    f"A {self.verdict.value} result needs an UPPER_SNAKE_CASE reason_code"
                )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

    @classmethod
    def passed(cls, **details: Any) -> PreFilterResult:
        return cls(Verdict.PASS, details=details)

    @classmethod
    def flagged(
        cls, reason_code: str, confidence: float | None = None, **details: Any
    ) -> PreFilterResult:
        return cls(Verdict.FLAG, reason_code, confidence, details)

    @classmethod
    def rejected(
        cls, reason_code: str, confidence: float | None = None, **details: Any
    ) -> PreFilterResult:
        return cls(Verdict.REJECT, reason_code, confidence, details)
