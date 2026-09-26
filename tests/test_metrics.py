"""Issue #24: what the stage did, including what shadow filters would have done."""

import dataclasses
import logging
import threading

import pytest

from s2e_validator.prefilters import (
    AnomalySettings,
    FilterMode,
    NewImage,
    PipelineConfig,
    PreFilterPipeline,
    PreFilterResult,
    StageMetrics,
    Verdict,
    build_pipeline,
    error_rate_monitor,
    reject_rate_monitor,
    render_snapshot,
)
from s2e_validator.prefilters.metrics import FLAG, REJECT, SHADOW

from helpers import ENFORCE_ALL, StubFilter, big_photo, good

#: Genuinely different submissions: distinct contributor, photo and caption, so
#: nothing trips the duplicate filters. `clean()` asserts it, so a collision
#: fails loudly instead of quietly changing what a test measures.
CLEAN_TEXTS = [
    "Zamek w Malborku o wschodzie slonca, widok od strony Nogatu",
    "Pierogi ruskie z cebulka na niedzielnym obiedzie u babci",
    "Fontanna Neptuna na Dlugim Targu, turysci robia zdjecia",
    "Kosciol Mariacki w Krakowie, hejnal z wiezy o pelnej godzinie",
    "Kluski slaskie z sosem pieczeniowym i modra kapusta",
    "Palac Kultury i Nauki widziany z ulicy Marszalkowskiej wieczorem",
    "Tatry zima, szlak na Kasprowy Wierch pod swiezym sniegiem",
    "Molo w Sopocie o zachodzie, mewy nad spokojnym Baltykiem",
    "Rynek we Wroclawiu, kolorowe kamienice i krasnal przy fontannie",
    "Zupa pomidorowa z makaronem w bialym talerzu na stole",
    "Most Poniatowskiego noca, swiatla odbijaja sie w Wisle",
    "Drewniany kosciol w Debnie, gont porosniety mchem",
]


def clean(pipeline, index: int):
    outcome = pipeline.run_pre_filters(
        good(
            submission_id=f"clean-{index}",
            user_id=f"user-{index}",
            image=NewImage(big_photo(100 + index)),
            text=CLEAN_TEXTS[index],
        )
    )
    assert outcome.verdict is Verdict.PASS and not outcome.flags, outcome.records
    return outcome


def junk(pipeline, index: int):
    return pipeline.run_pre_filters(good(submission_id=f"junk-{index}", text="aaaaaaaaaa"))


def stats_for(snapshot, name):
    return next(s for s in snapshot.filters if s.name == name)


def raiser():
    raise RuntimeError("store unavailable")


class CommitCrashes(StubFilter):
    """`check` answers normally; `commit` then crashes."""

    def commit(self, interaction):
        raise RuntimeError("disk full")


class CheckCrashesCommitRevises(StubFilter):
    """`check` crashes; `commit` still returns a revised verdict."""

    def commit(self, interaction):
        return PreFilterResult.flagged("LATE_FLAG")


@pytest.fixture
def metrics() -> StageMetrics:
    return StageMetrics()


@pytest.fixture
def pipeline(metrics):
    return build_pipeline(ENFORCE_ALL, metrics=metrics)


