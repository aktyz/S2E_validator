"""The batch log on disk, so a restart never forgets a send (#43, M4).

`InMemoryBatchLog` is enough to prove the submitter's rules; this is what makes
them hold across a crash. Every write is its own transaction, committed with
`synchronous=FULL` before the call returns. That matters most for SENDING: the
submitter broadcasts only after `update` returns, so a transaction can never be
on its way without the log on disk knowing.

One writer at a time, across processes: writes take an OS file lock next to the
database, which the operating system releases if the process dies. A second
validator pointed at the same log gets `LockBusy` rather than a race.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..filelock import FileLock, LockBusy
from .batches import Batch, Payable
from .chain import Ticket
from .log import BatchRecord, BatchStatus

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    position  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id  TEXT NOT NULL UNIQUE,
    status    TEXT NOT NULL,
    entries   TEXT NOT NULL,
    ticket    TEXT,
    attempts  INTEGER NOT NULL DEFAULT 0,
    failure   TEXT
);
CREATE TABLE IF NOT EXISTS claims (
    submission_id TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES batches (batch_id)
);
"""

__all__ = ["LockBusy", "SqliteBatchLog"]


class SqliteBatchLog:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._file_lock = FileLock(self._path.with_name(self._path.name + ".lock"))
        # Two locks, two jobs. The writer lock is held across a whole settle,
        # which can last minutes while a send is in doubt. The connection lock
        # is held for one statement or one transaction only, so a read never
        # waits for a send to finish.
        self._writer_lock = threading.RLock()
        self._db_lock = threading.RLock()
        self._depth = 0
        self._db = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # FULL: a commit is on disk before it returns. The default in WAL mode
        # (NORMAL) can lose the last commits on power loss, and the last
        # commit before a broadcast is exactly the one that must survive.
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(
                f"{self._path} has batch-log schema {version}; this code understands {SCHEMA_VERSION}"
            )
        # executescript commits as it goes; CREATE ... IF NOT EXISTS is safe to
        # repeat on every start.
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._writer_lock, self._db_lock:
            self._db.close()
            self._file_lock.release()

    @contextmanager
    def locked(self) -> Generator[None, None, None]:
        """Exclusive across threads and processes. Re-entrant in one object."""
        with self._writer_lock:
            if self._depth == 0:
                self._file_lock.acquire()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._file_lock.release()

    # -- reads: only the connection lock, never the writer lock ---------------

    def claimed(self, submission_id: str) -> bool:
        with self._db_lock:
            row = self._db.execute(
                "SELECT 1 FROM claims WHERE submission_id = ?", (submission_id,)
            ).fetchone()
        return row is not None

    def unfinished(self) -> list[BatchRecord]:
        return self._select(
            "WHERE status IN (?, ?)", (BatchStatus.PLANNED.value, BatchStatus.SENDING.value)
        )

    def records(self) -> list[BatchRecord]:
        return self._select("", ())

    def record_for(self, submission_id: str) -> BatchRecord | None:
        found = self._select(
            "WHERE batch_id = (SELECT batch_id FROM claims WHERE submission_id = ?)", (submission_id,)
        )
        return found[0] if found else None

    # -- writes: each one committed before it returns ------------------------

    def add(self, batch: Batch) -> BatchRecord:
        record = BatchRecord(batch)
        with self.locked(), self._transaction():
            try:
                self._db.execute(
                    "INSERT INTO batches (batch_id, status, entries) VALUES (?, ?, ?)",
                    (batch.batch_id, record.status.value, _entries_json(batch)),
                )
                self._db.executemany(
                    "INSERT INTO claims (submission_id, batch_id) VALUES (?, ?)",
                    [(s, batch.batch_id) for s in batch.submission_ids],
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"batch {batch.batch_id} or one of its submissions is already logged"
                ) from None
        return record

    def update(self, record: BatchRecord) -> None:
        with self.locked(), self._transaction():
            cursor = self._db.execute(
                "UPDATE batches SET status = ?, ticket = ?, attempts = ?, failure = ? "
                "WHERE batch_id = ?",
                (
                    record.status.value,
                    _ticket_json(record.ticket),
                    record.attempts,
                    record.failure,
                    record.batch_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(record.batch_id)

    def requeue(self, batch_id: str) -> BatchRecord:
        """Put a FAILED batch back in the queue, after a person has fixed why."""
        with self.locked():
            matches = [r for r in self.records() if r.batch_id == batch_id]
            if not matches:
                raise KeyError(batch_id)
            record = matches[0]
            if record.status is not BatchStatus.FAILED:
                raise ValueError(f"only a FAILED batch can be requeued, not {record.status.value}")
            requeued = replace(record, status=BatchStatus.PLANNED, ticket=None, failure=None)
            self.update(requeued)
            return requeued

    # -- internals ------------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        with self._db_lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _select(self, where: str, params: tuple) -> list[BatchRecord]:
        with self._db_lock:
            rows = self._db.execute(
                "SELECT entries, status, ticket, attempts, failure FROM batches "
                f"{where} ORDER BY position",
                params,
            ).fetchall()
        return [
            BatchRecord(
                batch=Batch(tuple(Payable(*entry) for entry in json.loads(entries))),
                status=BatchStatus(status),
                ticket=_ticket_from_json(ticket),
                attempts=attempts,
                failure=failure,
            )
            for entries, status, ticket, attempts, failure in rows
        ]


def _entries_json(batch: Batch) -> str:
    return json.dumps([[e.submission_id, e.user, e.quality] for e in batch.entries])


def _ticket_json(ticket: Ticket | None) -> str | None:
    if ticket is None:
        return None
    return json.dumps(
        {
            "sequence": ticket.sequence,
            "signature": ticket.signature,
            "last_valid_block_height": ticket.last_valid_block_height,
            "payload": base64.b64encode(ticket.payload).decode("ascii"),
        }
    )


def _ticket_from_json(raw: str | None) -> Ticket | None:
    if raw is None:
        return None
    data = json.loads(raw)
    return Ticket(
        sequence=data["sequence"],
        signature=data["signature"],
        last_valid_block_height=data["last_valid_block_height"],
        payload=base64.b64decode(data["payload"]),
    )
