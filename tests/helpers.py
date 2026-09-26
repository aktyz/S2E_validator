"""Shared test data. Plain helpers, not tests, so test modules never import
each other."""

from __future__ import annotations

import functools
import io
import random
from collections.abc import Callable
from datetime import datetime, timezone

from PIL import Image

from s2e_validator.prefilters import (
    FilterMode,
    Interaction,
    NewImage,
    PipelineConfig,
    PreFilter,
    PreFilterResult,
)


def make_interaction(**overrides) -> Interaction:
    """One submission with sensible defaults; override any field by name.

    Lives here rather than in conftest: pytest advises against importing
    conftest, and shared helpers should not depend on it.
    """
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


CASTLE = "Zamek w Malborku widziany od strony Nogatu, ceglane mury w porannym słońcu"
#: One typo away: the cheapest way to dodge exact matching.
CASTLE_TYPO = "Zamek w Malborku widziany od strony Nogatu, ceglane mury w poranym słońcu"
#: Diacritics stripped. Measured at 13 bits, above the same-user bar, so it is
#: deliberately left to the judge rather than risking honest rewordings.
CASTLE_NO_DIACRITICS = "Zamek w Malborku widziany od strony Nogatu, ceglane mury w porannym sloncu"
PIEROGI = "Pierogi ruskie"


@functools.lru_cache(maxsize=None)
def photo(seed: int, width: int = 320, height: int = 240, quality: int = 92) -> bytes:
    """A textured JPEG; `seed` changes its content.

    Deliberately noisy rather than a smooth gradient: a gradient survives any
    edit with an identical hash, which would make the tests far too kind. Noise
    is the opposite extreme (see #27 on calibrating against real photos).
    """
    rng = random.Random(seed)
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def recompressed(data: bytes, quality: int = 40, scale: float = 0.5) -> bytes:
    """The same photo re-saved smaller and at lower quality, as a re-upload would be."""
    with Image.open(io.BytesIO(data)) as image:
        resized = image.resize((int(image.width * scale), int(image.height * scale)))
        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


@functools.lru_cache(maxsize=None)
def phone_photo(seed: int = 1, width: int = 4032, height: int = 3024) -> bytes:
    """A 12 MP JPEG at phone resolution, for realistic latency figures.

    Smooth blocks rather than noise, so it compresses to a phone-like file size
    (a few MB) instead of the tens of MB pure noise would produce.
    """
    rng = random.Random(seed)
    small = Image.frombytes("RGB", (64, 48), rng.randbytes(64 * 48 * 3))
    image = small.resize((width, height), Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# -- The whole stage, used by the orchestrator and metrics tests ----------------

#: Every filter enforcing, with the cross-user caption bar lowered so the short
#: Polish sample captions above can trip it.
ENFORCE_ALL = PipelineConfig(
    default_mode=FilterMode.ENFORCE,
    settings={"text_near_duplicate": {"min_cross_user_chars": 20}},
)


def big_photo(seed: int = 1) -> bytes:
    """Phone-sized, so it clears the image_format minimum even after resizing."""
    return photo(seed, width=800, height=600)


def good(**overrides) -> Interaction:
    """A submission every filter passes, unless an override says otherwise."""
    fields = {"image": NewImage(big_photo()), "text": CASTLE}
    fields.update(overrides)
    return make_interaction(**fields)


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
