<!-- review-state
pr: 10
round: 1
reviewed_head: e86697c
base: origin/main
verdict: Request Changes
last_finding_id: F-08
updated: 2026-10-30
-->

# Round 1 — Initial review · reviewed e86697c · 2026-10-30

## PR Review: Review of #2: correct reason metric, checked labels, tested evaluation

## Pull request overview

This PR answers the eleven findings of the review of #2 (`docs/reviews/PR_2_REVIEW.md`) on the calibration script that measures a judge against hand-checked labels. These are the numbers that decide whether a judge may pay contributors. The measurement logic moves out of `calibration/measure_judge.py` into a new package module, `s2e_validator.judge.evaluation`, which has 24 tests. The reason-code metric now accepts a string or a list and counts only cases the judge scored. The labels file is checked in full before any model call, and a stopped run still writes what it has. Result rows are built from `JudgeVerdict.to_dict()`, so they carry provenance. A second commit drops the shared `--served-model-name judge` from the GPU deploy script, so each vLLM server answers to its real model name and provenance names the real models. The README and the deploy comment change to match. According to the description, saved runs re-summarise to the same agreement (259/268 and 260/268). On GitHub the PR is stacked ("Merge after #6"). This review diffed against `origin/main`, where the branch has exactly two commits of its own (`e6a130c`, `e86697c`). The owner has said the PR is being merged because of the submission deadline, with findings to be fixed together afterwards.

### Changes:
1. Evaluation module (`src/s2e_validator/judge/evaluation.py`, new):
   - `Label` (frozen dataclass) and `LabelError`, with `load_labels` checking fields, types, photo presence, repeated case ids and repeated (photo, kind) pairs
   - `judged_row` built on `JudgeVerdict.to_dict()`, and `rejected_row` for Stage 1 rejections
   - `summarise`, `held_out_pay_line` and `combine` moved from the script; the rubric is now a parameter
   - `_reasons_given`: a list of expected reasons needs every one; holds and Stage 1 rejections are counted apart
2. Calibration script (`calibration/measure_judge.py`):
   - Typed against the `Judge` Protocol; a missing or half-set setting exits with a sentence, as does a `ValueError` from the client
   - Timeout default lowered from 120 s to the service's 60 s
   - Loop body extracted to `measure`; output written in a `finally`, so a stopped run keeps its results; the top level of the results file names the judge
   - Usage says to install the package first
3. Tests (`tests/test_evaluation.py`, new): 24 tests for reasons, agreement, pay line, label loading, row building and combining
4. Deployment and docs (`deploy/judge-gpu.sh`, `README.md`):
   - `--served-model-name judge` removed; README and comments give the real model names as `S2E_JUDGE_MODEL` / `S2E_JUDGE_SECOND_MODEL`
   - README adds `python -m pip install -e .` to the reproduce steps

### Impact
- The calibration numbers are now computed by tested code, and a malformed labels file fails before any paid model call
- Every saved result row says which model, prompt and rubric produced it, so runs can be compared afterwards
- Operators: after redeploying the GPU script, anything still sending `model: "judge"` has to switch to the real model names (see F-08)

---

### Review Baseline
| Item            | Key Versions | Docs loaded |
|-----------------|--------------|-------------|
| PR description  | found (title, body, one owner comment: merging now, findings to be fixed together) | — |
| Validator       | Python ≥3.11; no dependency or extra touched (stdlib plus `s2e_validator.judge` / `prefilters`); pytest ≥8 | vLLM engine args (`--served-model-name`: "If not specified, the model name will be the same as the `--model` argument"); pytest stable (9.0.2) pages on import modes (default `prepend` puts `tests/` on `sys.path`, so `from helpers import item` resolves), `tmp_path`, `parametrize`. Project APIs checked against `judge/contract.py`, `judge/rubric.py`, `judge/__init__.py`, `judge/unanimous.py`, `judge/chat_client.py`, `api/main.py`, `tests/helpers.py`, `deploy/judge-gpu.sh` |

### Summary

All eleven findings of the review of #2 are addressed, and I checked each claim against the code. The reason metric is right now (old F-01, F-02). Labels are checked before any model call (old F-03, F-10). Rows come from `to_dict()` (old F-04, F-08). The evaluation logic is in the package with tests that pin the old bugs (old F-05). The timeout, temperature, length and JSON mode now match `api/main.py` (old F-11). There are 24 tests, as the description says. The vLLM change is correct by the docs, and the README model names match the script. One bug remains in code this PR moved into the package: `combine` pays a case that the second run's text check rejected, which breaks its own "paid only where both paid" rule (F-01). Old F-11 is resolved by copying the service's judge factory rather than sharing it, so the drift it warned about is still possible (F-02). A paid run can still be lost at the very end if `--out` points to a missing directory (F-03). None of these block the submission, but F-01 should be fixed before `--combine` numbers are quoted again.

