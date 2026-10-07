"""Sending reward batches so that nobody is ever paid twice.

**Why a retry can never pay twice.** The program accepts a batch only when its
`batch_sequence` equals the run's `expected_sequence`, then increments it. So
for any sequence number N, at most one transaction carrying N can ever land.
The submitter keeps two rules on top of that:

1. A batch keeps the sequence number it was first sent with until the chain
   has given a *final* answer about that send.
2. The answer is only final once the transaction has expired (its blockhash is
   past `last_valid_block_height`). Then the chain says either "expects N+1",
   so it landed, or "still expects N", so it never will, and resending under N
   is safe.

Batches go one at a time, and nothing new is signed while an earlier send is
unresolved. A sequence number that moves in any other way means something else
is signing with this run's key, and the submitter stops.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from ..clock import utc_now
from .batches import MAX_BATCH_SIZE, Payable, plan_batches
from .chain import BroadcastResult, ChainClient, Outcome, RunState
from .log import BatchLog, BatchRecord, BatchStatus

logger = logging.getLogger(__name__)


class SequenceConflict(RuntimeError):
    """The chain's sequence moved in a way this submitter did not cause."""


class StopReason(str, Enum):
    DONE = "DONE"
    PROTOCOL_FROZEN = "PROTOCOL_FROZEN"
    SURVEY_NOT_SETTLEABLE = "SURVEY_NOT_SETTLEABLE"
    RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
    WRONG_SIGNER = "WRONG_SIGNER"
    TOO_CLOSE_TO_STOP = "TOO_CLOSE_TO_STOP"
    OVER_BUDGET = "OVER_BUDGET"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class SettlementReport:
    """What one `settle` call did, and what is still waiting."""

    stopped: StopReason
    confirmed: tuple[BatchRecord, ...]
    failed: tuple[BatchRecord, ...]
    waiting: tuple[BatchRecord, ...]
    detail: str | None = None

    @property
    def paid_submission_ids(self) -> tuple[str, ...]:
        return tuple(s for record in self.confirmed for s in record.batch.submission_ids)


