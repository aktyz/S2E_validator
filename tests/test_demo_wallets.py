"""Registering the demo's contributor wallets. Like test_solana.py, this runs
against a pretend node only."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

import pytest

pytest.importorskip("solders", reason="the wallet tool needs the optional `solana` extra")

from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.system_program import ID as SYSTEM_PROGRAM  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from s2e_validator.settlement import solana as s  # noqa: E402
from s2e_validator.tools import demo_wallets as tool  # noqa: E402

from fake_node import MINT, PROGRAM, RUN, RUN_ID, SURVEY, SURVEY_ID, run_account  # noqa: E402

RENT = 1_510_320


class Node:
    def __init__(self, registered: set[str] = frozenset()):
        self.registered = set(registered)
        self.sent: list[Transaction] = []

    def call(self, method, params=()):
        if method == "getAccountInfo":
            if params[0] == str(RUN):
                return {"value": {"data": [base64.b64encode(run_account()).decode(), "base64"]}}
            return {"value": {"data": ["", "base64"]} if params[0] in self.registered else None}
        if method == "getMinimumBalanceForRentExemption":
            assert params == [89]
            return RENT
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Keypair().pubkey()), "lastValidBlockHeight": 1}}
        if method == "sendTransaction":
            self.sent.append(Transaction.from_bytes(base64.b64decode(params[0])))
            return str(self.sent[-1].signatures[0])
        raise AssertionError(method)


def user_survey(user: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"user_survey", bytes(user), bytes(SURVEY)], PROGRAM)[0]


class TestTheRegistrationTransaction:
    def test_it_funds_the_rent_opens_a_token_account_and_joins_the_survey(self):
        funder, user = Keypair().pubkey(), Keypair().pubkey()

        fund, token, join = tool.registration_instructions(PROGRAM, funder, user, SURVEY, MINT, RENT)

        assert fund.program_id == SYSTEM_PROGRAM
        assert bytes(fund.data) == struct.pack("<IQ", 2, RENT)  # system transfer
        assert [m.pubkey for m in fund.accounts] == [funder, user]

        assert token.program_id == s.ASSOCIATED_TOKEN_PROGRAM
        assert bytes(token.data) == bytes([1])  # create, unless it exists
        assert [(m.pubkey, m.is_signer, m.is_writable) for m in token.accounts] == [
            (funder, True, True),
            (s.associated_token_address(user, MINT), False, True),
            (user, False, False),
            (MINT, False, False),
            (SYSTEM_PROGRAM, False, False),
            (s.TOKEN_PROGRAM, False, False),
        ]

        # `InitializeUserSurveyAccount` in user_survey_account.rs.
        assert join.program_id == PROGRAM
        assert bytes(join.data) == hashlib.sha256(b"global:initialize_user_survey_account").digest()[:8]
        assert [(m.pubkey, m.is_signer, m.is_writable) for m in join.accounts] == [
            (user, True, True),
            (SURVEY, False, False),
            (user_survey(user), False, True),
            (SYSTEM_PROGRAM, False, False),
        ]

    def test_the_funder_and_the_wallet_both_sign(self):
        node, funder, wallet = Node(), Keypair(), Keypair()

        signatures = tool.register(node, PROGRAM, SURVEY_ID, RUN_ID, funder, [wallet])

        [transaction] = node.sent
        message = bytes(transaction.message)
        assert signatures == [str(transaction.signatures[0])]
        assert transaction.message.account_keys[:2] == [funder.pubkey(), wallet.pubkey()]
        assert transaction.signatures[0].verify(funder.pubkey(), message)
        assert transaction.signatures[1].verify(wallet.pubkey(), message)

    def test_a_wallet_that_is_registered_is_left_alone(self):
        old, new = Keypair(), Keypair()
        node = Node(registered={str(user_survey(old.pubkey()))})

        tool.register(node, PROGRAM, SURVEY_ID, RUN_ID, Keypair(), [old, new])

        [transaction] = node.sent
        assert transaction.message.account_keys[1] == new.pubkey()


class TestWalletFiles:
    def test_a_wallet_is_made_once_and_reused(self, tmp_path):
        path = tmp_path / "demo-wallet-0.json"

        first = tool.load_or_create_wallet(path)
        again = tool.load_or_create_wallet(path)

        assert first.pubkey() == again.pubkey()
        assert bytes(json.loads(path.read_text())) == bytes(first)

    @pytest.mark.skipif(os.name == "nt", reason="file modes are not meaningful on Windows")
    def test_the_file_is_private_to_its_owner(self, tmp_path):
        path = tmp_path / "demo-wallet-0.json"
        tool.load_or_create_wallet(path)

        assert path.stat().st_mode & 0o777 == 0o600

    def test_keys_are_never_written_inside_a_repository(self, tmp_path):
        (tmp_path / ".git").mkdir()

        with pytest.raises(SystemExit, match="inside the git working tree"):
            tool.wallet_directory(tmp_path / "wallets")

    @pytest.mark.parametrize("count", ["0", "65"])
    def test_the_count_is_bounded(self, tmp_path, count, capsys):
        with pytest.raises(SystemExit):
            tool.main([
                "--count", count, "--out-dir", str(tmp_path), "--funder", "f.json",
                "--rpc-url", "http://localhost:8899", "--program-id", str(PROGRAM),
                "--survey-id", "0", "--run-id", "0",
            ])

        assert "--count must be 1 to 64" in capsys.readouterr().err
