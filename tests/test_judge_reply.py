"""The judge's reply and verdict (#43, J1): strict validation, impossible verdicts."""

from __future__ import annotations

import json

import pytest

from s2e_validator.judge import (
    RUBRIC_V1,
    JudgeReplyInvalid,
    JudgeStatus,
    JudgeVerdict,
    Provenance,
    StubJudge,
    parse_reply,
)

from helpers import ALL_CRITERIA, item, reply

class TestReplyValidation:
    def test_a_valid_reply(self):
        parsed = parse_reply(reply(reason_codes=["LOW_EFFORT"]), RUBRIC_V1)

        assert parsed.criteria == ALL_CRITERIA
        assert parsed.reason_codes == ("LOW_EFFORT",)
        assert parsed.confidence == 0.9

    def test_one_markdown_fence_is_tolerated(self):
        assert parse_reply(f"```json\n{reply()}\n```", RUBRIC_V1).criteria == ALL_CRITERIA

    def test_confidence_is_optional(self):
        assert parse_reply(reply(confidence=None), RUBRIC_V1).confidence is None

    def test_repeated_reason_codes_count_once(self):
        parsed = parse_reply(reply(reason_codes=["LOW_EFFORT", "LOW_EFFORT"]), RUBRIC_V1)

        assert parsed.reason_codes == ("LOW_EFFORT",)

    @pytest.mark.parametrize(
        "raw, message",
        [
            ("I'd give this a 4.", "not JSON"),
            ("[1, 2, 3]", "JSON object"),
            ('Sure! {"criteria": {}}', "not JSON"),
            (reply(score=100), "unexpected keys"),
            (reply(criteria={"relevance": 4, "specificity": 4}), "missing"),
            (reply(criteria={**ALL_CRITERIA, "beauty": 5}), "unexpected"),
            (reply(criteria={**ALL_CRITERIA, "relevance": 6}), "1..5"),
            (reply(criteria={**ALL_CRITERIA, "relevance": 0}), "1..5"),
            (reply(criteria={**ALL_CRITERIA, "relevance": 4.5}), "whole number"),
            (reply(criteria={**ALL_CRITERIA, "relevance": "4"}), "whole number"),
            (reply(criteria={**ALL_CRITERIA, "relevance": True}), "whole number"),
            (reply(reason_codes=["VERY_GOOD"]), "outside the rubric"),
            (reply(reason_codes="LOW_EFFORT"), "list of strings"),
            (reply(confidence=1.5), "between 0 and 1"),
            (reply(confidence="high"), "number"),
            # Named, because pytest would otherwise put the whole reply in the test id.
            pytest.param("[" * 100_000, "not JSON: RecursionError", id="deeply-nested"),
            pytest.param(
                '{"criteria": {"relevance": ' + "9" * 5000 + "}}", "not JSON: ValueError", id="huge-integer"
            ),
            (None, "must be text, got NoneType"),
            (b'{"criteria": {}}', "must be text, got bytes"),
        ],
    )
    def test_anything_off_contract_is_refused(self, raw, message):
        with pytest.raises(JudgeReplyInvalid, match=message):
            parse_reply(raw, RUBRIC_V1)


class TestVerdict:
    PROVENANCE = Provenance("m", "p", "r")

    def test_serialises_to_json(self):
        verdict = StubJudge(RUBRIC_V1).judge(item(flags=["DUPLICATE_IMAGE_TEXT_ONLY"]))

        payload = json.loads(json.dumps(verdict.to_dict()))

        assert payload["quality_score"] == 75
        assert payload["judge"]["rubric_version"] == "rubric-v1.1"
        assert payload["flags"] == ["DUPLICATE_IMAGE_TEXT_ONLY"]

    @pytest.mark.parametrize(
        "fields, message",
        [
            ({"status": JudgeStatus.SCORED}, "needs a quality_score"),
            ({"status": JudgeStatus.SCORED, "quality_score": 101}, "0 to 100"),
            ({"status": JudgeStatus.SCORED, "quality_score": 50, "hold_reason": "X"}, "must not carry"),
            ({"status": JudgeStatus.HELD, "hold_reason": "X", "quality_score": 0}, "no score"),
            ({"status": JudgeStatus.HELD, "hold_reason": "X", "is_rewardable": True}, "never rewardable"),
            ({"status": JudgeStatus.HELD}, "say why"),
        ],
    )
    def test_impossible_verdicts_cannot_be_built(self, fields, message):
        with pytest.raises(ValueError, match=message):
            JudgeVerdict(submission_id="s", provenance=self.PROVENANCE, **fields)
