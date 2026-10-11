"""The validator as a service (#43, R1): one submission in, one outcome out."""

from __future__ import annotations

import asyncio
import re
import threading

import pytest

pytest.importorskip("fastapi", reason="the API needs the optional `api` extra")

from fastapi.testclient import TestClient  # noqa: E402

from s2e_validator.api import SubmissionState, ValidatorService  # noqa: E402
from s2e_validator.api.app import create_app, settle_periodically  # noqa: E402
from s2e_validator.api.service import SubmissionIdTaken  # noqa: E402
from s2e_validator.judge import RUBRIC_V1, JudgeVerdict, Provenance, StubJudge  # noqa: E402
from s2e_validator.prefilters import (  # noqa: E402
    FilterMode,
    PipelineConfig,
    PreFilterPipeline,
    PreFilterResult,
    PreflightFailed,
    build_pipeline,
)
from s2e_validator.settlement import BatchSubmitter, InMemoryBatchLog, SequenceConflict  # noqa: E402
from s2e_validator.settlement.dry_run import DryRunChain  # noqa: E402
from s2e_validator.settlement.mapping import ScoreMapping  # noqa: E402

from fake_chain import NOW, SIGNER, FakeChain, wallet  # noqa: E402
from helpers import CASTLE, ENFORCE_ALL, StubFilter, big_photo  # noqa: E402

OTHER_CAPTION = "Mój własny opis tego zamku, zupełnie inny od poprzedniego wpisu"
API_KEY = "test-key-" + "k" * 32
AUTH = {"Authorization": f"Bearer {API_KEY}"}
VISITOR, OTHER_VISITOR = wallet(1), wallet(2)
#: An id as the demo site makes them: uuid4().hex.
SITE_ID = "0123456789abcdef0123456789abcdef"
#: The demo site's setting: every filter observes, none stops a submission.
OBSERVE_ONLY = PipelineConfig(default_mode=FilterMode.SHADOW)


class Stage:
    """Everything behind the API, wired like the real service, with a chain
    the test controls."""

    def __init__(self, chain=None, judge=None, pipeline=None, mapping=None):
        self.chain = chain or FakeChain()
        self.log = InMemoryBatchLog()
        self.service = ValidatorService(
            pipeline=pipeline or build_pipeline(ENFORCE_ALL),
            judge=judge or StubJudge(RUBRIC_V1),
            submitter=BatchSubmitter(self.chain, self.log, SIGNER, clock=lambda: NOW),
            batch_log=self.log,
            mapping=mapping or ScoreMapping(max_units_per_submission=1_000_000),
            clock=lambda: NOW,
        )
        self.app = create_app(self.service, api_key=API_KEY, settled_on="test")


@pytest.fixture
def stage():
    return Stage()


@pytest.fixture
def client(stage):
    with TestClient(stage.app) as test_client:
        yield test_client


def submit(client, description=CASTLE, photo=None, wallet=VISITOR, headers=AUTH, submission_id=None):
    data = {"description": description, "wallet": wallet}
    if submission_id is not None:
        data["submission_id"] = submission_id
    return client.post(
        "/submissions",
        data=data,
        files={"photo": ("photo.jpg", photo if photo is not None else big_photo(), "image/jpeg")},
        headers=headers,
    )


class TestAGoodSubmission:
    def test_it_is_judged_priced_and_paid(self, client, stage):
        body = submit(client).json()

        assert body["state"] == "paid"
        assert body["quality_score"] == 75  # the stub rates every criterion 4 of 5
        assert body["reward_units"] == 75
        assert body["reward_display"] == "0.000075"
        assert body["transaction"]
        assert body["settled_on"] == "test"
        assert stage.chain.paid == {body["submission_id"]: 1}

    def test_the_outcome_can_be_fetched_again(self, client):
        first = submit(client).json()

        again = client.get(f"/submissions/{first['submission_id']}", headers=AUTH).json()

        assert again == first

    def test_the_reward_goes_to_the_wallet_that_was_sent(self, client, stage):
        submit(client, wallet=OTHER_VISITOR)

        [record] = stage.log.records()
        assert record.batch.entries[0].user == OTHER_VISITOR


