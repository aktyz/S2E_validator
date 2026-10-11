"""The real chain client: `execute_reward_batch` on the Survey2Earn program.

Needs the optional `solana` extra (`pip install ".[solana]"`), which brings
`solders` for signing and address derivation. Talking to the RPC node is plain
JSON over HTTPS from the standard library.

Written against the program as of #47 (one treasury vault per run). The layouts
below are copied from `solana/programs/survey2earn/src`; if an account or the
instruction changes there, change it here and in tests/test_solana.py.

**One run, chosen by the operator.** The client is given a `run_id` and never
follows a rotation on its own. A sequence number only means something inside
one run: if a send from run 0 were resolved by reading run 1, "the chain still
expects 0" would look like "it never landed", and the batch would be paid
again. Pinning the run removes that case. Moving to the next run is a restart
with the new `run_id`, once the log has no send in doubt.

**How many users fit.** The program allows 64 users per batch, but a Solana
transaction is at most 1232 bytes and every user costs 74 of them (two 32-byte
addresses, two index bytes, one 8-byte score). `MAX_USERS_PER_TRANSACTION` is
what actually fits; reaching 64 needs address lookup tables, which is not built.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from .batches import MAX_BATCH_SIZE, Payable
from .chain import BroadcastResult, Outcome, RunState, Ticket
from .keys import ValidatorKey

logger = logging.getLogger(__name__)

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

#: A transaction larger than this is dropped by the network.
MAX_TRANSACTION_BYTES = 1232
#: Users per `execute_reward_batch` that fit in one transaction, with the
#: compute budget instruction. tests/test_solana.py proves this number.
MAX_USERS_PER_TRANSACTION = MAX_BATCH_SIZE
#: The default budget is 200,000 units per transaction. A batch does a token
#: transfer and an address derivation per user, so it asks for more, up front.
#: Estimates, to be replaced by numbers measured on devnet.
_COMPUTE_UNITS_BASE = 60_000
_COMPUTE_UNITS_PER_USER = 40_000

# Survey lifecycle values that allow payouts, and the run's Active value
# (state/survey_config.rs, state/survey_run_config.rs).
_SURVEY_REFUNDABLE, _SURVEY_ROLLING = 3, 4
_RUN_ACTIVE = 2
_PROTOCOL_FROZEN = 2

# Anchor adds 6000 to a program's own error numbers.
_ANCHOR_CUSTOM_OFFSET = 6000
_PROGRAM_ERRORS = {
    1001: "AccountNotMutable",
    2000: "UnauthorizedSigner",
    3001: "InvalidPDABump",
    3002: "PDAAddressMismatch",
    4100: "SurveyRunNotActive",
    4102: "InvalidSurveyRunLifecycleState",
    4200: "InvalidSurveyLifecycleState",
    4201: "SurveyInvalidStateTransition",
    4203: "InvalidSurveyConfig",
    4205: "ProtocolFrozen",
    5000: "InvalidBatchSequence",
    5001: "DuplicateUserInBatch",
    5002: "InsufficientTreasuryBalance",
    5004: "BatchSumExceedsPool",
    5005: "BatchSizeTooLarge",
    6002: "InvalidTokenMint",
    7000: "RewardCalculationOverflow",
    7001: "RewardCalculationUnderflow",
    9000: "InvalidParameter",
}
# Anchor's own errors a batch can meet, e.g. a contributor who never joined.
_ANCHOR_ERRORS = {
    2006: "ConstraintSeeds",
    3002: "AccountDiscriminatorMismatch",
    3007: "AccountOwnedByWrongProgram",
    3012: "AccountNotInitialized",
}

_COMMITMENT_RANK = {"processed": 0, "confirmed": 1, "finalized": 2}
_SEND_PREFLIGHT_FAILED = -32002
_INVALID_BATCH_SEQUENCE = _ANCHOR_CUSTOM_OFFSET + 5000


def anchor_discriminator(namespace: str, name: str) -> bytes:
    return hashlib.sha256(f"{namespace}:{name}".encode()).digest()[:8]


EXECUTE_REWARD_BATCH = anchor_discriminator("global", "execute_reward_batch")


class RpcError(RuntimeError):
    """The RPC node answered with an error, or could not be reached. The
    message never includes the node's URL, which can carry an API key."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class ChainStateError(RuntimeError):
    """An account the client needs is missing or is not what was configured."""


