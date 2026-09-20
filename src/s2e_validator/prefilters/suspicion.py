"""Append-only record of patterns worth investigating later.

Separate from `ImageKeyStore` on purpose: that store answers "has this key been
claimed?", while this one accumulates evidence about accounts across campaigns.
Nothing here affects a submission's verdict.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DuplicateImageEvent:
    """Two different accounts submitted a byte-identical photo.

    Innocent explanations exist (a shared photo, a couple at the same event), so
    this is evidence to weigh, never a verdict on its own.
    """

    image_key: str
    campaign_id: str
    original_submission_id: str
    original_user_id: str
    duplicate_submission_id: str
    duplicate_user_id: str
    occurred_at: datetime


class SuspicionLog(Protocol):
    def record(self, event: DuplicateImageEvent) -> None:
        """Record one event.

        **Must be idempotent on `(image_key, duplicate_submission_id)`.** The
        same match is reported from both `check` and `commit`, and a retried
        `commit` reports it again; one submission duplicating one photo is a
        single fact, and counting it twice would inflate an account's record.
        Any durable implementation needs that key anyway.
        """
        ...


class InMemorySuspicionLog:
    """Append-only in-process log; also emits a structured log line.

    Replace with a durable, append-only store when the fraud repository is built
    — the whole point of the record is that it outlives a single campaign.
    """

    def __init__(self) -> None:
        self._events: list[DuplicateImageEvent] = []
        # Idempotency key, per the Protocol: one submission duplicating one
        # photo is one fact, however many times it is reported.
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    @property
    def events(self) -> tuple[DuplicateImageEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def record(self, event: DuplicateImageEvent) -> None:
        fact = (event.image_key, event.duplicate_submission_id)
        with self._lock:
            if fact in self._seen:
                return
            self._seen.add(fact)
            self._events.append(event)
        logger.info(
            "duplicate_image_across_users campaign=%s key=%s original=%s/%s duplicate=%s/%s",
            event.campaign_id,
            event.image_key[:16],
            event.original_user_id,
            event.original_submission_id,
            event.duplicate_user_id,
            event.duplicate_submission_id,
        )
