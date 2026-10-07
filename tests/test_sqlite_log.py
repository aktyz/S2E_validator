"""The durable batch log (#43, M4): the submitter's guarantees across restarts."""

from __future__ import annotations

import random
import sqlite3

import pytest

from s2e_validator.settlement import (
    BatchStatus,
    BatchSubmitter,
    LockBusy,
    Payable,
    SqliteBatchLog,
    StopReason,
    Ticket,
    plan_batches,
)
from s2e_validator.settlement.log import BatchRecord

from fake_chain import BEHAVIOURS, NOW, SIGNER, FakeChain, SimulatedCrash, wallet


def payables(count: int, start: int = 0, users: int | None = None) -> list[Payable]:
    return [
        Payable(f"sub-{start + i}", wallet(start + i if users is None else (start + i) % users), 60)
        for i in range(count)
    ]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "batches.sqlite"


@pytest.fixture
def opened(db):
    """Logs opened during a test, closed at the end even if it fails."""
    logs: list[SqliteBatchLog] = []

    def open_log() -> SqliteBatchLog:
        log = SqliteBatchLog(db)
        logs.append(log)
        return log

    yield open_log
    for log in logs:
        try:
            log.close()
        except sqlite3.ProgrammingError:
            pass


def restart(log: SqliteBatchLog, opened) -> SqliteBatchLog:
    """The process dies and a new one opens the same file."""
    log.close()
    return opened()


def submitter(chain, log, **kwargs) -> BatchSubmitter:
    return BatchSubmitter(chain, log, SIGNER, clock=lambda: NOW, **kwargs)


def paid_exactly_once(chain: FakeChain, log: SqliteBatchLog) -> bool:
    confirmed = {
        s for r in log.records() if r.status is BatchStatus.CONFIRMED for s in r.batch.submission_ids
    }
    return max(chain.paid.values(), default=0) <= 1 and set(chain.paid) == confirmed


class TestItSurvivesARestart:
    def test_every_field_comes_back(self, opened):
        log = opened()
        [batch] = plan_batches(payables(5))
        record = log.add(batch)
        ticket = Ticket(7, "sig-abc", 123_456, payload=bytes(range(256)))
        log.update(BatchRecord(batch, BatchStatus.SENDING, ticket, attempts=2))

        [back] = restart(log, opened).records()

        assert back.batch == record.batch
        assert back.status is BatchStatus.SENDING
        assert back.ticket == ticket
        assert back.attempts == 2

    def test_order_and_claims_come_back(self, opened):
        log = opened()
        for batch in plan_batches(payables(150)):
            log.add(batch)

        log = restart(log, opened)

        assert [len(r.batch.entries) for r in log.records()] == [64, 64, 22]
        assert log.claimed("sub-149")
        assert not log.claimed("sub-150")

    def test_a_requeue_is_kept(self, opened):
        log = opened()
        [batch] = plan_batches(payables(3))
        log.add(batch)
        log.update(BatchRecord(batch, BatchStatus.FAILED, failure="pool exhausted"))
        log.requeue(batch.batch_id)

        [back] = restart(log, opened).records()

        assert back.status is BatchStatus.PLANNED
        assert back.failure is None


class TestWritesAreAllOrNothing:
    def test_a_batch_with_a_claimed_submission_is_refused_whole(self, opened):
        log = opened()
        log.add(plan_batches(payables(3))[0])
        overlapping = plan_batches(payables(3, start=2))[0]  # sub-2 is taken

        with pytest.raises(ValueError, match="already logged"):
            log.add(overlapping)

        assert len(log.records()) == 1
        assert not log.claimed("sub-3")

    def test_updating_an_unknown_batch_is_refused(self, opened):
        batch = plan_batches(payables(1))[0]

        with pytest.raises(KeyError):
            opened().update(BatchRecord(batch))

    def test_only_a_failed_batch_can_be_requeued(self, opened):
        log = opened()
        batch = plan_batches(payables(1))[0]
        log.add(batch)

        with pytest.raises(ValueError, match="only a FAILED batch"):
            log.requeue(batch.batch_id)


