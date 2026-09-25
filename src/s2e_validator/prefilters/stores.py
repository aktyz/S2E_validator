"""Where seen-image keys are remembered between submissions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Protocol

DEFAULT_RETENTION = timedelta(days=30)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SeenImage:
    """The first submission that claimed an image key."""

    key: str
    submission_id: str
    user_id: str
    seen_at: datetime


class DuplicateClaimResult(NamedTuple):
    """Outcome of trying to claim an image key."""

    seen: SeenImage
    #: True when this call won the claim, i.e. it is the original submission.
    claimed: bool


class ImageKeyStore(Protocol):
    """Lookup of previously seen image keys.

    Only the first claim of a key is kept, so a later duplicate never displaces
    the original submitter. `add` must be atomic: it is what decides which of two
    concurrent submissions of one photo is the original.
    """

    def find(self, key: str) -> SeenImage | None: ...

    def add(self, seen: SeenImage) -> DuplicateClaimResult: ...

    def release(self, key: str, submission_id: str) -> bool: ...


class InMemoryImageKeyStore:
    """Process-local store with a time-to-live.

    Entries older than `retention` are dropped. Suitable while a campaign runs on
    a single validator process; it does not survive a restart, and a restart
    means previously seen photos become submittable again. Swapping in a Redis or
    database implementation of `ImageKeyStore` is the production path.
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
        # Insertion-ordered by seen_at, so expiry only has to walk the front.
        self._entries: dict[str, SeenImage] = {}
        # Re-entrant because add() and find() both purge. Held only around dict
        # work, so contention stays negligible even with many workers.
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def find(self, key: str) -> SeenImage | None:
        with self._lock:
            self.purge_expired()
            return self._entries.get(key)

    def add(self, seen: SeenImage) -> DuplicateClaimResult:
        """Claim `seen.key`, atomically. The first claim wins and is kept."""
        with self._lock:
            self.purge_expired()
            existing = self._entries.get(seen.key)
            if existing is not None:
                return DuplicateClaimResult(existing, claimed=False)
            self._entries[seen.key] = seen
            return DuplicateClaimResult(seen, claimed=True)

    def release(self, key: str, submission_id: str) -> bool:
        """Give a key back, but only to the submission that claimed it.

        The stage calls this when a later filter rejects at commit time, so a
        rejected submission leaves nothing claimed and can be fixed and sent
        again. The ownership test is what makes it safe: a submission that lost
        the race never holds the key, so it cannot release someone else's.
        """
        with self._lock:
            existing = self._entries.get(key)
            if existing is None or existing.submission_id != submission_id:
                return False
            del self._entries[key]
            return True

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired()

    # -- internal ---------------------------------------------------------

    def _purge_expired(self) -> int:
        cutoff = self._clock() - self._retention
        # Entries are insertion-ordered, so expiry only walks the front of the
        # dict and stops at the first live entry. Keys are collected first
        # because a dict cannot be mutated while it is being iterated.
        expired: list[str] = []
        for key, seen in self._entries.items():
            if seen.seen_at > cutoff:
                break
            expired.append(key)
        for key in expired:
            del self._entries[key]
        return len(expired)
