import threading
from datetime import datetime, timedelta, timezone

import pytest

from s2e_validator.prefilters import (
    ExistingImage,
    FilterMode,
    ImageExactDuplicateFilter,
    ImageKind,
    InMemoryImageKeyStore,
    NewImage,
    InMemorySuspicionLog,
    PipelineConfig,
    PreFilterPipeline,
    SeenImage,
    Verdict,
    image_identity,
)
from s2e_validator.prefilters.image_duplicate import (
    REASON_SAME_USER,
    REASON_SAME_USER_REFERENCE,
    REASON_TEXT_ONLY,
)

from conftest import make_interaction

PHOTO = NewImage(b"\xff\xd8 castle photo bytes", "image/jpeg")
OTHER_PHOTO = NewImage(b"\xff\xd8 pierogi photo bytes", "image/jpeg")


@pytest.fixture
def duplicate_filter() -> ImageExactDuplicateFilter:
    return ImageExactDuplicateFilter()


def submit(duplicate_filter: ImageExactDuplicateFilter, **overrides):
    """Run the filter as the orchestrator will: check, then commit unless rejected.

    A FLAG does not stop the pipeline, so those commit too, and a revised verdict
    from commit() replaces the one from check().
    """
    interaction = make_interaction(**overrides)
    result = duplicate_filter.check(interaction)
    if result.verdict is not Verdict.REJECT:
        revised = duplicate_filter.commit(interaction)
        if revised is not None:
            result = revised
    return result


class TestImageIdentity:
    def test_upload_is_hashed(self):
        identity = image_identity(PHOTO)
        assert identity.kind is ImageKind.UPLOAD
        assert len(identity.key) == 64  # sha256 hex

    def test_identical_bytes_share_a_key(self):
        assert image_identity(PHOTO).key == image_identity(NewImage(PHOTO.data)).key

    def test_different_bytes_differ(self):
        assert image_identity(PHOTO).key != image_identity(OTHER_PHOTO).key

    def test_reference_uses_the_pooled_photo_id(self):
        identity = image_identity(ExistingImage("img-42"))
        assert identity.kind is ImageKind.REFERENCE
        assert identity.key == "ref:img-42"


class TestUploads:
    def test_first_upload_passes(self, duplicate_filter):
        assert submit(duplicate_filter, image=PHOTO).verdict is Verdict.PASS

    def test_same_user_resubmitting_the_same_photo_is_rejected(self, duplicate_filter):
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)

        result = submit(duplicate_filter, submission_id="sub-2", user_id="ann", image=PHOTO)

        assert result.verdict is Verdict.REJECT
        assert result.reason_code == REASON_SAME_USER
        assert result.details["first_submission_id"] == "sub-1"

    def test_second_user_uploading_a_pooled_photo_is_downgraded_not_rejected(
        self, duplicate_filter
    ):
        first = submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)

        result = submit(duplicate_filter, submission_id="sub-2", user_id="bob", image=PHOTO)

        assert first.verdict is Verdict.PASS  # first submitter keeps credit
        assert result.verdict is Verdict.FLAG  # the text is still a contribution
        assert result.reason_code == REASON_TEXT_ONLY
        assert result.details["contribution"] == "text_only"
        assert result.details["first_submission_id"] == "sub-1"

    def test_first_submitter_is_unaffected_by_later_duplicates(self, duplicate_filter):
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)
        submit(duplicate_filter, submission_id="sub-2", user_id="bob", image=PHOTO)

        # Re-validating the original submission still passes.
        interaction = make_interaction(submission_id="sub-1", user_id="ann", image=PHOTO)
        assert duplicate_filter.check(interaction).verdict is Verdict.PASS

    def test_different_photos_from_one_user_pass(self, duplicate_filter):
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)

        result = submit(
            duplicate_filter, submission_id="sub-2", user_id="ann", image=OTHER_PHOTO
        )

        assert result.verdict is Verdict.PASS

    def test_revalidating_the_same_submission_is_not_a_duplicate(self, duplicate_filter):
        interaction = make_interaction(submission_id="sub-1", image=PHOTO)
        duplicate_filter.commit(interaction)

        assert duplicate_filter.check(interaction).verdict is Verdict.PASS


