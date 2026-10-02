"""Issue #20: how fast one contributor is submitting.

The "must pass" case matters most here. Someone emptying a holiday camera roll
in one sitting is a good contributor, not a script.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from s2e_validator.prefilters import (
    FilterDependencies,
    FilterMode,
    InMemorySubmissionTimes,
    Interaction,
    PipelineConfig,
    PreFilter,
    PreFilterPipeline,
    PreFilterResult,
    SubmissionVelocityFilter,
    Verdict,
    VelocitySettings,
    build_pipeline,
)
from s2e_validator.prefilters.velocity import REASON_RATE_AUTOMATED, REASON_RATE_ELEVATED

from helpers import make_interaction

START = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
STRICT = VelocitySettings(window_seconds=60, flag_threshold=2, reject_threshold=3)


def at(seconds: float, submission_id: str, user_id: str = "ann") -> Interaction:
    return make_interaction(
        submission_id=submission_id,
        user_id=user_id,
        submitted_at=START + timedelta(seconds=seconds),
    )


def submit(velocity_filter, index: int, seconds: float, user_id: str = "ann"):
    """One submission `seconds` after the start, committed unless rejected."""
    interaction = at(seconds, f"{user_id}-{index}", user_id)
    result = velocity_filter.check(interaction)
    if result.verdict is not Verdict.REJECT:
        velocity_filter.commit(interaction)
    return result


def burst(velocity_filter, count: int, gap: float, user_id: str = "ann"):
    """`count` submissions `gap` seconds apart; returns the last verdict."""
    result = None
    for index in range(count):
        result = submit(velocity_filter, index, index * gap, user_id)
    return result


@pytest.fixture
def velocity_filter() -> SubmissionVelocityFilter:
    return SubmissionVelocityFilter()


class TestHonestContributors:
    def test_a_holiday_batch_in_one_sitting_passes(self, velocity_filter):
        """Ten photos over half an hour: about three minutes each, which is
        what writing a 100 character description actually takes."""
        assert burst(velocity_filter, 10, gap=180).verdict is Verdict.PASS

    def test_a_steady_hour_of_work_passes(self, velocity_filter):
        assert burst(velocity_filter, 20, gap=180).verdict is Verdict.PASS

    def test_a_few_quick_submissions_pass(self, velocity_filter):
        """Three in a row while the photos are already picked out."""
        assert burst(velocity_filter, 3, gap=20).verdict is Verdict.PASS


class TestAutomation:
    def test_a_sustained_burst_is_rejected(self, velocity_filter):
        """Thirty submissions in five minutes: six a minute, nobody types that."""
        result = burst(velocity_filter, 30, gap=10)

        assert result.verdict is Verdict.REJECT
        assert result.reason_code == REASON_RATE_AUTOMATED
        assert result.details["submissions"] >= 30

    def test_an_elevated_rate_is_flagged_not_rejected(self, velocity_filter):
        """Twelve in ten minutes is fast for 100 character descriptions, but a
        determined person could do it, so the judge decides."""
        result = burst(velocity_filter, 12, gap=45)

        assert result.verdict is Verdict.FLAG
        assert result.reason_code == REASON_RATE_ELEVATED
        assert result.details["per_minute"] > 1


class TestScoping:
    def test_one_users_burst_does_not_affect_another(self, velocity_filter):
        burst(velocity_filter, 30, gap=10, user_id="bot")

        assert burst(velocity_filter, 3, gap=60, user_id="ann").verdict is Verdict.PASS

    def test_the_window_moves(self, velocity_filter):
        """Yesterday's burst is not today's problem."""
        burst(velocity_filter, 11, gap=30)

        later = make_interaction(
            submission_id="later", user_id="ann", submitted_at=START + timedelta(hours=2)
        )
        assert velocity_filter.check(later).verdict is Verdict.PASS

    def test_check_alone_does_not_count(self, velocity_filter):
        """A submission rejected by another filter never happened, rate-wise."""
        for index in range(30):
            velocity_filter.check(at(index, f"sub-{index}"))

        assert velocity_filter.check(at(31, "sub-31")).verdict is Verdict.PASS


class TestRetries:
    def test_a_retried_commit_is_one_event(self, velocity_filter):
        interaction = at(0, "sub-1")

        for _ in range(20):
            velocity_filter.commit(interaction)

        assert velocity_filter.check(at(1, "sub-2")).verdict is Verdict.PASS

    def test_a_revalidated_submission_does_not_count_itself_twice(self):
        """Already recorded, then checked again: it is one submission, so it
        must not tip itself over the threshold."""
        strict = SubmissionVelocityFilter(STRICT)
        first = at(0, "sub-1")
        strict.commit(first)

        assert strict.check(first).verdict is Verdict.PASS


class TestCommitIsAuthoritative:
    def test_submissions_that_passed_check_together_are_caught_at_commit(self):
        """Three arrive at once. Each sees an empty history at check; only the
        count taken while recording is true."""
        strict = SubmissionVelocityFilter(STRICT)
        batch = [at(i, f"sub-{i}") for i in range(3)]
        assert all(strict.check(i).verdict is Verdict.PASS for i in batch)

        revisions = [strict.commit(i) for i in batch]

        assert revisions[0] is None
        assert revisions[1].verdict is Verdict.FLAG
        assert revisions[2].verdict is Verdict.REJECT

    def test_concurrent_commits_count_every_submission(self):
        store = InMemorySubmissionTimes()
        fast = SubmissionVelocityFilter(
            VelocitySettings(window_seconds=600, flag_threshold=500, reject_threshold=1000), store
        )
        start = threading.Barrier(16)

        def worker(n):
            start.wait()
            for i in range(25):
                fast.commit(at(i, f"sub-{n}-{i}"))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert store.count_within("ann", START - timedelta(seconds=1)) == 400


class TestRollback:
    def test_a_rolled_back_submission_leaves_no_record(self):
        store = InMemorySubmissionTimes()
        velocity_filter = SubmissionVelocityFilter(store=store)
        rejected_later = at(0, "sub-1")
        velocity_filter.commit(rejected_later)

        velocity_filter.rollback(rejected_later)

        assert len(store) == 0

    def test_rollback_is_safe_when_nothing_was_recorded(self, velocity_filter):
        velocity_filter.rollback(at(0, "never-committed"))

    def test_a_submission_rejected_at_commit_does_not_count(self):
        """Through the stage: a later filter rejects at commit time, so this
        contributor's rate must not have moved."""

        class RejectsOnCommit(PreFilter):
            name = "rejects_on_commit"
            cost_rank = 99

            def check(self, interaction):
                return PreFilterResult.passed()

            def commit(self, interaction):
                return PreFilterResult.rejected("LOST_THE_RACE")

        store = InMemorySubmissionTimes()
        config = PipelineConfig(default_mode=FilterMode.ENFORCE)
        pipeline = PreFilterPipeline(
            [SubmissionVelocityFilter(store=store), RejectsOnCommit()], config
        )

        outcome = pipeline.run_pre_filters(at(0, "sub-1"))

        assert outcome.decided_by == "rejects_on_commit"
        assert len(store) == 0

    def test_velocitys_own_commit_reject_is_rolled_back(self):
        """The filter rejects at its own commit and must still be rolled back.
        That works because the stage lists a filter as committed *before*
        calling commit; if that ever changed, a rejected submission would
        quietly start counting."""
        store = InMemorySubmissionTimes()
        store.record("ann", "earlier-1", START, START)
        store.record("ann", "earlier-2", START, START)

        class RaceAtCheck:  # check sees an empty history, as when arrivals race
            retention = store.retention

            def count_within(self, *args, **kwargs):
                return 0

            def record(self, *args):
                return store.record(*args)

            def forget(self, *args):
                store.forget(*args)

        pipeline = PreFilterPipeline(
            [SubmissionVelocityFilter(STRICT, RaceAtCheck())],
            PipelineConfig(default_mode=FilterMode.ENFORCE),
        )
        outcome = pipeline.run_pre_filters(at(1, "sub-3"))

        assert outcome.decided_by == "submission_velocity"
        assert len(store) == 2