class JsonRpc:
    """Just enough JSON-RPC to read accounts and send a transaction."""

    def __init__(
        self,
        url: str,
        timeout: float = 20.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        parts = urlsplit(url)
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError("the RPC URL must be https (http is accepted for localhost only)")
        self._url = url
        self._timeout = timeout
        self._opener = opener

    def call(self, method: str, params: Sequence[Any] = ()) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)})
        request = urllib.request.Request(
            self._url, data=body.encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                reply = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # `from None`: urllib's errors can quote the URL.
            raise RpcError(f"{method}: the RPC node could not be reached ({type(exc).__name__})") from None
        if "error" in reply:
            error = reply["error"]
            raise RpcError(f"{method}: {error.get('message')}", error.get("code"), error.get("data"))
        return reply["result"]


@dataclass(frozen=True)
class _Run:
    survey_config: Pubkey
    run_id: int
    validator_authority: Pubkey
    reward_mint: Pubkey
    lifecycle: int
    stop_timestamp: int
    expected_sequence: int
    reward_cap: int
    emitted_rewards: int


_RUN_LAYOUT = struct.Struct("<32sQQ32s32sQBQqqBQQqBB")


def _body(data: bytes, account: str, minimum: int) -> bytes:
    """The fields of an Anchor account, after checking it is that account."""
    if data[:8] != anchor_discriminator("account", account) or len(data) < 8 + minimum:
        raise ChainStateError(f"the account is not a {account}")
    return data[8:]


def decode_run(data: bytes) -> _Run:
    fields = _RUN_LAYOUT.unpack_from(_body(data, "SurveyRunConfigAccount", _RUN_LAYOUT.size))
    return _Run(
        survey_config=Pubkey.from_bytes(fields[0]),
        run_id=fields[2],
        validator_authority=Pubkey.from_bytes(fields[3]),
        reward_mint=Pubkey.from_bytes(fields[4]),
        stop_timestamp=fields[9],
        lifecycle=fields[10],
        expected_sequence=fields[12],
        reward_cap=fields[5],
        emitted_rewards=fields[11],
    )


def token_amount(data: bytes) -> int:
    """The balance of an SPL token account: mint, owner, then amount as u64."""
    if len(data) < 72:
        raise ChainStateError("the account is not a token account")
    return struct.unpack_from("<Q", data, 64)[0]


def decode_survey_lifecycle(data: bytes) -> int:
    # survey_id: u64, creator: Pubkey, lifecycle: u8, ...
    return _body(data, "SurveyConfigAccount", 41)[40]


def decode_program_config(data: bytes) -> tuple[Pubkey, int]:
    """(authority, protocol_state)."""
    body = _body(data, "ProgramConfigAccount", 34)
    # authority: Pubkey, validator_registry_flag: bool, protocol_state: u8, ...
    return Pubkey.from_bytes(body[:32]), body[33]


def associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    address, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)], ASSOCIATED_TOKEN_PROGRAM
    )
    return address


def survey_config_address(program_id: Pubkey, survey_id: int) -> Pubkey:
    return Pubkey.find_program_address([b"survey_config", struct.pack("<Q", survey_id)], program_id)[0]


def user_survey_address(program_id: Pubkey, user: Pubkey, survey_config: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"user_survey", bytes(user), bytes(survey_config)], program_id)[0]


def describe_error(err: Any) -> str:
    """A transaction error from the RPC node, in words a person can act on."""
    if isinstance(err, dict) and "InstructionError" in err:
        _, detail = err["InstructionError"]
        if isinstance(detail, dict) and "Custom" in detail:
            code = detail["Custom"]
            name = _PROGRAM_ERRORS.get(code - _ANCHOR_CUSTOM_OFFSET) or _ANCHOR_ERRORS.get(code)
            return f"{name} (program error {code})" if name else f"program error {code}"
        return str(detail)
    return json.dumps(err) if not isinstance(err, str) else err


