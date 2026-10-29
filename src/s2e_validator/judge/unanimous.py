"""Several judges, and a submission is paid only if every one of them would pay it.

Measured on our labelled set (README, "The judge, measured"): Qwen2.5-VL-7B
alone agreed on 259 of 268 cases, NVIDIA Nemotron Nano 12B v2 VL alone on 193
(it pays good descriptions every time, but most generic ones too), and the two
together, paying only when both would, on 260. A second model with a veto can
only stop payments, so it cannot make a wrong payout more likely.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .contract import JudgeInput, JudgeStatus, JudgeVerdict, Provenance
from .judge import Judge

#: Added to a verdict when the judges disagreed about paying it, so every veto
#: can be found in the audit log.
REASON_JUDGES_DISAGREE = "JUDGES_DISAGREE"


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
        provenance = _combined(v.provenance for v in verdicts)
        attempts = max(v.attempts for v in verdicts)

        held = next((v for v in verdicts if v.status is JudgeStatus.HELD), None)
        if held is not None:
            return JudgeVerdict.held(item, provenance, held.hold_reason, attempts)

        primary = verdicts[0]
        votes = [v.is_rewardable for v in verdicts]
        reasons: list[str] = []
        for verdict in verdicts:
            reasons.extend(code for code in verdict.reason_codes if code not in reasons)
        if len(set(votes)) > 1:
            reasons.append(REASON_JUDGES_DISAGREE)
        return replace(
            primary,
            provenance=provenance,
            is_rewardable=all(votes),
            reason_codes=tuple(reasons),
            attempts=attempts,
        )


def _combined(provenances) -> Provenance:
    provenances = list(provenances)
    return Provenance(
        model=" + ".join(p.model for p in provenances),
        prompt_version=" + ".join(dict.fromkeys(p.prompt_version for p in provenances)),
        rubric_version=" + ".join(dict.fromkeys(p.rubric_version for p in provenances)),
    )