class TestSettings:
    def test_thresholds_are_configurable(self):
        strict = SubmissionVelocityFilter(STRICT)

        assert submit(strict, 0, 0).verdict is Verdict.PASS
        assert submit(strict, 1, 10).verdict is Verdict.FLAG
        assert submit(strict, 2, 20).verdict is Verdict.REJECT

    def test_from_dict(self):
        settings = VelocitySettings.from_dict({"window_seconds": 300, "flag_threshold": 5})
        assert settings.window == timedelta(minutes=5)

    @pytest.mark.parametrize(
        "raw, message",
        [
            ({"window_seconds": 0}, "at least 1"),
            ({"flag_threshold": 0}, "at least 1"),
            ({"flag_threshold": 20, "reject_threshold": 5}, "reject_threshold must be at least"),
            ({"flag_threshold": 20, "reject_threshold": 20}, "reject_threshold must be at least 21"),
            ({"window_seconds": "600"}, "whole number"),
            ({"windows_seconds": 600}, "Unknown"),
        ],
    )
    def test_bad_settings_fail_at_load(self, raw, message):
        with pytest.raises(ValueError, match=message):
            VelocitySettings.from_dict(raw)

    def test_a_store_that_forgets_sooner_than_the_window_is_refused(self):
        """It would undercount silently; better to fail when the stage is built."""
        short_memory = InMemorySubmissionTimes(retention=timedelta(minutes=5))

        with pytest.raises(ValueError, match="undercounted"):
            SubmissionVelocityFilter(VelocitySettings(window_seconds=600), short_memory)


