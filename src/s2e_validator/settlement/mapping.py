"""From a judge verdict to the number sent on-chain as Q_i (#43, M1).

The program pays `Q` in the reward mint's base units and caps nothing below
the run's remaining pool, so this mapping sets the money scale and is the only
limit on a single payout (D2).

A pure function of (judge score, Stage 1 flags, static config): no state,
nothing learned, nothing adaptive. Tuning changes a config value, never logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..judge.contract import JudgeStatus, JudgeVerdict

#: Stage 1 findings that mean "this photo was submitted before", by the same
#: contributor or by someone else, exactly or nearly. On the demo site Stage 1
#: only observes, so this rule is what stops a re-uploaded photo being paid.
DUPLICATE_PHOTO_FLAGS = frozenset(
    {
        "DUPLICATE_IMAGE_SAME_USER",
        "DUPLICATE_IMAGE_TEXT_ONLY",
        "NEAR_DUPLICATE_IMAGE_SAME_USER",
        "NEAR_DUPLICATE_IMAGE_TEXT_ONLY",
    }
)
_U64_MAX = 2**64 - 1


def _whole(value: object, name: str, minimum: int) -> None:
    # bool is an int in Python; `True` as an amount is always a mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")


@dataclass(frozen=True)
class ScoreMapping:
    #: The most one submission can ever be paid, in base units. Required, with
    #: no default: until the program caps a single payout (#74), this is the
    #: only thing between a mapping mistake and the run's whole pool.
    max_units_per_submission: int
    #: Base units paid per quality point. 1 means a perfect score of 100 pays
    #: 100 base units, which is 0.0001 of a 6-decimal token.
    units_per_point: int = 1
    #: Flags that make a submission unrewardable whatever its score.
    unrewardable_flags: frozenset[str] = DUPLICATE_PHOTO_FLAGS
    #: Decimals of the reward mint, for showing an amount to a person.
    decimals: int = 6

    def __post_init__(self) -> None:
        _whole(self.max_units_per_submission, "max_units_per_submission", 1)
        if self.max_units_per_submission > _U64_MAX:
            raise ValueError("max_units_per_submission must fit in a u64")
        _whole(self.units_per_point, "units_per_point", 1)
        _whole(self.decimals, "decimals", 0)
        if self.decimals > 18:
            raise ValueError("decimals must be between 0 and 18")

    def blocking_flag(self, flags: tuple[str, ...]) -> str | None:
        """The first Stage 1 flag that makes a submission unrewardable, if any."""
        return next((flag for flag in flags if flag in self.unrewardable_flags), None)

    def units(self, verdict: JudgeVerdict) -> int:
        """Q_i for one scored submission. Zero when it is not rewardable."""
        if verdict.status is not JudgeStatus.SCORED or not verdict.is_rewardable:
            return 0
        if self.blocking_flag(verdict.flags) is not None:
            return 0
        # Whole numbers throughout: no float ever touches a payout.
        return min((verdict.quality_score or 0) * self.units_per_point, self.max_units_per_submission)

    def display(self, units: int) -> str:
        """Base units as a decimal amount of the reward token, e.g. "0.000075"."""
        if self.decimals == 0:
            return str(units)
        whole, fraction = divmod(units, 10**self.decimals)
        return f"{whole}.{fraction:0{self.decimals}d}"
