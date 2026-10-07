"""Score to Q (#43, M1): a pure function of the score, the flags and config."""

from __future__ import annotations

import pytest

from s2e_validator.judge import JudgeStatus, JudgeVerdict, Provenance
from s2e_validator.settlement.mapping import DUPLICATE_PHOTO_FLAGS, ScoreMapping

PROVENANCE = Provenance("m", "p", "r")


def scored(score: int, flags=(), rewardable: bool = True) -> JudgeVerdict:
    return JudgeVerdict(
        "s", JudgeStatus.SCORED, PROVENANCE, quality_score=score, is_rewardable=rewardable, flags=tuple(flags)
    )


def mapping(**settings) -> ScoreMapping:
    return ScoreMapping(**{"max_units_per_submission": 1_000_000, **settings})


@pytest.mark.parametrize(
    "score, units_per_point, units, shown",
    [
        (0, 10_000, 0, "0.000000"),
        (40, 10_000, 400_000, "0.400000"),
        (75, 10_000, 750_000, "0.750000"),
        (100, 10_000, 1_000_000, "1.000000"),
        (75, 1, 75, "0.000075"),
    ],
)
def test_what_a_score_pays_in_a_six_decimal_token(score, units_per_point, units, shown):
    priced = mapping(units_per_point=units_per_point)

    assert priced.units(scored(score)) == units
    assert priced.display(units) == shown


def test_no_payout_can_exceed_the_configured_maximum():
    capped = ScoreMapping(max_units_per_submission=500_000, units_per_point=10_000)

    assert capped.units(scored(100)) == 500_000
    assert capped.units(scored(40)) == 400_000


def test_the_maximum_has_no_default():
    """Until the program caps a single payout (#74) this is the only limit."""
    with pytest.raises(TypeError, match="max_units_per_submission"):
        ScoreMapping()


@pytest.mark.parametrize("flag", sorted(DUPLICATE_PHOTO_FLAGS))
def test_a_duplicate_photo_pays_nothing_whatever_the_score(flag):
    priced = mapping()

    assert priced.units(scored(100, flags=["LOW_CHARACTER_VARIETY", flag])) == 0
    assert priced.blocking_flag(("LOW_CHARACTER_VARIETY", flag)) == flag


def test_other_flags_do_not_block():
    priced = mapping()

    assert priced.units(scored(80, flags=["NEAR_DUPLICATE_TEXT_TEMPLATE", "IMAGE_CONTAINS_FACE"])) == 80
    assert priced.blocking_flag(("IMAGE_CONTAINS_FACE",)) is None


def test_a_payout_is_exact_at_any_scale():
    """Whole numbers only: a float product would drift at this size."""
    huge = ScoreMapping(max_units_per_submission=2**64 - 1, units_per_point=10**16 + 1)

    assert huge.units(scored(99)) == 99 * (10**16 + 1)


def test_a_held_or_unrewardable_verdict_is_worth_nothing():
    held = JudgeVerdict("s", JudgeStatus.HELD, PROVENANCE, hold_reason="JUDGE_UNAVAILABLE")

    assert mapping().units(held) == 0
    assert mapping().units(scored(10, rewardable=False)) == 0


def test_the_same_input_always_gives_the_same_answer():
    priced = mapping(units_per_point=7)

    assert {priced.units(scored(63, flags=["X"])) for _ in range(5)} == {441}


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"max_units_per_submission": 0}, "max_units_per_submission must be at least 1"),
        ({"max_units_per_submission": 1.5}, "max_units_per_submission must be a whole number"),
        ({"max_units_per_submission": True}, "whole number"),
        ({"max_units_per_submission": 2**64}, "fit in a u64"),
        ({"units_per_point": 0}, "units_per_point must be at least 1"),
        ({"units_per_point": 1.5}, "whole number"),
        ({"decimals": 30}, "between 0 and 18"),
    ],
)
def test_bad_settings_are_refused(settings, message):
    with pytest.raises(ValueError, match=message):
        mapping(**settings)
