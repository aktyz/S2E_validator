"""Grouping payable submissions into reward batches the program will accept.

`execute_reward_batch` takes at most `MAX_BATCH_SIZE` (10) users, each at most
once. A contributor with several scored submissions therefore spreads across
batches, one entry per submission, rather than being combined. That is the
same total under today's linear reward formula; whether the target tokenomics
wants a combined entry instead is decision D3 in #43, and would change only
this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from . import base58

#: The most users one legacy transaction carries (#82): 428 fixed bytes plus
#: 74 per user is 1168 of the 1232 allowed at 10, and 1242 at 11. The program's
#: own `MAX_BATCH_SIZE` (constants.rs) is being lowered to the same number.
MAX_BATCH_SIZE = 10
_U64_MAX = 2**64 - 1


@dataclass(frozen=True)
class Payable:
    """One scored submission, ready to be paid.

    `user` is the contributor's wallet address; `quality` is the number sent
    on-chain as Q_i, already mapped from the judge's score (#43, M1).
    """

    submission_id: str
    user: str
    quality: int

    def __post_init__(self) -> None:
        if not self.submission_id:
            raise ValueError("Payable.submission_id must not be empty")
        try:
            wallet = base58.decode(self.user)
        except ValueError:
            wallet = b""
        if len(wallet) != 32:
            raise ValueError(f"Payable.user must be a Solana address, got {self.user!r}")
        # bool is an int in Python; `True` as a score is always a mistake.
        if isinstance(self.quality, bool) or not isinstance(self.quality, int):
            raise ValueError("Payable.quality must be a whole number")
        if not 0 <= self.quality <= _U64_MAX:
            raise ValueError("Payable.quality must fit in a u64")


@dataclass(frozen=True)
class Batch:
    """Entries destined for one `execute_reward_batch` call.

    `batch_id` depends only on the entries, never on the sequence number the
    chain assigns later, so the same batch is recognisable across retries and
    restarts.
    """

    entries: tuple[Payable, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.entries) <= MAX_BATCH_SIZE:
            raise ValueError(f"a batch holds 1 to {MAX_BATCH_SIZE} entries")
        users = [entry.user for entry in self.entries]
        if len(set(users)) != len(users):
            raise ValueError("a user may appear at most once per batch")

    @property
    def batch_id(self) -> str:
        digest = hashlib.sha256()
        for entry in self.entries:
            digest.update(f"{entry.submission_id}\0{entry.user}\0{entry.quality}\n".encode())
        return digest.hexdigest()[:16]

    @property
    def submission_ids(self) -> tuple[str, ...]:
        return tuple(entry.submission_id for entry in self.entries)


def plan_batches(payables: Iterable[Payable], max_size: int = MAX_BATCH_SIZE) -> list[Batch]:
    """Split payables into as few valid batches as the rules allow, in order.

    Each entry goes into the earliest batch that has room and does not already
    hold that user, so submissions are paid roughly first come, first served,
    and one prolific contributor never holds up everyone else. Deterministic:
    the same input always gives the same batches.
    """
    if not 1 <= max_size <= MAX_BATCH_SIZE:
        raise ValueError(f"max_size must be 1 to {MAX_BATCH_SIZE}")
    batches: list[list[Payable]] = []
    users_in: list[set[str]] = []
    seen: set[str] = set()
    for payable in payables:
        if payable.submission_id in seen:
            raise ValueError(f"submission {payable.submission_id} appears twice")
        seen.add(payable.submission_id)
        for entries, users in zip(batches, users_in):
            if len(entries) < max_size and payable.user not in users:
                entries.append(payable)
                users.add(payable.user)
                break
        else:
            batches.append([payable])
            users_in.append({payable.user})
    return [Batch(tuple(entries)) for entries in batches]
