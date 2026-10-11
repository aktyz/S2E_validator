"""Reward batches (#43, M2): planning, sending, and never paying twice."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from s2e_validator.settlement.batches import MAX_BATCH_SIZE, Batch, Payable, plan_batches
from s2e_validator.settlement.log import BatchStatus, InMemoryBatchLog
from s2e_validator.settlement.submitter import (
    BatchSubmitter,
    SequenceConflict,
    StopReason,
)

from fake_chain import BEHAVIOURS, NOW, SIGNER, FakeChain, SimulatedCrash, wallet


def payables(count: int, users: int | None = None, start: int = 0) -> list[Payable]:
    """`count` payables. By default every one from a different contributor;
    with `users`, contributors repeat, cycling through that many wallets."""
    return [
        Payable(
            f"sub-{start + i}",
            wallet(start + i if users is None else (start + i) % users),
            quality=50 + i % 50,
        )
        for i in range(count)
    ]


def submitter(chain: FakeChain, log: InMemoryBatchLog | None = None, **kwargs) -> BatchSubmitter:
    return BatchSubmitter(
        chain, log if log is not None else InMemoryBatchLog(), SIGNER, clock=lambda: NOW, **kwargs
    )


def assert_paid_exactly_once(chain: FakeChain, log: InMemoryBatchLog) -> None:
    """The one property that matters: confirmed means paid once, and nothing
    else is paid at all."""
    confirmed = {
        s
        for record in log.records()
        if record.status is BatchStatus.CONFIRMED
        for s in record.batch.submission_ids
    }
    assert max(chain.paid.values(), default=0) <= 1, "someone was paid twice"
    assert set(chain.paid) == confirmed


class TestPayable:
    def test_a_valid_payable(self):
        Payable("sub-1", wallet(1), 0)

    @pytest.mark.parametrize(
        "fields, message",
        [
            ({"submission_id": ""}, "submission_id"),
            ({"user": "not-a-wallet"}, "Solana address"),
            ({"user": "abc"}, "Solana address"),
            ({"quality": -1}, "u64"),
            ({"quality": 2**64}, "u64"),
            ({"quality": 7.5}, "whole number"),
            ({"quality": True}, "whole number"),
        ],
    )
    def test_invalid_payables_are_refused(self, fields, message):
        values = {"submission_id": "sub-1", "user": wallet(1), "quality": 10, **fields}
        with pytest.raises(ValueError, match=message):
            Payable(**values)


class TestPlanning:
    def test_batches_hold_at_most_ten(self):
        """What one transaction carries (#82)."""
        assert MAX_BATCH_SIZE == 10

        batches = plan_batches(payables(25))

        assert [len(b.entries) for b in batches] == [10, 10, 5]

    def test_each_user_appears_once_per_batch(self):
        """Ten submissions from one prolific contributor spread over ten
        batches; everyone else still fits in the first one beside them."""
        heavy = [Payable(f"heavy-{i}", wallet(0), 60) for i in range(10)]
        batches = plan_batches(heavy + payables(9, start=1))

        assert len(batches) == 10
        for batch in batches:
            users = [e.user for e in batch.entries]
            assert len(users) == len(set(users))
        assert len(batches[0].entries) == 10

    def test_order_is_first_come_first_served(self):
        batches = plan_batches(payables(130))

        flat = [e.submission_id for b in batches for e in b.entries]
        assert flat == [f"sub-{i}" for i in range(130)]

    def test_deterministic(self):
        items = payables(200, users=37)

        assert [b.batch_id for b in plan_batches(items)] == [b.batch_id for b in plan_batches(items)]

    def test_a_submission_listed_twice_is_refused(self):
        item = Payable("sub-1", wallet(1), 10)

        with pytest.raises(ValueError, match="appears twice"):
            plan_batches([item, item])

    def test_batch_rules_hold_even_when_built_by_hand(self):
        with pytest.raises(ValueError, match="at most once"):
            Batch((Payable("a", wallet(1), 1), Payable("b", wallet(1), 1)))
        with pytest.raises(ValueError, match="1 to 10"):
            Batch(tuple(payables(MAX_BATCH_SIZE + 1)))
        with pytest.raises(ValueError, match="1 to 10"):
            Batch(())


class TestHappyPath:
    def test_every_batch_lands_in_sequence(self):
        chain = FakeChain()
        log = InMemoryBatchLog()

        report = submitter(chain, log).settle(payables(25))

        assert report.stopped is StopReason.DONE
        assert [seq for seq, _ in chain.landed] == [0, 1, 2]
        assert len(report.paid_submission_ids) == 25
        assert report.waiting == ()
        assert_paid_exactly_once(chain, log)

    def test_settling_the_same_payables_again_sends_nothing(self):
        chain = FakeChain()
        log = InMemoryBatchLog()
        items = payables(15)
        submitter(chain, log).settle(items)

        again = submitter(chain, log).settle(items)

        assert chain.broadcasts == 2
        assert again.confirmed == ()
        assert_paid_exactly_once(chain, log)

    def test_new_payables_join_later_batches(self):
        chain = FakeChain()
        log = InMemoryBatchLog()
        submitter(chain, log).settle(payables(10))

        submitter(chain, log).settle(payables(10) + payables(5, start=10))

        assert [seq for seq, _ in chain.landed] == [0, 1]
        assert_paid_exactly_once(chain, log)


class TestWhenSendingGoesWrong:
    """Each case is one way a real send fails. The answer is always the same:
    resolve it on-chain, never guess."""

    def run(self, *script):
        chain = FakeChain(script=list(script))
        log = InMemoryBatchLog()
        report = submitter(chain, log).settle(payables(10))
        assert_paid_exactly_once(chain, log)
        return chain, log, report

    def test_a_dropped_send_is_resent_under_the_same_sequence(self):
        chain, log, report = self.run("drop", "land")

        assert report.stopped is StopReason.DONE
        assert chain.landed == [(0, tuple(f"sub-{i}" for i in range(10)))]
        assert log.records()[0].attempts == 2

    def test_a_silent_landing_is_found_and_not_resent(self):
        chain, _, report = self.run("land_silently")

        assert len(report.confirmed) == 1
        assert chain.broadcasts == 1

    def test_a_late_landing_is_found_and_not_resent(self):
        chain, _, report = self.run("late_land")

        assert len(report.confirmed) == 1
        assert chain.broadcasts == 1

    def test_a_transport_error_before_sending_is_retried(self):
        chain, _, report = self.run("raise", "land")

        assert len(report.confirmed) == 1
        assert chain.broadcasts == 2

    def test_a_transport_error_after_landing_is_not_retried(self):
        chain, _, report = self.run("raise_landed")

        assert len(report.confirmed) == 1
        assert chain.broadcasts == 1

    def test_a_rejected_batch_fails_and_frees_its_sequence(self):
        chain = FakeChain(script=["fail", "land"])
        log = InMemoryBatchLog()

        report = submitter(chain, log).settle(payables(15))

        assert [r.status for r in log.records()] == [BatchStatus.FAILED, BatchStatus.CONFIRMED]
        assert log.records()[0].failure == "simulated program error"
        assert chain.landed[0][0] == 0  # the second batch took the freed sequence
        assert len(report.failed) == 1
        assert_paid_exactly_once(chain, log)

    def test_a_failed_batch_is_not_resent_until_a_person_requeues_it(self):
        chain = FakeChain(script=["fail"])
        log = InMemoryBatchLog()
        items = payables(10)
        submitter(chain, log).settle(items)
        submitter(chain, log).settle(items)
        assert chain.broadcasts == 1

        log.requeue(log.records()[0].batch_id)
        submitter(chain, log).settle(items)

        assert log.records()[0].status is BatchStatus.CONFIRMED
        assert_paid_exactly_once(chain, log)

    def test_repeated_drops_stop_and_resume_later(self):
        chain = FakeChain(script=["drop", "drop", "drop"])
        log = InMemoryBatchLog()

        first = submitter(chain, log).settle(payables(10))

        assert first.stopped is StopReason.UNRESOLVED
        assert len(first.waiting) == 1

        second = submitter(chain, log).settle([])

        assert second.stopped is StopReason.DONE
        assert len(second.confirmed) == 1
        assert_paid_exactly_once(chain, log)


class TestCrashes:
    def test_a_crash_after_landing_is_resolved_without_resending(self):
        chain = FakeChain(script=["crash_landed"])
        log = InMemoryBatchLog()
        with pytest.raises(SimulatedCrash):
            submitter(chain, log).settle(payables(10))
        assert log.records()[0].status is BatchStatus.SENDING

        report = submitter(chain, log).settle([])

        assert len(report.confirmed) == 1
        assert chain.broadcasts == 1
        assert_paid_exactly_once(chain, log)

    def test_a_crash_before_sending_is_resent_once_it_is_safe(self):
        chain = FakeChain(script=["crash"])
        log = InMemoryBatchLog()
        with pytest.raises(SimulatedCrash):
            submitter(chain, log).settle(payables(10))

        report = submitter(chain, log).settle([])

        assert len(report.confirmed) == 1
        assert chain.broadcasts == 2
        assert [seq for seq, _ in chain.landed] == [0]
        assert_paid_exactly_once(chain, log)

    def test_an_unresolved_send_is_settled_before_anything_new_is_signed(self):
        """New payables arrive after a crash. The old send has to be resolved
        first, or the new batch could take its sequence number."""
        chain = FakeChain(script=["crash_landed"])
        log = InMemoryBatchLog()
        with pytest.raises(SimulatedCrash):
            submitter(chain, log).settle(payables(10))

        submitter(chain, log).settle(payables(10, start=10))

        assert [seq for seq, _ in chain.landed] == [0, 1]
        assert_paid_exactly_once(chain, log)


class TestWhenNotToSend:
    @pytest.mark.parametrize(
        "change, reason",
        [
            ({"protocol_active": False}, StopReason.PROTOCOL_FROZEN),
            ({"survey_settleable": False}, StopReason.SURVEY_NOT_SETTLEABLE),
            ({"run_active": False}, StopReason.RUN_NOT_ACTIVE),
            ({"validator_authority": wallet(999)}, StopReason.WRONG_SIGNER),
            ({"stop_timestamp": NOW + timedelta(minutes=4)}, StopReason.TOO_CLOSE_TO_STOP),
        ],
    )
    def test_nothing_is_sent_and_nothing_is_lost(self, change, reason):
        chain = FakeChain(**change)
        log = InMemoryBatchLog()

        report = submitter(chain, log).settle(payables(10))

        assert report.stopped is reason
        assert chain.broadcasts == 0
        assert len(report.waiting) == 1
        assert report.detail

    def test_the_margin_is_measured_from_the_stop_time(self):
        chain = FakeChain(stop_timestamp=NOW + timedelta(minutes=6))

        assert submitter(chain).settle(payables(3)).stopped is StopReason.DONE

    def test_an_unresolved_send_is_still_resolved_while_frozen(self):
        """Reading the chain is always allowed; only sending stops."""
        chain = FakeChain(script=["crash_landed"])
        log = InMemoryBatchLog()
        with pytest.raises(SimulatedCrash):
            submitter(chain, log).settle(payables(10))
        chain.protocol_active = False

        report = submitter(chain, log).settle([])

        assert len(report.confirmed) == 1
        assert_paid_exactly_once(chain, log)

    def test_the_margin_must_outlast_a_transaction(self):
        with pytest.raises(ValueError, match="2 minutes"):
            submitter(FakeChain(), safety_margin=timedelta(seconds=30))


class TestSomethingElseIsSigning:
    def test_a_sequence_that_jumps_stops_everything(self):
        chain = FakeChain(script=["drop"])
        log = InMemoryBatchLog()
        original_wait = chain.wait_until_expired

        def another_signer_acts(ticket):
            chain.expected_sequence += 3
            original_wait(ticket)

        chain.wait_until_expired = another_signer_acts

        with pytest.raises(SequenceConflict, match="Something else is signing"):
            submitter(chain, log).settle(payables(10))


@pytest.mark.parametrize("seed", range(150))
def test_nobody_is_paid_twice_whatever_goes_wrong(seed):
    """Random failures, crashes and restarts, over several settle calls. At the
    end every confirmed submission was paid exactly once and nothing else was
    paid at all."""
    rng = random.Random(seed)
    chain = FakeChain(script=[rng.choice(BEHAVIOURS) for _ in range(60)])
    log = InMemoryBatchLog()
    items = payables(rng.randint(1, 300), users=rng.randint(1, 80))

    for _ in range(40):
        try:
            report = submitter(chain, log, max_attempts=2).settle(items)
        except SimulatedCrash:
            continue
        if report.stopped is StopReason.DONE and not report.waiting:
            break

    assert_paid_exactly_once(chain, log)
    assert not log.unfinished()
    sequences = [seq for seq, _ in chain.landed]
    assert sequences == list(range(len(sequences)))


class TestTheRunsBudget:
    """A batch above the remaining cap is not refused by the program: it is
    scaled down and takes the whole cap. So it must never be sent."""

    def test_a_batch_the_run_cannot_pay_in_full_waits_unsent(self):
        chain = FakeChain(budget=100)
        log = InMemoryBatchLog()

        report = submitter(chain, log).settle([Payable("sub-1", wallet(1), 60), Payable("sub-2", wallet(2), 60)])

        assert report.stopped is StopReason.OVER_BUDGET
        assert "totals 120 base units; the run can pay 100" in report.detail
        assert chain.broadcasts == 0
        assert [r.status for r in log.records()] == [BatchStatus.PLANNED]

    def test_it_is_sent_unchanged_once_the_budget_allows(self):
        chain = FakeChain(budget=100)
        log = InMemoryBatchLog()
        sender = submitter(chain, log)
        sender.settle([Payable("sub-1", wallet(1), 60), Payable("sub-2", wallet(2), 60)])

        chain.budget = 120
        report = sender.settle([])

        assert report.stopped is StopReason.DONE
        assert chain.landed == [(0, ("sub-1", "sub-2"))]
        assert_paid_exactly_once(chain, log)

    def test_a_batch_exactly_at_the_budget_is_sent(self):
        chain = FakeChain(budget=120)

        report = submitter(chain).settle([Payable("sub-1", wallet(1), 60), Payable("sub-2", wallet(2), 60)])

        assert report.stopped is StopReason.DONE

    def test_an_empty_vault_sends_nothing(self):
        chain = FakeChain(budget=0)

        report = submitter(chain).settle(payables(1))

        assert report.stopped is StopReason.OVER_BUDGET
        assert chain.broadcasts == 0

    def test_a_client_that_cannot_tell_is_not_checked(self):
        assert submitter(FakeChain(budget=None)).settle(payables(3)).stopped is StopReason.DONE


class TestRecordingAndSendingAreSeparate:
    def test_enqueue_records_without_sending(self):
        chain = FakeChain()
        log = InMemoryBatchLog()

        submitter(chain, log).enqueue(payables(3))

        assert [r.status for r in log.records()] == [BatchStatus.PLANNED]
        assert chain.broadcasts == 0

    def test_what_was_enqueued_is_sent_by_the_next_settle(self):
        chain = FakeChain()
        log = InMemoryBatchLog()
        sender = submitter(chain, log)
        sender.enqueue(payables(3))

        assert sender.settle([]).stopped is StopReason.DONE
        assert_paid_exactly_once(chain, log)

    def test_enqueueing_twice_plans_once(self):
        log = InMemoryBatchLog()
        sender = submitter(FakeChain(), log)

        sender.enqueue(payables(3))
        sender.enqueue(payables(3))

        assert len(log.records()) == 1


class TestABatchOverBudgetDoesNotBlockTheRest:
    def test_a_smaller_batch_behind_it_is_still_paid(self):
        chain = FakeChain(budget=100)
        log = InMemoryBatchLog()
        sender = submitter(chain, log, max_batch_size=1)

        report = sender.settle([Payable("big", wallet(1), 150), Payable("small", wallet(2), 40)])

        assert report.stopped is StopReason.OVER_BUDGET  # something is still waiting
        assert chain.landed == [(0, ("small",))]
        assert [r.batch.submission_ids for r in log.unfinished()] == [("big",)]

    def test_the_waiting_batch_goes_out_once_it_fits(self):
        chain = FakeChain(budget=100)
        log = InMemoryBatchLog()
        sender = submitter(chain, log, max_batch_size=1)
        sender.settle([Payable("big", wallet(1), 150), Payable("small", wallet(2), 40)])

        chain.budget = 150
        assert sender.settle([]).stopped is StopReason.DONE

        assert chain.landed == [(0, ("small",)), (1, ("big",))]
        assert_paid_exactly_once(chain, log)


def test_a_record_is_found_by_any_of_its_submissions():
    log = InMemoryBatchLog()
    submitter(FakeChain(), log).settle(payables(3))

    assert log.record_for("sub-1").batch.submission_ids == ("sub-0", "sub-1", "sub-2")
    assert log.record_for("never-seen") is None
