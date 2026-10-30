<!-- review-state
pr: 2
round: 1
reviewed_head: 14e837f
base: origin/main
verdict: Request Changes
last_finding_id: F-11
updated: 2026-10-29
-->

# Round 1 — Initial review · reviewed 14e837f · 2026-10-29

## PR Review: Measure a judge against hand-checked labels

## Pull request overview

Before any judge is allowed to decide who gets paid, the team needs to know how often it agrees with a person. This PR adds `calibration/measure_judge.py`, a standalone script that runs either the stub judge or the self-hosted model judge over a JSONL file of hand-checked labels and the photos they name. It then reports agreement on "should this be paid" (overall and per kind: good, generic, mismatch, injection), whether each photo's good description outscored its generic one, how often the expected reason code was given, and how many cases were held. Labels and photos deliberately stay outside the repository, because the photos are contributors' own. The PR description gives a stub baseline of 31.5% agreement on 232 cases (the stub pays everything). On GitHub the PR targets `feat/judge-model-client` ("Merge after #1"). This review diffed against `origin/main`, and both bases show the same single commit.

### Changes:
1. Calibration script (`calibration/measure_judge.py`, new):
   - `build_judge` builds a `StubJudge` or a `ModelJudge` over `ChatCompletionsClient`, configured from `S2E_JUDGE_*` environment variables
   - `judge_one` wraps each label in an `Interaction` / `JudgeInput` and judges it
   - `summarise` computes agreement, good-vs-generic wins, and expected-reason hits
   - `main` parses arguments, loops over the labels with progress on stderr, prints the summary and optionally writes summary plus per-case results to `--out`

### Impact
- Gives a repeatable number for "is this judge good enough to pay with", and a baseline (the stub) to beat
- Developer-facing only: no change to the package, the service or the dependencies

---

### Review Baseline
| Item            | Key Versions                                                                 | Docs loaded |
|-----------------|-------------------------------------------------------------------------------|-------------|
| PR description  | found (title and body; no comments or reviews yet)                           | —           |
| Validator       | Python ≥3.11; no dependency or extra touched (stdlib plus `s2e_validator.judge` and `s2e_validator.prefilters.interaction` only); pytest ≥8 | None needed: no third-party library in the diff. Project APIs checked against `src/s2e_validator/judge/__init__.py`, `judge/contract.py`, `judge/judge.py`, `judge/chat_client.py` and `prefilters/interaction.py` |

### Summary

The script delivers what the description promises, and every call into the judge package matches the real APIs: constructor signatures, verdict fields and enum values. Agreement is computed the right way, with holds kept out. The reason-code metric is wrong, though. A list-shaped `expect_reasons` never matches (F-01), and holds are counted as misses (F-02). Because the labels file isn't validated, one missing photo or mistyped field can end, or quietly skew, a run that has already paid for model calls (F-03). Both bugs come from a pure function that has no test and sits where it can't easily be tested (F-05). These should be fixed before the script's numbers are trusted.

### Findings

#### 🔴 Bugs

**F-01 · `calibration/measure_judge.py`**
`"given": sum(r["expect_reasons"] in r["reasons"] for r in expected if r["status"] == "SCORED"),`
The field is named `expect_reasons` (plural) and the docstring lists it among the label keys, but the check is `expected in reasons`. A string label (`"GENERIC"`) works. A list label (`["GENERIC"]`), which the plural name invites, gives `["GENERIC"] in ["GENERIC", "OFF_TOPIC"]`, which is `False` for every case. The metric then reports 0 given, and nothing flags it. A metric that decides whether a judge is fit to pay contributors should not depend on which JSON shape the label author happened to pick. Suggested fix: normalise once when loading, and compare as sets:

```python
def expected_codes(label: dict) -> list[str]:
    raw = label.get("expect_reasons") or []
    return [raw] if isinstance(raw, str) else list(raw)

"given": sum(set(r["expect_reasons"]) <= set(r["reasons"]) for r in scored_expected),
```

Use `<=` if every expected code must be given, or `&` if any one is enough. Whichever you choose, say so in the docstring.

