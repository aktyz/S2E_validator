import logging

import pytest

from s2e_validator.prefilters import (
    FilterMode,
    NoOpFilter,
    PipelineConfig,
    PreFilterPipeline,
    PreFilterResult,
    Verdict,
)

from conftest import StubFilter

PASS = PreFilterResult.passed()
REJECT = PreFilterResult.rejected("DUPLICATE_IMAGE", confidence=1.0)
FLAG = PreFilterResult.flagged("LANGUAGE_MISMATCH", confidence=0.6)


def enforce(*names: str) -> PipelineConfig:
    return PipelineConfig(modes={name: FilterMode.ENFORCE for name in names})


def test_noop_filter_smoke(interaction):
    pipeline = PreFilterPipeline([NoOpFilter()], enforce("noop"))

    outcome = pipeline.run_pre_filters(interaction)

    assert outcome.verdict is Verdict.PASS
    assert outcome.proceeds_to_judge
    assert outcome.reason_code is None
    assert [r.filter_name for r in outcome.records] == ["noop"]


def test_existing_photo_submission_flows_through(existing_image_interaction):
    outcome = PreFilterPipeline([NoOpFilter()]).run_pre_filters(existing_image_interaction)
    assert outcome.verdict is Verdict.PASS


def test_empty_pipeline_passes(interaction):
    assert PreFilterPipeline([]).run_pre_filters(interaction).verdict is Verdict.PASS


def test_filters_run_cheapest_first(interaction):
    expensive = StubFilter("expensive", 40, PASS)
    cheap = StubFilter("cheap", 10, PASS)
    middle = StubFilter("middle", 20, PASS)

    pipeline = PreFilterPipeline([expensive, cheap, middle])

    assert pipeline.filter_names == ("cheap", "middle", "expensive")


def test_enforced_reject_short_circuits(interaction):
    first = StubFilter("first", 10, REJECT)
    later = StubFilter("later", 20, PASS)
    pipeline = PreFilterPipeline([first, later], enforce("first", "later"))

    outcome = pipeline.run_pre_filters(interaction)

    assert outcome.verdict is Verdict.REJECT
    assert not outcome.proceeds_to_judge
    assert outcome.reason_code == "DUPLICATE_IMAGE"
    assert outcome.decided_by == "first"
    assert later.calls == 0


def test_shadow_reject_is_recorded_but_does_not_block(interaction, caplog):
    shadowed = StubFilter("shadowed", 10, REJECT)
    later = StubFilter("later", 20, PASS)
    pipeline = PreFilterPipeline([shadowed, later])  # default mode is shadow

    with caplog.at_level(logging.INFO):
        outcome = pipeline.run_pre_filters(interaction)

    assert outcome.verdict is Verdict.PASS
    assert [r.filter_name for r in outcome.shadow_rejections] == ["shadowed"]
    assert later.calls == 1
    assert "prefilter_shadow_reject filter=shadowed" in caplog.text


def test_off_filter_does_not_run(interaction):
    disabled = StubFilter("disabled", 10, REJECT)
    pipeline = PreFilterPipeline(
        [disabled], PipelineConfig(modes={"disabled": FilterMode.OFF})
    )

    outcome = pipeline.run_pre_filters(interaction)

    assert outcome.verdict is Verdict.PASS
    assert disabled.calls == 0
    assert outcome.records == ()


def test_modes_are_independent_per_filter(interaction):
    shadowed = StubFilter("shadowed", 10, REJECT)
    enforced = StubFilter("enforced", 20, REJECT)
    pipeline = PreFilterPipeline([shadowed, enforced], enforce("enforced"))

    outcome = pipeline.run_pre_filters(interaction)

    assert outcome.decided_by == "enforced"
    assert [r.filter_name for r in outcome.shadow_rejections] == ["shadowed"]


def test_flags_continue_and_are_collected(interaction):
    flagger = StubFilter("language", 10, FLAG)
    later = StubFilter("later", 20, PASS)
    pipeline = PreFilterPipeline([flagger, later], enforce("language", "later"))

    outcome = pipeline.run_pre_filters(interaction)

    assert outcome.verdict is Verdict.PASS
    assert later.calls == 1
    assert [r.result.reason_code for r in outcome.flags] == ["LANGUAGE_MISMATCH"]


def test_crashing_filter_fails_open(interaction):
    def boom() -> PreFilterResult:
        raise RuntimeError("hash store unavailable")

    broken = StubFilter("broken", 10, boom)
    later = StubFilter("later", 20, PASS)
    pipeline = PreFilterPipeline([broken, later], enforce("broken", "later"))

    outcome = pipeline.run_pre_filters(interaction)

    assert outcome.verdict is Verdict.PASS
    assert later.calls == 1
    assert outcome.records[0].error == "RuntimeError: hash store unavailable"


def test_duplicate_filter_names_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        PreFilterPipeline([StubFilter("same", 10, PASS), StubFilter("same", 20, PASS)])
