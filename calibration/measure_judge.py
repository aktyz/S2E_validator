"""How often a judge agrees with hand-checked labels, before anyone is paid by it.

First install the package (`pip install -e .` from the repository root), then:

    python calibration/measure_judge.py LABELS.jsonl PHOTOS_DIR [--judge stub|model]
        [--campaign config/demo_campaign.json] [--out results.json]

With `--campaign`, that campaign's text check runs first, as in the service: a
description it rejects never reaches the judge and is not paid.

With `--judge model`, the same S2E_JUDGE_URL / S2E_JUDGE_MODEL / S2E_JUDGE_API_KEY
/ S2E_JUDGE_TIMEOUT settings as the service, built the same way (the client's
own defaults for temperature, length and JSON mode, and the service's default
timeout), and with S2E_JUDGE_SECOND_URL / _SECOND_MODEL both models, paying
only what both would pay.

    python calibration/measure_judge.py --summarise results.json
    python calibration/measure_judge.py --combine FIRST.json SECOND.json

recompute a saved run's summary, or combine two saved runs on the same labels
the same way as two live judges, without a model.

The labels and photos stay outside this repository: the photos are
contributors' own. Each label line is a JSON object with "case", "photo",
"kind", "description", "expect_rewardable" and optionally "expect_reasons" (a
string or a list); the file is checked before any model is called (see
s2e_validator.judge.evaluation, which also defines everything reported).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from s2e_validator.judge import RUBRIC_V1, Judge, JudgeInput, ModelJudge, StubJudge, UnanimousJudge
from s2e_validator.judge.chat_client import ChatCompletionsClient
from s2e_validator.judge.evaluation import (
    Label,
    LabelError,
    RunMismatch,
    combine,
    judged_row,
    load_labels,
    rejected_row,
    summarise,
)
from s2e_validator.prefilters import TextFormatFilter, TextFormatSettings, Verdict, load_config
from s2e_validator.prefilters.interaction import Interaction, NewImage

#: The service's default (s2e_validator/api/main.py), so a measurement times
#: out the way the service would.
DEFAULT_TIMEOUT = "60"


def build_judge(kind: str) -> Judge:
    if kind == "stub":
        return StubJudge(RUBRIC_V1)
    missing = [name for name in ("S2E_JUDGE_URL", "S2E_JUDGE_MODEL") if not os.environ.get(name)]
    second = [name for name in ("S2E_JUDGE_SECOND_URL", "S2E_JUDGE_SECOND_MODEL") if os.environ.get(name)]
    if missing:
        raise SystemExit(f"--judge model needs {', '.join(missing)} set")
    if len(second) == 1:
        raise SystemExit("S2E_JUDGE_SECOND_URL and S2E_JUDGE_SECOND_MODEL go together")
    endpoints = [(os.environ["S2E_JUDGE_URL"], os.environ["S2E_JUDGE_MODEL"])]
    if second:
        endpoints.append((os.environ["S2E_JUDGE_SECOND_URL"], os.environ["S2E_JUDGE_SECOND_MODEL"]))
    try:
        judges: list[Judge] = [
            ModelJudge(
                ChatCompletionsClient(
                    url,
                    model,
                    api_key=os.environ.get("S2E_JUDGE_API_KEY") or None,
                    timeout=float(os.environ.get("S2E_JUDGE_TIMEOUT", DEFAULT_TIMEOUT)),
                ),
                RUBRIC_V1,
            )
            for url, model in endpoints
        ]
    except ValueError as exc:
        raise SystemExit(f"judge settings: {exc}") from None
    return judges[0] if len(judges) == 1 else UnanimousJudge(judges)


def text_filter(campaign: Path | None) -> TextFormatFilter | None:
    if campaign is None:
        return None
    config = load_config(campaign)
    if config.mode_for("text_format").value != "enforce":
        return None
    return TextFormatFilter(TextFormatSettings.from_dict(dict(config.settings_for("text_format"))))


def interaction_for(label: Label, photos: Path) -> Interaction:
    return Interaction(
        submission_id=label.case,
        campaign_id="judge-eval",
        user_id="eval",
        image=NewImage((photos / label.photo).read_bytes()),
        text=label.description,
        submitted_at=datetime.now(timezone.utc),
    )


def measure(judge: Judge, check: TextFormatFilter | None, label: Label, photos: Path) -> dict:
    interaction = interaction_for(label, photos)
    stopped = check.check(interaction) if check is not None else None
    if stopped is not None and stopped.verdict is Verdict.REJECT:
        return rejected_row(label, stopped.reason_code)
    return judged_row(label, judge.judge(JudgeInput(interaction)))


def write(out: Path | None, results: list[dict]) -> dict:
    summary = summarise(results)
    if out is not None:
        out.write_text(
            json.dumps({"judge": summary["judge"], "summary": summary, "results": results}, indent=1),
            encoding="utf-8",
        )
    return summary


def saved_results(path: str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["results"]


def main() -> int:
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--combine":
            print(json.dumps(summarise(combine(saved_results(sys.argv[2]), saved_results(sys.argv[3]))), indent=2))
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "--summarise":
            print(json.dumps(summarise(saved_results(sys.argv[2])), indent=2))
            return 0
    except (LabelError, RunMismatch) as exc:
        raise SystemExit(str(exc)) from None
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels", type=Path)
    parser.add_argument("photos", type=Path)
    parser.add_argument("--judge", choices=("stub", "model"), default="stub")
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    try:
        labels = load_labels(args.labels, args.photos)
    except LabelError as exc:
        raise SystemExit(f"labels: {exc}") from None
    if args.out is not None and not args.out.resolve().parent.is_dir():
        raise SystemExit(f"--out: {args.out.resolve().parent} is not a folder")
    judge = build_judge(args.judge)
    check = text_filter(args.campaign)

    results: list[dict] = []
    try:
        for n, label in enumerate(labels, 1):
            results.append(measure(judge, check, label, args.photos))
            print(f"\r{n}/{len(labels)}", end="", file=sys.stderr)
    finally:
        # Even a stopped run keeps what it paid the model for.
        print(file=sys.stderr)
        summary = write(args.out, results)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
