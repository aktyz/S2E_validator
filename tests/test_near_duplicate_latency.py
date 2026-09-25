"""Latency budget for the most expensive pre-filter step (issue #16).

Bounds are loose on purpose: this guards the order of magnitude, not the
machine. Measured numbers live in validator/README.md.

Wall-clock assertions flake on a shared runner, so these are marked `slow` and
left out of the default run. Run them deliberately, on an idle machine:

    python -m pytest -m slow
"""

from __future__ import annotations

import random
import time

import pytest

from s2e_validator.prefilters import (
    GLOBAL_BUCKET,
    HashEntry,
    HashWindow,
    ImageNearDuplicateFilter,
    NewImage,
    TextNearDuplicateFilter,
    Verdict,
    user_bucket,
)
from s2e_validator.prefilters.hashing import _DHASH_CACHE
from s2e_validator.prefilters.stores import utc_now

from conftest import make_interaction
from helpers import CASTLE, phone_photo, photo

pytestmark = pytest.mark.slow

WINDOW = 10_000
#: Longer than min_cross_user_chars, so both the per-user and the global scan run.
FRESH_CAPTION = (
    "Kluski śląskie z sosem pieczeniowym i modrą kapustą, podane na niedzielny obiad u babci"
)


def filled(window: HashWindow, bucket, count: int, user_id: str, seed: int) -> HashWindow:
    """Put `count` unrelated fingerprints in a bucket.

    Seeded directly rather than through `commit`: committing each entry rescans
    a growing bucket, so filling a 10k window that way costs tens of millions of
    comparisons before the timed section even starts. What a scan costs does not
    depend on where its entries came from, and a fixed seed keeps the distances
    reproducible.
    """
    rng = random.Random(seed)
    now = utc_now()
    for index in range(count):
        window.add(
            bucket,
            HashEntry(
                value=rng.getrandbits(64),
                submission_id=f"sub-{seed}-{index}",
                user_id=user_id,
                seen_at=now,
                image_key=f"photo-{index}",
            ),
        )
    return window


def test_a_12_megapixel_photo_is_checked_quickly():
    """A phone photo, not a thumbnail, against a populated window."""
    window = filled(HashWindow(), GLOBAL_BUCKET, 2_000, user_id="ann", seed=1)
    image_filter = ImageNearDuplicateFilter(window=window)
    fresh = make_interaction(submission_id="sub-new", image=NewImage(phone_photo(7)))
    _DHASH_CACHE.clear()  # measure the decode, not a cache hit

    started = time.perf_counter()
    result = image_filter.check(fresh)
    elapsed = time.perf_counter() - started

    assert result.verdict is Verdict.PASS
    assert elapsed < 0.3, f"image near-duplicate check took {elapsed * 1000:.1f}ms"


def test_text_check_worst_case_scans_both_buckets():
    """The worst case is both scans on full buckets: 10k captions in the user's
    own bucket and 10k more in the global one."""
    window = HashWindow()
    filled(window, user_bucket("ann"), WINDOW, user_id="ann", seed=2)
    filled(window, GLOBAL_BUCKET, WINDOW, user_id="bob", seed=3)
    text_filter = TextNearDuplicateFilter(window=window)
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


def test_the_window_really_held_both_buckets():
    """Guards the guard: a mis-seeded window would make the scans trivial."""
    window = HashWindow()
    filled(window, user_bucket("ann"), 10, user_id="ann", seed=4)
    filled(window, GLOBAL_BUCKET, 10, user_id="bob", seed=5)

    assert len(window) == 20
