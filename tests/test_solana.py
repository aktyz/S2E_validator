"""The Solana client (#43, M2): the bytes the program will read, and how a send
is judged. Everything here runs against a pretend RPC node; nothing in this
file has touched a real cluster."""

from __future__ import annotations

import base64
import json
import struct

import pytest

pytest.importorskip("solders", reason="the Solana client needs the optional `solana` extra")

from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from s2e_validator.settlement import (  # noqa: E402
    BatchStatus,
    BatchSubmitter,
    InMemoryBatchLog,
    Network,
    Outcome,
    Payable,
    StopReason,
    ValidatorKey,
)
from s2e_validator.settlement import solana as s  # noqa: E402

from fake_node import (  # noqa: E402
    AUTHORITY, MINT, NOW, PROGRAM, PROGRAM_CONFIG, RUN, RUN_ID, STOP, SURVEY, SURVEY_ID, VALIDATOR, VAULT,
    Clock, FakeNode, decode_batch, program_config_account, run_account, survey_account, token_account,
)


def validator_key(keypair: Keypair = VALIDATOR) -> ValidatorKey:
    return ValidatorKey(bytes(keypair), Network.LOCALNET)


def chain(node: FakeNode, clock: Clock | None = None, **kwargs) -> s.SolanaChain:
    clock = clock or Clock()
    return s.SolanaChain(
        node, validator_key(), str(PROGRAM), SURVEY_ID, RUN_ID,
        sleep=clock.sleep, monotonic=clock.monotonic, **kwargs,
    )


def payables(count: int, start: int = 0) -> list[Payable]:
    return [Payable(f"sub-{start + i}", str(Keypair().pubkey()), 50 + i) for i in range(count)]


class TestTheInstruction:
    def test_the_discriminator_is_anchors_for_this_instruction(self):
        # sha256("global:execute_reward_batch")[:8]
        assert s.EXECUTE_REWARD_BATCH.hex() == "782a80bdf09bbd98"

    def test_the_data_is_sequence_then_the_scores_in_order(self):
        entries = payables(3)
        instruction = chain(FakeNode()).instruction(41, entries, MINT, AUTHORITY)

        data = bytes(instruction.data)
        assert data[:8] == s.EXECUTE_REWARD_BATCH
        assert struct.unpack_from("<Q", data, 8) == (41,)
        assert struct.unpack_from("<I", data, 16) == (3,)  # borsh Vec length
        assert struct.unpack_from("<3Q", data, 20) == (50, 51, 52)
        assert len(data) == 20 + 3 * 8

    def test_the_accounts_are_in_the_programs_order(self):
        """The order of `ExecuteRewardBatch` in reward_execution.rs, then one
        (UserSurvey, token account) pair per user."""
        entries = payables(2)
        instruction = chain(FakeNode()).instruction(0, entries, MINT, AUTHORITY)

        metas = [(m.pubkey, m.is_signer, m.is_writable) for m in instruction.accounts]
        assert instruction.program_id == PROGRAM
        assert metas[:7] == [
            (VALIDATOR.pubkey(), True, False),
            (PROGRAM_CONFIG, False, False),
            (SURVEY, False, False),
            (RUN, False, True),
            (VAULT, False, True),
            (s.associated_token_address(AUTHORITY, MINT), False, True),
            (s.TOKEN_PROGRAM, False, False),
        ]
        expected = []
        for entry in entries:
            user = Pubkey.from_string(entry.user)
            user_survey = Pubkey.find_program_address(
                [b"user_survey", bytes(user), bytes(SURVEY)], PROGRAM
            )[0]
            expected += [(user_survey, False, True), (s.associated_token_address(user, MINT), False, True)]
        assert metas[7:] == expected

    def test_the_dust_account_can_be_named_when_it_is_not_the_associated_one(self):
        dust = Keypair().pubkey()
        client = s.SolanaChain(
            FakeNode(), validator_key(), str(PROGRAM), SURVEY_ID, RUN_ID, dust_token_account=str(dust)
        )

        instruction = client.instruction(0, payables(1), MINT, AUTHORITY)

        assert instruction.accounts[5].pubkey == dust
        assert instruction.accounts[5].is_writable

    def test_a_token_account_address_is_the_standard_associated_one(self):
        # USDC's mint and a known wallet give a known associated token account.
        owner = Pubkey.from_string("5AcNQ8yeWA97ATsrwY4SzCx4QhCNjDQkU8T5XkEe3q5J")
        usdc = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

        address = s.associated_token_address(owner, usdc)

        assert address == Pubkey.find_program_address(
            [bytes(owner), bytes(s.TOKEN_PROGRAM), bytes(usdc)], s.ASSOCIATED_TOKEN_PROGRAM
        )[0]
        assert not address.is_on_curve()