class TestWhatAnEnforcedFilterStops:
    def test_junk_text_is_rejected_with_the_reason_and_the_filter(self, client, stage):
        body = submit(client, description="aaaaaaaaaa").json()

        assert body["state"] == "rejected"
        assert body["reason_code"] == "TEXT_REPEATED_CHARACTERS"
        assert body["rejected_by"] == "text_format"
        assert body["reward_units"] is None
        assert stage.chain.broadcasts == 0

    def test_a_file_that_is_not_a_photo_is_rejected(self, client):
        body = submit(client, photo=b"definitely not a photo").json()

        assert body["state"] == "rejected"
        assert body["rejected_by"] == "image_format"

    def test_the_same_photo_twice_from_one_visitor_is_rejected(self, client):
        assert submit(client).json()["state"] == "paid"

        body = submit(client, description=OTHER_CAPTION).json()

        assert body["state"] == "rejected"
        assert body["rejected_by"] == "image_exact_duplicate"


class TestDuplicatePhotosAreNeverPaid:
    """M1's rule. It has to hold when Stage 1 only observes, as on the demo
    site, because then nothing else stops a re-uploaded photo."""

    def test_someone_elses_photo_is_not_rewarded(self, client, stage):
        submit(client, wallet=VISITOR)

        body = submit(client, description=OTHER_CAPTION, wallet=OTHER_VISITOR).json()

        assert body["state"] == "not_rewarded"
        assert body["reason_code"] == "DUPLICATE_IMAGE_TEXT_ONLY"
        assert body["quality_score"] == 75  # judged, and still not paid
        assert stage.chain.broadcasts == 1  # only the first submission

    def test_a_visitors_own_re_upload_is_not_rewarded_when_filters_only_observe(self):
        observing = Stage(pipeline=build_pipeline(OBSERVE_ONLY))
        with TestClient(observing.app) as client:
            assert submit(client).json()["state"] == "paid"

            body = submit(client, description=OTHER_CAPTION).json()

        assert body["state"] == "not_rewarded"
        assert body["reason_code"] == "DUPLICATE_IMAGE_SAME_USER"
        assert "DUPLICATE_IMAGE_SAME_USER" in body["flags"]
        assert observing.chain.broadcasts == 1

    def test_other_observed_findings_travel_as_flags_without_blocking(self):
        """Junk text is only observed here: the judge decides, and the flag
        says what Stage 1 saw."""
        observing = Stage(pipeline=build_pipeline(OBSERVE_ONLY))
        with TestClient(observing.app) as client:
            body = submit(client, description="aaaaaaaaaa").json()

        assert body["state"] == "paid"
        assert "TEXT_REPEATED_CHARACTERS" in body["flags"]


class TestWhatTheOutcomeSaysAboutTheChecks:
    """`filters` and `judge` name each field that leaves (api/report.py).
    Nothing else of a filter's result or a verdict reaches the caller."""

    def test_every_check_and_the_judge_are_reported(self, client):
        body = submit(client).json()

        assert body["filters"][:2] == [
            {"name": "text_format", "mode": "enforce", "verdict": "PASS", "reason_code": None},
            {"name": "image_format", "mode": "enforce", "verdict": "PASS", "reason_code": None},
        ]
        assert all(set(row) == {"name", "mode", "verdict", "reason_code"} for row in body["filters"])
        assert len({row["name"] for row in body["filters"]}) == len(body["filters"])
        assert body["judge"] == {"model": "stub", "criteria": {"relevance": 4, "specificity": 4, "effort": 4}}

    def test_a_copied_photo_never_reveals_the_first_submission(self):
        """A duplicate's details carry the first submission's id and time."""
        observing = Stage(pipeline=build_pipeline(OBSERVE_ONLY))
        with TestClient(observing.app) as client:
            first = submit(client, wallet=VISITOR).json()
            response = submit(client, description=OTHER_CAPTION, wallet=OTHER_VISITOR)
            fetched = client.get(f"/submissions/{response.json()['submission_id']}", headers=AUTH)

        for text in (response.text, fetched.text):
            assert first["submission_id"] not in text
            assert "first_submission_id" not in text and "first_seen_at" not in text
        rows = {row["name"]: row for row in response.json()["filters"]}
        assert rows["image_exact_duplicate"] == {
            "name": "image_exact_duplicate", "mode": "shadow", "verdict": "FLAG",
            "reason_code": "DUPLICATE_IMAGE_TEXT_ONLY",
        }

    def test_an_own_re_upload_is_an_observed_reject(self):
        observing = Stage(pipeline=build_pipeline(OBSERVE_ONLY))
        with TestClient(observing.app) as client:
            first = submit(client).json()
            response = submit(client, description=OTHER_CAPTION)

        assert first["submission_id"] not in response.text
        rows = {row["name"]: row for row in response.json()["filters"]}
        assert rows["image_exact_duplicate"] == {
            "name": "image_exact_duplicate", "mode": "shadow", "verdict": "REJECT",
            "reason_code": "DUPLICATE_IMAGE_SAME_USER",
        }

    def test_an_enforced_reject_lists_only_the_checks_that_ran(self, client):
        body = submit(client, description="aaaaaaaaaa").json()

        assert body["filters"] == [
            {"name": "text_format", "mode": "enforce", "verdict": "REJECT", "reason_code": "TEXT_REPEATED_CHARACTERS"},
        ]
        assert body["judge"] is None

    def test_a_crashed_check_is_an_error_without_its_message(self):
        class Crashes(StubFilter):
            def check(self, interaction):
                raise RuntimeError("internal path /var/lib/s2e/keys")

        crashing = Stage(
            pipeline=PreFilterPipeline(
                [Crashes("fragile", 10, PreFilterResult.passed())],
                PipelineConfig(default_mode=FilterMode.ENFORCE),
            )
        )
        with TestClient(crashing.app) as client:
            response = submit(client)

        assert response.json()["state"] == "paid"  # fail open
        assert response.json()["filters"] == [
            {"name": "fragile", "mode": "enforce", "verdict": "ERROR", "reason_code": None},
        ]
        assert "internal path" not in response.text


