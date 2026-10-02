"""How fast one contributor is submitting.

The only check in the stage that is not content-based. A script can produce
submissions that each look fine; what it cannot do is slow down.

Rates are measured on `Interaction.submitted_at`, which must be the time the
platform received the submission. A timestamp the contributor could set would
let a script spread itself out on paper.
"""

from __future__ import annotations

import threading
from bisect import bisect_left, insort
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from .base import PreFilter
from .interaction import Interaction
from .result import PreFilterResult
from .settings import require_int, settings_from_dict

REASON_RATE_ELEVATED = "SUBMISSION_RATE_ELEVATED"
REASON_RATE_AUTOMATED = "SUBMISSION_RATE_AUTOMATED"

#: How long the shared default store remembers a submission. Enough for any
#: window up to an hour; a longer window needs a store built with a longer
#: retention, and the filter refuses to start otherwise. Kept short on purpose:
#: in shadow mode a script's submissions are all recorded, so what is retained
#: is what an attacker can make the store hold.
DEFAULT_RETENTION = timedelta(hours=1)
DEFAULT_SWEEP_EVERY = 1000


@dataclass(frozen=True)
class VelocitySettings:
    """Loose on purpose: someone emptying a holiday camera roll in one sitting
    is a good contributor having a productive evening.

    The campaign asks for at least 100 characters of description per photo, so
    a person needs roughly half a minute per submission even when hurrying.
    The defaults sit well above that: a flag at over one a minute sustained,
    a rejection only at three a minute, which no one is typing.
    """

    window_seconds: int = 600
    #: Submissions inside the window that make this worth the judge's attention.
    flag_threshold: int = 12
    #: Clearly not a person typing descriptions.
    reject_threshold: int = 30

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.window_seconds)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> VelocitySettings:
        parsed = settings_from_dict(cls, raw)
        require_int(parsed.window_seconds, "window_seconds", minimum=1)
        require_int(parsed.flag_threshold, "flag_threshold", minimum=1)
        # Strictly above the flag threshold: at the same value the rejection
        # always wins, and the flag could never fire.
        require_int(
            parsed.reject_threshold, "reject_threshold", minimum=parsed.flag_threshold + 1
        )
        return parsed


class SubmissionTimesStore(Protocol):
    """Recent submission times per contributor.

    `retention` is how far back the store remembers; a filter whose window is
    longer would undercount, so it refuses such a store at construction.
    """

    retention: timedelta

    def count_within(
        self, user_id: str, since: datetime, exclude_submission_id: str | None = None
    ) -> int:
        """How many of this user's recorded submissions are at or after `since`."""
        ...

    def record(self, user_id: str, submission_id: str, at: datetime, since: datetime) -> int:
        """Record one submission and return the user's count at or after `since`,
        this one included, **as one atomic step**.

        Atomic because several submissions from one user can pass `check`
        together, each seeing the count before the others are recorded; only
        the count taken while recording is true. Idempotent on `submission_id`:
        a retry is the same event, not a faster contributor.
        """
        ...

    def forget(self, user_id: str, submission_id: str) -> None:
        """Remove one submission's record. Safe when there is none."""
        ...


class _UserTimes:
    """One contributor's history: sorted by time, indexed by submission id.

    Sorted, so counting is a binary search rather than a scan and trimming is
    right even when a replayed backlog arrives out of order. Indexed, so the
    idempotency check and `forget` never walk the list. A filter this early in
    the stage must not get slower as the traffic it is there to catch grows.
    """

    __slots__ = ("times", "ids")

    def __init__(self) -> None:
        self.times: list[tuple[datetime, str]] = []
        self.ids: dict[str, datetime] = {}

    def __len__(self) -> int:
        return len(self.times)

    def add(self, submission_id: str, at: datetime) -> None:
        # A retried submission is the same event, not a faster contributor.
        if submission_id not in self.ids:
            insort(self.times, (at, submission_id))
            self.ids[submission_id] = at

    def count_since(self, since: datetime, exclude: str | None = None) -> int:
        count = len(self.times) - bisect_left(self.times, (since,))
        excluded_at = self.ids.get(exclude) if exclude is not None else None
        if excluded_at is not None and excluded_at >= since:
            count -= 1
        return count

    def trim(self, cutoff: datetime) -> None:
        drop = bisect_left(self.times, (cutoff,))
        for _, submission_id in self.times[:drop]:
            del self.ids[submission_id]
        del self.times[:drop]

    def forget(self, submission_id: str) -> None:
        at = self.ids.pop(submission_id, None)
        if at is not None:
            del self.times[bisect_left(self.times, (at, submission_id))]


