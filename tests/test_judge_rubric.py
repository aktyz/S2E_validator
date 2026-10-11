"""The rubric (#43, J1): scoring, and refusing a bad config at load."""

from __future__ import annotations

import pytest

from s2e_validator.judge import (
    RUBRIC_V1,
    Criterion,
    Provenance,
    Rubric,
    prompt_version,
)

from helpers import photo

class TestRubricScore:
    def test_the_ends_of_the_scale(self):
        assert RUBRIC_V1.score({n: 5 for n in RUBRIC_V1.criterion_names}) == 100
        assert RUBRIC_V1.score({n: 1 for n in RUBRIC_V1.criterion_names}) == 0

    def test_weights_count(self):
        """Relevance weighs double: an accurate but brief description outscores
        a long, effortful one about something else."""
        accurate = {"relevance": 5, "specificity": 3, "effort": 3}
        effortful = {"relevance": 3, "specificity": 3, "effort": 5}

        assert RUBRIC_V1.score(accurate) > RUBRIC_V1.score(effortful)

    def test_threshold_decides_rewardable(self):
        assert RUBRIC_V1.is_rewardable(40, ())
        assert not RUBRIC_V1.is_rewardable(39, ())

    @pytest.mark.parametrize("code", sorted(RUBRIC_V1.blocking_reasons))
    def test_a_blocking_reason_overrides_any_score(self, code):
        assert not RUBRIC_V1.is_rewardable(100, (code,))

    def test_a_soft_reason_does_not_block(self):
        assert RUBRIC_V1.is_rewardable(60, ("GENERIC_DESCRIPTION",))

    @pytest.mark.parametrize(
        "ratings, score",
        [
            ({"a": 3, "b": 4}, 63),  # exactly 62.5: Python's round() would say 62
            ({"a": 4, "b": 5}, 88),  # exactly 87.5
            ({"a": 1, "b": 2}, 13),  # exactly 12.5: round() would say 12
            ({"a": 3, "b": 3}, 50),
        ],
    )
    def test_a_half_always_rounds_up(self, ratings, score):
        even = Rubric.from_dict({**TestRubricConfig.RAW, "criteria": [
            {"name": "a", "question": "q"}, {"name": "b", "question": "q"},
        ]})

        assert even.score(ratings) == score

    def test_float_weights_do_not_tip_a_half(self):
        """0.1 and 0.3 are not exact in binary; the score is computed as if they were."""
        rubric = Rubric.from_dict({**TestRubricConfig.RAW, "criteria": [
            {"name": "a", "question": "q", "weight": 0.1},
            {"name": "b", "question": "q", "weight": 0.3},
        ]})

        # (0.1 * 2 + 0.3 * 0) / (0.4 * 4) = 12.5 exactly
        assert rubric.score({"a": 3, "b": 1}) == 13


class TestRubricConfig:
    RAW = {
        "version": "demo-v1",
        "campaign_brief": "Describe the photo.",
        "criteria": [
            {"name": "relevance", "question": "Does it match?", "weight": 2},
            {"name": "effort", "question": "Is it a real attempt?"},
        ],
        "reasons": {"LOW_EFFORT": "Minimal.", "INSTRUCTIONS_TO_JUDGE": "Talks to the grader."},
        "blocking_reasons": ["INSTRUCTIONS_TO_JUDGE"],
        "min_rewardable_score": 50,
    }

    def test_loads_from_config(self):
        rubric = Rubric.from_dict(self.RAW)

        assert rubric.criterion_names == ("relevance", "effort")
        assert rubric.min_rewardable_score == 50
        assert rubric.blocking_reasons == {"INSTRUCTIONS_TO_JUDGE"}

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"min_score": 3}, "Unknown rubric keys"),
            ({"criteria": []}, "at least one criterion"),
            ({"criteria": [{"name": "a", "question": "q"}, {"name": "a", "question": "q"}]}, "named twice"),
            ({"criteria": [{"name": "Relevance", "question": "q"}]}, "snake_case"),
            ({"criteria": [{"name": "a", "question": "q", "weight": 0}]}, "positive"),
            ({"criteria": [{"name": "a", "question": "q", "colour": "red"}]}, "name, question"),
            ({"blocking_reasons": ["NOT_IN_VOCABULARY"]}, "not in the vocabulary"),
            ({"blocking_reasons": "INSTRUCTIONS_TO_JUDGE"}, "list of strings"),
            ({"blocking_reasons": [7]}, "only strings"),
            ({"criteria": [{"name": "a", "question": "q", "weight": float("nan")}]}, "finite"),
            ({"criteria": [{"name": "a", "question": "q", "weight": float("inf")}]}, "finite"),
            ({"blocking_reasons": []}, "INSTRUCTIONS_TO_JUDGE must be a blocking reason"),
            ({"reasons": {"LOW_EFFORT": "Minimal."}, "blocking_reasons": []}, "INSTRUCTIONS_TO_JUDGE must be"),
            ({"campaign_brief": ""}, "campaign_brief must be non-empty text"),
            ({"version": None}, "version must be non-empty text"),
            ({"criteria": [{"name": "a"}]}, "name, question"),
            ({"criteria": [{"name": "a", "question": 7}]}, "a.question must be non-empty text"),
            ({"criteria": [{"name": 7, "question": "q"}]}, "criterion name must be non-empty text"),
            ({"reasons": {"INSTRUCTIONS_TO_JUDGE": 7}}, "the meaning of INSTRUCTIONS_TO_JUDGE"),
            ({"reasons": {7: "x"}}, "UPPER_SNAKE_CASE"),
            ({"reasons": {"low effort": "x"}}, "UPPER_SNAKE_CASE"),
            ({"min_rewardable_score": 101}, "at most 100"),
            ({"min_rewardable_score": "40"}, "whole number"),
            ({"version": ""}, "version"),
        ],
    )
    def test_bad_config_fails_at_load(self, change, message):
        with pytest.raises(ValueError, match=message):
            Rubric.from_dict({**self.RAW, **change})

    def test_a_missing_brief_is_refused(self):
        raw = {k: v for k, v in self.RAW.items() if k != "campaign_brief"}

        with pytest.raises(ValueError, match="campaign_brief"):
            Rubric.from_dict(raw)

    def test_the_vocabulary_cannot_change_after_the_rubric_is_built(self):
        """Provenance stamps a prompt version once; the reasons behind it must
        not move afterwards, through the rubric or through the caller's dict."""
        mine = {"INSTRUCTIONS_TO_JUDGE": "Talks to the grader."}
        rubric = Rubric.from_dict({**self.RAW, "reasons": mine})
        before = prompt_version(rubric)

        with pytest.raises(TypeError):
            rubric.reasons["NEW_CODE"] = "added later"
        mine["NEW_CODE"] = "added later"

        assert "NEW_CODE" not in rubric.reasons
        assert prompt_version(rubric) == before


def test_criterion_rejects_an_empty_question():
    with pytest.raises(ValueError, match="relevance.question must be non-empty text"):
        Criterion("relevance", "   ")


def test_a_rubric_built_in_code_gets_the_same_checks_as_config():
    with pytest.raises(ValueError, match="INSTRUCTIONS_TO_JUDGE must be a blocking reason"):
        Rubric(
            version="v1",
            campaign_brief="Describe the photo.",
            criteria=(Criterion("relevance", "Does it match?"),),
            reasons={"INSTRUCTIONS_TO_JUDGE": "Talks to the grader."},
            blocking_reasons=frozenset(),
        )