class TestOneWriterAtATime:
    def test_a_second_validator_on_the_same_log_is_refused(self, opened):
        first, second = opened(), opened()
        batch = plan_batches(payables(1))[0]

        with first.locked():
            with pytest.raises(LockBusy):
                second.add(batch)

        second.add(batch)  # free again once the first lets go

    def test_the_lock_goes_when_the_process_does(self, opened):
        first = opened()
        with first.locked():
            first.close()  # stands in for the process dying

        opened().add(plan_batches(payables(1))[0])

    def test_one_log_can_nest_its_own_lock(self, opened):
        log = opened()
        with log.locked(), log.locked():
            log.add(plan_batches(payables(1))[0])


def test_a_log_from_a_newer_schema_is_refused(db, opened):
    opened().close()
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=99")
    raw.close()

    with pytest.raises(RuntimeError, match="schema 99"):
        SqliteBatchLog(db)


class TestCrashesWithARealRestart:
    def test_a_crash_after_landing_is_not_paid_again(self, opened):
        chain = FakeChain(script=["crash_landed"])
        log = opened()
        with pytest.raises(SimulatedCrash):
            submitter(chain, log).settle(payables(10))

        log = restart(log, opened)
        report = submitter(chain, log).settle(payables(10))

        assert len(report.confirmed) == 1
        assert chain.broadcasts == 1
        assert paid_exactly_once(chain, log)

    def test_a_crash_before_sending_is_sent_once(self, opened):
        chain = FakeChain(script=["crash"])
        log = opened()
        with pytest.raises(SimulatedCrash):
            submitter(chain, log).settle(payables(10))

        log = restart(log, opened)
        submitter(chain, log).settle(payables(10))

        assert [seq for seq, _ in chain.landed] == [0]
        assert paid_exactly_once(chain, log)


@pytest.mark.parametrize("seed", range(40))
def test_nobody_is_paid_twice_across_restarts(seed, opened):
    """As the in-memory property test, but every settle runs in a "new
    process": the log is closed and reopened from disk each time."""
    rng = random.Random(seed)
    chain = FakeChain(script=[rng.choice(BEHAVIOURS) for _ in range(60)])
    items = payables(rng.randint(1, 200), users=rng.randint(1, 60))
    log = opened()

    for _ in range(40):
        log = restart(log, opened)
        try:
            report = submitter(chain, log, max_attempts=2).settle(items)
        except SimulatedCrash:
            continue
        if report.stopped is StopReason.DONE and not report.waiting:
            break

    assert paid_exactly_once(chain, log)
    assert not log.unfinished()


def test_a_record_is_found_by_submission_without_reading_the_whole_log(tmp_path):
    from s2e_validator.settlement import Batch, Payable, SqliteBatchLog

    from fake_chain import wallet

    log = SqliteBatchLog(tmp_path / "batches.sqlite")
    try:
        for n in range(5):
            log.add(Batch((Payable(f"sub-{n}", wallet(n), 10 + n),)))
        read = []
        original = log._select
        log._select = lambda where, params: read.append(where) or original(where, params)

        found = log.record_for("sub-3")

        assert found.batch.submission_ids == ("sub-3",)
        assert log.record_for("never-seen") is None
        assert all("claims" in where for where in read)  # never the unfiltered read
    finally:
        log.close()


def test_a_read_does_not_wait_for_a_send_in_progress(tmp_path):
    """One thread sits inside locked(), as a settle waiting on the chain does.
    Another must still be able to look a submission up."""
    import threading

    from s2e_validator.settlement import Batch, Payable, SqliteBatchLog

    from fake_chain import wallet

    log = SqliteBatchLog(tmp_path / "batches.sqlite")
    log.add(Batch((Payable("sub-1", wallet(1), 10),)))
    sending, release = threading.Event(), threading.Event()

    def settle():
        with log.locked():
            sending.set()
            release.wait(timeout=30)

    writer = threading.Thread(target=settle)
    writer.start()
    try:
        assert sending.wait(timeout=10)
        answers: list = []
        reader = threading.Thread(
            target=lambda: answers.extend([log.record_for("sub-1"), log.claimed("sub-1"), log.unfinished()])
        )
        reader.start()
        reader.join(timeout=5)

        assert not reader.is_alive(), "the read waited for the send to finish"
        assert answers[0].batch.submission_ids == ("sub-1",) and answers[1] is True
    finally:
        release.set()
        writer.join(timeout=10)
        log.close()
