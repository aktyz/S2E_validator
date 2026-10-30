"""How often a judge agrees with hand-checked labels, before anyone is paid by it.

    python calibration/measure_judge.py LABELS.jsonl PHOTOS_DIR [--judge stub|model]
        [--campaign config/demo_campaign.json] [--out results.json]

With `--campaign`, that campaign's text check runs first, as in the service: a
description it rejects never reaches the judge and is not paid.

With `--judge model`, the same S2E_JUDGE_URL / S2E_JUDGE_MODEL / S2E_JUDGE_API_KEY
settings as the service, and with S2E_JUDGE_SECOND_URL / _SECOND_MODEL both
models, paying only what both would pay.

    python calibration/measure_judge.py --combine FIRST.json SECOND.json

combines two saved runs on the same labels the same way, without a model. The labels and photos stay outside this repository:
the photos are contributors' own.

Each label line: {"case", "photo", "kind", "description", "expect_rewardable",
"expect_reasons"}. Reported:

- agreement on "should this be paid", overall and per kind;
- for every photo, whether its good description outscored its generic one;
- how often an expected reason code was given;
- holds, which pay nothing and are counted apart from disagreements;
- a pay line chosen on half the photos and checked on the other half, so the
  line is not fitted to the cases that judge it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from s2e_validator.judge import RUBRIC_V1, JudgeInput, JudgeStatus, ModelJudge, StubJudge, UnanimousJudge
from s2e_validator.prefilters import TextFormatFilter, TextFormatSettings, Verdict, load_config
from s2e_validator.judge.chat_client import ChatCompletionsClient
from s2e_validator.prefilters.interaction import Interaction, NewImage


def build_judge(kind: str):
    if kind == "stub":
        return StubJudge(RUBRIC_V1)
    endpoints = [(os.environ["S2E_JUDGE_URL"], os.environ["S2E_JUDGE_MODEL"])]
    if os.environ.get("S2E_JUDGE_SECOND_URL"):
        endpoints.append((os.environ["S2E_JUDGE_SECOND_URL"], os.environ["S2E_JUDGE_SECOND_MODEL"]))
    judges = [
        ModelJudge(
            ChatCompletionsClient(
                url,
                model,
                api_key=os.environ.get("S2E_JUDGE_API_KEY") or None,
                timeout=float(os.environ.get("S2E_JUDGE_TIMEOUT", "120")),
            ),
            RUBRIC_V1,
        )
        for url, model in endpoints
    ]
    return judges[0] if len(judges) == 1 else UnanimousJudge(judges)


def combine(first: list[dict], second: list[dict]) -> list[dict]:
    """Two saved runs on the same labels, paid only where both paid.

    The same rule as UnanimousJudge: a hold in either is a hold, the first
    run's score and ratings stand, and reasons from both are kept.
    """
    others = {r["case"]: r for r in second}
    if set(others) != {r["case"] for r in first}:
        raise SystemExit("the two runs are not on the same cases")
    combined = []
    for mine in first:
        other = others[mine["case"]]
        row = dict(mine)
        held = next((r for r in (mine, other) if r["status"] == "HELD"), None)
        if held is not None:
            row.update(status="HELD", score=None, rewardable=None, hold_reason=held["hold_reason"])
        elif mine["status"] == "SCORED" and other["status"] == "SCORED":
            row["rewardable"] = bool(mine["rewardable"] and other["rewardable"])
            row["reasons"] = list(dict.fromkeys(mine["reasons"] + other["reasons"]))
        combined.append(row)
    return combined


def text_filter(campaign: Path | None) -> TextFormatFilter | None:
    if campaign is None:
        return None
    config = load_config(campaign)
    if config.mode_for("text_format").value != "enforce":
        return None
    return TextFormatFilter(TextFormatSettings.from_dict(dict(config.settings_for("text_format"))))


def interaction_for(label: dict, photos: Path) -> Interaction:
    return Interaction(
        submission_id=label["case"],
        campaign_id="judge-eval",
        user_id="eval",
        image=NewImage((photos / label["photo"]).read_bytes()),
        text=label["description"],
        submitted_at=datetime.now(timezone.utc),
    )


def summarise(results: list[dict]) -> dict:
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_kind[r["kind"]].append(r)

    def agreement(rows):
        decided = [r for r in rows if r["status"] in ("SCORED", "REJECTED")]
        right = sum(r["rewardable"] == r["expect_rewardable"] for r in decided)
        return {"cases": len(rows), "held": len(rows) - len(decided), "agree": right,
                "rate": round(right / len(decided), 3) if decided else None}

    scores: dict[str, dict[str, int]] = defaultdict(dict)
    for r in results:
        if r["status"] == "SCORED":
            scores[r["photo"]][r["kind"]] = r["score"]
    pairs = [s for s in scores.values() if "good" in s and "generic" in s]
    expected = [r for r in results if r["expect_reasons"]]
    return {
        "stopped_by_text_check": {kind: sum(r["status"] == "REJECTED" for r in rows) for kind, rows in sorted(by_kind.items())},
        "pay_line_held_out": held_out_pay_line(results),
        "overall": agreement(results),
        "per_kind": {kind: agreement(rows) for kind, rows in sorted(by_kind.items())},
        "good_beats_generic": {
            "photos": len(pairs),
            "wins": sum(s["good"] > s["generic"] for s in pairs),
        },
        "expected_reason_given": {
            "cases": len(expected),
            "given": sum(r["expect_reasons"] in r["reasons"] for r in expected if r["status"] == "SCORED"),
        },
    }


def held_out_pay_line(results: list[dict]) -> dict:
    """Choose the pay line on even-numbered photos, report it on the odd ones.

    Only good, generic and mismatch cases: injections are the text check's and
    the reason codes' job, not the line's. The rule is "pay nothing that should
    not be paid, then as much as possible": the lowest line at which no
    unrewardable case on the first half would be paid. Maximising plain
    agreement instead picks a knife edge, because the judge's scores take only
    a few values, and it did worse on the other half.
    """
    blocking = RUBRIC_V1.blocking_reasons
    photos = sorted({r["photo"] for r in results})
    half = {photo: n % 2 for n, photo in enumerate(photos)}
    rows = [r for r in results if r["kind"] in ("good", "generic", "mismatch") and r["status"] == "SCORED"]

    def agree(line: int, side: int) -> tuple[int, int]:
        chosen = [r for r in rows if half[r["photo"]] == side]
        paid = lambda r: r["score"] >= line and not set(r["reasons"]) & blocking
        return sum(paid(r) == r["expect_rewardable"] for r in chosen), len(chosen)

    wrongly_paid = [
        r["score"] for r in rows
        if half[r["photo"]] == 0 and not r["expect_rewardable"] and not set(r["reasons"]) & blocking
    ]
    best = min(100, max(wrongly_paid, default=-1) + 1)
    fit, fit_cases = agree(best, 0)
    held, held_cases = agree(best, 1)
    current, _ = agree(RUBRIC_V1.min_rewardable_score, 1)
    return {"line": best, "chosen_on": f"{fit}/{fit_cases}", "held_out": f"{held}/{held_cases}",
            "held_out_at_current_line": f"{current}/{held_cases} at {RUBRIC_V1.min_rewardable_score}"}


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--combine":
        runs = [json.loads(Path(name).read_text(encoding="utf-8"))["results"] for name in sys.argv[2:]]
        print(json.dumps(summarise(combine(*runs)), indent=2))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--summarise":
        # Recompute the summary of an earlier --out file without a model.
        saved = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(summarise(saved["results"]), indent=2))
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels", type=Path)
    parser.add_argument("photos", type=Path)
    parser.add_argument("--judge", choices=("stub", "model"), default="stub")
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    judge = build_judge(args.judge)
    check = text_filter(args.campaign)
    labels = [json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = []
    for n, label in enumerate(labels, 1):
        interaction = interaction_for(label, args.photos)
        stopped = check.check(interaction) if check is not None else None
        if stopped is not None and stopped.verdict is Verdict.REJECT:
            results.append({
                "case": label["case"], "photo": label["photo"], "kind": label["kind"],
                "expect_rewardable": label["expect_rewardable"], "expect_reasons": label.get("expect_reasons", ""),
                "status": "REJECTED", "score": 0, "rewardable": False, "reasons": [stopped.reason_code],
                "criteria": {}, "hold_reason": None,
            })
            continue
        verdict = judge.judge(JudgeInput(interaction))
        results.append({
            "case": label["case"], "photo": label["photo"], "kind": label["kind"],
            "expect_rewardable": label["expect_rewardable"], "expect_reasons": label.get("expect_reasons", ""),
            "status": verdict.status.value,
            "score": verdict.quality_score,
            "rewardable": bool(verdict.is_rewardable) if verdict.status is JudgeStatus.SCORED else None,
            "reasons": list(verdict.reason_codes or ()),
            "criteria": dict(verdict.criteria or {}),
            "hold_reason": verdict.hold_reason,
        })
        print(f"\r{n}/{len(labels)}", end="", file=sys.stderr)
    print(file=sys.stderr)

    summary = summarise(results)
    print(json.dumps(summary, indent=2))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