class SolanaChain:
    """`ChainClient` for one run of one survey on the deployed program."""

    def __init__(
        self,
        rpc: JsonRpc,
        key: ValidatorKey,
        program_id: str,
        survey_id: int,
        run_id: int,
        *,
        dust_token_account: str | None = None,
        commitment: str = "confirmed",
        confirm_timeout: float = 60.0,
        expiry_timeout: float = 300.0,
        poll_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        """`dust_token_account` is the program authority's token account for
        the reward mint. It must exist: the program reads it on every batch,
        and sends it the rounding dust when a batch is scaled down to the cap.
        Left out, it is the authority's associated token account."""
        if commitment not in ("confirmed", "finalized"):
            raise ValueError("commitment must be confirmed or finalized")
        self._dust = Pubkey.from_string(dust_token_account) if dust_token_account else None
        self._rpc = rpc
        # solders checks that the secret half matches the public half, which
        # keys.py could not (see its docstring).
        self._signer = Keypair.from_bytes(key.keypair_bytes())
        self._program = Pubkey.from_string(program_id)
        self._commitment = commitment
        self._confirm_timeout = confirm_timeout
        self._expiry_timeout = expiry_timeout
        self._poll = poll_interval
        self._sleep = sleep
        self._monotonic = monotonic

        self._program_config = Pubkey.find_program_address([b"program_config"], self._program)[0]
        self._survey = survey_config_address(self._program, survey_id)
        self._run_id = run_id
        self._run = Pubkey.find_program_address(
            [b"survey_run", bytes(self._survey), struct.pack("<Q", run_id)], self._program
        )[0]
        self._vault = Pubkey.find_program_address([b"survey_treasury", bytes(self._run)], self._program)[0]

    # -- reading -----------------------------------------------------------

    def _accounts(self) -> tuple[Pubkey, int, int, _Run, int]:
        """(program authority, protocol state, survey lifecycle, run, vault
        balance), read together so they describe one moment."""
        addresses = [self._program_config, self._survey, self._run, self._vault]
        reply = self._rpc.call(
            "getMultipleAccounts",
            [[str(a) for a in addresses], {"encoding": "base64", "commitment": self._commitment}],
        )
        raw: list[bytes] = []
        vault = reply["value"][3]
        # A vault that does not exist holds nothing. A keeper-created run
        # starts that way until it is funded.
        vault_balance = 0 if vault is None else token_amount(base64.b64decode(vault["data"][0]))
        for name, address, value in zip(("program config", "survey", "run"), addresses, reply["value"]):
            if value is None:
                raise ChainStateError(f"no {name} account at {address}. Wrong program, survey or run id?")
            if value["owner"] != str(self._program):
                raise ChainStateError(f"the {name} account at {address} is not owned by the program")
            raw.append(base64.b64decode(value["data"][0]))
        authority, protocol_state = decode_program_config(raw[0])
        run = decode_run(raw[2])
        if run.survey_config != self._survey or run.run_id != self._run_id:
            raise ChainStateError(f"the run account at {self._run} belongs to another survey or run")
        return authority, protocol_state, decode_survey_lifecycle(raw[1]), run, vault_balance

    def run_state(self) -> RunState:
        _, protocol_state, survey_lifecycle, run, vault_balance = self._accounts()
        return RunState(
            expected_sequence=run.expected_sequence,
            stop_timestamp=datetime.fromtimestamp(run.stop_timestamp, tz=timezone.utc),
            protocol_active=protocol_state != _PROTOCOL_FROZEN,
            survey_settleable=survey_lifecycle in (_SURVEY_ROLLING, _SURVEY_REFUNDABLE),
            run_active=run.lifecycle == _RUN_ACTIVE,
            validator_authority=str(run.validator_authority),
            # `reward_cap - emitted_rewards` is a counter, not a balance.
            budget=min(max(run.reward_cap - run.emitted_rewards, 0), vault_balance),
        )

    # -- building ----------------------------------------------------------

    def instruction(
        self, sequence: int, entries: Sequence[Payable], reward_mint: Pubkey, authority: Pubkey
    ) -> Instruction:
        data = EXECUTE_REWARD_BATCH + struct.pack("<QI", sequence, len(entries))
        data += b"".join(struct.pack("<Q", entry.quality) for entry in entries)
        accounts = [
            AccountMeta(self._signer.pubkey(), is_signer=True, is_writable=False),
            AccountMeta(self._program_config, is_signer=False, is_writable=False),
            AccountMeta(self._survey, is_signer=False, is_writable=False),
            AccountMeta(self._run, is_signer=False, is_writable=True),
            AccountMeta(self._vault, is_signer=False, is_writable=True),
            AccountMeta(
                self._dust or associated_token_address(authority, reward_mint),
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
        ]
        for entry in entries:
            user = Pubkey.from_string(entry.user)
            accounts.append(AccountMeta(user_survey_address(self._program, user, self._survey), False, True))
            accounts.append(AccountMeta(associated_token_address(user, reward_mint), False, True))
        return Instruction(self._program, data, accounts)

    def prepare(self, sequence: int, entries: Sequence[Payable]) -> Ticket:
        if not 1 <= len(entries) <= MAX_USERS_PER_TRANSACTION:
            raise ValueError(
                f"a transaction holds 1 to {MAX_USERS_PER_TRANSACTION} users, got {len(entries)}. "
                "Plan batches with max_batch_size=MAX_USERS_PER_TRANSACTION."
            )
        authority, _, _, run, _ = self._accounts()
        latest = self._rpc.call("getLatestBlockhash", [{"commitment": self._commitment}])["value"]
        blockhash = Hash.from_string(latest["blockhash"])
        units = _COMPUTE_UNITS_BASE + _COMPUTE_UNITS_PER_USER * len(entries)
        message = Message.new_with_blockhash(
            [
                set_compute_unit_limit(units),
                self.instruction(sequence, entries, run.reward_mint, authority),
            ],
            self._signer.pubkey(),
            blockhash,
        )
        transaction = Transaction([self._signer], message, blockhash)
        payload = bytes(transaction)
        if len(payload) > MAX_TRANSACTION_BYTES:
            raise ValueError(f"the transaction is {len(payload)} bytes, over the {MAX_TRANSACTION_BYTES} limit")
        return Ticket(sequence, str(transaction.signatures[0]), latest["lastValidBlockHeight"], payload)

    # -- sending -----------------------------------------------------------

    def broadcast(self, ticket: Ticket) -> BroadcastResult:
        try:
            self._rpc.call(
                "sendTransaction",
                [
                    base64.b64encode(ticket.payload).decode("ascii"),
                    {"encoding": "base64", "preflightCommitment": self._commitment},
                ],
            )
        except RpcError as exc:
            err = exc.data.get("err") if isinstance(exc.data, dict) else None
            if (
                exc.code == _SEND_PREFLIGHT_FAILED
                and isinstance(err, dict)
                and "InstructionError" in err
                and err["InstructionError"][1] != {"Custom": _INVALID_BATCH_SEQUENCE}
            ):
                # The node ran it, the program refused it, and the node did not
                # pass it on. Nobody else has this transaction, so it is final.
                return BroadcastResult(Outcome.FAILED, describe_error(err))
            # A wrong sequence in simulation is the one program error a node
            # that lags the chain produces by itself, so it proves nothing.
            # Anything else (node behind, blockhash not found, already
            # processed) proves nothing either way: the submitter resolves it.
            raise

        deadline = self._monotonic() + self._confirm_timeout
        while self._monotonic() < deadline:
            status = self._rpc.call("getSignatureStatuses", [[ticket.signature]])["value"][0]
            # A status below our commitment could still be on a fork that loses.
            if status is not None and self._reached(status.get("confirmationStatus")):
                if status.get("err") is not None:
                    return BroadcastResult(Outcome.FAILED, describe_error(status["err"]))
                return BroadcastResult(Outcome.LANDED)
            self._sleep(self._poll)
        return BroadcastResult(Outcome.UNKNOWN)

    def _reached(self, confirmation: str | None) -> bool:
        return _COMMITMENT_RANK.get(confirmation or "", -1) >= _COMMITMENT_RANK[self._commitment]

    def wait_until_expired(self, ticket: Ticket) -> None:
        deadline = self._monotonic() + self._expiry_timeout
        while True:
            height = self._rpc.call("getBlockHeight", [{"commitment": self._commitment}])
            if height > ticket.last_valid_block_height:
                return
            if self._monotonic() >= deadline:
                # The send stays logged as in doubt; the next settle call
                # comes back here. Nothing is resent in the meantime.
                raise TimeoutError(
                    f"the chain did not pass block height {ticket.last_valid_block_height} "
                    f"within {self._expiry_timeout:.0f}s"
                )
            self._sleep(self._poll)