class TestPrepare:
    def test_the_ticket_is_a_signed_transaction_that_was_not_sent(self):
        node = FakeNode()
        ticket = chain(node).prepare(0, payables(2))

        transaction = Transaction.from_bytes(ticket.payload)
        assert ticket.sequence == 0
        assert ticket.signature == str(transaction.signatures[0])
        assert ticket.last_valid_block_height == node.height + 150
        assert transaction.message.recent_blockhash == node.blockhash
        assert transaction.message.account_keys[0] == VALIDATOR.pubkey()  # pays the fee
        assert transaction.signatures[0].verify(VALIDATOR.pubkey(), bytes(transaction.message))
        assert "sendTransaction" not in node.calls

    def test_it_asks_for_compute_units_before_the_batch(self):
        transaction = Transaction.from_bytes(chain(FakeNode()).prepare(0, payables(4)).payload)

        first, second = transaction.message.instructions
        keys = transaction.message.account_keys
        assert str(keys[first.program_id_index]) == "ComputeBudget111111111111111111111111111111"
        assert bytes(first.data) == bytes([2]) + struct.pack("<I", 60_000 + 4 * 40_000)
        assert keys[second.program_id_index] == PROGRAM

    def test_the_most_users_that_fit_is_what_the_constant_says(self):
        """The program allows 64 per batch; a 1232-byte transaction does not."""
        client = chain(FakeNode())

        full = client.prepare(0, payables(s.MAX_USERS_PER_TRANSACTION))
        assert len(full.payload) <= s.MAX_TRANSACTION_BYTES
        # One more user is 74 bytes: two addresses, two indexes, one score.
        assert len(full.payload) + 74 > s.MAX_TRANSACTION_BYTES

        with pytest.raises(ValueError, match="1 to 10 users, got 11"):
            client.prepare(0, payables(s.MAX_USERS_PER_TRANSACTION + 1))

    def test_an_empty_batch_is_refused(self):
        with pytest.raises(ValueError, match="got 0"):
            chain(FakeNode()).prepare(0, [])

    def test_a_key_whose_halves_do_not_match_is_refused(self):
        """keys.py cannot check this; the signing library does."""
        forged = bytes(Keypair())[:32] + bytes(Keypair())[32:]

        with pytest.raises(ValueError):
            s.SolanaChain(FakeNode(), ValidatorKey(forged, Network.LOCALNET), str(PROGRAM), SURVEY_ID, RUN_ID)