class TestRetention:
    def test_the_shared_default_store_covers_windows_up_to_an_hour(self):
        config = PipelineConfig(settings={"submission_velocity": {"window_seconds": 3600}})

        build_pipeline(config, FilterDependencies())

    def test_a_longer_window_needs_a_longer_lived_store(self):
        """Kept short so a script cannot make the store hold a day of history.
        A window it cannot cover fails when the stage is built."""
        config = PipelineConfig(settings={"submission_velocity": {"window_seconds": 7200}})

        with pytest.raises(ValueError, match="undercounted"):
            build_pipeline(config, FilterDependencies())

        build_pipeline(
            config,
            FilterDependencies(submission_times=InMemorySubmissionTimes(retention=timedelta(hours=4))),
        )

    def test_a_filter_built_alone_sizes_its_own_store(self):
        SubmissionVelocityFilter(VelocitySettings(window_seconds=7200))

    def test_the_sweep_interval_must_be_positive(self):
        with pytest.raises(ValueError, match="sweep_every"):
            InMemorySubmissionTimes(sweep_every=0)


class TestInsideThePipeline:
    def test_an_automated_burst_stops_the_stage(self):
        velocity_filter = SubmissionVelocityFilter(STRICT)
        pipeline = PreFilterPipeline(
            [velocity_filter], PipelineConfig(modes={velocity_filter.name: FilterMode.ENFORCE})
        )
        for index in range(2):
            pipeline.run_pre_filters(at(index, f"sub-{index}"))

        outcome = pipeline.run_pre_filters(at(3, "sub-3"))

        assert outcome.verdict is Verdict.REJECT
        assert outcome.decided_by == "submission_velocity"

    def test_the_rate_survives_a_config_reload(self):
        """Rebuilding the stage with the same dependencies keeps the history;
        a filter left to build its own store would forget it."""
        config = PipelineConfig(
            modes={"submission_velocity": FilterMode.ENFORCE},
            settings={"submission_velocity": {"window_seconds": 60, "flag_threshold": 2, "reject_threshold": 3}},
        )
        deps = FilterDependencies()
        first = build_pipeline(config, deps)
        for index in range(2):
            first.run_pre_filters(at(index, f"sub-{index}"))

        outcome = build_pipeline(config, deps).run_pre_filters(at(3, "sub-3"))

        assert outcome.decided_by == "submission_velocity"


