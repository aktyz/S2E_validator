"""A pretend RPC node with the Survey2Earn program behind it, as far as the
Solana client can tell. Needs `solders`; import it only from tests that skip
without it."""

from __future__ import annotations

import base64
import struct
from datetime import datetime, timedelta, timezone

from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from s2e_validator.settlement import solana as s

PROGRAM = Pubkey.from_string("S2EdoVooa24ZL3H5KUkdq7jj9uU5aoJhHUAKLPdqmYD")
SURVEY_ID, RUN_ID = 7, 2
MINT = Keypair().pubkey()
AUTHORITY = Keypair().pubkey()
VALIDATOR = Keypair()
# Far in the future: the service tests run on the real clock, and a run that
# has stopped pays nothing.
STOP = datetime(2100, 1, 1, tzinfo=timezone.utc)
NOW = STOP - timedelta(days=3)

SURVEY = Pubkey.find_program_address([b"survey_config", struct.pack("<Q", SURVEY_ID)], PROGRAM)[0]
RUN = Pubkey.find_program_address([b"survey_run", bytes(SURVEY), struct.pack("<Q", RUN_ID)], PROGRAM)[0]
VAULT = Pubkey.find_program_address([b"survey_treasury", bytes(RUN)], PROGRAM)[0]
PROGRAM_CONFIG = Pubkey.find_program_address([b"program_config"], PROGRAM)[0]


def account(name: str, body: bytes) -> bytes:
    return s.anchor_discriminator("account", name) + body


def program_config_account(protocol_state: int = 1) -> bytes:
    # authority, validator_registry_flag, protocol_state, next_survey_id, created_at, bump
    return account("ProgramConfigAccount", bytes(AUTHORITY) + struct.pack("<BBQqB", 0, protocol_state, 8, 0, 254))


def survey_account(lifecycle: int = 4) -> bytes:
    # survey_id, creator, lifecycle, governance_council, next_run_id, active_run_id (Some), ...
    body = struct.pack("<Q", SURVEY_ID) + bytes(32) + bytes([lifecycle]) + bytes(32)
    body += struct.pack("<QBQQQqB", RUN_ID + 2, 1, RUN_ID, 5, RUN_ID + 1, 0, 255)
    return account("SurveyConfigAccount", body)


def token_account(amount: int) -> bytes:
    # mint, owner, amount, then the rest of an SPL token account
    return bytes(MINT) + bytes(RUN) + struct.pack("<Q", amount) + bytes(93)


def run_account(
    expected_sequence: int = 0,
    lifecycle: int = 2,
    validator: Pubkey | None = None,
    survey: Pubkey = SURVEY,
    run_id: int = RUN_ID,
    reward_cap: int = 1_000_000,
    emitted_rewards: int = 0,
) -> bytes:
    body = struct.pack(
        "<32sQQ32s32sQBQqqBQQqBB",
        bytes(survey), SURVEY_ID, run_id, bytes(validator or VALIDATOR.pubkey()), bytes(MINT),
        reward_cap, 6, 1, 0, int(STOP.timestamp()), lifecycle, emitted_rewards, expected_sequence, 0, 255, 254,
    )
    return account("SurveyRunConfigAccount", body)


class Clock:
    """Time that only moves when the client sleeps."""

    def __init__(self):
        self.now = 0.0
        self.on_sleep = lambda: None

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.on_sleep()

    def monotonic(self) -> float:
        return self.now


class FakeNode:
    """An RPC node and the program behind it, as far as the client can tell.

    `on_send` decides what happens to a transaction: land, fail (it ran and the
    program refused it), drop (never seen again), or preflight:<what>.
    """

    def __init__(self, clock: Clock | None = None):
        self.owner = str(PROGRAM)
        self.expected_sequence = 0
        self.accounts: dict[str, bytes | None] = {
            str(PROGRAM_CONFIG): program_config_account(),
            str(SURVEY): survey_account(),
            str(VAULT): token_account(1_000_000),
        }
        self.height = 5_000
        self.blockhash = Hash.new_unique()
        self.on_send: list[str] = []
        self.sent: list[Transaction] = []
        self.landed: list[tuple[int, tuple[int, ...]]] = []
        self.statuses: dict[str, dict | None] = {}
        self.calls: list[str] = []
        if clock is not None:
            clock.on_sleep = self._tick

    def _tick(self) -> None:
        self.height += 5

    def call(self, method, params=()):
        self.calls.append(method)
        return getattr(self, method)(*params)

    def getMultipleAccounts(self, addresses, options):
        assert options == {"encoding": "base64", "commitment": "confirmed"}
        values = []
        for address in addresses:
            data = run_account(self.expected_sequence) if address == str(RUN) else None
            data = self.accounts.get(address, data)
            values.append(
                None if data is None
                else {"owner": self.owner, "data": [base64.b64encode(data).decode(), "base64"]}
            )
        return {"value": values}

    def getLatestBlockhash(self, options):
        return {"value": {"blockhash": str(self.blockhash), "lastValidBlockHeight": self.height + 150}}

    def getBlockHeight(self, options):
        return self.height

    def getSignatureStatuses(self, signatures):
        return {"value": [self.statuses.get(sig) for sig in signatures]}

    def sendTransaction(self, encoded, options):
        assert options == {"encoding": "base64", "preflightCommitment": "confirmed"}
        transaction = Transaction.from_bytes(base64.b64decode(encoded))
        self.sent.append(transaction)
        signature = str(transaction.signatures[0])
        behaviour = self.on_send.pop(0) if self.on_send else "land"
        if behaviour.startswith("preflight:"):
            err = {
                "program": {"InstructionError": [1, {"Custom": 11_002}]},
                "sequence": {"InstructionError": [1, {"Custom": 11_000}]},
                "blockhash": "BlockhashNotFound",
            }[behaviour.split(":")[1]]
            raise s.RpcError("Transaction simulation failed", -32002, {"err": err, "logs": []})
        if behaviour == "land":
            sequence, scores = decode_batch(transaction)
            assert sequence == self.expected_sequence, "the fake program would reject this"
            self.expected_sequence += 1
            self.landed.append((sequence, scores))
            self.statuses[signature] = {"confirmationStatus": "confirmed", "err": None}
        elif behaviour == "fail":
            self.statuses[signature] = {
                "confirmationStatus": "confirmed",
                "err": {"InstructionError": [1, {"Custom": 11_000}]},
            }
        elif behaviour == "processed_only":
            self.statuses[signature] = {"confirmationStatus": "processed", "err": None}
        elif behaviour != "drop":
            raise ValueError(behaviour)
        return signature


def decode_batch(transaction: Transaction) -> tuple[int, tuple[int, ...]]:
    data = bytes(transaction.message.instructions[1].data)
    sequence, count = struct.unpack_from("<QI", data, 8)
    return sequence, struct.unpack_from(f"<{count}Q", data, 20)