class TestRunState:
    def test_an_active_run_in_a_rolling_survey(self):
        node = FakeNode()
        node.expected_sequence = 12

        state = chain(node).run_state()

        assert state.expected_sequence == 12
        assert state.stop_timestamp == STOP
        assert state.protocol_active and state.survey_settleable and state.run_active
        assert state.validator_authority == str(VALIDATOR.pubkey())

    @pytest.mark.parametrize("protocol_state, active", [(0, True), (1, True), (2, False)])
    def test_only_a_frozen_protocol_blocks(self, protocol_state, active):
        node = FakeNode()
        node.accounts[str(PROGRAM_CONFIG)] = program_config_account(protocol_state)

        assert chain(node).run_state().protocol_active is active

    @pytest.mark.parametrize(
        "lifecycle, settleable",
        [(0, False), (1, False), (2, False), (3, True), (4, True)],
        ids=["draft", "funded", "closed", "refundable", "rolling"],
    )
    def test_payouts_need_a_rolling_or_refundable_survey(self, lifecycle, settleable):
        node = FakeNode()
        node.accounts[str(SURVEY)] = survey_account(lifecycle)

        assert chain(node).run_state().survey_settleable is settleable

    @pytest.mark.parametrize("lifecycle, active", [(0, False), (1, False), (2, True), (3, False)])
    def test_only_an_active_run_pays(self, lifecycle, active):
        node = FakeNode()
        node.accounts[str(RUN)] = run_account(lifecycle=lifecycle)

        assert chain(node).run_state().run_active is active

    def test_a_closed_run_still_reports_its_sequence(self):
        """So a send that was in doubt when the run closed can still be resolved."""
        node = FakeNode()
        node.accounts[str(RUN)] = run_account(expected_sequence=9, lifecycle=3)

        state = chain(node).run_state()

        assert (state.run_active, state.expected_sequence) == (False, 9)

    def test_a_missing_account_names_what_is_missing(self):
        node = FakeNode()
        node.accounts[str(RUN)] = None

        with pytest.raises(s.ChainStateError, match="no run account"):
            chain(node).run_state()

    def test_an_account_owned_by_another_program_is_refused(self):
        node = FakeNode()
        node.owner = str(Keypair().pubkey())

        with pytest.raises(s.ChainStateError, match="not owned by the program"):
            chain(node).run_state()

    def test_an_account_of_the_wrong_type_is_refused(self):
        node = FakeNode()
        node.accounts[str(RUN)] = survey_account()

        with pytest.raises(s.ChainStateError, match="not a SurveyRunConfigAccount"):
            chain(node).run_state()

    def test_a_run_of_another_survey_is_refused(self):
        node = FakeNode()
        node.accounts[str(RUN)] = run_account(survey=Keypair().pubkey())

        with pytest.raises(s.ChainStateError, match="another survey or run"):
            chain(node).run_state()


class TestBudget:
    """min(reward_cap - emitted_rewards, vault balance): the cap is a counter,
    the vault is what is actually there."""

    @pytest.mark.parametrize(
        "cap, emitted, vault, budget",
        [
            (1_000, 0, 1_000, 1_000),
            (1_000, 400, 1_000, 600),  # the cap is the limit
            (1_000, 400, 250, 250),  # the vault is the limit
            (1_000, 1_000, 500, 0),  # the cap is used up
            (1_000, 0, 0, 0),  # a keeper-created run before it is funded
        ],
    )
    def test_it_is_the_smaller_of_remaining_cap_and_vault(self, cap, emitted, vault, budget):
        node = FakeNode()
        node.accounts[str(RUN)] = run_account(reward_cap=cap, emitted_rewards=emitted)
        node.accounts[str(VAULT)] = token_account(vault)

        assert chain(node).run_state().budget == budget

    def test_a_vault_that_does_not_exist_holds_nothing(self):
        node = FakeNode()
        node.accounts[str(VAULT)] = None

        assert chain(node).run_state().budget == 0

    def test_a_batch_over_the_budget_is_never_signed(self):
        node = FakeNode()
        node.accounts[str(VAULT)] = token_account(100)
        log = InMemoryBatchLog()
        sender = BatchSubmitter(
            chain(node), log, str(VALIDATOR.pubkey()), clock=lambda: NOW,
            max_batch_size=s.MAX_USERS_PER_TRANSACTION,
        )

        report = sender.settle(payables(3))  # 50 + 51 + 52 = 153

        assert report.stopped is StopReason.OVER_BUDGET
        assert node.sent == [] and "getLatestBlockhash" not in node.calls