class TestCounts:
    def test_a_clean_submission_counts_as_a_pass_everywhere(self, pipeline, metrics):
        pipeline.run_pre_filters(good())

        snapshot = metrics.snapshot()
        assert (snapshot.submissions, snapshot.passed, snapshot.rejected) == (1, 1, 0)
        assert stats_for(snapshot, "text_format").passed == 1

    def test_a_rejection_is_attributed_to_the_deciding_filter(self, pipeline, metrics):
        junk(pipeline, 0)

        snapshot = metrics.snapshot()
        assert snapshot.rejected == 1
        text = stats_for(snapshot, "text_format")
        assert text.rejected == 1
        assert text.reason_codes[(REJECT, "TEXT_REPEATED_CHARACTERS")] == 1
        # Short-circuit: later filters never saw this submission.
        assert [s.name for s in snapshot.filters] == ["text_format"]

    def test_flags_are_counted_without_being_rejections(self, pipeline, metrics):
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        pipeline.run_pre_filters(
            good(
                submission_id="sub-2",
                user_id="bob",
                text="Mój własny opis tego zamku, zupełnie inny od poprzedniego wpisu",
            )
        )

        snapshot = metrics.snapshot()
        assert snapshot.rejected == 0
        assert snapshot.flagged == 1
        exact = stats_for(snapshot, "image_exact_duplicate")
        assert exact.flagged == 1
        assert exact.reason_codes[(FLAG, "DUPLICATE_IMAGE_TEXT_ONLY")] == 1

    def test_reject_rate_and_judge_calls_saved(self, pipeline, metrics):
        for index in range(3):
            junk(pipeline, index)
        pipeline.run_pre_filters(good(submission_id="clean"))

        snapshot = metrics.snapshot()
        assert snapshot.reject_rate == 0.75
        assert snapshot.judge_calls_saved == 3
        assert snapshot.judge_cost_saved_usd == pytest.approx(3 * 0.004)

    def test_a_crashing_filter_is_counted_as_an_error(self, metrics):
        pipeline = PreFilterPipeline([StubFilter("crashy", 10, raiser)], ENFORCE_ALL, metrics)

        outcome = pipeline.run_pre_filters(good())

        snapshot = metrics.snapshot()
        assert outcome.verdict is Verdict.PASS  # fail open
        assert snapshot.errors == 1
        assert stats_for(snapshot, "crashy").errors == 1

    def test_a_verdict_survives_a_crashed_commit(self, metrics):
        """A shadow REJECT from check, then a crash in commit: both count. The
        shadow evidence is what this module exists to collect."""
        rejects = CommitCrashes("crashy", 10, PreFilterResult.rejected("SUSPICIOUS"))
        config = PipelineConfig(default_mode=FilterMode.SHADOW)
        outcome = PreFilterPipeline([rejects], config, metrics).run_pre_filters(good())

        crashy = stats_for(metrics.snapshot(), "crashy")
        assert (crashy.errors, crashy.would_reject) == (1, 1)
        assert crashy.reason_codes[(SHADOW, "SUSPICIOUS")] == 1
        # Filter row and stage summary agree about the shadow rejection.
        assert len(outcome.shadow_rejections) == 1


class TestTheSnapshotIsACopy:
    def test_a_snapshot_never_changes_afterwards(self, pipeline, metrics):
        junk(pipeline, 0)
        before = metrics.snapshot()
        as_dict = before.to_dict()

        for index in range(1, 4):
            junk(pipeline, index)

        text = stats_for(before, "text_format")
        assert (before.submissions, text.checked, text.rejected) == (1, 1, 1)
        assert text.reason_codes[(REJECT, "TEXT_REPEATED_CHARACTERS")] == 1
        assert before.to_dict() == as_dict

    def test_it_cannot_be_edited(self, pipeline, metrics):
        junk(pipeline, 0)
        text = stats_for(metrics.snapshot(), "text_format")

        with pytest.raises(dataclasses.FrozenInstanceError):
            text.rejected = 99
        with pytest.raises(TypeError):
            text.reason_codes[(REJECT, "X")] = 1


class TestShadowVisibility:
    def test_shadow_rejections_are_reported_separately(self, metrics):
        pipeline = build_pipeline(
            PipelineConfig(
                default_mode=FilterMode.ENFORCE, modes={"text_format": FilterMode.SHADOW}
            ),
            metrics=metrics,
        )

        junk(pipeline, 0)

        snapshot = metrics.snapshot()
        text = stats_for(snapshot, "text_format")
        assert snapshot.rejected == 0  # nothing was actually blocked
        assert (text.rejected, text.would_reject) == (0, 1)
        assert text.reason_codes[(SHADOW, "TEXT_REPEATED_CHARACTERS")] == 1
        assert (REJECT, "TEXT_REPEATED_CHARACTERS") not in text.reason_codes
        assert text.reject_rate == 1.0

    def test_a_filter_that_is_off_never_appears(self, metrics):
        pipeline = build_pipeline(
            PipelineConfig(default_mode=FilterMode.ENFORCE, modes={"text_format": FilterMode.OFF}),
            metrics=metrics,
        )

        pipeline.run_pre_filters(good())

        assert "text_format" not in [s.name for s in metrics.snapshot().filters]