### Findings

#### 🔴 Bugs

**F-01 · `src/s2e_validator/judge/evaluation.py`**
`elif mine["status"] == "SCORED" and other["status"] == "SCORED":`
The docstring promises "paid only where both paid", but a case that one run rejected at Stage 1 is only handled when it is the *first* run. If `mine` is SCORED and rewardable while `other` is REJECTED, neither branch runs, and the row keeps `rewardable=True`. The combined run then pays a case the second run refused. `--campaign` is optional, so the trigger is easy to hit: combining a run made with `--campaign` and one made without, in that order, inflates agreement for the injection cases. In the reverse order the same pair comes out correct, so the result depends on argument order. Live, this can't happen, because Stage 1 runs before both judges. So two runs that disagree on REJECTED were made with different text checks and should not be combined at all. Suggested fix: refuse the mismatch, and add a test for it:

```python
statuses = {mine["status"], other["status"]}
if "REJECTED" in statuses and statuses != {"REJECTED"}:
    raise LabelError(
        f"case {mine['case']!r}: one run's text check rejected it and the other's did not "
        "(were both runs made with the same --campaign?)"
    )
```

#### 🟠 Design Issues

**F-02 · `calibration/measure_judge.py`**
`def build_judge(kind: str) -> Judge:`
Old F-11 asked whether the script could share the service's factory "so the two can't drift". Instead, `build_judge` is now a line-for-line copy of `_judge` in `src/s2e_validator/api/main.py`: the missing-settings check, the both-or-neither second pair, the endpoint list, the `ValueError` wrapping and the timeout default. The service's `"60"` is also duplicated as `DEFAULT_TIMEOUT`, with a comment pointing back to it. The two match today, but the next setting added to one (an egress proxy, `max_tokens`, a third judge) will silently be missing from the other, which brings back the "measures a judge that isn't the one deployed" problem. That is a DRY / single-source-of-truth problem. The script can't just import `api.main`, because that module imports `fastapi` (the `api` extra). So the shared factory has to live in the judge package. Suggested fix:

```python
# src/s2e_validator/judge/settings.py
DEFAULT_TIMEOUT = 60.0

def model_judge_from_env(env: Mapping[str, str]) -> Judge:
    """The model judge (or two, unanimous) that the S2E_JUDGE_* settings describe.

    Raises ValueError with a sentence naming what is missing or wrong."""
    ...
```

`api/main._judge` keeps the stub/model choice and turns `ValueError` into `SettingsError`. `build_judge` keeps the stub branch and turns `ValueError` into `SystemExit`. Both call `model_judge_from_env`, so the D5 guards, defaults and second-judge rule exist exactly once.

**F-03 · `calibration/measure_judge.py`**
`summary = write(args.out, results)`
Old F-03's point was that a paid run must not be thrown away. The `finally` handles a run that stops partway, but `--out` itself is never checked. If its directory doesn't exist (a typo, or a path relative to another directory), every model call is paid for, and then `write_text` raises `FileNotFoundError` inside the `finally`. Nothing is saved, and the summary isn't printed either, because the exception skips the `print`. This is the same project rule, *validate inputs at the boundary*, applied to the output argument. Suggested fix: check it next to `load_labels`, before `build_judge`:

```python
if args.out is not None and not args.out.parent.is_dir():
    raise SystemExit(f"--out: {args.out.parent} is not a directory")
```

#### 🟡 Style / Readability

**F-04 · `calibration/measure_judge.py`**
`def write(out: Path | None, results: list[dict]) -> dict:`
A run stopped after 120 of 268 cases writes a file that looks the same as a finished one: `overall.cases` is 120 and nothing says the run is incomplete. Someone re-summarising it later could quote a partial number as the run's result. Suggested fix: pass the label count, and record it at the top level:

```python
def write(out: Path | None, results: list[dict], expected: int) -> dict:
    ...
    json.dumps({"judge": summary["judge"], "complete": len(results) == expected,
                "labels": expected, "summary": summary, "results": results}, indent=1)
```

**F-05 · `src/s2e_validator/judge/evaluation.py`**
`row = dict(mine)`
`combine` copies the first run's row, including its `judge`, so `summarise(combine(a, b))["judge"]` names only the first model. The live `UnanimousJudge` gives `"Qwen/… + nvidia/…"` (`_combined` in `src/s2e_validator/judge/unanimous.py`). Old F-08 was about results naming their judge, and the `--combine` summary names the wrong one. The docstring's "same rule as UnanimousJudge" also skips the `JUDGES_DISAGREE` reason the live judge adds. That reason doesn't change any current metric, but it does mean combined rows differ from live rows. Suggested fix: join the provenance the same way `_combined` does (model joined with `" + "`, versions de-duplicated) and assign it to `row["judge"]`. Add `REASON_JUDGES_DISAGREE` when the two `rewardable` votes differ.