**F-02 · `calibration/measure_judge.py`**
`"cases": len(expected),`
`cases` counts every row that has an expected reason, but `given` counts only SCORED rows. A hold therefore lands in the denominator and can never reach the numerator, so holds are counted as misses. The module docstring promises the opposite ("holds … counted apart from disagreements"), and `agreement()` does keep them apart. With a flaky model server, this metric drops for reasons that have nothing to do with judgement quality. Suggested fix: mirror `agreement()`:

```python
scored_expected = [r for r in expected if r["status"] == "SCORED"]
"expected_reason_given": {
    "cases": len(expected),
    "held": len(expected) - len(scored_expected),
    "given": sum(... for r in scored_expected),
},
```

#### 🟠 Design Issues

**F-03 · `calibration/measure_judge.py`**
`labels = [json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines() if line.strip()]`
The labels file is parsed but not checked. A missing photo raises `FileNotFoundError` inside `judge_one`, and a missing key raises `KeyError` when the row is built. Either way the run stops partway through, after paying for every model call so far, and none of those results are kept, because `--out` is written only at the end. A wrongly typed value is worse: `"expect_rewardable": "true"` never equals `True`, so that case quietly counts as a disagreement. This breaks the project rule *validate inputs at the boundary*: check at the entry point, before doing any work, with errors that say what failed. Suggested fix: a `load_labels()` that checks every line and every photo up front and reports all problems at once:

```python
REQUIRED = {"case": str, "photo": str, "kind": str, "description": str, "expect_rewardable": bool}

def load_labels(path: Path, photos: Path) -> list[dict]:
    labels, problems = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        label = json.loads(line)
        problems += [f"line {n}: {key} missing or not {t.__name__}"
                     for key, t in REQUIRED.items() if not isinstance(label.get(key), t)]
        if not (photos / str(label.get("photo", ""))).is_file():
            problems.append(f"line {n}: photo not found: {label.get('photo')}")
        labels.append(label)
    if problems:
        raise SystemExit("invalid labels file:\n" + "\n".join(problems))
    return labels
```

**F-04 · `calibration/measure_judge.py`**
`results.append({`
`main()` parses arguments, loads labels, runs the loop, builds each row, reports progress and writes the output, which breaks Single Responsibility. The row-building half is a hand-written projection of `JudgeVerdict` that `JudgeVerdict.to_dict()` (in `src/s2e_validator/judge/contract.py`) already provides. The `or ()` / `or {}` fallbacks guard against `None` values that the verdict's own defaults rule out. Two projections of one type will drift apart. Suggested fix: extract a small `to_row()` built on `to_dict()`, and have `summarise` read `is_rewardable`, `quality_score` and `reason_codes`. `summarise` already filters by status, so a held verdict's `is_rewardable=False` is never counted.

```python
def to_row(label: dict, verdict: JudgeVerdict) -> dict:
    return {
        "case": label["case"], "photo": label["photo"], "kind": label["kind"],
        "expect_rewardable": label["expect_rewardable"],
        "expect_reasons": expected_codes(label),
        **verdict.to_dict(),
    }
```

**F-05 · `calibration/measure_judge.py`**
`def summarise(results: list[dict]) -> dict:`
`summarise()` is a pure function that produces the numbers used to decide whether a judge may pay contributors, and it has no test. A test would have caught F-01 and F-02. It also can't easily be tested where it is: `calibration/` is not a package, and pytest's `pythonpath` covers only `src`. Suggested fix: move `summarise` (with `load_labels` and `to_row`) into the package, for example `src/s2e_validator/judge/calibration.py`, leaving the script as argument parsing plus I/O. Then add `tests/test_calibration.py` with a few hand-built rows: one held, one with a list-shaped `expect_reasons`, and one good/generic pair.

#### 🟡 Style / Readability

**F-06 · `calibration/measure_judge.py`**
`os.environ["S2E_JUDGE_URL"],`
A missing setting surfaces as a bare `KeyError: 'S2E_JUDGE_URL'` traceback, which falls short of the project's *explicit, descriptive errors* rule. Suggested fix: check all the required settings together and exit with a sentence:

