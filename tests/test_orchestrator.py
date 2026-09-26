"""Issue #23: the whole stage, wired from config, with real filters."""

import json
from pathlib import Path

import pytest

from s2e_validator.prefilters import (
    FILTER_BUILDERS,
    ExistingImage,
    FilterDependencies,
    FilterMode,
    InMemorySuspicionLog,
    Interaction,
    NewImage,
    PipelineConfig,
    PreFilter,
    PreFilterPipeline,
    PreFilterResult,
    Verdict,
    build_filters,
    build_pipeline,
    load_config,
)

from helpers import CASTLE, ENFORCE_ALL, big_photo, good, recompressed


@pytest.fixture
def pipeline():
    return build_pipeline(ENFORCE_ALL)


class TestCleanSubmission:
    def test_a_real_submission_passes_every_filter(self, pipeline):
        outcome = pipeline.run_pre_filters(good())

        assert outcome.verdict is Verdict.PASS
        assert outcome.proceeds_to_judge
        assert outcome.reason_code is None
        assert len(outcome.records) == len(FILTER_BUILDERS)

    def test_filters_run_cheapest_first(self, pipeline):
        assert pipeline.filter_names == (
            "text_format",
            "image_format",
            "image_exact_duplicate",
            "image_near_duplicate",
            "text_near_duplicate",
        )

    def test_pooled_photo_description_passes(self, pipeline):
        outcome = pipeline.run_pre_filters(good(image=ExistingImage("img-42")))
        assert outcome.verdict is Verdict.PASS


class TestShortCircuit:
    def test_junk_text_stops_before_the_image_is_decoded(self, pipeline):
        outcome = pipeline.run_pre_filters(good(text="aaaaaaaaaa"))

        assert outcome.verdict is Verdict.REJECT
        assert outcome.decided_by == "text_format"
        assert outcome.reason_code == "TEXT_REPEATED_CHARACTERS"
        # Nothing after the deciding filter ran.
        assert [r.filter_name for r in outcome.records] == ["text_format"]

    def test_broken_image_stops_before_hashing(self, pipeline):
        outcome = pipeline.run_pre_filters(good(image=NewImage(b"not a photo" * 500)))

        assert outcome.decided_by == "image_format"
        assert [r.filter_name for r in outcome.records] == ["text_format", "image_format"]

    def test_rejected_submission_claims_nothing(self, pipeline):
        """A submission rejected for its text must be able to come back fixed."""
        rejected = good(submission_id="sub-1", text="aaaaaaaaaa")
        pipeline.run_pre_filters(rejected)

        fixed = good(submission_id="sub-2", text=CASTLE)
        assert pipeline.run_pre_filters(fixed).verdict is Verdict.PASS


class TestDuplicatesAcrossTheStage:
    def test_same_user_resubmitting_one_photo_is_rejected(self, pipeline):
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        outcome = pipeline.run_pre_filters(
            good(submission_id="sub-2", user_id="ann", text="Inny opis tego samego zamku")
        )

        assert outcome.verdict is Verdict.REJECT
        assert outcome.decided_by == "image_exact_duplicate"

    def test_another_users_edited_copy_becomes_a_text_only_flag(self, pipeline):
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        outcome = pipeline.run_pre_filters(
            good(
                submission_id="sub-2",
                user_id="bob",
                image=NewImage(recompressed(big_photo())),
                text="Zupełnie inny opis tego zamku nad Nogatem, napisany samodzielnie",
            )
        )

        assert outcome.verdict is Verdict.PASS  # a flag still reaches the judge
        assert [r.result.reason_code for r in outcome.flags] == ["NEAR_DUPLICATE_IMAGE_TEXT_ONLY"]

    def test_copying_both_photo_and_caption_is_rejected_on_the_text(self, pipeline):
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        outcome = pipeline.run_pre_filters(good(submission_id="sub-2", user_id="bob"))

        assert outcome.verdict is Verdict.REJECT
        assert outcome.decided_by == "text_near_duplicate"

    def test_one_shared_suspicion_log_records_the_pair_once(self):
        """Both duplicate filters see the same match; it is one fact, not two."""
        log = InMemorySuspicionLog()
        pipeline = build_pipeline(ENFORCE_ALL, FilterDependencies(suspicion_log=log))
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        pipeline.run_pre_filters(
            good(
                submission_id="sub-2",
                user_id="bob",
                text="Mój własny opis tego zamku, zupełnie inny od poprzedniego wpisu",
            )
        )

        assert len(log) == 1
        assert log.events[0].duplicate_user_id == "bob"

    def test_a_rejected_copy_leaves_no_fraud_record(self):
        """`check` detects the copy, but the stage rejects the submission, so
        nothing is written: a fraud record has to describe a contribution that
        actually stood, and a corrected resubmission carries a new id."""
        log = InMemorySuspicionLog()
        pipeline = build_pipeline(ENFORCE_ALL, FilterDependencies(suspicion_log=log))
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        # Same photo *and* same caption: rejected on the text.
        outcome = pipeline.run_pre_filters(good(submission_id="sub-2", user_id="bob"))

        assert outcome.verdict is Verdict.REJECT
        assert len(log) == 0


