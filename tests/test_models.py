from datetime import datetime

import pytest

from s2e_validator.prefilters import (
    ExistingImage,
    FilterMode,
    NewImage,
    PipelineConfig,
    PreFilter,
    PreFilterResult,
    Verdict,
)

from conftest import make_interaction


class TestPreFilterResult:
    def test_pass_needs_no_reason_code(self):
        assert PreFilterResult.passed().verdict is Verdict.PASS

    @pytest.mark.parametrize("verdict", [Verdict.REJECT, Verdict.FLAG])
    def test_reject_and_flag_require_reason_code(self, verdict):
        with pytest.raises(ValueError, match="reason_code"):
            PreFilterResult(verdict)

    def test_reason_code_must_be_upper_snake_case(self):
        with pytest.raises(ValueError):
            PreFilterResult.rejected("duplicate image")

    def test_pass_must_not_carry_reason_code(self):
        with pytest.raises(ValueError):
            PreFilterResult(Verdict.PASS, reason_code="DUPLICATE_IMAGE")

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_bounds(self, confidence):
        with pytest.raises(ValueError, match="confidence"):
            PreFilterResult.rejected("TOO_SHORT", confidence=confidence)

    def test_details_are_kept(self):
        result = PreFilterResult.rejected("TOO_SHORT", confidence=1.0, length=3)
        assert result.reason_code == "TOO_SHORT"
        assert result.details == {"length": 3}


class TestPreFilterSubclassing:
    @pytest.mark.parametrize(
        "attrs, missing",
        [({}, "name"), ({"name": "x"}, "cost_rank"), ({"cost_rank": 10}, "name")],
    )
    def test_missing_name_or_cost_rank_fails_at_definition(self, attrs, missing):
        with pytest.raises(TypeError, match=missing):
            type("Incomplete", (PreFilter,), {**attrs, "check": lambda self, i: None})

    def test_complete_subclass_is_accepted(self):
        cls = type(
            "Complete",
            (PreFilter,),
            {"name": "complete", "cost_rank": 10, "check": lambda self, i: None},
        )
        assert cls.name == "complete"


class TestInteraction:
    def test_accepts_new_upload_and_existing_photo(self):
        assert isinstance(make_interaction().image, NewImage)
        assert isinstance(make_interaction(image=ExistingImage("img-1")).image, ExistingImage)

    def test_rejects_naive_timestamp(self):
        with pytest.raises(ValueError, match="timezone"):
            make_interaction(submitted_at=datetime(2026, 9, 14, 12, 0))

    @pytest.mark.parametrize("field", ["submission_id", "campaign_id", "user_id"])
    def test_rejects_empty_ids(self, field):
        with pytest.raises(ValueError, match=field):
            make_interaction(**{field: ""})

    def test_rejects_unknown_image_type(self):
        with pytest.raises(TypeError):
            make_interaction(image="https://example.com/photo.jpg")


class TestPipelineConfig:
    def test_unlisted_filters_default_to_shadow(self):
        assert PipelineConfig().mode_for("anything") is FilterMode.SHADOW

    def test_from_dict(self):
        config = PipelineConfig.from_dict(
            {"default_mode": "off", "filters": {"length_format": "enforce"}}
        )
        assert config.mode_for("length_format") is FilterMode.ENFORCE
        assert config.mode_for("other") is FilterMode.OFF

    def test_from_dict_rejects_invalid_mode(self):
        with pytest.raises(ValueError):
            PipelineConfig.from_dict({"filters": {"length_format": "strict"}})

    def test_from_dict_rejects_unknown_keys(self):
        with pytest.raises(ValueError, match="Unknown"):
            PipelineConfig.from_dict({"filter": {"length_format": "enforce"}})