class TestBroadcast:
    def test_a_confirmed_transaction_landed(self):
        node = FakeNode()
        client = chain(node)

        result = client.broadcast(client.prepare(0, payables(2)))

        assert result.outcome is Outcome.LANDED
        assert node.landed == [(0, (50, 51))]

    def test_a_transaction_the_program_refused_is_failed_with_the_reason(self):
        node = FakeNode()
        node.on_send = ["fail"]
        client = chain(node)

        result = client.broadcast(client.prepare(0, payables(1)))

        assert result.outcome is Outcome.FAILED
        assert result.reason == "InvalidBatchSequence (program error 11000)"

    def test_a_program_error_in_preflight_is_final(self):
        node = FakeNode()
        node.on_send = ["preflight:program"]
        client = chain(node)

        result = client.broadcast(client.prepare(0, payables(1)))

        assert result.outcome is Outcome.FAILED
        assert result.reason == "InsufficientTreasuryBalance (program error 11002)"

    def test_a_wrong_sequence_in_preflight_is_not_final(self):
        """A node behind the chain says this by itself; the submitter resolves
        it by reading the sequence once the transaction has expired."""
        node = FakeNode()
        node.on_send = ["preflight:sequence"]
        client = chain(node)

        with pytest.raises(s.RpcError):
            client.broadcast(client.prepare(0, payables(1)))

    def test_any_other_preflight_error_proves_nothing(self):
        """A node that is behind must not get a batch marked as failed."""
        node = FakeNode()
        node.on_send = ["preflight:blockhash"]
        client = chain(node)

        with pytest.raises(s.RpcError):
            client.broadcast(client.prepare(0, payables(1)))

    @pytest.mark.parametrize("behaviour", ["drop", "processed_only"])
    def test_no_confirmation_in_time_is_unknown_not_failed(self, behaviour):
        """`processed` can still be on a fork that loses, so it is not an answer."""
        clock = Clock()
        node = FakeNode(clock)
        node.on_send = [behaviour]
        client = chain(node, clock, confirm_timeout=10.0)

        result = client.broadcast(client.prepare(0, payables(1)))

        assert result.outcome is Outcome.UNKNOWN
        assert clock.now >= 10.0

    def test_waiting_for_expiry_returns_once_the_height_has_passed(self):
        clock = Clock()
        node = FakeNode(clock)
        client = chain(node, clock)
        ticket = client.prepare(0, payables(1))

        client.wait_until_expired(ticket)

        assert node.height > ticket.last_valid_block_height

    def test_waiting_for_expiry_gives_up_loudly_on_a_stalled_chain(self):
        clock = Clock()
        node = FakeNode()  # the height never moves
        client = chain(node, clock, expiry_timeout=30.0)
        ticket = client.prepare(0, payables(1))

        with pytest.raises(TimeoutError, match="did not pass block height"):
            client.wait_until_expired(ticket)

    def test_only_confirmed_or_finalized_is_accepted_as_commitment(self):
        with pytest.raises(ValueError, match="confirmed or finalized"):
            chain(FakeNode(), commitment="processed")


class TestWithTheSubmitter:
    def submitter(self, client: s.SolanaChain) -> tuple[BatchSubmitter, InMemoryBatchLog]:
        log = InMemoryBatchLog()
        return (
            BatchSubmitter(
                client, log, str(VALIDATOR.pubkey()),
                clock=lambda: NOW, max_batch_size=s.MAX_USERS_PER_TRANSACTION,
            ),
            log,
        )

    def test_many_payables_go_out_in_transactions_that_fit(self):
        node = FakeNode()
        submitter, log = self.submitter(chain(node))

        report = submitter.settle(payables(25))

        assert report.stopped is StopReason.DONE
        assert [sequence for sequence, _ in node.landed] == [0, 1, 2]
        assert [len(scores) for _, scores in node.landed] == [10, 10, 5]
        assert all(len(bytes(t)) <= s.MAX_TRANSACTION_BYTES for t in node.sent)
        assert {r.status for r in log.records()} == {BatchStatus.CONFIRMED}

    def test_a_dropped_send_is_resent_under_the_same_sequence_and_lands_once(self):
        clock = Clock()
        node = FakeNode(clock)
        node.on_send = ["drop", "land"]
        submitter, _ = self.submitter(chain(node, clock, confirm_timeout=10.0))

        report = submitter.settle(payables(3))

        assert report.stopped is StopReason.DONE
        assert [decode_batch(t)[0] for t in node.sent] == [0, 0]
        assert node.landed == [(0, (50, 51, 52))]

    def test_a_preflight_blip_is_retried_not_failed(self):
        clock = Clock()
        node = FakeNode(clock)
        node.on_send = ["preflight:blockhash", "land"]
        submitter, log = self.submitter(chain(node, clock))

        submitter.settle(payables(1))

        assert [r.status for r in log.records()] == [BatchStatus.CONFIRMED]
        assert len(node.landed) == 1

    def test_another_validators_key_stops_before_anything_is_signed(self):
        node = FakeNode()
        node.accounts[str(RUN)] = run_account(validator=Keypair().pubkey())
        submitter, _ = self.submitter(chain(node))

        report = submitter.settle(payables(1))

        assert report.stopped is StopReason.WRONG_SIGNER
        assert node.sent == []


