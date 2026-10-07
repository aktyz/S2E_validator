"""What the batch submitter needs from Solana, independent of any library.

The real client (building `execute_reward_batch` for Zyta's program) comes
next; everything here is what the submitter relies on it to do.

One property of Solana carries the whole retry design. A transaction is
signed against a recent blockhash and is only valid until
`last_valid_block_height`. After that height it can never land. So "I sent it
and heard nothing back" is only ever a *temporary* unknown: wait until the
transaction has expired, then read the chain, and the answer is final.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from .batches import Payable


@dataclass(frozen=True)
class RunState:
    """What the program says about the run right now.

    `execute_reward_batch` refuses unless the protocol is not frozen, the
    survey is in a state that allows payouts (Closed or Refundable on main;
    Rolling or Refundable once #44 lands), the run is Active, the clock is before
    `stop_timestamp`, and the signer is the run's `validator_authority`.
    """

    expected_sequence: int
    stop_timestamp: datetime
    protocol_active: bool
    survey_settleable: bool
    run_active: bool
    validator_authority: str
    #: The most a batch may total right now, in base units: the smaller of the
    #: run's remaining cap and what its vault actually holds. A batch above
    #: the remaining cap is not refused by the program, it is scaled down and
    #: takes the whole cap, so the submitter must never send one. None means
    #: the client cannot tell (a dry run), and nothing is checked.
    budget: int | None = None


@dataclass(frozen=True)
class Ticket:
    """A signed transaction, before or after it was broadcast.

    Everything here is known *before* sending: Solana transactions are signed
    locally, and the signature is the transaction's id. That is what lets the
    log record a send before it happens, so a crash can never leave a
    transaction in flight that the log does not know about. A signed
    transaction is not secret; it becomes public the moment it is broadcast.
    """

    sequence: int
    signature: str
    last_valid_block_height: int
    payload: bytes = b""


class Outcome(str, Enum):
    LANDED = "LANDED"
    FAILED = "FAILED"  # final: the transaction ran and was rejected, or never can run
    UNKNOWN = "UNKNOWN"  # no answer yet; resolve by waiting until the ticket expires


@dataclass(frozen=True)
class BroadcastResult:
    outcome: Outcome
    reason: str | None = None


class ChainClient(Protocol):
    def run_state(self) -> RunState: ...

    def prepare(self, sequence: int, entries: Sequence[Payable]) -> Ticket:
        """Build and sign, but do not send."""
        ...

    def broadcast(self, ticket: Ticket) -> BroadcastResult:
        """Send and wait for confirmation, up to the client's own timeout.

        May raise on a transport error; the submitter treats that exactly like
        UNKNOWN, since the transaction may have gone out.
        """
        ...

    def wait_until_expired(self, ticket: Ticket) -> None:
        """Return once the chain is past `ticket.last_valid_block_height`, so
        the transaction can no longer land."""
        ...
