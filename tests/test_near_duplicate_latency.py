"""Latency budget for the most expensive pre-filter step (issue #16).

Bounds are loose on purpose: this guards the order of magnitude, not the
machine. Measured numbers live in validator/README.md.
"""

from __future__ import annotations

import time

from s2e_validator.prefilters import (
    ImageNearDuplicateFilter,
    NewImage,
    TextNearDuplicateFilter,
    Verdict,
)
from s2e_validator.prefilters.hashing import _DHASH_CACHE

from conftest import make_interaction
from helpers import CASTLE, phone_photo, photo

WINDOW = 10_000
#: Longer than min_cross_user_chars, so both the per-user and the global scan run.
FRESH_CAPTION = (
    "Kluski śląskie z sosem pieczeniowym i modrą kapustą, podane na niedzielny obiad u babci"
)


def test_a_12_megapixel_photo_is_checked_quickly():
    """A phone photo, not a thumbnail, against a populated window."""
    image_filter = ImageNearDuplicateFilter()
    for index in range(2_000):
        image_filter.commit(
            make_interaction(
                submission_id=f"sub-{index}",
                user_id=f"user-{index % 200}",
                image=NewImage(photo(index, width=64, height=64)),
            )
        )
    fresh = make_interaction(submission_id="sub-new", image=NewImage(phone_photo(7)))
    _DHASH_CACHE.clear()  # measure the decode, not a cache hit

    started = time.perf_counter()
    result = image_filter.check(fresh)
    elapsed = time.perf_counter() - started

    assert result.verdict is Verdict.PASS
    assert elapsed < 0.3, f"image near-duplicate check took {elapsed * 1000:.1f}ms"


def test_text_check_worst_case_scans_both_buckets():
    """One user's 10k captions sit in their own bucket and the global one, and
    the query is long enough and on a different photo, so both scans run."""
    text_filter = TextNearDuplicateFilter()
    for index in range(WINDOW):
        text_filter.commit(
            make_interaction(submission_id=f"sub-{index}", user_id="ann", text=f"{CASTLE} {index}")
        )
    fresh = make_interaction(
        submission_id="sub-new",
        user_id="ann",
        image=NewImage(photo(123)),
        text=FRESH_CAPTION,
    )

    started = time.perf_counter()
    result = text_filter.check(fresh)
    elapsed = time.perf_counter() - started

    assert result.verdict is Verdict.PASS
    assert elapsed < 0.5, f"text near-duplicate check took {elapsed * 1000:.1f}ms"