class TestLatency:
    def test_time_is_recorded_per_filter_and_per_stage(self, pipeline, metrics):
        pipeline.run_pre_filters(good())

        snapshot = metrics.snapshot()
        image = stats_for(snapshot, "image_format")
        assert image.mean_ms > 0
        assert image.max_ms >= image.mean_ms
        assert snapshot.mean_stage_ms > 0


class TestReviewableOutput:
    def test_the_table_shows_every_filter_and_the_summary(self, pipeline, metrics):
        pipeline.run_pre_filters(good())
        junk(pipeline, 0)

        rendered = render_snapshot(metrics.snapshot())

        assert "text_format" in rendered
        assert "judge_calls_saved=1" in rendered
        assert "reject_rate=50.0%" in rendered

    def test_reason_codes_serialise_by_what_they_did(self, pipeline, metrics):
        junk(pipeline, 0)

        payload = metrics.snapshot().to_dict()

        assert payload["rejected"] == 1
        assert payload["filters"][0]["reason_codes"] == {
            "reject": {"TEXT_REPEATED_CHARACTERS": 1},
            "shadow": {},
            "flag": {},
        }


class TestAMetricsSinkCannotBreakTheStage:
    class Exploding:
        def observe(self, result):
            raise RuntimeError("dashboard down")

    def test_the_result_still_comes_back(self, caplog):
        pipeline = build_pipeline(ENFORCE_ALL, metrics=self.Exploding())

        with caplog.at_level(logging.ERROR):
            outcome = pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        assert outcome.verdict is Verdict.PASS
        assert "prefilter_metrics_error" in caplog.text

    def test_the_claims_it_made_still_count(self):
        """The photo was claimed before the sink ran; the next copy must
        still meet that claim."""
        pipeline = build_pipeline(ENFORCE_ALL, metrics=self.Exploding())
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        again = pipeline.run_pre_filters(good(submission_id="sub-2", user_id="ann", text="Inny opis zamku nad rzeka"))

        assert again.decided_by == "image_exact_duplicate"


class TestPipelineKeepsErrors:
    def test_a_check_crash_survives_a_commit_revision(self):
        filters = [CheckCrashesCommitRevises("crashy", 10, raiser)]

        [record] = PreFilterPipeline(filters, ENFORCE_ALL).run_pre_filters(good()).records

        assert record.error == "RuntimeError: store unavailable"
        assert record.result.reason_code == "LATE_FLAG"

    def test_a_crash_in_both_halves_keeps_both(self):
        class BothCrash(StubFilter):
            def commit(self, interaction):
                raise OSError("disk full")

        [record] = PreFilterPipeline([BothCrash("crashy", 10, raiser)], ENFORCE_ALL).run_pre_filters(
            good()
        ).records

        assert record.error == "RuntimeError: store unavailable; commit: OSError: disk full"


