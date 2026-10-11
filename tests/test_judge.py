"""The judges (#43, J1): the model judge holds what it cannot trust; the stub."""

from __future__ import annotations

import json

import pytest

from s2e_validator.judge import (
    HOLD_IMAGE_UNAVAILABLE,
    HOLD_JUDGE_UNAVAILABLE,
    HOLD_REPLY_INVALID,
    RUBRIC_V1,
    ImagePart,
    JudgeReplyInvalid,
    JudgeStatus,
    ModelJudge,
    Provenance,
    StubJudge,
    prompt_version,
)
from s2e_validator.prefilters import ExistingImage, NewImage

from helpers import CASTLE, ScriptedClient, item, photo, reply

class TestModelJudge:
    def test_a_good_reply_is_scored(self):
        verdict = ModelJudge(ScriptedClient(reply()), RUBRIC_V1).judge(item())

        assert verdict.status is JudgeStatus.SCORED
        assert verdict.quality_score == 75
        assert verdict.is_rewardable
        assert verdict.attempts == 1
        assert verdict.provenance == Provenance("scripted", prompt_version(RUBRIC_V1), "rubric-v1")

    def test_a_manipulation_attempt_is_never_rewardable(self):
        top_marks = {n: 5 for n in RUBRIC_V1.criterion_names}
        client = ScriptedClient(reply(top_marks, reason_codes=["INSTRUCTIONS_TO_JUDGE"]))

        verdict = ModelJudge(client, RUBRIC_V1).judge(item())

        assert verdict.quality_score == 100
        assert not verdict.is_rewardable

    def test_a_malformed_reply_is_retried(self):
        client = ScriptedClient("not json", reply())

        verdict = ModelJudge(client, RUBRIC_V1).judge(item())

        assert verdict.status is JudgeStatus.SCORED
        assert verdict.attempts == 2

    def test_a_reply_that_stays_malformed_is_held_not_scored(self):
        """The core rule: a judge failure is never a score, high or low."""
        client = ScriptedClient("not json", reply(score=100))

        verdict = ModelJudge(client, RUBRIC_V1).judge(item())

        assert verdict.status is JudgeStatus.HELD
        assert verdict.hold_reason == HOLD_REPLY_INVALID
        assert verdict.quality_score is None
        assert not verdict.is_rewardable

    def test_an_unreachable_model_is_held(self):
        client = ScriptedClient(TimeoutError("provider down"), ConnectionError("still down"))

        verdict = ModelJudge(client, RUBRIC_V1).judge(item())

        assert verdict.status is JudgeStatus.HELD
        assert verdict.hold_reason == HOLD_JUDGE_UNAVAILABLE
        assert verdict.attempts == 2

    def test_the_hold_reason_is_the_last_failure(self):
        client = ScriptedClient(TimeoutError("provider down"), "not json")

        assert ModelJudge(client, RUBRIC_V1).judge(item()).hold_reason == HOLD_REPLY_INVALID

    def test_a_pooled_photo_without_a_loader_is_held_without_calling_the_model(self):
        client = ScriptedClient()

        verdict = ModelJudge(client, RUBRIC_V1).judge(item(image=ExistingImage("img-7")))

        assert verdict.hold_reason == HOLD_IMAGE_UNAVAILABLE
        assert verdict.attempts == 0
        assert client.prompts == []

    def test_a_pooled_photo_is_loaded_and_shown(self):
        pooled = NewImage(photo(9))
        client = ScriptedClient(reply())
        judge = ModelJudge(client, RUBRIC_V1, image_loader=lambda ref: pooled)

        verdict = judge.judge(item(image=ExistingImage("img-7")))

        assert verdict.status is JudgeStatus.SCORED
        [image_part] = [p for p in client.prompts[0].content if isinstance(p, ImagePart)]
        assert image_part.data == pooled.data

    def test_a_loader_that_fails_is_a_hold(self):
        def broken(ref):
            raise FileNotFoundError(ref.image_id)

        judge = ModelJudge(ScriptedClient(), RUBRIC_V1, image_loader=broken)

        assert judge.judge(item(image=ExistingImage("img-7"))).hold_reason == HOLD_IMAGE_UNAVAILABLE

    @pytest.mark.parametrize("broken", [None, "[" * 100_000], ids=["none", "deeply-nested"])
    def test_a_reply_that_would_crash_a_parser_is_held(self, broken):
        """judge() never raises, whatever the model sends back."""
        verdict = ModelJudge(ScriptedClient(broken, broken), RUBRIC_V1).judge(item())

        assert verdict.status is JudgeStatus.HELD
        assert verdict.hold_reason == HOLD_REPLY_INVALID
        assert verdict.attempts == 2

    def test_the_declared_type_is_never_trusted(self):
        """Bytes of no known type are held, whatever type the uploader claimed."""
        client = ScriptedClient(reply())
        verdict = ModelJudge(client, RUBRIC_V1).judge(
            item(image=NewImage(b"GIF89a-not-sniffed", mime_type="image/gif"))
        )

        assert verdict.hold_reason == HOLD_IMAGE_UNAVAILABLE
        assert client.prompts == []

    def test_an_image_of_unknown_type_is_a_hold(self):
        verdict = ModelJudge(ScriptedClient(), RUBRIC_V1).judge(
            item(image=NewImage(b"not an image"))
        )

        assert verdict.hold_reason == HOLD_IMAGE_UNAVAILABLE

    def test_stage_1_flags_reach_the_verdict(self):
        verdict = ModelJudge(ScriptedClient(reply()), RUBRIC_V1).judge(
            item(flags=["DUPLICATE_IMAGE_TEXT_ONLY"])
        )

        assert verdict.flags == ("DUPLICATE_IMAGE_TEXT_ONLY",)

    def test_max_attempts_must_be_positive(self):
        with pytest.raises(ValueError, match="at least 1"):
            ModelJudge(ScriptedClient(), RUBRIC_V1, max_attempts=0)


class TestStubJudge:
    def test_default_ratings_score_and_pass(self):
        verdict = StubJudge(RUBRIC_V1).judge(item())

        assert verdict.status is JudgeStatus.SCORED
        assert verdict.quality_score == 75
        assert verdict.provenance.model == "stub"

    def test_ratings_can_be_scripted_per_submission(self):
        def by_text(judge_input):
            low = judge_input.interaction.text == "Zamek"
            return {n: 1 if low else 5 for n in RUBRIC_V1.criterion_names}

        stub = StubJudge(RUBRIC_V1, ratings=by_text)

        assert stub.judge(item(text="Zamek")).quality_score == 0
        assert stub.judge(item(text=CASTLE)).quality_score == 100

    def test_a_stub_cannot_produce_what_the_real_judge_could_not(self):
        with pytest.raises(JudgeReplyInvalid):
            StubJudge(RUBRIC_V1, ratings={"relevance": 9})

    def test_a_mis_scripted_stub_fails_when_it_is_built(self):
        """Not inside judge(), which never raises."""
        with pytest.raises(JudgeReplyInvalid, match="outside the rubric"):
            StubJudge(RUBRIC_V1, reason_codes=("NOT_A_CODE",))

    def test_empty_ratings_are_a_mistake_not_the_defaults(self):
        with pytest.raises(JudgeReplyInvalid, match="missing"):
            StubJudge(RUBRIC_V1, ratings={})
