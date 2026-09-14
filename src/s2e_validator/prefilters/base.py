"""The interface every pre-filter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from .interaction import Interaction
from .result import PreFilterResult


class PreFilter(ABC):
    """One deterministic, local check. No paid or remote calls.

    Subclasses set:
      name      - unique id, used as the config key and in logs/metrics.
      cost_rank - lower runs first. Rough guide: 10 structural checks,
                  20 hashing, 30 stateful lookups, 40 local models.
    """

    name: ClassVar[str]
    cost_rank: ClassVar[int]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Surface a forgotten attribute here, at the point of the mistake, rather
        # than as an AttributeError inside the pipeline's cost sort.
        missing = [attr for attr in ("name", "cost_rank") if not hasattr(cls, attr)]
        if missing:
            raise TypeError(f"{cls.__name__} must define {' and '.join(missing)}")

    @abstractmethod
    def check(self, interaction: Interaction) -> PreFilterResult:
        """Inspect one submission and return a verdict."""


class NoOpFilter(PreFilter):
    """Always passes. Smoke test for the pipeline wiring."""

    name = "noop"
    # Deliberately below the 10 guide minimum: this is not a real check, and
    # keeping it first makes it a pure pass-through in any pipeline it joins.
    cost_rank = 0

    def check(self, interaction: Interaction) -> PreFilterResult:
        return PreFilterResult.passed()