```python
missing = [k for k in ("S2E_JUDGE_URL", "S2E_JUDGE_MODEL") if not os.environ.get(k)]
if missing:
    raise SystemExit(f"--judge model needs {', '.join(missing)} set")
```

**F-07 · `calibration/measure_judge.py`**
`def build_judge(kind: str):`
`build_judge` and `judge_one` take and return an untyped judge, even though `s2e_validator.judge` exports the `Judge` Protocol for exactly this purpose (Dependency Inversion). Annotate them as `build_judge(kind: str) -> Judge` and `judge_one(judge: Judge, label: dict, photos: Path) -> JudgeVerdict`. The script then visibly depends on the abstraction, and a type checker can catch a mismatched stub.

**F-08 · `calibration/measure_judge.py`**
`args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=1), encoding="utf-8")`
The results file doesn't record which model, prompt version or rubric version produced it. Two runs can't be compared after the fact, and a changed `S2E_JUDGE_MODEL` is invisible. F-04's `to_dict()` brings provenance into every row for free. Also add one copy at the top level, e.g. `{"judge": results[0]["judge"], "summary": ..., "results": ...}`, so the summary carries it too.

**F-09 · `calibration/measure_judge.py`**
`python calibration/measure_judge.py LABELS.jsonl PHOTOS_DIR [--judge stub|model] [--out results.json]`
The script imports `s2e_validator`, but `pythonpath = ["src"]` in `pyproject.toml` applies only to pytest. Run as written from a fresh checkout, it fails with `ModuleNotFoundError`. Add the prerequisite (`pip install -e .` first) to the usage line. `--judge model` needs nothing more, because the client uses the standard library only.

#### 🟢 Praise

**`calibration/measure_judge.py`**
`decided = [r for r in rows if r["status"] == "SCORED"]`
`agreement()` counts only SCORED rows and reports holds separately. That carries the contract's rule ("a judge failure is never a score") into the measurement: an outage shows up as holds, not as a judge that seems to disagree.

**`calibration/measure_judge.py`**
`client = ChatCompletionsClient(`
Building the real `ChatCompletionsClient`, rather than a separate calibration client, means calibration runs get the same D5 guards: https or localhost only, and commercial model APIs refused. Contributor photos can't leak to a commercial API through the measurement tool either.

**`calibration/measure_judge.py`**
`the photos are contributors' own.`
Keeping labels and photos out of the repository, and saying why in the docstring, is the right call for contributors' own photos.

#### 🔵 Questions for the Author

**F-10 · `calibration/measure_judge.py`**
`scores[r["photo"]][r["kind"]] = r["score"]`
If the labels contain two cases with the same photo and kind (say, two "good" descriptions of one photo), the second score silently overwrites the first, and the pair is compared on whichever came last. Does the way the labels are made guarantee one case per (photo, kind)? If so, could `load_labels()` (F-03) enforce it, so a duplicate is an error rather than a hidden overwrite?

**F-11 · `calibration/measure_judge.py`**
`client = ChatCompletionsClient(`
The docstring says this uses "the same settings as the service". Does the service build its `ChatCompletionsClient` from these same variables in the same way? This script defaults the timeout to 120 s where the client default is 60 s, and leaves temperature, `max_tokens` and `json_mode` at the client defaults. If the service sets any of these differently, the calibration measures a judge that isn't the one deployed. If the service has a factory for this, could the script call it, so the two can't drift?

### Verdict
**Request Changes**
The reason-code metric is wrong in two ways (F-01, F-02), and an unvalidated labels file can throw away a paid run (F-03). All three need fixing before the script's numbers are relied on.

---

### GitHub Review Comment
> Becomes the summary body of the draft GitHub review; also usable as-is in the GitHub "Leave a comment" box.

Thanks, this is a useful tool to have before any model is allowed to pay, and keeping holds out of the agreement rate is exactly right. Two things need fixing before merge. First, the reason-code metric is wrong in two ways: a list-shaped `expect_reasons` never matches (F-01), and holds count as misses (F-02). Second, the labels file should be validated before the first model call, so one missing photo can't throw away a paid run (F-03). Moving `summarise` into the package with a small test (F-05) would have caught the first two. Requesting changes for now.