class TestExistingPhotoDescriptions:
    def test_many_users_may_describe_the_same_pooled_photo(self, duplicate_filter):
        photo = ExistingImage("img-42")
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=photo)

        result = submit(duplicate_filter, submission_id="sub-2", user_id="bob", image=photo)

        assert result.verdict is Verdict.PASS

    def test_same_user_describing_one_photo_twice_is_rejected(self, duplicate_filter):
        photo = ExistingImage("img-42")
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=photo)

        result = submit(duplicate_filter, submission_id="sub-2", user_id="ann", image=photo)

        assert result.reason_code == REASON_SAME_USER_REFERENCE

    def test_one_user_may_describe_different_photos(self, duplicate_filter):
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=ExistingImage("a"))

        result = submit(
            duplicate_filter, submission_id="sub-2", user_id="ann", image=ExistingImage("b")
        )

        assert result.verdict is Verdict.PASS


class TestCommitDiscipline:
    def test_check_alone_claims_nothing(self, duplicate_filter):
        interaction = make_interaction(image=PHOTO)

        duplicate_filter.check(interaction)

        # A submission rejected by a later filter must be able to come back.
        assert duplicate_filter.check(interaction).verdict is Verdict.PASS

    def test_first_claim_wins(self):
        store = InMemoryImageKeyStore()
        duplicate_filter = ImageExactDuplicateFilter(store=store)
        first = make_interaction(submission_id="sub-1", user_id="ann", image=PHOTO)

        assert duplicate_filter.commit(first) is None  # winner gets no revision
        duplicate_filter.commit(make_interaction(submission_id="sub-2", user_id="bob", image=PHOTO))

        assert store.find(image_identity(PHOTO).key).submission_id == "sub-1"

    def test_losing_a_claim_race_downgrades_the_loser(self, duplicate_filter):
        """Both pass check() before either commits — only one can own the photo."""
        ann = make_interaction(submission_id="sub-1", user_id="ann", image=PHOTO)
        bob = make_interaction(submission_id="sub-2", user_id="bob", image=PHOTO)
        assert duplicate_filter.check(ann).verdict is Verdict.PASS
        assert duplicate_filter.check(bob).verdict is Verdict.PASS

        assert duplicate_filter.commit(ann) is None
        revised = duplicate_filter.commit(bob)

        assert revised is not None
        assert revised.verdict is Verdict.FLAG
        assert revised.reason_code == REASON_TEXT_ONLY

    def test_same_user_repeat_racing_itself_is_still_rejected(self, duplicate_filter):
        first = make_interaction(submission_id="sub-1", user_id="ann", image=PHOTO)
        second = make_interaction(submission_id="sub-2", user_id="ann", image=PHOTO)
        duplicate_filter.check(second)
        duplicate_filter.commit(first)

        revised = duplicate_filter.commit(second)

        assert revised.verdict is Verdict.REJECT
        assert revised.reason_code == REASON_SAME_USER


class TestRollback:
    """The stage calls `rollback` when a *later* filter rejects at commit time,
    so that submission ends up having claimed nothing."""

    def test_rollback_frees_the_photo_for_a_fresh_attempt(self, duplicate_filter):
        rejected = make_interaction(submission_id="sub-1", user_id="ann", image=PHOTO)
        duplicate_filter.commit(rejected)

        duplicate_filter.rollback(rejected)

        again = make_interaction(submission_id="sub-2", user_id="ann", image=PHOTO)
        assert duplicate_filter.check(again).verdict is Verdict.PASS

    def test_rollback_cannot_take_another_submissions_claim(self):
        store = InMemoryImageKeyStore()
        duplicate_filter = ImageExactDuplicateFilter(store=store)
        owner = make_interaction(submission_id="sub-1", user_id="ann", image=PHOTO)
        loser = make_interaction(submission_id="sub-2", user_id="bob", image=PHOTO)
        duplicate_filter.commit(owner)
        duplicate_filter.commit(loser)  # downgraded to text-only; owns nothing here

        duplicate_filter.rollback(loser)

        assert store.find(image_identity(PHOTO).key).submission_id == "sub-1"

    def test_rollback_is_safe_when_nothing_was_committed(self, duplicate_filter):
        never_committed = make_interaction(submission_id="sub-9", user_id="ann", image=PHOTO)

        duplicate_filter.rollback(never_committed)  # must not raise

        assert duplicate_filter.check(never_committed).verdict is Verdict.PASS