class TestRejectRateAlert:
    SETTINGS = AnomalySettings(window=10, max_rate=0.5, min_submissions=4)

    def stage(self, config=ENFORCE_ALL):
        metrics = StageMetrics(reject_alert=reject_rate_monitor(self.SETTINGS))
        return build_pipeline(config, metrics=metrics)

    def test_a_spike_warns_once(self, caplog):
        pipeline = self.stage()

        with caplog.at_level(logging.WARNING):
            for index in range(6):
                junk(pipeline, index)

        assert caplog.text.count("prefilter_reject_rate_spike") == 1

    def test_it_fires_in_the_all_shadow_default(self, caplog):
        """Nothing is ever really rejected in shadow mode, which is exactly
        when a filter gone wrong would otherwise be invisible."""
        pipeline = self.stage(PipelineConfig())

        with caplog.at_level(logging.WARNING):
            for index in range(6):
                assert junk(pipeline, index).verdict is Verdict.PASS

        assert caplog.text.count("prefilter_reject_rate_spike") == 1

    def test_no_alert_below_the_ceiling(self, caplog):
        pipeline = self.stage()

        with caplog.at_level(logging.WARNING):
            for index in range(6):
                clean(pipeline, index)

        assert "prefilter_reject_rate_spike" not in caplog.text

    def test_the_minimum_is_respected_at_its_boundary(self, caplog):
        metrics = StageMetrics(
            reject_alert=reject_rate_monitor(AnomalySettings(window=10, max_rate=0.1, min_submissions=8))
        )
        pipeline = build_pipeline(ENFORCE_ALL, metrics=metrics)

        with caplog.at_level(logging.WARNING):
            for index in range(7):
                junk(pipeline, index)
            assert "prefilter_reject_rate_spike" not in caplog.text

            junk(pipeline, 7)

        assert "prefilter_reject_rate_spike" in caplog.text

    def test_recovery_is_logged(self, caplog):
        pipeline = self.stage()
        for index in range(6):
            junk(pipeline, index)

        with caplog.at_level(logging.INFO):
            for index in range(10):
                clean(pipeline, index)

        assert "prefilter_reject_rate_recovered" in caplog.text


class TestErrorRateAlert:
    def test_crashing_filters_raise_their_own_alarm(self, caplog):
        """A crash fails open into a PASS, so the reject rate goes *down*.
        Only a monitor on errors sees it."""
        metrics = StageMetrics(
            error_alert=error_rate_monitor(AnomalySettings(window=10, max_rate=0.2, min_submissions=5))
        )
        pipeline = PreFilterPipeline([StubFilter("crashy", 10, raiser)], ENFORCE_ALL, metrics)

        with caplog.at_level(logging.WARNING):
            for index in range(5):
                pipeline.run_pre_filters(good(submission_id=f"sub-{index}"))

        assert "prefilter_error_rate_spike" in caplog.text
        assert "prefilter_reject_rate_spike" not in caplog.text


class TestAlertsNeverBlockTheCollector:
    def test_a_log_handler_may_read_the_metrics(self):
        """Attaching the current table to an alert is natural. With the alert
        logged under the collector's lock, the handler below would deadlock."""
        metrics = StageMetrics(
            reject_alert=reject_rate_monitor(AnomalySettings(window=4, max_rate=0.5, min_submissions=2))
        )
        pipeline = build_pipeline(ENFORCE_ALL, metrics=metrics)
        seen = []

        class AttachTable(logging.Handler):
            def emit(self, record):
                if "spike" in record.getMessage():
                    seen.append(render_snapshot(metrics.snapshot()))

        handler = AttachTable(level=logging.WARNING)
        alerts = logging.getLogger("s2e_validator.prefilters.metrics")
        alerts.addHandler(handler)
        try:
            worker = threading.Thread(target=lambda: [junk(pipeline, i) for i in range(3)], daemon=True)
            worker.start()
            worker.join(timeout=10)
            assert not worker.is_alive(), "observe() deadlocked on its own alert"
        finally:
            alerts.removeHandler(handler)

        assert len(seen) == 1 and "text_format" in seen[0]


class TestAnomalySettings:
    @pytest.mark.parametrize(
        "fields, message",
        [
            ({"window": 0}, "window"),
            ({"window": True}, "window"),
            ({"max_rate": 1.0}, "below 1"),
            ({"max_rate": 1.5}, "below 1"),
            ({"max_rate": -0.1}, "at least 0"),
            ({"window": 10, "min_submissions": 50}, "1 to window"),
            ({"min_submissions": 0}, "1 to window"),
        ],
    )
    def test_settings_that_could_never_fire_are_refused(self, fields, message):
        with pytest.raises(ValueError, match=message):
            AnomalySettings(**fields)