class RejectsOnCommit(PreFilter):
    """Stands in for any filter that only learns the truth at commit time, e.g.
    losing a claim race to a submission that committed a moment earlier."""

    name = "rejects_on_commit"
    cost_rank = 99

    def __init__(self, cost_rank: int = 99, only: str | None = None):
        # Instance attribute shadows the ClassVar, as helpers.StubFilter does.
        self.cost_rank = cost_rank
        #: Reject just this submission id, so an earlier original can stand.
        self._only = only

    def check(self, interaction: Interaction) -> PreFilterResult:
        return PreFilterResult.passed()

    def commit(self, interaction: Interaction) -> PreFilterResult | None:
        if self._only is not None and interaction.submission_id != self._only:
            return None
        return PreFilterResult.rejected("LOST_THE_RACE", confidence=1.0)


def stage_rejecting_on_commit(
    cost_rank: int = 99,
    only: str | None = None,
    deps: FilterDependencies | None = None,
) -> PreFilterPipeline:
    """The real stage plus one filter that rejects when it commits.

    Default 99: every real filter has already committed by then, which is what
    the rollback has to undo.
    """
    deps = deps or FilterDependencies()
    return PreFilterPipeline(
        build_filters(ENFORCE_ALL, deps) + [RejectsOnCommit(cost_rank, only)], ENFORCE_ALL
    )


class TestCommitTimeRejectClaimsNothing:
    """The guarantee is "a rejected submission claims nothing". A REJECT during
    `commit` has to honour it too, or the loser of a race is locked out of its
    own corrected resubmission."""

    @pytest.fixture
    def pipeline(self):
        return stage_rejecting_on_commit()

    def test_the_photo_can_be_submitted_again(self, pipeline):
        rejected = good(submission_id="sub-1", user_id="ann")
        assert pipeline.run_pre_filters(rejected).verdict is Verdict.REJECT

        again = good(submission_id="sub-2", user_id="ann")
        outcome = pipeline.run_pre_filters(again)

        assert outcome.verdict is Verdict.REJECT
        # Rejected again by the same stub, not by a leftover claim of its own.
        assert outcome.decided_by == "rejects_on_commit"

    def test_another_user_is_not_told_the_photo_is_taken(self, pipeline):
        pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann"))

        outcome = pipeline.run_pre_filters(good(submission_id="sub-2", user_id="bob"))

        flagged = [r.result.reason_code for r in outcome.records if r.result is not None]
        assert "DUPLICATE_IMAGE_TEXT_ONLY" not in flagged
        assert "NEAR_DUPLICATE_IMAGE_TEXT_ONLY" not in flagged

    def test_a_copy_rejected_at_commit_leaves_no_fraud_record(self):
        """Both duplicate filters record Bob's copy during their commit. The
        stage then rejects him, so the record has to go too: his corrected
        resubmission comes back under a new id and would count the same photo
        a second time."""
        log = InMemorySuspicionLog()
        pipeline = stage_rejecting_on_commit(
            only="sub-2", deps=FilterDependencies(suspicion_log=log)
        )
        assert pipeline.run_pre_filters(good(submission_id="sub-1", user_id="ann")).verdict is (
            Verdict.PASS
        )

        outcome = pipeline.run_pre_filters(
            good(
                submission_id="sub-2",
                user_id="bob",
                text="Mój własny opis tego zamku, zupełnie inny od poprzedniego wpisu",
            )
        )

        assert outcome.decided_by == "rejects_on_commit"
        assert len(log) == 0

    def test_the_later_filters_check_results_survive(self):
        """Rejecting mid-commit skips the later filters' commit, but their check
        already ran. Dropping those records would hide exactly what shadow mode
        exists to collect."""
        # Rank 25: after image_exact_duplicate (20), before both near-duplicate
        # filters (30, 32), so there are later records to keep.
        outcome = stage_rejecting_on_commit(cost_rank=25).run_pre_filters(good())

        assert outcome.verdict is Verdict.REJECT
        assert outcome.decided_by == "rejects_on_commit"
        assert len(outcome.records) == len(FILTER_BUILDERS) + 1
        assert [r.filter_name for r in outcome.records][-2:] == [
            "image_near_duplicate",
            "text_near_duplicate",
        ]