class TestTheJudge:
    def test_a_held_verdict_pays_nothing_and_says_why(self):
        class AlwaysHolds:
            def judge(self, item):
                return JudgeVerdict.held(item, Provenance("m", "p", "r"), "JUDGE_UNAVAILABLE", 2)

        held = Stage(judge=AlwaysHolds())
        with TestClient(held.app) as client:
            body = submit(client).json()

        assert body["state"] == "held"
        assert body["reason_code"] == "JUDGE_UNAVAILABLE"
        # The model's name only: never the prompt ("p") or rubric ("r") version.
        assert body["judge"] == {"model": "m", "criteria": {}}
        assert held.chain.broadcasts == 0

    def test_a_low_score_is_not_rewarded(self):
        low = Stage(judge=StubJudge(RUBRIC_V1, ratings={n: 1 for n in RUBRIC_V1.criterion_names}))
        with TestClient(low.app) as client:
            body = submit(client).json()

        assert body["state"] == "not_rewarded"
        assert body["quality_score"] == 0
        assert low.chain.broadcasts == 0

    def test_a_blocking_reason_is_reported(self):
        blocked = Stage(judge=StubJudge(RUBRIC_V1, reason_codes=("INSTRUCTIONS_TO_JUDGE",)))
        with TestClient(blocked.app) as client:
            body = submit(client).json()

        assert body["state"] == "not_rewarded"
        assert body["reason_code"] == "INSTRUCTIONS_TO_JUDGE"


class TestSettlement:
    def test_a_run_that_is_not_active_leaves_the_submission_batched(self):
        waiting = Stage(chain=FakeChain(run_active=False))
        with TestClient(waiting.app) as client:
            body = submit(client).json()

            assert body["state"] == "batched"
            assert body["detail"] == "the run is not Active"
            assert body["reward_units"] == 75

            waiting.chain.run_active = True
            waiting.service.settle_pending()
            later = client.get(f"/submissions/{body['submission_id']}", headers=AUTH).json()

        assert later["state"] == "paid"
        assert later["transaction"]

    def test_a_batch_the_program_rejects_is_reported_as_failed(self):
        failing = Stage(chain=FakeChain(script=["fail"]))
        with TestClient(failing.app) as client:
            body = submit(client).json()

        assert body["state"] == "failed"
        assert body["reason_code"] == "BATCH_REJECTED"
        assert body["detail"] == "simulated program error"

    def test_a_dropped_send_is_retried_and_pays_once(self):
        flaky = Stage(chain=FakeChain(script=["drop", "land"]))
        with TestClient(flaky.app) as client:
            body = submit(client).json()

        assert body["state"] == "paid"
        assert flaky.chain.paid == {body["submission_id"]: 1}

    def test_a_dry_run_pays_nothing_and_says_so(self):
        dry = Stage(chain=DryRunChain(SIGNER, clock=lambda: NOW))
        dry.app = create_app(dry.service, api_key=API_KEY, settled_on="dry-run")
        with TestClient(dry.app) as client:
            body = submit(client).json()

        assert body["state"] == "paid"
        assert body["settled_on"] == "dry-run"
        assert body["transaction"].startswith("dryrun-")


