"""How often a judge agrees with hand-checked labels, before anyone is paid by it.

    python calibration/measure_judge.py LABELS.jsonl PHOTOS_DIR [--judge stub|model] [--out results.json]

With `--judge model`, the same S2E_JUDGE_URL / S2E_JUDGE_MODEL / S2E_JUDGE_API_KEY
settings as the service. The labels and photos stay outside this repository:
the photos are contributors' own.

Each label line: {"case", "photo", "kind", "description", "expect_rewardable",
"expect_reasons"}. Reported:

- agreement on "should this be paid", overall and per kind;
- for every photo, whether its good description outscored its generic one;
- how often an expected reason code was given;
- holds, which pay nothing and are counted apart from disagreements.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from s2e_validator.judge import RUBRIC_V1, JudgeInput, JudgeStatus, ModelJudge, StubJudge
from s2e_validator.judge.chat_client import ChatCompletionsClient
from s2e_validator.prefilters.interaction import Interaction, NewImage


def build_judge(kind: str):
    if kind == "stub":
        return StubJudge(RUBRIC_V1)
    client = ChatCompletionsClient(
        os.environ["S2E_JUDGE_URL"],
        os.environ["S2E_JUDGE_MODEL"],
        api_key=os.environ.get("S2E_JUDGE_API_KEY") or None,
        timeout=float(os.environ.get("S2E_JUDGE_TIMEOUT", "120")),
    )
    return ModelJudge(client, RUBRIC_V1)


def judge_one(judge, label: dict, photos: Path):
    interaction = Interaction(
        submission_id=label["case"],
        campaign_id="judge-eval",
        user_id="eval",
        image=NewImage((photos / label["photo"]).read_bytes()),
        text=label["description"],
        submitted_at=datetime.now(timezone.utc),
    )
    return judge.judge(JudgeInput(interaction))


def summarise(results: list[dict]) -> dict:
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_kind[r["kind"]].append(r)

    def agreement(rows):
        decided = [r for r in rows if r["status"] == "SCORED"]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels", type=Path)
    parser.add_argument("photos", type=Path)
    parser.add_argument("--judge", choices=("stub", "model"), default="stub")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    judge = build_judge(args.judge)
    labels = [json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = []
    for n, label in enumerate(labels, 1):
        verdict = judge_one(judge, label, args.photos)
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
