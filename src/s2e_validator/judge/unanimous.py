"""Several judges, and a submission is paid only if every one of them would pay it.

The measured numbers are in the README ("Two judges: pay only when both
agree"). The trade-off: a veto can only stop payments, so a second model cannot
make a wrong payout more likely, but it can withhold a correct one, it costs a
second model's time on every submission, and either judge being down holds the
submission.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .contract import JudgeInput, JudgeStatus, JudgeVerdict, Provenance
from .judge import Judge

#: Added to a verdict when the judges disagreed about paying it, so every veto
#: can be found in the audit log.
REASON_JUDGES_DISAGREE = "JUDGES_DISAGREE"


def veto(votes: Sequence[bool], reasons: Sequence[Sequence[str]]) -> tuple[bool, tuple[str, ...]]:
    """The rule itself: paid only if every vote pays, reasons from all kept.

    Reasons keep their order without repeats, and a disagreement is marked.
    Live verdicts and saved runs both decide here, so the two cannot drift.
    """
    merged: list[str] = []
    for codes in reasons:
        merged.extend(code for code in codes if code not in merged)
    if len(set(votes)) > 1:
        merged.append(REASON_JUDGES_DISAGREE)
    return all(votes), tuple(merged)


class UnanimousJudge:
    """Pays only what every judge would pay; holds if any judge holds.

    The first judge is the primary: its score and ratings are the verdict's.
    The others only decide whether it is paid. Reason codes from all of them
    are kept, in order and without repeats. Provenance names every model.
    """

    def __init__(self, judges: Sequence[Judge]):
        if len(judges) < 2:
            raise ValueError("UnanimousJudge needs at least two judges")
        self._judges = tuple(judges)

    def judge(self, item: JudgeInput) -> JudgeVerdict:
        verdicts = [judge.judge(item) for judge in self._judges]
        provenance = combined_provenance([v.provenance for v in verdicts])
        attempts = max(v.attempts for v in verdicts)

        held = next((v for v in verdicts if v.status is JudgeStatus.HELD), None)
        if held is not None:
            return JudgeVerdict.held(item, provenance, held.hold_reason, attempts)

        primary = verdicts[0]
        rewardable, reasons = veto(
            [v.is_rewardable for v in verdicts], [v.reason_codes for v in verdicts]
        )
        return replace(
            primary,
            provenance=provenance,
            is_rewardable=rewardable,
            reason_codes=reasons,
            attempts=attempts,
        )


def combined_provenance(provenances: Sequence[Provenance]) -> Provenance:
    """Every model named, even a repeat; each version once."""
    return Provenance(
        model=" + ".join(p.model for p in provenances),
        prompt_version=" + ".join(dict.fromkeys(p.prompt_version for p in provenances)),
        rubric_version=" + ".join(dict.fromkeys(p.rubric_version for p in provenances)),
    )