class NodeDown(FakeChain):
    """A chain whose RPC node cannot be reached for the first `outages` reads."""

    outages = 1

    def run_state(self):
        if self.outages:
            self.outages -= 1
            raise ConnectionError("RPC node unreachable")
        return super().run_state()


class TestWhenTheChainCannotBeReached:
    def test_the_submission_waits_and_is_not_lost(self):
        down = Stage(chain=NodeDown())
        with TestClient(down.app) as client:
            response = submit(client)
            body = response.json()

            assert response.status_code == 200
            assert body["state"] == "batched"
            assert body["detail"] == "settlement is temporarily unavailable; the reward stays queued"
            assert body["reward_units"] == 75

            down.service.settle_pending()
            later = client.get(f"/submissions/{body['submission_id']}", headers=AUTH).json()

        assert later["state"] == "paid"
        assert down.chain.paid == {body["submission_id"]: 1}

    def test_a_second_signer_still_halts_settlement(self):
        class Conflict(FakeChain):
            def run_state(self):
                raise SequenceConflict("the chain expects 9")

        with TestClient(Stage(chain=Conflict()).app) as client:
            response = submit(client)

        assert response.status_code == 503
        assert response.json() == {"detail": "settlement is halted"}


class TestWhenTheRewardCannotBeRecorded:
    """A busy lock or a full disk, before anything is in the batch log."""

    def unrecordable(self):
        stage = Stage()

        def full_disk(batch):
            raise OSError("no space left on device")

        stage.log.add = full_disk
        return stage

    def test_the_answer_is_an_error_not_a_promise(self):
        stage = self.unrecordable()
        with TestClient(stage.app) as client:
            response = submit(client)

        assert response.status_code == 503
        assert response.json()["detail"] == "the reward could not be recorded; nothing is owed"
        assert stage.log.records() == [] and stage.chain.broadcasts == 0

    def test_nothing_reports_it_as_waiting_afterwards(self):
        stage = self.unrecordable()
        with TestClient(stage.app) as client:
            failed = submit(client).json()

            assert client.get(f"/submissions/{failed['submission_id']}", headers=AUTH).status_code == 404


