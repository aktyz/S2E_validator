"""Recent-fingerprint windows for near-duplicate lookup.

Separate from `ImageKeyStore`: that answers "is this exact key claimed?" with a
dict lookup, while this answers "is anything in the recent window close to this
fingerprint?", which is a scan.

**Scale.** Lookup is a linear scan: an XOR and a popcount per entry, about
0.1 microseconds each. That keeps v1 free of an index, and it is fine into the
tens of thousands of entries per bucket, which is what the tests measure. The
global bucket holds every photo in the retention window, so at hundreds of
thousands of photos a lookup costs tens of milliseconds and serialises behind
the lock. Multi-index hashing (split the 64 bits into chunks, look up only
candidates sharing one) is the next step if the metrics show the need; at a
10-bit threshold it narrows candidates by a factor of only about six, so it is
not free either.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from .hashing import hamming
from .stores import DEFAULT_RETENTION, utc_now

#: A bucket is a scope. Tuples keep the two kinds apart, so no user id, not
#: even "*", can land in the global bucket.
BucketKey = tuple[str, ...]
GLOBAL_BUCKET: BucketKey = ("global",)
#: Full sweeps of every bucket happen at most this often; otherwise only the
#: buckets a call touches are expired.
_FULL_SWEEP_EVERY = 1000


def user_bucket(user_id: str) -> BucketKey:
    return ("user", user_id)


@dataclass(frozen=True)
class HashEntry:
    """One fingerprint previously accepted into the window."""

    value: int
    submission_id: str
    user_id: str
    seen_at: datetime
    #: Which photo the text belonged to, so "same caption, different photo" is
    #: distinguishable from "same caption, same photo". None for image entries.
    image_key: str | None = None


@dataclass(frozen=True)
class HashMatch:
    entry: HashEntry
    distance: int


class HashWindow:
    """Fingerprints seen inside the retention window, grouped into buckets.

    Scoping is the caller's decision, and it is the whole point: comparing text
    globally would reject honest contributors describing the same landmark.

    `locked()` lets a caller run a lookup and an insert as one step, which is
    what stops two concurrent near-identical uploads from both winning.
    """

    def __init__(
        self,
        retention: timedelta = DEFAULT_RETENTION,
        clock: Callable[[], datetime] = utc_now,
    ):
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        self._retention = retention
        self._clock = clock
        # Each bucket stays insertion-ordered, so expiry walks only its front.
        self._buckets: dict[BucketKey, list[HashEntry]] = {}
        self._lock = threading.RLock()
        self._operations = 0

    def __len__(self) -> int:
        with self._lock:
            return sum(len(entries) for entries in self._buckets.values())

    @contextmanager
    def locked(self) -> Generator[None, None, None]:
        """Hold the window across several calls, so they act as one."""
        with self._lock:
            yield

    def add(self, bucket: BucketKey, entry: HashEntry) -> None:
        with self._lock:
            self._expire(bucket)
            self._buckets.setdefault(bucket, []).append(entry)
            self._maybe_sweep()

    def nearest(
        self,
        bucket: BucketKey,
        value: int,
        max_distance: int,
        exclude_submission_id: str | None = None,
        accept: Callable[[HashEntry], bool] | None = None,
    ) -> HashMatch | None:
        """Closest acceptable entry within `max_distance` bits, or None.

        `accept` narrows the question, for example to "any entry from another
        user". Asking the narrow question is what makes a verdict independent of
        whose entry happens to be nearest.
        """
        with self._lock:
            self._expire(bucket)
            best: HashMatch | None = None
            for entry in self._buckets.get(bucket, ()):
                if entry.submission_id == exclude_submission_id:
                    continue
                if accept is not None and not accept(entry):
                    continue
                distance = hamming(entry.value, value)
                if distance <= max_distance and (best is None or distance < best.distance):
                    best = HashMatch(entry, distance)
                    if distance == 0:
                        break
            return best

    def remove(self, bucket: BucketKey, submission_id: str) -> int:
        """Drop everything one submission put in a bucket; returns how many.

        Used when a later filter rejects at commit time: a rejected submission
        must not leave a fingerprint behind that its own corrected resubmission
        would then match against. A submission can hold more than one entry in a
        bucket (a text-only downgrade also records the original's fingerprint),
        so every one of them goes.
        """
        with self._lock:
            entries = self._buckets.get(bucket)
            if not entries:
                return 0
            kept = [entry for entry in entries if entry.submission_id != submission_id]
            removed = len(entries) - len(kept)
            if kept:
                self._buckets[bucket] = kept
            else:
                del self._buckets[bucket]
            return removed

    def purge_expired(self) -> int:
        """Expire every bucket now. Normally this happens a bucket at a time."""
        with self._lock:
            return sum(self._expire(bucket) for bucket in list(self._buckets))

    def _expire(self, bucket: BucketKey) -> int:
        entries = self._buckets.get(bucket)
        if not entries:
            return 0
        cutoff = self._clock() - self._retention
        stale = 0
        for entry in entries:
            if entry.seen_at > cutoff:
                break
            stale += 1
        if stale:
            del entries[:stale]
        if not entries:
            del self._buckets[bucket]
        return stale

    def _maybe_sweep(self) -> None:
        # Buckets nobody touches again would otherwise keep their entries for
        # ever; an occasional full sweep keeps memory bounded without making
        # every call O(number of users).
        self._operations += 1
        if self._operations % _FULL_SWEEP_EVERY == 0:
            for bucket in list(self._buckets):
                self._expire(bucket)
