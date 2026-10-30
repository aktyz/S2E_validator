"""Two judges, and a submission is paid only if both would pay it."""

from __future__ import annotations

import pytest

from s2e_validator.judge import (
    HOLD_JUDGE_UNAVAILABLE,
    REASON_JUDGES_DISAGREE,
    RUBRIC_V1,
    JudgeStatus,
    ModelJudge,
    StubJudge,
    UnanimousJudge,
)

from helpers import ScriptedClient, item, reply

GOOD = {"relevance": 4, "specificity": 3, "effort": 3}
POOR = {"relevance": 1, "specificity": 1, "effort": 1}


def judge(name: str, *replies) -> ModelJudge:
    client = ScriptedClient(*replies)
    client.name = name
    return ModelJudge(client, RUBRIC_V1)


class TestPaying:
    def test_paid_when_both_would_pay(self):
        verdict = UnanimousJudge([judge("qwen", reply(GOOD)), judge("nemotron", reply(GOOD))]).judge(item())

        assert verdict.status is JudgeStatus.SCORED
        assert verdict.is_rewardable
        assert REASON_JUDGES_DISAGREE not in verdict.reason_codes

    @pytest.mark.parametrize("first, second", [(GOOD, POOR), (POOR, GOOD)])
    def test_either_judge_can_veto(self, first, second):
        verdict = UnanimousJudge([judge("qwen", reply(first)), judge("nemotron", reply(second))]).judge(item())

        assert not verdict.is_rewardable
        assert verdict.reason_codes[-1] == REASON_JUDGES_DISAGREE

    def test_a_blocking_reason_from_the_second_judge_vetoes(self):
        both = UnanimousJudge([
            judge("qwen", reply(GOOD)),
            judge("nemotron", reply(GOOD, reason_codes=["DESCRIPTION_MISMATCH"])),
        ])

        verdict = both.judge(item())

        assert not verdict.is_rewardable
        assert "DESCRIPTION_MISMATCH" in verdict.reason_codes


class TestTheVerdict:
    def test_the_first_judge_gives_the_score_and_ratings(self):
        verdict = UnanimousJudge([judge("qwen", reply(GOOD)), judge("nemotron", reply(POOR))]).judge(item())

        assert verdict.criteria == GOOD
        assert verdict.quality_score == RUBRIC_V1.score(GOOD)

    def test_reasons_from_both_once_each(self):
        both = UnanimousJudge([
            judge("qwen", reply(GOOD, reason_codes=["LOW_EFFORT"])),
            judge("nemotron", reply(GOOD, reason_codes=["LOW_EFFORT", "GENERIC_DESCRIPTION"])),
        ])

        assert both.judge(item()).reason_codes == ("LOW_EFFORT", "GENERIC_DESCRIPTION")

    def test_provenance_names_every_model(self):
        verdict = UnanimousJudge([judge("qwen", reply(GOOD)), judge("nemotron", reply(GOOD))]).judge(item())

        assert verdict.provenance.model == "qwen + nemotron"
        assert " + " not in verdict.provenance.prompt_version  # one prompt for both


class TestHolding:
    @pytest.mark.parametrize("held_first", [True, False])
    def test_a_hold_in_either_judge_holds_the_submission(self, held_first):
        down = judge("down", RuntimeError("503"), RuntimeError("503"))
        up = judge("up", reply(GOOD))
        judges = [down, up] if held_first else [up, down]

        verdict = UnanimousJudge(judges).judge(item())

        assert verdict.status is JudgeStatus.HELD
        assert verdict.hold_reason == HOLD_JUDGE_UNAVAILABLE
        assert not verdict.is_rewardable


def test_one_judge_is_not_a_panel():
    with pytest.raises(ValueError, match="at least two"):
        UnanimousJudge([StubJudge(RUBRIC_V1)])


class TestChoosingTwoModels:
    URL = "https://judge.example.org/v1"

    @pytest.fixture(autouse=True)
    def _needs_api(self):
        pytest.importorskip("fastapi")

    def test_a_second_model_makes_a_unanimous_judge(self):
        from s2e_validator.api.main import _judge

        built = _judge({
            "S2E_JUDGE": "model", "S2E_JUDGE_URL": self.URL, "S2E_JUDGE_MODEL": "qwen",
            "S2E_JUDGE_SECOND_URL": self.URL, "S2E_JUDGE_SECOND_MODEL": "nemotron",
        })

        assert isinstance(built, UnanimousJudge)

    @pytest.mark.parametrize("half", [{"S2E_JUDGE_SECOND_URL": URL}, {"S2E_JUDGE_SECOND_MODEL": "nemotron"}])
    def test_half_a_second_model_does_not_start(self, half):
        from s2e_validator.api.main import SettingsError, _judge

        with pytest.raises(SettingsError, match="go together"):
            _judge({"S2E_JUDGE": "model", "S2E_JUDGE_URL": self.URL, "S2E_JUDGE_MODEL": "qwen", **half})