class Gate:
    """A judge that answers only once the test lets it, so a submission can be
    caught while it is still being processed."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._stub = StubJudge(RUBRIC_V1)

    def judge(self, item):
        self.entered.set()
        self.release.wait(10)
        return self._stub.judge(item)


class TestTheSitesSubmissionId:
    """The site hands out its own id, so it can poll before the POST answers."""

    def test_the_sites_id_is_used(self, client):
        body = submit(client, submission_id=SITE_ID).json()

        assert body["submission_id"] == SITE_ID
        assert client.get(f"/submissions/{SITE_ID}", headers=AUTH).json() == body

    def test_without_one_the_service_makes_its_own(self, client):
        assert re.fullmatch(r"[0-9a-f]{32}", submit(client).json()["submission_id"])

    @pytest.mark.parametrize(
        "bad",
        ["0123456789ABCDEF0123456789ABCDEF", "0" * 31, "0" * 33, "../" + "0" * 29, "sub-1"],
        ids=["uppercase", "short", "long", "path", "not-hex"],
    )
    def test_a_malformed_id_is_refused_before_any_work(self, client, stage, bad):
        response = submit(client, submission_id=bad)

        assert response.status_code == 422
        assert stage.log.records() == [] and stage.chain.broadcasts == 0

    def test_an_id_already_answered_is_refused(self, client, stage):
        assert submit(client, submission_id=SITE_ID).json()["state"] == "paid"

        response = submit(client, description=OTHER_CAPTION, photo=big_photo(2), submission_id=SITE_ID)

        assert response.status_code == 409
        assert response.json() == {"detail": "submission_id already used"}
        assert stage.chain.broadcasts == 1

    def test_an_id_owed_a_reward_is_refused_after_a_restart(self):
        """The outcomes in memory are gone after a restart; the batch log is not."""
        before = Stage()
        with TestClient(before.app) as client:
            submit(client, submission_id=SITE_ID)
        restarted = ValidatorService(
            pipeline=build_pipeline(ENFORCE_ALL),
            judge=StubJudge(RUBRIC_V1),
            submitter=BatchSubmitter(before.chain, before.log, SIGNER, clock=lambda: NOW),
            batch_log=before.log,
            mapping=ScoreMapping(max_units_per_submission=1_000_000),
            clock=lambda: NOW,
        )

        with pytest.raises(SubmissionIdTaken):
            restarted.submit(OTHER_VISITOR, OTHER_CAPTION, big_photo(2), SITE_ID)
        assert before.chain.broadcasts == 1

    def test_an_id_being_processed_reads_as_processing_and_cannot_be_reused(self):
        gate = Gate()
        slow = Stage(judge=gate)
        with TestClient(slow.app) as client:
            worker = threading.Thread(
                target=slow.service.submit, args=(VISITOR, CASTLE, big_photo(), SITE_ID)
            )
            worker.start()
            try:
                assert gate.entered.wait(10)
                processing = client.get(f"/submissions/{SITE_ID}", headers=AUTH)
                reused = submit(client, description=OTHER_CAPTION, photo=big_photo(2), submission_id=SITE_ID)
            finally:
                gate.release.set()
                worker.join(10)
            done = client.get(f"/submissions/{SITE_ID}", headers=AUTH)

        assert processing.status_code == 202
        assert processing.json() == {"submission_id": SITE_ID, "status": "processing"}
        assert reused.status_code == 409
        assert done.status_code == 200 and done.json()["state"] == "paid"
        assert not slow.service.is_processing(SITE_ID)

    def test_a_submission_finishing_mid_poll_is_never_unknown(self):
        """The submission finishes right after the poll reads the outcome. A
        poll that read the outcome first would miss it there, then find the id
        no longer in flight, and answer 404."""
        gate = Gate()
        slow = Stage(judge=gate)
        worker = threading.Thread(target=slow.service.submit, args=(VISITOR, CASTLE, big_photo(), SITE_ID))
        read_outcome = slow.service.result

        def read_then_finish(submission_id):
            outcome = read_outcome(submission_id)
            gate.release.set()
            worker.join(10)
            return outcome

        slow.service.result = read_then_finish
        with TestClient(slow.app) as client:
            worker.start()
            try:
                assert gate.entered.wait(10)
                polled = client.get(f"/submissions/{SITE_ID}", headers=AUTH)
            finally:
                gate.release.set()
                worker.join(10)

        assert polled.status_code == 202

    def test_processing_still_needs_the_key(self):
        gate = Gate()
        slow = Stage(judge=gate)
        with TestClient(slow.app) as client:
            worker = threading.Thread(
                target=slow.service.submit, args=(VISITOR, CASTLE, big_photo(), SITE_ID)
            )
            worker.start()
            try:
                assert gate.entered.wait(10)
                response = client.get(f"/submissions/{SITE_ID}")
            finally:
                gate.release.set()
                worker.join(10)

        assert response.status_code == 401


class TestRetryingWaitingRewards:
    def run(self, service, rounds: int) -> list[float]:
        """Run the retry loop for `rounds` sleeps, then cancel it."""
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            if len(slept) == rounds:
                raise asyncio.CancelledError
            slept.append(seconds)

        async def main() -> None:
            try:
                await settle_periodically(service, 30.0, sleep)
            except asyncio.CancelledError:
                pass

        asyncio.run(main())
        return slept

    def test_a_waiting_reward_is_sent_once_the_run_is_active(self):
        waiting = Stage(chain=FakeChain(run_active=False))
        batched = waiting.service.submit(VISITOR, CASTLE, big_photo())
        assert batched.state is SubmissionState.BATCHED

        waiting.chain.run_active = True
        assert self.run(waiting.service, rounds=1) == [30.0]

        assert waiting.service.result(batched.submission_id).state is SubmissionState.PAID

    def test_one_failed_round_does_not_end_the_retries(self):
        down = Stage(chain=NodeDown())
        batched = down.service.submit(VISITOR, CASTLE, big_photo())
        down.chain.outages = 1

        self.run(down.service, rounds=2)

        assert down.service.result(batched.submission_id).state is SubmissionState.PAID

    def test_a_sequence_conflict_ends_them(self):
        class Conflict:
            calls = 0

            def settle_pending(self):
                self.calls += 1
                raise SequenceConflict("the chain expects 9")

        service = Conflict()

        assert self.run(service, rounds=5) == [30.0]
        assert service.calls == 1


def test_only_the_newest_outcomes_are_kept():
    stage = Stage()
    small = ValidatorService(
        pipeline=build_pipeline(ENFORCE_ALL),
        judge=StubJudge(RUBRIC_V1),
        submitter=BatchSubmitter(stage.chain, stage.log, SIGNER, clock=lambda: NOW),
        batch_log=stage.log,
        mapping=ScoreMapping(max_units_per_submission=100),
        max_outcomes=2,
    )

    ids = [small.submit(VISITOR, "aaaaaaaaaa", big_photo()).submission_id for _ in range(3)]

    assert small.result(ids[0]) is None
    assert small.result(ids[1]) is not None and small.result(ids[2]) is not None


class TestOnlyTheSiteMayCall:
    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer wrong-" + "k" * 32}, {"Authorization": API_KEY}, {"X-API-Key": API_KEY}],
        ids=["no-key", "wrong-key", "no-bearer", "wrong-header"],
    )
    def test_a_submission_without_the_key_is_refused_before_any_work(self, client, stage, headers):
        response = submit(client, headers=headers)

        assert response.status_code == 401
        assert stage.log.records() == []

    def test_an_outcome_cannot_be_read_without_the_key(self, client):
        body = submit(client).json()

        assert client.get(f"/submissions/{body['submission_id']}").status_code == 401

    def test_a_short_key_is_refused_at_startup(self, stage):
        with pytest.raises(ValueError, match="at least 32 characters"):
            create_app(stage.service, api_key="short")

    def test_there_are_no_public_docs(self, client):
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404

    def test_health_needs_no_key(self, client):
        assert client.get("/healthz").json() == {"status": "ok", "settled_on": "test", "version": "unknown"}

    def test_health_reports_the_version(self, stage):
        with TestClient(create_app(stage.service, api_key=API_KEY, version="9.9.9")) as client:
            assert client.get("/healthz").json()["version"] == "9.9.9"

    def test_the_version_is_the_installed_package(self, monkeypatch):
        from s2e_validator.api import main

        monkeypatch.setattr(main, "installed_version", lambda name: f"{name} 1.2.3")
        assert main.service_version() == "s2e-validator 1.2.3"

    def test_a_plain_checkout_reports_an_unknown_version(self, monkeypatch):
        from importlib.metadata import PackageNotFoundError

        from s2e_validator.api import main

        def not_installed(name):
            raise PackageNotFoundError(name)

        monkeypatch.setattr(main, "installed_version", not_installed)
        assert main.service_version() == "unknown"

    def test_there_is_no_browser_access(self, client):
        response = client.get("/healthz", headers={"Origin": "https://demo.example"})

        assert "access-control-allow-origin" not in response.headers


class TestBoundsOnWhatComesIn:
    def test_an_oversized_photo_is_refused_while_reading(self, stage):
        small = create_app(stage.service, api_key=API_KEY, max_photo_bytes=1000)
        with TestClient(small) as client:
            response = submit(client, photo=b"x" * 5000)

        assert response.status_code == 413
        assert stage.chain.broadcasts == 0

    @pytest.mark.parametrize(
        "data",
        [
            {"description": CASTLE},  # no wallet
            {"wallet": VISITOR},  # no description
            {"description": CASTLE, "wallet": ""},
            {"description": CASTLE, "wallet": "visitor-1"},
            {"description": CASTLE, "wallet": "0" * 40},  # not base58
            {"description": CASTLE, "wallet": "1" * 44},  # base58, wrong length
            {"description": "x" * 20_001, "wallet": VISITOR},
        ],
    )
    def test_malformed_requests_are_refused_before_any_work(self, client, stage, data):
        response = client.post(
            "/submissions", data=data, files={"photo": ("p.jpg", big_photo(), "image/jpeg")}, headers=AUTH
        )

        assert response.status_code == 422
        assert stage.log.records() == []

    def test_a_missing_photo_is_refused(self, client):
        response = client.post("/submissions", data={"description": CASTLE, "wallet": VISITOR}, headers=AUTH)

        assert response.status_code == 422

    def test_an_unknown_submission_is_a_404(self, client):
        assert client.get("/submissions/nope", headers=AUTH).status_code == 404


def test_the_service_refuses_to_start_when_a_filter_cannot_run():
    class NeverReady(StubFilter):
        def preflight(self):
            raise FileNotFoundError("model missing")

    broken = Stage(pipeline=PreFilterPipeline([NeverReady("needs_model", 10, PreFilterResult.passed())]))

    with pytest.raises(PreflightFailed, match="needs_model cannot run"):
        with TestClient(broken.app):
            pass


def test_state_names_are_the_api_contract():
    assert {s.value for s in SubmissionState} == {
        "rejected", "held", "not_rewarded", "batched", "paid", "failed",
    }
