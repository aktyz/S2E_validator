"""A chain client that settles nothing, for building against the API.

It accepts every batch, hands back a made-up signature and advances a sequence
number, so the demo page and the API can be developed before the program is
deployed. **Nothing reaches Solana.** Its signatures start with `dryrun-`, and
the API reports `settled_on: "dry-run"`, so a dry run can never be mistaken for
a payout.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from ..clock import utc_now
from .batches import Payable
from .chain import BroadcastResult, Outcome, RunState, Ticket

NETWORK_NAME = "dry-run"


class DryRunChain:
    def __init__(
        self,
        validator_authority: str,
        clock: Callable[[], datetime] = utc_now,
        run_length: timedelta = timedelta(days=365),
    ):
        self._validator_authority = validator_authority
        self._stop = clock() + run_length
        self._lock = threading.Lock()
        self._expected_sequence = 0
        self._height = 0

    def run_state(self) -> RunState:
        with self._lock:
            return RunState(
                expected_sequence=self._expected_sequence,
                stop_timestamp=self._stop,
                protocol_active=True,
                survey_settleable=True,
                run_active=True,
                validator_authority=self._validator_authority,
            )

    def prepare(self, sequence: int, entries: Sequence[Payable]) -> Ticket:
        digest = hashlib.sha256(
            f"{sequence}:".encode() + b"|".join(e.submission_id.encode() for e in entries)
        ).hexdigest()
        with self._lock:
            return Ticket(sequence, f"dryrun-{digest[:32]}", self._height + 150)

    def broadcast(self, ticket: Ticket) -> BroadcastResult:
        with self._lock:
            self._height += 1
            if ticket.sequence != self._expected_sequence:
                return BroadcastResult(Outcome.FAILED, "InvalidBatchSequence")
            self._expected_sequence += 1
            return BroadcastResult(Outcome.LANDED)

    def wait_until_expired(self, ticket: Ticket) -> None:
        with self._lock:
            self._height = max(self._height, ticket.last_valid_block_height + 1)