class BatchSubmitter:
    def __init__(
        self,
        chain: ChainClient,
        log: BatchLog,
        signer_public_key: str,
        *,
        clock: Callable[[], datetime] = utc_now,
        safety_margin: timedelta = timedelta(minutes=5),
        max_attempts: int = 3,
        max_batch_size: int = MAX_BATCH_SIZE,
    ):
        """`safety_margin` is how long before the run's stop time sending ends.
        It has to outlast a transaction's lifetime (about 90 seconds on Solana)
        plus confirmation, so a batch sent just in time can still land."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if safety_margin < timedelta(minutes=2):
            raise ValueError("safety_margin must outlast a transaction's lifetime: 2 minutes at least")
        self._chain = chain
        self._log = log
        self._signer = signer_public_key
        self._clock = clock
        self._margin = safety_margin
        self._max_attempts = max_attempts
        self._max_batch_size = max_batch_size

    def settle(self, payables: Iterable[Payable]) -> SettlementReport:
        """Plan new payables into batches, then send everything unfinished.

        Safe to call again with the same payables, and safe to call after a
        crash: submissions already in a batch are never planned twice, and a
        send the previous process left unresolved is resolved before anything
        new is signed.
        """
        with self._log.locked():
            self.enqueue(payables)

            touched: list[str] = []
            stopped, detail = StopReason.DONE, None
            waiting: tuple[StopReason, str | None] | None = None
            # An unresolved send from before must be settled first: nothing new
            # may take a sequence number while an older one is in doubt.
            queue = sorted(self._log.unfinished(), key=lambda r: r.status is not BatchStatus.SENDING)
            for record in queue:
                touched.append(record.batch_id)
                stopped, detail = self._settle_one(record)
                if stopped is StopReason.OVER_BUDGET:
                    # It waits; a smaller batch behind it may still fit. Safe,
                    # because a sequence number is taken when a batch is sent,
                    # not when it is planned.
                    waiting = waiting or (stopped, detail)
                    stopped, detail = StopReason.DONE, None
                    continue
                if stopped is not StopReason.DONE:
                    break
            if stopped is StopReason.DONE and waiting is not None:
                stopped, detail = waiting
            return self._report(stopped, detail, touched)

    def enqueue(self, payables: Iterable[Payable]) -> None:
        """Plan new payables into batches and record them, without sending.

        Once this returns, the rewards are owed and `settle` will send them.
        If it raises, nothing was promised: the caller must not tell anyone
        their reward is waiting.
        """
        with self._log.locked():
            fresh = [p for p in payables if not self._log.claimed(p.submission_id)]
            for batch in plan_batches(fresh, self._max_batch_size):
                self._log.add(batch)

    def _settle_one(self, record: BatchRecord) -> tuple[StopReason, str | None]:
        for _ in range(self._max_attempts):
            if record.status is BatchStatus.SENDING:
                if self._landed(record):
                    self._finish(record, BatchStatus.CONFIRMED)
                    return StopReason.DONE, None
                # Final answer: it never landed and never will. Free to resend.
                record = replace(record, status=BatchStatus.PLANNED, ticket=None)
                self._log.update(record)

            state = self._chain.run_state()
            refusal = self._refusal(state)
            if refusal is not None:
                return refusal
            total = sum(entry.quality for entry in record.batch.entries)
            if state.budget is not None and total > state.budget:
                # It waits, unchanged, until the run can pay it in full. Sent
                # now, the program would scale it down and close the run's cap.
                return (
                    StopReason.OVER_BUDGET,
                    f"batch {record.batch_id} totals {total} base units; the run can pay {state.budget}",
                )

            ticket = self._chain.prepare(state.expected_sequence, record.batch.entries)
            # Logged before it is broadcast: whatever happens next, the log
            # knows this transaction may exist.
            record = replace(
                record, status=BatchStatus.SENDING, ticket=ticket, attempts=record.attempts + 1
            )
            self._log.update(record)
            result = self._broadcast(record)

            if result.outcome is Outcome.LANDED:
                self._finish(record, BatchStatus.CONFIRMED)
                return StopReason.DONE, None
            if result.outcome is Outcome.FAILED:
                # Definitely not landed, so the sequence number is free again,
                # and the next batch can use it. This one needs a person.
                self._finish(record, BatchStatus.FAILED, result.reason or "rejected")
                return StopReason.DONE, None
            # UNKNOWN: loop, and the SENDING branch above resolves it.

        return (
            StopReason.UNRESOLVED,
            f"batch {record.batch_id} did not land after {self._max_attempts} attempts",
        )

    def _landed(self, record: BatchRecord) -> bool:
        """Resolve a send whose outcome is unknown. Blocks until it is final."""
        ticket = record.ticket
        assert ticket is not None
        self._chain.wait_until_expired(ticket)
        expected = self._chain.run_state().expected_sequence
        if expected == ticket.sequence + 1:
            return True
        if expected == ticket.sequence:
            return False
        raise SequenceConflict(
            f"batch {record.batch_id} was sent as sequence {ticket.sequence}, but the chain "
            f"now expects {expected}. Something else is signing with this run's key."
        )

    def _broadcast(self, record: BatchRecord) -> BroadcastResult:
        assert record.ticket is not None
        try:
            return self._chain.broadcast(record.ticket)
        except Exception:  # noqa: BLE001 - it may have gone out; resolve, never assume
            logger.exception(
                "batch_broadcast_error batch=%s sequence=%d", record.batch_id, record.ticket.sequence
            )
            return BroadcastResult(Outcome.UNKNOWN)

    def _refusal(self, state: RunState) -> tuple[StopReason, str] | None:
        if not state.protocol_active:
            return StopReason.PROTOCOL_FROZEN, "the protocol is frozen"
        if not state.survey_settleable:
            return StopReason.SURVEY_NOT_SETTLEABLE, "the survey is not in a state that allows payouts"
        if not state.run_active:
            return StopReason.RUN_NOT_ACTIVE, "the run is not Active"
        if state.validator_authority != self._signer:
            return (
                StopReason.WRONG_SIGNER,
                f"the run's validator_authority is {state.validator_authority}, not {self._signer}",
            )
        if self._clock() + self._margin >= state.stop_timestamp:
            return (
                StopReason.TOO_CLOSE_TO_STOP,
                f"the run stops at {state.stop_timestamp.isoformat()}, inside the "
                f"{self._margin} safety margin",
            )
        return None

    def _finish(self, record: BatchRecord, status: BatchStatus, failure: str | None = None) -> None:
        self._log.update(replace(record, status=status, failure=failure))
        logger.info(
            "batch_%s batch=%s sequence=%s entries=%d%s",
            status.value.lower(),
            record.batch_id,
            record.sequence,
            len(record.batch.entries),
            f" reason={failure}" if failure else "",
        )

    def _report(self, stopped: StopReason, detail: str | None, touched: list[str]) -> SettlementReport:
        by_id = {r.batch_id: r for r in self._log.records()}
        mine = [by_id[batch_id] for batch_id in touched]
        return SettlementReport(
            stopped=stopped,
            confirmed=tuple(r for r in mine if r.status is BatchStatus.CONFIRMED),
            failed=tuple(r for r in mine if r.status is BatchStatus.FAILED),
            waiting=tuple(self._log.unfinished()),
            detail=detail,
        )