class TestTextOnlyIsCappedPerUser:
    def test_submitting_another_users_photo_twice_is_rejected(self, duplicate_filter):
        """A text-only downgrade is one contribution, not an unlimited licence."""
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)

        first = submit(duplicate_filter, submission_id="sub-2", user_id="bob", image=PHOTO)
        second = submit(duplicate_filter, submission_id="sub-3", user_id="bob", image=PHOTO)

        assert first.verdict is Verdict.FLAG
        assert second.verdict is Verdict.REJECT
        assert second.reason_code == REASON_SAME_USER_REFERENCE

    def test_a_third_user_still_gets_their_one_text_only_contribution(self, duplicate_filter):
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)
        submit(duplicate_filter, submission_id="sub-2", user_id="bob", image=PHOTO)

        result = submit(duplicate_filter, submission_id="sub-3", user_id="cara", image=PHOTO)

        assert result.reason_code == REASON_TEXT_ONLY

    def test_text_only_is_keyed_on_the_text_not_the_photo(self, duplicate_filter):
        """Settlement must not treat the copy and the original as one entry."""
        original = submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)

        copy = submit(
            duplicate_filter,
            submission_id="sub-2",
            user_id="bob",
            image=PHOTO,
            text="Zupełnie inny opis tego samego zdjęcia",
        )

        assert original.verdict is Verdict.PASS
        assert copy.details["uniqueness_key"].startswith("text:")
        assert copy.details["uniqueness_key"] != duplicate_filter.uniqueness_key(
            make_interaction(image=PHOTO)
        )
        assert copy.details["photo_key"] == image_identity(PHOTO).key


class TestSuspicionLog:
    def test_cross_user_match_is_recorded(self):
        log = InMemorySuspicionLog()
        duplicate_filter = ImageExactDuplicateFilter(suspicion_log=log)
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)

        submit(duplicate_filter, submission_id="sub-2", user_id="bob", image=PHOTO)

        assert len(log) == 1
        event = log.events[0]
        assert (event.original_user_id, event.duplicate_user_id) == ("ann", "bob")
        assert event.original_submission_id == "sub-1"
        assert event.image_key == image_identity(PHOTO).key

    def test_rollback_retracts_only_this_submissions_record(self):
        """Carol's copy stands; Bob's is rolled back. Only Bob's record goes."""
        log = InMemorySuspicionLog()
        duplicate_filter = ImageExactDuplicateFilter(suspicion_log=log)
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)
        submit(duplicate_filter, submission_id="sub-2", user_id="carol", image=PHOTO)
        bob = make_interaction(submission_id="sub-3", user_id="bob", image=PHOTO)
        duplicate_filter.commit(bob)
        assert len(log) == 2

        duplicate_filter.rollback(bob)

        assert [event.duplicate_user_id for event in log.events] == ["carol"]

    def test_same_user_repeat_is_not_a_cross_user_match(self):
        log = InMemorySuspicionLog()
        duplicate_filter = ImageExactDuplicateFilter(suspicion_log=log)
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)

        submit(duplicate_filter, submission_id="sub-2", user_id="ann", image=PHOTO)

        assert len(log) == 0

    def test_a_retried_commit_logs_the_match_once(self):
        """Retry safety: a crash between commit and persistence must not inflate
        the account's fraud record."""
        log = InMemorySuspicionLog()
        duplicate_filter = ImageExactDuplicateFilter(suspicion_log=log)
        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)
        bob = make_interaction(submission_id="sub-2", user_id="bob", image=PHOTO)
        duplicate_filter.check(bob)

        duplicate_filter.commit(bob)
        duplicate_filter.commit(bob)

        assert len(log) == 1

    def test_unique_photos_record_nothing(self):
        log = InMemorySuspicionLog()
        duplicate_filter = ImageExactDuplicateFilter(suspicion_log=log)

        submit(duplicate_filter, submission_id="sub-1", user_id="ann", image=PHOTO)
        submit(duplicate_filter, submission_id="sub-2", user_id="bob", image=OTHER_PHOTO)

        assert len(log) == 0

    def test_uniqueness_key_matches_the_image_identity(self, duplicate_filter):
        interaction = make_interaction(image=PHOTO)
        assert duplicate_filter.uniqueness_key(interaction) == image_identity(PHOTO).key


