"""The record of every batch, written before anything is sent.

The order of writes is the safety argument:

1. a batch is logged as PLANNED before it is sent anywhere, so a submission is
   claimed by exactly one batch;
2. the signed transaction is logged as SENDING *before* it is broadcast, so a
   crash can never leave something in flight that the log does not know about;
3. only then is it broadcast, and the outcome recorded.

`InMemoryBatchLog` is enough for tests and a single run. A restart forgets it;
`SqliteBatchLog` is the durable one (#43, M4), and this interface is what both
implement.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from .batches import Batch
from .chain import Ticket


class BatchStatus(str, Enum):
    PLANNED = "PLANNED"  # not sent; no sequence number yet
    SENDING = "SENDING"  # a signed transaction exists and may be on its way
    CONFIRMED = "CONFIRMED"  # landed: these submissions are paid
    FAILED = "FAILED"  # definitely did not land; a person decides what next


@dataclass(frozen=True)
class BatchRecord:
    batch: Batch
    status: BatchStatus = BatchStatus.PLANNED
    #: The latest signed transaction. Kept after CONFIRMED, for the audit trail.
    ticket: Ticket | None = None
    attempts: int = 0
    failure: str | None = None

    @property
    def batch_id(self) -> str:
        return self.batch.batch_id

    @property
    def sequence(self) -> int | None:
        return self.ticket.sequence if self.ticket is not None else None


class BatchLog(Protocol):
    def locked(self) -> AbstractContextManager[None]:
        """Hold the log so only one submitter works on it at a time."""
        ...

    def claimed(self, submission_id: str) -> bool:
        """True once a submission is in any batch, whatever its status."""
        ...

    def add(self, batch: Batch) -> BatchRecord: ...

    def update(self, record: BatchRecord) -> None: ...

    def unfinished(self) -> list[BatchRecord]:
        """PLANNED and SENDING batches, oldest first."""
        ...

    def records(self) -> list[BatchRecord]: ...

    def record_for(self, submission_id: str) -> BatchRecord | None:
        """The batch a submission is in, without reading the whole log."""
        ...

    def requeue(self, batch_id: str) -> BatchRecord:
        """Put a FAILED batch back in the queue. Only ever done by a person."""
        ...


class InMemoryBatchLog:
    def __init__(self) -> None:
        self._records: dict[str, BatchRecord] = {}
        self._claimed: dict[str, str] = {}
        self._lock = threading.RLock()

    @contextmanager
    def locked(self) -> Generator[None, None, None]:
        with self._lock:
            yield

    def claimed(self, submission_id: str) -> bool:
        with self._lock:
            return submission_id in self._claimed

    def add(self, batch: Batch) -> BatchRecord:
        with self._lock:
            taken = [s for s in batch.submission_ids if s in self._claimed]
            if taken:
                raise ValueError(f"already in another batch: {taken}")
            if batch.batch_id in self._records:
                raise ValueError(f"batch {batch.batch_id} is already logged")
            record = BatchRecord(batch)
            self._records[batch.batch_id] = record
            for submission_id in batch.submission_ids:
                self._claimed[submission_id] = batch.batch_id
            return record

    def update(self, record: BatchRecord) -> None:
        with self._lock:
            if record.batch_id not in self._records:
                raise KeyError(record.batch_id)
            self._records[record.batch_id] = record

    def unfinished(self) -> list[BatchRecord]:
        with self._lock:
            return [
                r
                for r in self._records.values()
                if r.status in (BatchStatus.PLANNED, BatchStatus.SENDING)
            ]

    def records(self) -> list[BatchRecord]:
        with self._lock:
            return list(self._records.values())

    def record_for(self, submission_id: str) -> BatchRecord | None:
        with self._lock:
            batch_id = self._claimed.get(submission_id)
            return self._records[batch_id] if batch_id is not None else None

    def requeue(self, batch_id: str) -> BatchRecord:
        """Put a FAILED batch back in the queue, after a person has fixed why.

        Never automatic: a batch fails for a reason (an account the program
        rejects, an exhausted pool), and resending blindly would fail the same
        way on every run.
        """
        with self._lock:
            record = self._records[batch_id]
            if record.status is not BatchStatus.FAILED:
                raise ValueError(f"only a FAILED batch can be requeued, not {record.status.value}")
            requeued = replace(record, status=BatchStatus.PLANNED, ticket=None, failure=None)
            self._records[batch_id] = requeued
            return requeued
