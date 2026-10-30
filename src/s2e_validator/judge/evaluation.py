"""Measuring a judge against hand-checked labels (used by calibration/measure_judge.py).

Pure functions over labels and result rows, so the numbers that decide whether
a judge may pay contributors are themselves tested.

A label is one case: a photo, a description, what kind of case it is, and
whether it should be paid. A result row is a label plus what the judge (or the
Stage 1 text check) decided. Holds are counted apart from disagreements
everywhere: an outage is not a judge that disagrees.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import JudgeVerdict
from .rubric import RUBRIC_V1, Rubric
from .unanimous import REASON_JUDGES_DISAGREE

#: Statuses that decided a case: the judge scored it, or Stage 1 rejected it.
DECIDED = ("SCORED", "REJECTED")
#: The kinds the pay line is chosen on. Injections are the text check's and the
#: reason codes' job, not the line's.
LINE_KINDS = ("good", "generic", "mismatch")


class LabelError(ValueError):
    """The labels file cannot be measured as it is."""


class RunMismatch(ValueError):
    """Two saved runs cannot be combined."""


@dataclass(frozen=True)
class Label:
    case: str
    photo: str
    kind: str
    description: str
    expect_rewardable: bool
    expect_reasons: tuple[str, ...] = ()


def load_labels(path: Path, photos: Path) -> list[Label]:
    """Every label, checked before any model is called.

    Refuses a missing or mistyped field, a photo that is not there, a repeated
    case id, and two cases with the same photo and kind (one would silently
    overwrite the other in the good-against-generic comparison).
    """
    labels: list[Label] = []
    seen_cases: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        where = f"{path.name} line {number}"
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise LabelError(f"{where}: not JSON ({exc})") from None
        label = _label(raw, where)
        if label.case in seen_cases:
            raise LabelError(f"{where}: case {label.case!r} appears twice")
        if (label.photo, label.kind) in seen_pairs:
            raise LabelError(f"{where}: a second {label.kind!r} case for {label.photo!r}")
        if not (photos / label.photo).is_file():
            raise LabelError(f"{where}: photo {label.photo!r} is not in {photos}")
        seen_cases.add(label.case)
        seen_pairs.add((label.photo, label.kind))
        labels.append(label)
    if not labels:
        raise LabelError(f"{path.name} has no labels")
    return labels


def _label(raw: Any, where: str) -> Label:
    if not isinstance(raw, dict):
        raise LabelError(f"{where}: a label must be a JSON object")
    for key in ("case", "photo", "kind", "description"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise LabelError(f"{where}: {key!r} must be a non-empty string")
    if not isinstance(raw.get("expect_rewardable"), bool):
        raise LabelError(f"{where}: 'expect_rewardable' must be true or false")
    return Label(
        case=raw["case"],
        photo=raw["photo"],
        kind=raw["kind"],
        description=raw["description"],
        expect_rewardable=raw["expect_rewardable"],
        expect_reasons=_reasons(raw.get("expect_reasons", ()), where),
    )


def _reasons(value: Any, where: str = "a row") -> tuple[str, ...]:
    """A string (one reason, or none if empty) or a list of strings."""
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) and v for v in value):
        return tuple(value)
    raise LabelError(f"{where}: 'expect_reasons' must be a string or a list of strings")


def _label_fields(label: Label) -> dict[str, Any]:
    return {
        "case": label.case,
        "photo": label.photo,
        "kind": label.kind,
        "expect_rewardable": label.expect_rewardable,
        "expect_reasons": list(label.expect_reasons),
    }


def judged_row(label: Label, verdict: JudgeVerdict) -> dict[str, Any]:
    """A result row from the judge's verdict, with the verdict's provenance."""
    seen = verdict.to_dict()
    scored = seen["status"] == "SCORED"
    return {
        **_label_fields(label),
        "status": seen["status"],
        "score": seen["quality_score"],
        "rewardable": seen["is_rewardable"] if scored else None,
        "reasons": seen["reason_codes"],
        "criteria": seen["criteria"],
        "hold_reason": seen["hold_reason"],
        "judge": seen["judge"],
    }


def rejected_row(label: Label, reason_code: str) -> dict[str, Any]:
    """A result row for a case Stage 1 rejected before the judge saw it."""
    return {
        **_label_fields(label),
        "status": "REJECTED",
        "score": 0,
        "rewardable": False,
        "reasons": [reason_code],
        "criteria": {},
        "hold_reason": None,
        "judge": None,
    }


def summarise(results: Sequence[Mapping[str, Any]], rubric: Rubric = RUBRIC_V1) -> dict[str, Any]:
    by_kind: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in results:
        by_kind[row["kind"]].append(row)

    scores: dict[str, dict[str, int]] = defaultdict(dict)
    for row in results:
        if row["status"] == "SCORED":
            scores[row["photo"]][row["kind"]] = row["score"]
    pairs = [s for s in scores.values() if "good" in s and "generic" in s]

    return {
        "judge": next((row.get("judge") for row in results if row.get("judge")), None),
        "overall": _agreement(results),
        "per_kind": {kind: _agreement(rows) for kind, rows in sorted(by_kind.items())},
        "stopped_by_text_check": {
            kind: sum(row["status"] == "REJECTED" for row in rows) for kind, rows in sorted(by_kind.items())
        },
        "good_beats_generic": {"photos": len(pairs), "wins": sum(s["good"] > s["generic"] for s in pairs)},
        "expected_reason_given": _reasons_given(results),
        "pay_line_held_out": held_out_pay_line(results, rubric),
    }


def _agreement(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    decided = [row for row in rows if row["status"] in DECIDED]
    right = sum(row["rewardable"] == row["expect_rewardable"] for row in decided)
    return {
        "cases": len(rows),
        "held": len(rows) - len(decided),
        "agree": right,
        "rate": round(right / len(decided), 3) if decided else None,
    }


def _reasons_given(results: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How often the judge gave every expected reason, on the cases it scored.

    Holds and cases Stage 1 rejected never reached a judge's reply, so they
    are counted apart, not as misses.
    """
    expecting = [row for row in results if _reasons(row.get("expect_reasons", ()))]
    scored = [row for row in expecting if row["status"] == "SCORED"]
    given = sum(set(_reasons(row["expect_reasons"])) <= set(row["reasons"]) for row in scored)
    return {
        "cases": len(scored),
        "given": given,
        "held": sum(row["status"] == "HELD" for row in expecting),
        "stopped_before_the_judge": sum(row["status"] == "REJECTED" for row in expecting),
    }