class TestStore:
    def test_old_entries_are_dropped(self):
        store = InMemorySubmissionTimes(retention=timedelta(minutes=10))
        for index in range(5):
            store.record("ann", f"sub-{index}", START + timedelta(minutes=index), START)

        store.record("ann", "late", START + timedelta(hours=1), START)

        assert len(store) == 1

    def test_contributors_who_never_return_are_swept(self):
        """Memory follows active contributors, not everyone who ever submitted."""
        store = InMemorySubmissionTimes(retention=timedelta(minutes=10), sweep_every=10)
        for n in range(9):
            store.record(f"user-{n}", "only", START, START)
        assert len(store) == 9

        # The 10th record triggers the full sweep, an hour later.
        store.record("ann", "late", START + timedelta(hours=1), START)

        assert len(store) == 1

    def test_no_sweep_before_the_interval(self):
        """Between sweeps only the contributor being touched is trimmed."""
        store = InMemorySubmissionTimes(retention=timedelta(minutes=10), sweep_every=100)
        store.record("idle", "only", START, START)

        store.record("ann", "late", START + timedelta(hours=1), START)

        assert len(store) == 2

    def test_a_backlog_arriving_out_of_order_is_counted_and_trimmed(self):
        """Arrival order is not time order when a backlog is replayed. Counts
        and trimming both go by time."""
        store = InMemorySubmissionTimes(retention=timedelta(minutes=10))
        store.record("ann", "newer", START + timedelta(minutes=5), START)
        store.record("ann", "older", START + timedelta(minutes=1), START)
        store.record("ann", "middle", START + timedelta(minutes=3), START)

        assert store.count_within("ann", START + timedelta(minutes=2)) == 2
        assert store.count_within("ann", START + timedelta(minutes=4)) == 1

        store.record("ann", "late", START + timedelta(minutes=14), START)

        # Older (1 min) and middle (3 min) are past the 10 minute retention.
        assert len(store) == 2

    def test_the_boundary_and_the_exclusion(self):
        store = InMemorySubmissionTimes()
        store.record("ann", "a", START, START)
        store.record("ann", "b", START + timedelta(seconds=30), START)

        assert store.count_within("ann", START) == 2  # at-or-after counts the boundary
        assert store.count_within("ann", START, exclude_submission_id="a") == 1
        assert store.count_within("ann", START + timedelta(seconds=1), exclude_submission_id="a") == 1
        assert store.count_within("ann", START, exclude_submission_id="unknown") == 2

    def test_forget_removes_exactly_one_submission(self):
        store = InMemorySubmissionTimes()
        for name in ("a", "b", "c"):
            store.record("ann", name, START, START)  # identical timestamps

        store.forget("ann", "b")

        assert store.count_within("ann", START) == 2
        assert store.record("ann", "b", START, START) == 3  # really gone: this is a new insert

    def test_a_long_history_does_not_change_the_answer(self):
        """A script's own volume is what this filter must not slow down on.
        50,000 retained entries, and the window count is still exact."""
        store = InMemorySubmissionTimes(retention=timedelta(days=1), sweep_every=10**9)
        for i in range(50_000):
            store.record("bot", f"s-{i}", START + timedelta(seconds=i), START)

        last = START + timedelta(seconds=49_999)
        assert store.count_within("bot", last - timedelta(seconds=599)) == 600

    def test_record_returns_the_count_including_itself(self):
        store = InMemorySubmissionTimes()

        assert store.record("ann", "a", START, START) == 1
        assert store.record("ann", "b", START, START) == 2
        assert store.record("ann", "b", START, START) == 2  # a retry