class TestStateSurvivesAConfigReload:
    def test_rebuilding_with_the_same_dependencies_keeps_the_windows(self):
        """Retuning a threshold rebuilds every filter. Fresh windows would
        silently disable near-duplicate detection for a whole retention
        period."""
        deps = FilterDependencies()
        build_pipeline(ENFORCE_ALL, deps).run_pre_filters(
            good(submission_id="sub-1", user_id="ann")
        )

        retuned = build_pipeline(ENFORCE_ALL, deps)
        outcome = retuned.run_pre_filters(
            good(
                submission_id="sub-2",
                user_id="ann",
                image=NewImage(recompressed(big_photo())),
                text="Inny opis tego samego zamku nad Nogatem, pisany osobno",
            )
        )

        assert outcome.verdict is Verdict.REJECT
        assert outcome.decided_by == "image_near_duplicate"

    def test_a_reload_keeps_the_exact_duplicate_claims(self):
        """A byte-identical resubmission only the exact filter can catch: the
        near-duplicate test above never reaches it."""
        deps = FilterDependencies()
        build_pipeline(ENFORCE_ALL, deps).run_pre_filters(
            good(submission_id="sub-1", user_id="ann")
        )

        outcome = build_pipeline(ENFORCE_ALL, deps).run_pre_filters(
            good(submission_id="sub-2", user_id="ann")
        )

        assert outcome.decided_by == "image_exact_duplicate"


class TestConfigControlsEverything:
    def test_a_filter_can_be_switched_off(self):
        pipeline = build_pipeline(
            PipelineConfig(
                default_mode=FilterMode.ENFORCE, modes={"text_format": FilterMode.OFF}
            )
        )

        outcome = pipeline.run_pre_filters(good(text="aaaaaaaaaa"))

        assert outcome.verdict is Verdict.PASS
        assert "text_format" not in [r.filter_name for r in outcome.records]

    def test_a_filter_can_be_shadowed_instead_of_enforcing(self):
        pipeline = build_pipeline(
            PipelineConfig(
                default_mode=FilterMode.ENFORCE, modes={"text_format": FilterMode.SHADOW}
            )
        )

        outcome = pipeline.run_pre_filters(good(text="aaaaaaaaaa"))

        assert outcome.verdict is Verdict.PASS
        assert [r.filter_name for r in outcome.shadow_rejections] == ["text_format"]
        # Shadow does not short-circuit: the rest of the stage still ran.
        assert len(outcome.records) == len(FILTER_BUILDERS)

    def test_everything_defaults_to_shadow(self):
        """A fresh config can never reject a real contributor."""
        outcome = build_pipeline().run_pre_filters(good(text="aaaaaaaaaa"))

        assert outcome.verdict is Verdict.PASS
        assert [r.filter_name for r in outcome.shadow_rejections] == ["text_format"]

    def test_thresholds_come_from_config(self):
        pipeline = build_pipeline(
            PipelineConfig(
                default_mode=FilterMode.ENFORCE,
                settings={"text_format": {"min_chars": 200}},
            )
        )

        outcome = pipeline.run_pre_filters(good())

        assert outcome.reason_code == "TEXT_TOO_SHORT"

    def test_config_naming_an_unknown_filter_fails_loudly(self):
        with pytest.raises(ValueError, match="unknown filters"):
            build_filters(PipelineConfig(modes={"face_detection": FilterMode.ENFORCE}))

    def test_the_shipped_example_config_builds_a_pipeline(self):
        """The example in the README is loaded here, so it cannot rot."""
        path = Path(__file__).parent.parent / "config" / "example_campaign.json"

        pipeline = build_pipeline(load_config(path))

        assert len(pipeline.filter_names) == len(FILTER_BUILDERS)
        assert pipeline.run_pre_filters(good()).verdict is Verdict.PASS

    def test_a_config_file_drives_the_stage(self, tmp_path):
        path = tmp_path / "campaign.json"
        path.write_text(
            json.dumps(
                {
                    "default_mode": "shadow",
                    "filters": {
                        "image_format": "enforce",
                        "text_format": {"mode": "enforce", "settings": {"min_chars": 12}},
                    },
                }
            ),
            encoding="utf-8",
        )

        pipeline = build_pipeline(load_config(path))

        assert pipeline.run_pre_filters(good(text="Zamek")).reason_code == "TEXT_TOO_SHORT"
        assert pipeline.run_pre_filters(good()).verdict is Verdict.PASS
