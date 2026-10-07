"""A stand-in for Solana and the Survey2Earn program, for settlement tests.

It enforces the two rules the no-double-payment argument rests on, the way the
real program and network do: a batch lands only with the run's current
`expected_sequence`, and a transaction can never land after its
`last_valid_block_height`. Everything that goes wrong in between (a dropped
send, a send that landed but whose confirmation was lost, a transport error, a
transaction that lands late) is scripted per broadcast.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from s2e_validator.settlement import base58
from s2e_validator.settlement.chain import BroadcastResult, Outcome, RunState, Ticket

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
SIGNER = base58.encode(bytes(range(32)))
#: Blocks a transaction stays valid for, as on Solana (about 60 to 90 seconds).
LIFETIME_BLOCKS = 150


def wallet(n: int) -> str:
    return base58.encode(hashlib.sha256(f"user-{n}".encode()).digest())


class SimulatedCrash(BaseException):
    """The validator process dies mid-send. BaseException, so no `except
    Exception` in the code under test can swallow it, just like a real kill."""


# Behaviours, one consumed per broadcast:
#   land            lands and confirms
#   fail            the program rejects it; final, nothing lands
#   drop            lost on the way; never lands, no answer
#   land_silently   lands, but the confirmation never arrives
#   late_land       no answer now; lands later, before it expires
#   raise           transport error; nothing went out
#   raise_landed    transport error after it went out and landed
#   crash_landed    lands, then the validator process dies
#   crash           the validator dies after logging the send, before it went out
BEHAVIOURS = (
    "land", "fail", "drop", "land_silently", "late_land",
    "raise", "raise_landed", "crash_landed", "crash",
)


@dataclass
class FakeChain:
    validator_authority: str = SIGNER
    stop_timestamp: datetime = NOW + timedelta(hours=6)
    script: list[str] = field(default_factory=list)
    protocol_active: bool = True
    survey_settleable: bool = True
    run_active: bool = True
    budget: int | None = None

    expected_sequence: int = 0
    height: int = 1_000
    paid: Counter = field(default_factory=Counter)
    landed: list[tuple[int, tuple[str, ...]]] = field(default_factory=list)
    broadcasts: int = 0
    _entries: dict[str, tuple] = field(default_factory=dict)
    _in_flight: list[Ticket] = field(default_factory=list)

    def run_state(self) -> RunState:
        return RunState(
            expected_sequence=self.expected_sequence,
            stop_timestamp=self.stop_timestamp,
            protocol_active=self.protocol_active,
            survey_settleable=self.survey_settleable,
            run_active=self.run_active,
            validator_authority=self.validator_authority,
            budget=self.budget,
        )

    def prepare(self, sequence, entries) -> Ticket:
        signature = hashlib.sha256(f"{sequence}:{self.height}:{len(self._entries)}".encode()).hexdigest()
        self._entries[signature] = tuple(entries)
        return Ticket(sequence, signature, self.height + LIFETIME_BLOCKS)

    def broadcast(self, ticket: Ticket) -> BroadcastResult:
        self.broadcasts += 1
        behaviour = self.script.pop(0) if self.script else "land"
        self.height += 1
        if behaviour == "land":
            if self._apply(ticket):
                return BroadcastResult(Outcome.LANDED)
            return BroadcastResult(Outcome.FAILED, "InvalidBatchSequence")
        if behaviour == "fail":
            return BroadcastResult(Outcome.FAILED, "simulated program error")
        if behaviour == "drop":
            return BroadcastResult(Outcome.UNKNOWN)
        if behaviour == "land_silently":
            self._apply(ticket)
            return BroadcastResult(Outcome.UNKNOWN)
        if behaviour == "late_land":
            self._in_flight.append(ticket)
            return BroadcastResult(Outcome.UNKNOWN)
        if behaviour == "raise":
            raise ConnectionError("RPC node unreachable")
        if behaviour == "raise_landed":
            self._apply(ticket)
            raise ConnectionError("connection reset after send")
        if behaviour == "crash_landed":
            self._apply(ticket)
            raise SimulatedCrash()
        if behaviour == "crash":
            raise SimulatedCrash()
        raise ValueError(behaviour)

    def wait_until_expired(self, ticket: Ticket) -> None:
        # Anything still in flight gets its chance to land first, as it would
        # while the validator waits.
        for pending in list(self._in_flight):
            self._in_flight.remove(pending)
            self._apply(pending)
        self.height = max(self.height, ticket.last_valid_block_height + 1)

    def _apply(self, ticket: Ticket) -> bool:
        """What the program does on receipt. False means it did not land."""
        if self.height > ticket.last_valid_block_height:
            return False
        if ticket.sequence != self.expected_sequence:
            return False
        entries = self._entries[ticket.signature]
        for entry in entries:
            self.paid[entry.submission_id] += 1
        self.landed.append((ticket.sequence, tuple(e.submission_id for e in entries)))
        self.expected_sequence += 1
        return True