class InMemorySubmissionTimes:
    """Process-local store. Memory stays proportional to contributors active
    inside `retention`: a user's entries are trimmed as they are touched, and
    every `sweep_every` records a full sweep drops users who never came back."""

    def __init__(
        self,
        retention: timedelta = DEFAULT_RETENTION,
        sweep_every: int = DEFAULT_SWEEP_EVERY,
    ):
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        if sweep_every < 1:
            raise ValueError("sweep_every must be at least 1")
        self.retention = retention
        self._sweep_every = sweep_every
        self._users: dict[str, _UserTimes] = {}
        self._lock = threading.Lock()
        self._records = 0

    def __len__(self) -> int:
        with self._lock:
            return sum(len(user) for user in self._users.values())

    def count_within(
        self, user_id: str, since: datetime, exclude_submission_id: str | None = None
    ) -> int:
        with self._lock:
            return self._count(user_id, since, exclude_submission_id)

    def record(self, user_id: str, submission_id: str, at: datetime, since: datetime) -> int:
        with self._lock:
            self._users.setdefault(user_id, _UserTimes()).add(submission_id, at)
            self._housekeep(user_id, at)
            return self._count(user_id, since, None)

    def forget(self, user_id: str, submission_id: str) -> None:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return
            user.forget(submission_id)
            if not user:
                del self._users[user_id]

    def _count(self, user_id: str, since: datetime, exclude: str | None) -> int:
        user = self._users.get(user_id)
        return user.count_since(since, exclude) if user is not None else 0

    def _housekeep(self, user_id: str, now: datetime) -> None:
        """Bound memory: trim the contributor just touched, and now and then
        everyone, so users who never return do not stay for ever."""
        self._trim(user_id, now)
        self._records += 1
        if self._records % self._sweep_every == 0:
            for other in list(self._users):
                self._trim(other, now)

    def _trim(self, user_id: str, now: datetime) -> None:
        user = self._users.get(user_id)
        if user is None:
            return
        user.trim(now - self.retention)
        if not user:
            del self._users[user_id]


class SubmissionVelocityFilter(PreFilter):
    """Flags a fast contributor, rejects an impossible one.

    `check` reads the rate; `commit` records the submission and counts again in
    the same step, so its verdict is the authoritative one when several
    submissions from one user arrive together. Only submissions that pass the
    whole stage are recorded: one rejected anywhere never happened, rate-wise.

    That makes this a throughput cap, not a ban. A script is held to just under
    `reject_threshold` accepted submissions per window for as long as it keeps
    trying, because its rejected attempts are not counted (`check` must not
    change state). Blocking a caught script outright would need a separate
    attempts counter and cooldown, written by the stage on an enforced reject.
    """

    name = "submission_velocity"
    # Cheap: one dict lookup and a short scan, no hashing and no decoding.
    cost_rank = 15

    def __init__(
        self,
        settings: VelocitySettings | None = None,
        store: SubmissionTimesStore | None = None,
    ):
        self._settings = settings or VelocitySettings()
        self._store = store if store is not None else InMemorySubmissionTimes(
            retention=max(self._settings.window * 2, DEFAULT_RETENTION)
        )
        if self._store.retention < self._settings.window:
            raise ValueError(
                f"the submission-times store remembers {self._store.retention}, less than the "
                f"{self._settings.window} window: the rate would be undercounted"
            )

    def check(self, interaction: Interaction) -> PreFilterResult:
        # Excluding this submission's own record, then adding one for it, so a
        # re-validated submission is not counted twice.
        count = 1 + self._store.count_within(
            interaction.user_id, self._since(interaction), interaction.submission_id
        )
        return self._verdict(count) or PreFilterResult.passed()

    def commit(self, interaction: Interaction) -> PreFilterResult | None:
        count = self._store.record(
            interaction.user_id,
            interaction.submission_id,
            interaction.submitted_at,
            self._since(interaction),
        )
        return self._verdict(count)

    def rollback(self, interaction: Interaction) -> None:
        """A submission the stage rejected at commit time leaves no record.

        The record belongs to the submission id, not to one validation run: if
        an already-accepted submission is validated again and the stage now
        rejects it, its record goes too. That matches `ImageKeyStore.release`,
        which also keys ownership on the submission id, so every stateful
        filter agrees on what a rejected submission leaves behind: nothing.
        """
        self._store.forget(interaction.user_id, interaction.submission_id)

    def _since(self, interaction: Interaction) -> datetime:
        # The submission's own timestamp, not wall clock: a backlog replayed
        # after an outage is not a burst.
        return interaction.submitted_at - self._settings.window

    def _verdict(self, count: int) -> PreFilterResult | None:
        settings = self._settings
        if count < settings.flag_threshold:
            return None
        details = {
            "submissions": count,
            "window_seconds": settings.window_seconds,
            "per_minute": round(count / (settings.window_seconds / 60), 2),
        }
        if count >= settings.reject_threshold:
            return PreFilterResult.rejected(REASON_RATE_AUTOMATED, confidence=0.9, **details)
        return PreFilterResult.flagged(REASON_RATE_ELEVATED, confidence=0.6, **details)
