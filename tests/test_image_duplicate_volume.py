"""Volume check for the duplicate store (issue #15 acceptance criterion).

Bielik's stated target is ~1M photos total; a busy campaign day is far below the
50k entries loaded here. Bounds are deliberately loose so the test measures order
of magnitude, not machine speed. Measured numbers live in validator/README.md.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from s2e_validator.prefilters import (
    ImageExactDuplicateFilter,
    InMemoryImageKeyStore,
    NewImage,
    Verdict,
)

from helpers import make_interaction

ENTRIES = 50_000


def build_loaded_filter() -> tuple[ImageExactDuplicateFilter, float]:
    store = InMemoryImageKeyStore()
    duplicate_filter = ImageExactDuplicateFilter(store=store)
    started = time.perf_counter()
    for i in range(ENTRIES):
        interaction = make_interaction(
            submission_id=f"sub-{i}",
            user_id=f"user-{i % 500}",
            image=NewImage(f"photo-{i}".encode()),
        )
        duplicate_filter.check(interaction)
        duplicate_filter.commit(interaction)
    return duplicate_filter, time.perf_counter() - started


def test_fifty_thousand_submissions_stay_fast():
    duplicate_filter, elapsed = build_loaded_filter()

    assert elapsed < 10.0, f"{ENTRIES} check+commit pairs took {elapsed:.1f}s"


def test_lookup_stays_fast_on_a_loaded_store():
    duplicate_filter, _ = build_loaded_filter()
    fresh = make_interaction(
        submission_id="sub-new",
        image=NewImage(b"a photo nobody has submitted"),
        submitted_at=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
    )

    started = time.perf_counter()
    result = duplicate_filter.check(fresh)
    elapsed = time.perf_counter() - started

    assert result.verdict is Verdict.PASS
    assert elapsed < 0.5, f"single lookup took {elapsed * 1000:.1f}ms"