def held_out_pay_line(results: Sequence[Mapping[str, Any]], rubric: Rubric = RUBRIC_V1) -> dict[str, Any]:
    """Choose the pay line on even-numbered photos, report it on the odd ones.

    The rule is "pay nothing that should not be paid, then as much as
    possible": the lowest line at which no unrewardable case on the first half
    would be paid. Maximising plain agreement instead picks a knife edge,
    because the judge's scores take only a few values, and it did worse on the
    other half.

    In combined runs the line applies to the lower of the two scores, so a
    veto counts. A live two-model run records only the primary score, so its
    line says so in "scored_on": it describes the first judge, not the pair.
    """
    blocking = rubric.blocking_reasons
    photos = sorted({row["photo"] for row in results})
    half = {photo: n % 2 for n, photo in enumerate(photos)}
    rows = [row for row in results if row["kind"] in LINE_KINDS and row["status"] == "SCORED"]

    def score(row: Mapping[str, Any]) -> int:
        return min(row["score"], row.get("second_score", row["score"]))

    def paid(row: Mapping[str, Any], line: int) -> bool:
        return score(row) >= line and not set(row["reasons"]) & blocking

    def agree(line: int, side: int) -> tuple[int, int]:
        chosen = [row for row in rows if half[row["photo"]] == side]
        return sum(paid(row, line) == row["expect_rewardable"] for row in chosen), len(chosen)

    wrongly_paid = [
        score(row) for row in rows
        if half[row["photo"]] == 0 and not row["expect_rewardable"] and not set(row["reasons"]) & blocking
    ]
    best = min(100, max(wrongly_paid, default=-1) + 1)
    fit, fit_cases = agree(best, 0)
    held, held_cases = agree(best, 1)
    current, _ = agree(rubric.min_rewardable_score, 1)
    return {
        "line": best,
        "chosen_on": f"{fit}/{fit_cases}",
        "held_out": f"{held}/{held_cases}",
        "held_out_at_current_line": f"{current}/{held_cases} at {rubric.min_rewardable_score}",
        "scored_on": "both judges" if any("second_score" in row for row in rows) else "first judge only",
    }


def combine(first: Sequence[Mapping[str, Any]], second: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Two saved runs on the same labels, paid only where both paid.

    The same rule as UnanimousJudge: a hold in either is a hold, the first
    run's score and ratings stand, reasons from both are kept, and a
    disagreement is marked. The second score is kept for the pay line. A case
    only one run rejected at Stage 1 means the runs used different text checks,
    so they are refused: the result would depend on their order.
    """
    others = {row["case"]: row for row in second}
    if set(others) != {row["case"] for row in first}:
        raise RunMismatch("the two runs are not on the same cases")
    combined = []
    for mine in first:
        other = others[mine["case"]]
        if (mine["status"] == "REJECTED") != (other["status"] == "REJECTED"):
            raise RunMismatch(f"case {mine['case']!r} was rejected at Stage 1 in only one run: different text checks?")
        row = dict(mine)
        row["judge"] = _combined_judge(mine.get("judge"), other.get("judge"))
        held = next((r for r in (mine, other) if r["status"] == "HELD"), None)
        if held is not None:
            row.update(status="HELD", score=None, rewardable=None, hold_reason=held["hold_reason"])
        elif mine["status"] == "SCORED":
            votes = (bool(mine["rewardable"]), bool(other["rewardable"]))
            row["rewardable"] = all(votes)
            row["second_score"] = other["score"]
            row["reasons"] = list(dict.fromkeys(list(mine["reasons"]) + list(other["reasons"])))
            if len(set(votes)) > 1:
                row["reasons"].append(REASON_JUDGES_DISAGREE)
        combined.append(row)
    return combined


def _combined_judge(first: Mapping[str, Any] | None, second: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Provenance naming both runs, as UnanimousJudge does for live verdicts."""
    if not first or not second:
        return dict(first or second) if (first or second) else None
    return {key: " + ".join(dict.fromkeys((first[key], second[key]))) for key in first}