class TestInsideThePipeline:
    def test_enforced_duplicate_stops_the_pipeline(self):
        duplicate_filter = ImageExactDuplicateFilter()
        pipeline = PreFilterPipeline(
            [duplicate_filter],
            PipelineConfig(modes={duplicate_filter.name: FilterMode.ENFORCE}),
        )
        first = make_interaction(submission_id="sub-1", image=PHOTO)
        pipeline.run_pre_filters(first)
        duplicate_filter.commit(first)

        outcome = pipeline.run_pre_filters(make_interaction(submission_id="sub-2", image=PHOTO))

        assert outcome.verdict is Verdict.REJECT
        assert outcome.reason_code == REASON_SAME_USER
        assert outcome.decided_by == "image_exact_duplicate"

    def test_shadow_mode_records_without_blocking(self):
        duplicate_filter = ImageExactDuplicateFilter()
        pipeline = PreFilterPipeline([duplicate_filter])  # shadow by default
        first = make_interaction(submission_id="sub-1", image=PHOTO)
        duplicate_filter.commit(first)

        outcome = pipeline.run_pre_filters(make_interaction(submission_id="sub-2", image=PHOTO))

        assert outcome.verdict is Verdict.PASS
        assert [r.filter_name for r in outcome.shadow_rejections] == ["image_exact_duplicate"]


class TestThroughThePipeline:
    def test_a_text_only_flag_does_not_stop_the_pipeline(self):
        duplicate_filter = ImageExactDuplicateFilter()
        pipeline = PreFilterPipeline(
            [duplicate_filter],
            PipelineConfig(modes={duplicate_filter.name: FilterMode.ENFORCE}),
        )
        ann = make_interaction(submission_id="sub-1", user_id="ann", image=PHOTO)
        pipeline.run_pre_filters(ann)
        duplicate_filter.commit(ann)

        outcome = pipeline.run_pre_filters(
            make_interaction(submission_id="sub-2", user_id="bob", image=PHOTO)
        )

        assert outcome.verdict is Verdict.PASS
        assert outcome.proceeds_to_judge
        assert [r.result.reason_code for r in outcome.flags] == [REASON_TEXT_ONLY]

    def test_only_one_of_many_racing_submissions_keeps_the_photo(self):
        """Filter-level version of the store race: one winner, the rest text-only."""
        duplicate_filter = ImageExactDuplicateFilter()
        contenders = [
            make_interaction(submission_id=f"sub-{i}", user_id=f"user-{i}", image=PHOTO)
            for i in range(16)
        ]
        for contender in contenders:
            assert duplicate_filter.check(contender).verdict is Verdict.PASS

        revisions = []
        barrier = threading.Barrier(len(contenders))
        lock = threading.Lock()

        def commit(interaction):
            barrier.wait()
            revision = duplicate_filter.commit(interaction)
            with lock:
                revisions.append(revision)

        threads = [threading.Thread(target=commit, args=(c,)) for c in contenders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(revision is None for revision in revisions) == 1
        downgraded = [r for r in revisions if r is not None]
        assert len(downgraded) == 15
        assert {r.reason_code for r in downgraded} == {REASON_TEXT_ONLY}


class TestRetention:
    def test_entries_expire_after_the_retention_window(self):
        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        clock = lambda: now  # noqa: E731
        store = InMemoryImageKeyStore(retention=timedelta(days=30), clock=clock)
        duplicate_filter = ImageExactDuplicateFilter(store=store, clock=clock)
        submit(duplicate_filter, submission_id="sub-1", image=PHOTO)

        now = now + timedelta(days=31)

        assert duplicate_filter.check(make_interaction(submission_id="sub-2", image=PHOTO)).verdict is Verdict.PASS
        assert len(store) == 0

    def test_entries_survive_inside_the_window(self):
        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        clock = lambda: now  # noqa: E731
        store = InMemoryImageKeyStore(retention=timedelta(days=30), clock=clock)
        duplicate_filter = ImageExactDuplicateFilter(store=store, clock=clock)
        submit(duplicate_filter, submission_id="sub-1", image=PHOTO)

        now = now + timedelta(days=29)

        assert duplicate_filter.check(
            make_interaction(submission_id="sub-2", image=PHOTO)
        ).verdict is Verdict.REJECT

    def test_retention_must_be_positive(self):
        with pytest.raises(ValueError):
            InMemoryImageKeyStore(retention=timedelta(0))


class TestConcurrentClaims:
    def test_only_one_of_many_threads_wins_a_key(self):
        store = InMemoryImageKeyStore()
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        barrier = threading.Barrier(16)
        results: list[bool] = []
        lock = threading.Lock()

        def claim(index: int) -> None:
            seen = SeenImage("shared-key", f"sub-{index}", f"user-{index}", now)
            barrier.wait()
            outcome = store.add(seen)
            with lock:
                results.append(outcome.claimed)

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(results) == 1
        assert len(store) == 1