class TestTheServiceOnSolana:
    """The entry point, wired to the client (needs the `api` extra too)."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        self.node = FakeNode()
        monkeypatch.setattr(s, "JsonRpc", lambda url: self.node)
        keypair = tmp_path / "validator.json"
        keypair.write_text(json.dumps(list(bytes(VALIDATOR))))
        keypair.chmod(0o600)
        config = tmp_path / "campaign.json"
        config.write_text(json.dumps({"default_mode": "enforce", "filters": {"face_detection": "off"}}))
        return {
            "S2E_CHAIN": "solana",
            "S2E_PIPELINE_CONFIG": str(config),
            "S2E_BATCH_LOG": str(tmp_path / "batches.sqlite"),
            "S2E_API_KEY": "k" * 40,
            "S2E_MAX_UNITS_PER_SUBMISSION": "1000",
            "S2E_RPC_URL": "http://localhost:8899",
            "S2E_PROGRAM_ID": str(PROGRAM),
            "S2E_SURVEY_ID": str(SURVEY_ID),
            "S2E_RUN_ID": str(RUN_ID),
            "S2E_VALIDATOR_KEYPAIR": str(keypair),
            "S2E_NETWORK": "localnet",
            "S2E_VALIDATOR_PUBKEY": str(VALIDATOR.pubkey()),
        }

    def test_it_pays_through_the_client_in_transactions_that_fit(self, env):
        from s2e_validator.api.main import ServiceSettings, build_service
        from helpers import CASTLE, big_photo

        service, network = build_service(ServiceSettings.from_env(env), env)
        outcome = service.submit(str(Keypair().pubkey()), CASTLE, big_photo())

        assert network == "localnet"
        assert service._submitter._max_batch_size == s.MAX_USERS_PER_TRANSACTION
        assert outcome.state.value == "paid"
        assert self.node.landed == [(0, (75,))]
        assert outcome.transaction == str(self.node.sent[0].signatures[0])

    def test_it_refuses_to_start_for_a_run_another_key_pays(self, env):
        from s2e_validator.api.main import ServiceSettings, SettingsError, build_service

        self.node.accounts[str(RUN)] = run_account(validator=Keypair().pubkey())

        with pytest.raises(SettingsError, match="not by this service's key"):
            build_service(ServiceSettings.from_env(env), env)

    def test_it_refuses_to_start_when_one_payout_is_more_than_the_run_can_pay(self, env):
        """Otherwise every reward would wait as batched, with no error (#87)."""
        from s2e_validator.api.main import ServiceSettings, SettingsError, build_service

        self.node.accounts[str(VAULT)] = token_account(999)  # the setting asks for up to 1000

        with pytest.raises(SettingsError, match="can only pay 999 more base units"):
            build_service(ServiceSettings.from_env(env), env)

    def test_it_starts_when_the_maximum_is_exactly_what_the_run_can_pay(self, env):
        from s2e_validator.api.main import ServiceSettings, build_service

        self.node.accounts[str(VAULT)] = token_account(1000)

        build_service(ServiceSettings.from_env(env), env)

    def test_it_refuses_to_start_for_a_run_that_does_not_exist(self, env):
        from s2e_validator.api.main import ServiceSettings, build_service

        self.node.accounts[str(RUN)] = None

        with pytest.raises(s.ChainStateError, match="no run account"):
            build_service(ServiceSettings.from_env(env), env)

    def test_ids_must_be_numbers(self, env):
        from s2e_validator.api.main import ServiceSettings, SettingsError, build_service

        env["S2E_RUN_ID"] = "first"

        with pytest.raises(SettingsError, match="must be whole numbers"):
            build_service(ServiceSettings.from_env(env), env)
