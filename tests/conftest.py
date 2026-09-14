from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from s2e_validator.prefilters import (
    ExistingImage,
    Interaction,
    NewImage,
    PreFilter,
    PreFilterResult,
)


def make_interaction(**overrides) -> Interaction:
    fields = {
        "submission_id": "sub-1",
        "campaign_id": "bielik-mvp",
        "user_id": "user-1",
        "image": NewImage(b"\xff\xd8fake-jpeg", "image/jpeg"),
        "text": "Pomnik Neptuna w Gdańsku",
        "submitted_at": datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return Interaction(**fields)


@pytest.fixture
def interaction() -> Interaction:
    return make_interaction()


@pytest.fixture
def existing_image_interaction() -> Interaction:
    return make_interaction(image=ExistingImage("img-42"))


class StubFilter(PreFilter):
    """Test filter that returns a fixed result and records whether it ran."""

    # Placeholders satisfying PreFilter.__init_subclass__; each instance
    # overrides both in __init__.
    name = "stub"
    cost_rank = 10

    def __init__(
        self,
        name: str,
        cost_rank: int,
        result: PreFilterResult | Callable[[], PreFilterResult],
    ):
        # Instance attributes shadow the ClassVars, which is fine for a test double.
        self.name = name
        self.cost_rank = cost_rank
        self._result = result
        self.calls = 0

    def check(self, interaction: Interaction) -> PreFilterResult:
        self.calls += 1
        return self._result() if callable(self._result) else self._result