**F-06 · `calibration/measure_judge.py`**
`print(json.dumps(summarise(combine(saved_results(sys.argv[2]), saved_results(sys.argv[3]))), indent=2))`
Before this PR, combining two runs on different cases exited with a sentence (`SystemExit("the two runs are not on the same cases")`). Now `combine` raises `LabelError`, which the `--combine` and `--summarise` branches don't catch, so the user gets a traceback. The same happens for a malformed `expect_reasons` in a saved file, which `_reasons` reports as a `LabelError`. That is a small regression against *explicit, descriptive errors*. `LabelError` ("the labels file cannot be measured") is also the wrong name for "these two runs don't match". Suggested fix: catch it in both branches, as the labels path already does (`raise SystemExit(f"--combine: {exc}") from None`), and consider a separate `RunMismatch(ValueError)` for `combine`.

**F-07 · `src/s2e_validator/judge/evaluation.py`**
`def load_labels(path: Path, photos: Path) -> list[Label]:`
`load_labels` raises on the first problem, so a file with five bad lines takes five runs to fix. Old F-03 suggested reporting every problem at once. Nothing is paid for in the meantime, so this is about convenience only. Suggested fix: collect `problems.append(f"{where}: …")` instead of raising, keep going, and raise one `LabelError("\n".join(problems))` at the end. `_label` can return `None` plus a problem rather than raise.

#### 🟢 Praise

**`src/s2e_validator/judge/evaluation.py`**
`"stopped_before_the_judge": sum(row["status"] == "REJECTED" for row in expecting),`
`_reasons_given` reports scored, held and Stage-1-stopped cases as three separate counts, and the docstring says why. That is the contract's "a judge failure is never a score" rule carried into the measurement, and it goes beyond the minimum old F-02 asked for.

**`src/s2e_validator/judge/evaluation.py`**
`def summarise(results: Sequence[Mapping[str, Any]], rubric: Rubric = RUBRIC_V1) -> dict[str, Any]:`
The rubric is now a parameter instead of the module-level `RUBRIC_V1` hard-wired into `held_out_pay_line`. The pay line can be measured for another campaign's rubric without editing this module (Dependency Inversion, Open/Closed), and the tests can pin it.

**`tests/test_evaluation.py`**
`"""F-01 and F-02 of the review of #2."""`
Each earlier finding is pinned by a test that names it, including both shapes of `expect_reasons` through `parametrize`. That makes it easy to check that the fix covers what the review asked for, and stops the bug coming back.

**`deploy/judge-gpu.sh`**
`# Each server answers to its model's own name, so every verdict's provenance`
Correct by vLLM's docs: without `--served-model-name`, the API name is the `--model` value. The README's new `S2E_JUDGE_MODEL` values match `QWEN` and `NEMOTRON` in the script exactly. Provenance that said `judge + judge` couldn't trace a payout to a model, and now it can.

#### 🔵 Questions for the Author

**F-08 · `deploy/judge-gpu.sh`**
`--served-model-name judge \` *(removed line)*
Is any running service, `.env`, systemd unit or deployment note still set to `S2E_JUDGE_MODEL=judge` / `S2E_JUDGE_SECOND_MODEL=judge`? Once `judge-gpu.sh` is re-run, those servers no longer answer to `judge`. vLLM's docs don't say what an unknown name returns, but if it's an HTTP error, `ChatCompletionsClient` raises `ChatClientError` and every submission is quietly held rather than failing at startup. If such a setting exists, could the same PR update it? A one-time `GET /v1/models` check against `S2E_JUDGE_MODEL` at service start would also turn this into a startup error instead of a stream of holds.

### Verdict
**Request Changes**
All eleven earlier findings are addressed and the new module is well tested, but `combine` can still pay a case one run rejected (F-01), and old F-11 was fixed by copying the service's factory rather than sharing it (F-02). Since the PR is already merged, both belong in the follow-up fix.

---

### GitHub Review Comment
> Becomes the summary body of the draft GitHub review; also usable as-is in the GitHub "Leave a comment" box.

Thanks, this is a thorough response to the review of #2. I checked all eleven findings against the code, and each one is addressed. The evaluation logic in the package, with tests named after the findings, is a real improvement, and dropping the shared served-model name makes provenance meaningful. The main issue is in `combine`: it can still pay a case that one run's text check rejected, depending on argument order (F-01). Also, the judge settings are now a copy of the service's factory rather than shared with it, so they can drift again (F-02). As you're merging for the deadline, I've marked this Request Changes so these two go into the follow-up fix together with the smaller points.
