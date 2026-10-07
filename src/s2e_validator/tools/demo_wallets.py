"""Set up the demo's contributor wallets on-chain.

    python -m s2e_validator.tools.demo_wallets --count 5 \\
        --out-dir ~/.config/s2e/demo-wallets --funder ~/.config/s2e/devnet-funder.json \\
        --rpc-url https://api.devnet.solana.com --program-id <id> --survey-id 0 --run-id 0

The program pays a contributor only if they hold a UserSurvey account for the
survey and a token account for the reward mint, and the UserSurvey account must
be created and signed for by the contributor. Demo visitors have no wallet, so
this makes a few wallets, registers each one, and prints their addresses.
The demo site (#70) registers its own visitors; this is for running the
validator without the site, e.g. for a recording: submit with one of these
addresses as `wallet`.

Safe to run again: a wallet file that exists is reused, and a wallet that is
already registered is skipped. A new survey needs the same wallets registered
again, which is why their keys are kept (private to the owner, outside every
repository). They only ever receive demo rewards. **Devnet and demo only.**

Needs the `solana` extra. The funder pays the fees and each account's rent, so
it needs a little SOL; it is not the validator's signing key and should not be.
"""

from __future__ import annotations

import argparse
import base64
import sys
from collections.abc import Sequence
from pathlib import Path

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from ..settlement import create_keypair_file, enclosing_git_tree, read_keypair_file
from ..settlement.solana import (
    ASSOCIATED_TOKEN_PROGRAM,
    TOKEN_PROGRAM,
    JsonRpc,
    anchor_discriminator,
    associated_token_address,
    decode_run,
    survey_config_address,
    user_survey_address,
)

INITIALIZE_USER_SURVEY_ACCOUNT = anchor_discriminator("global", "initialize_user_survey_account")
#: `UserSurveyAccount::SPACE` in state/user_survey.rs.
USER_SURVEY_SPACE = 89
#: "Create if it does not exist yet" in the associated token account program.
_CREATE_IDEMPOTENT = bytes([1])
_MAX_WALLETS = 64


def registration_instructions(
    program_id: Pubkey, funder: Pubkey, user: Pubkey, survey: Pubkey, mint: Pubkey, rent: int
) -> list[Instruction]:
    """Everything one wallet needs before it can be paid, as one transaction."""
    token_account = associated_token_address(user, mint)
    return [
        # The contributor pays the rent of their own UserSurvey account
        # (`payer = user` in the program), so they are handed exactly that.
        transfer(TransferParams(from_pubkey=funder, to_pubkey=user, lamports=rent)),
        Instruction(
            ASSOCIATED_TOKEN_PROGRAM,
            _CREATE_IDEMPOTENT,
            [
                AccountMeta(funder, is_signer=True, is_writable=True),
                AccountMeta(token_account, is_signer=False, is_writable=True),
                AccountMeta(user, is_signer=False, is_writable=False),
                AccountMeta(mint, is_signer=False, is_writable=False),
                AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
            ],
        ),
        Instruction(
            program_id,
            INITIALIZE_USER_SURVEY_ACCOUNT,
            [
                AccountMeta(user, is_signer=True, is_writable=True),
                AccountMeta(survey, is_signer=False, is_writable=False),
                AccountMeta(user_survey_address(program_id, user, survey), is_signer=False, is_writable=True),
                AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            ],
        ),
    ]


def load_or_create_wallet(path: Path) -> Keypair:
    """The wallet in `path`, made first if the file is not there."""
    if not path.exists():
        create_keypair_file(path, bytes(Keypair()))
    return Keypair.from_bytes(read_keypair_file(path))


def wallet_directory(out_dir: Path) -> Path:
    directory = out_dir.expanduser().resolve()
    repository = enclosing_git_tree(directory)
    if repository is not None:
        raise SystemExit(f"{directory} is inside the git working tree at {repository}. Keep keys outside every repository.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def register(
    rpc: JsonRpc, program_id: Pubkey, survey_id: int, run_id: int, funder: Keypair, wallets: Sequence[Keypair]
) -> list[str]:
    """Register each wallet that is not registered yet. Returns the signatures sent."""
    survey = survey_config_address(program_id, survey_id)
    run_address = Pubkey.find_program_address(
        [b"survey_run", bytes(survey), run_id.to_bytes(8, "little")], program_id
    )[0]
    options = {"encoding": "base64", "commitment": "confirmed"}
    run = rpc.call("getAccountInfo", [str(run_address), options])["value"]
    if run is None:
        raise SystemExit(f"no run account at {run_address}. Wrong program, survey or run id?")
    mint = decode_run(base64.b64decode(run["data"][0])).reward_mint
    rent = rpc.call("getMinimumBalanceForRentExemption", [USER_SURVEY_SPACE])

    signatures = []
    for wallet in wallets:
        user = wallet.pubkey()
        existing = rpc.call("getAccountInfo", [str(user_survey_address(program_id, user, survey)), options])
        if existing["value"] is not None:
            print(f"{user} already registered")
            continue
        latest = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
        blockhash = Hash.from_string(latest["blockhash"])
        message = Message.new_with_blockhash(
            registration_instructions(program_id, funder.pubkey(), user, survey, mint, rent),
            funder.pubkey(),
            blockhash,
        )
        transaction = Transaction([funder, wallet], message, blockhash)
        signature = rpc.call(
            "sendTransaction",
            [base64.b64encode(bytes(transaction)).decode("ascii"), {"encoding": "base64", "preflightCommitment": "confirmed"}],
        )
        print(f"{user} registration sent: {signature}")
        signatures.append(signature)
    return signatures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and register the demo's contributor wallets.")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, required=True, help="where the wallet key files are kept")
    parser.add_argument("--funder", type=Path, required=True, help="keypair file that pays fees and rent")
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--program-id", required=True)
    parser.add_argument("--survey-id", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.count <= _MAX_WALLETS:
        parser.error(f"--count must be 1 to {_MAX_WALLETS}")

    directory = wallet_directory(args.out_dir)
    wallets = [load_or_create_wallet(directory / f"demo-wallet-{n}.json") for n in range(args.count)]
    funder = Keypair.from_bytes(read_keypair_file(args.funder.expanduser()))
    register(
        JsonRpc(args.rpc_url), Pubkey.from_string(args.program_id), args.survey_id, args.run_id, funder, wallets
    )
    print("\nRegistered wallets (use one as `wallet` in a submission):")
    for wallet in wallets:
        print(wallet.pubkey())
    return 0


if __name__ == "__main__":
    sys.exit(main())
