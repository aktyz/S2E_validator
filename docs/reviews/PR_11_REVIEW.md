<!-- review-state
pr: 11
round: 1
reviewed_head: 79d2006
base: origin/main
verdict: Request Changes
last_finding_id: F-15
updated: 2026-10-30
-->

# Round 1 — Initial review · reviewed 79d2006 · 2026-10-30

## PR Review: Pay only what two judges would both pay

## Pull request overview

The validator's Stage 2 judge has so far been a single self-hosted vision model (Qwen2.5-VL-7B). This PR adds `UnanimousJudge`, which wraps several judges and pays a submission only if every one of them would. It holds the submission if any judge holds, and marks every veto with a `JUDGES_DISAGREE` reason code. The service can now be given a second model through `S2E_JUDGE_SECOND_URL` / `S2E_JUDGE_SECOND_MODEL`. The calibration script can measure both models live, or combine two saved runs with `--combine`. On the 268 labelled cases the PR reports 260 agreeing offline (combined runs) against 259 for Qwen alone, and 259 live with both models served on one L40S. The idea is defence in depth: a second model can stop payments but can never cause one. That makes it a low-risk safety net for the payment decision.

### Changes:
1. Judge composition (`src/s2e_validator/judge/`):
   - New `unanimous.py` with `UnanimousJudge` and `REASON_JUDGES_DISAGREE`. The first judge supplies the score, ratings and confidence. Reason codes from all judges are merged without repeats. Provenance joins every model name.
   - Both names exported from `judge/__init__.py`.
2. Service wiring (`src/s2e_validator/api/main.py`):
   - `_judge()` accepts an optional second endpoint and refuses to start if only one of the two second-model variables is set. It builds one `ChatCompletionsClient` per endpoint with a shared key and timeout, and logs all model names.
   - The settings table in the module docstring documents the new variables.
3. Calibration (`calibration/measure_judge.py`):
   - `build_judge()` builds a `UnanimousJudge` when `S2E_JUDGE_SECOND_URL` is set.
   - New `combine()` and `--combine FIRST.json SECOND.json` replay the unanimous rule over two saved runs, with no model call.
4. Tests (`tests/test_unanimous.py`):
   - Paying, vetoing in both orders, a blocking reason from the second judge, primary score and ratings, reason merging, provenance, holds in both orders, the minimum of two judges, and the service settings (both set, half set).

### Impact
- A wrong payout now needs two independent models to be wrong in the same direction. This hardens the payment decision against one model's blind spots and against prompt injection that fools only one model.
- Each submission now costs two model calls, run one after the other. Per-submission latency roughly doubles, and an outage of either server holds every submission.
- The audit log shows each veto (`JUDGES_DISAGREE`) and names both models in provenance, so any payout or veto can be traced back to the models behind it.
- The calibration tooling gains a cheap offline way to evaluate model pairs. Two of its paths currently report figures that do not match the unanimous rule (see F-01, F-02).

---

### Review Baseline
| Item            | Key Versions | Docs loaded |
|-----------------|--------------|-------------|
| PR description  | found. At review time the change targeted `feat/judge-generic-claims`; it reached `main` through PR #11, which is the reference for these findings. The diff was taken against `origin/main`, and a `--stat` against `origin/feat/judge-generic-claims` lists the same 5 files, so the reviewed diff matches what GitHub shows | — |
| Validator       | Python ≥3.11. No dependency changes. Touched code uses the `api` extra (`fastapi>=0.115,<0.142`, unchanged). `pytest>=8` | docs.python.org/3/library/dataclasses.html (`replace()` calls `__init__`, so `__post_init__` re-runs), docs.pytest.org/en/stable/reference/reference.html (`importorskip`, `parametrize` argnames) |

`dataclasses.replace()` re-runs `JudgeVerdict.__post_init__`. For a SCORED verdict that only checks the score range and that no `hold_reason` is set, so `replace(primary, is_rewardable=all(votes), …)` is valid. No contradictions with this skill's static rules.

### Summary
The core of the PR is sound. `UnanimousJudge` keeps the "a judge failure is never a score" contract, cannot turn a non-payment into a payment, and is well tested in both judge orders. The service rejects a half-configured second model at startup. The problems are in the calibration tooling that produces the PR's headline numbers. `build_judge()` does not check the second-model settings the way the service does, so a half-set environment silently measures one model (F-01). The held-out pay line ignores the veto for combined rows (F-02). The endpoint-building logic is also duplicated between the service and the script, which is how F-01 arose (F-03, F-04). Neither problem affects live payouts, but both can mislead the measurements used to decide whether to ship. Fix them before merge. The questions (F-13 to F-15) concern whether the extra GPU, latency and availability cost is worth it, and how downstream code treats vetoed verdicts.

### Findings

#### 🔴 Bugs

**F-01 · `calibration/measure_judge.py`**
`if os.environ.get("S2E_JUDGE_SECOND_URL"):`
Only the URL decides whether a second judge is built. With `S2E_JUDGE_SECOND_MODEL` set and the URL missing, the run silently measures Qwen alone while the operator believes it measured the pair. With the URL set and the model missing, the run crashes with a bare `KeyError`. The module docstring promises "the same … settings as the service", and the service refuses both half-configurations ("go together"). This breaks *Validate inputs at the boundary*, and because it fails silently it can produce wrong calibration numbers. Fix: apply the same both-or-neither check, preferably by sharing one builder with the service (F-03, F-04):
```python
second = [n for n in ("S2E_JUDGE_SECOND_URL", "S2E_JUDGE_SECOND_MODEL") if os.environ.get(n)]
if len(second) == 1:
    raise SystemExit("S2E_JUDGE_SECOND_URL and S2E_JUDGE_SECOND_MODEL go together")
```

**F-02 · `calibration/measure_judge.py`**
`row["rewardable"] = bool(mine["rewardable"] and other["rewardable"])`
`combine()` keeps the first run's `score` and sets `rewardable` from both runs. `held_out_pay_line()`, however, ignores `rewardable` and recomputes payment as `r["score"] >= line and not set(r["reasons"]) & blocking`. Suppose the second judge vetoed a case only because its own score was low, with no blocking code. The held-out pay line then counts that case as paid, so the `pay_line_held_out` block of `--combine` output describes "Qwen's scores + both models' blocking codes", not the unanimous rule. The `overall` figure (260) uses `rewardable` and is correct. Live `UnanimousJudge` runs have the same gap, because their rows only carry the primary's score. This is a wrong result in a tool whose job is to justify the pay line. Fix: carry the second score and apply the line to both:
```python
# combine(), SCORED/SCORED branch
row["second_score"] = other["score"]

# held_out_pay_line()
line_score = lambda r: min(r["score"], r.get("second_score", r["score"]))
paid = lambda r: line_score(r) >= line and not set(r["reasons"]) & blocking
# and use line_score(r) in wrongly_paid too
```
For live two-model runs, either record each judge's score in the row or label the pay-line block "primary only" in the summary.

#### 🟠 Design Issues

**F-03 · `calibration/measure_judge.py`**
`def build_judge(kind: str):`
`build_judge()` now repeats the service's endpoint list, client construction and single-or-unanimous choice almost line for line. The copies have already drifted: one lacks the both-or-neither check (F-01), and the default timeouts differ (120 here, 60 in the service). Two copies of "how env becomes a judge" give one rule two places to change, which breaks SRP and DRY. That matters most for a script whose stated purpose is to measure *the service's* judge. Fix: call the shared builder from F-04, for example `judge_from_env(os.environ, default_timeout=120)`, and drop the local copy.

**F-04 · `src/s2e_validator/api/main.py`**
`endpoints = [(env["S2E_JUDGE_URL"], env["S2E_JUDGE_MODEL"])]`
`_judge()` now validates two groups of variables, builds an endpoint list, constructs clients, logs, and chooses between a single judge and a panel. That is several jobs in one function (*functions small and single-purpose*). The logic also lives in the `api` module, which needs FastAPI, so the calibration script cannot reuse it and copies it instead (F-03). Fix: move the env-to-judge logic into the core package, for example `src/s2e_validator/judge/settings.py`, with no extras imports:
```python
class JudgeSettingsError(ValueError): ...

def judge_from_env(env: Mapping[str, str], default_timeout: float) -> Judge:
    endpoints = _endpoints(env)          # both-or-neither check lives here
    clients = [_client(url, model, env, default_timeout) for url, model in endpoints]
    judges = [ModelJudge(c, RUBRIC_V1) for c in clients]
    return judges[0] if len(judges) == 1 else UnanimousJudge(judges)
```
`api/main.py` then wraps `JudgeSettingsError` into `SettingsError`, and `measure_judge.py` calls the same function.

**F-05 · `src/s2e_validator/judge/unanimous.py`**
`verdicts = [judge.judge(item) for judge in self._judges]`
The judges run one after the other, and every judge runs even after an earlier one has held. If the first server is down, the request spends `max_attempts × timeout` (2 × 60 s by default) on it. It then still calls the second model, whose answer cannot change the outcome (HELD). In the normal case, latency is the sum of both models rather than the slower of the two. This goes against *Mind the cost*. Fix: stop at the first HELD verdict. Also consider running the judges concurrently, since both calls are I/O-bound HTTP requests and vLLM batches concurrent requests well:
```python
verdicts = []
for judge in self._judges:
    verdict = judge.judge(item)
    verdicts.append(verdict)
    if verdict.status is JudgeStatus.HELD:
        break
```
If the API request has a time budget, check that 2 × 2 × timeout still fits inside it.

**F-06 · `src/s2e_validator/api/main.py`**
`api_key=env.get("S2E_JUDGE_API_KEY") or None,`
Both clients get the same bearer token, so the first server's credential is sent to whatever host `S2E_JUDGE_SECOND_URL` names. The docstring says this ("It shares the key"), but it is a credential-scoping problem. A second model on another host or provider receives a key it was never issued, and anyone who can set the second URL can collect the first server's key. Fix: add an optional `S2E_JUDGE_SECOND_API_KEY`. Fall back to the shared key only when the second URL has the same scheme and host as the first, so a one-gateway, two-model setup keeps working.

#### 🟡 Style / Readability

**F-07 · `calibration/measure_judge.py`**
`elif mine["status"] == "SCORED" and other["status"] == "SCORED":`
The docstring says `combine()` uses "the same rule as UnanimousJudge", but two cases differ. (a) When the runs disagree about paying, no `JUDGES_DISAGREE` is added, unlike in the service. (b) When one run is SCORED and the other REJECTED, which happens if the two runs were made with different `--campaign` files, the row silently keeps the first run's verdict, even if that is a payment the other run rejected. Fix: append `"JUDGES_DISAGREE"` when the two `rewardable` values differ. Raise `SystemExit("the two runs used different text checks")` when exactly one of the two rows is REJECTED.

**F-08 · `src/s2e_validator/judge/unanimous.py`**
`only stop payments, so it cannot make a wrong payout more likely.`
The module docstring includes measured figures (259/193/260) that will go stale as soon as the prompt, rubric or models change. It also states only the benefit. A veto cannot add a wrong payout, but it can withhold correct ones, and an outage of either server now holds every submission. Fix: keep the numbers in the README section the docstring already cites, and state the trade-off here. For example: "A veto can only stop payments: it cannot cause a wrong payout, but it can withhold a right one, and either judge being unavailable holds the submission."

**F-09 · `calibration/measure_judge.py`**
`combines two saved runs on the same labels the same way, without a model. The labels and photos stay outside this repository:`
An unrelated sentence about where the labels and photos are kept was joined onto the new paragraph, which left a line of about 130 characters followed by an orphaned fragment ("the photos are contributors' own."). Fix: end the `--combine` paragraph after "without a model." and rewrap "The labels and photos stay outside this repository: the photos are contributors' own." as its own paragraph.

**F-10 · `src/s2e_validator/api/main.py`**
`from ..judge.unanimous import UnanimousJudge`
`UnanimousJudge` is exported from `..judge`, as are `ModelJudge` and `StubJudge` on the line above. Importing it from the submodule is inconsistent and depends on an internal module path. Fix: `from ..judge import RUBRIC_V1, Judge, ModelJudge, StubJudge, UnanimousJudge`.

**F-11 · `src/s2e_validator/judge/unanimous.py`**
`def _combined(provenances) -> Provenance:`
This is the one untyped parameter in a module that is otherwise fully annotated, and the function rebinds the parameter name to a list. Fix: `def _combined(provenances: Sequence[Provenance]) -> Provenance:`. Pass the list directly (`_combined([v.provenance for v in verdicts])`) and remove the `list(...)` rebinding.

**F-12 · `tests/test_unanimous.py`**
`class TestTheVerdict:`
Several behaviours are untested (absent tests):
- (a) Both judges decline, so the verdict is not rewardable and carries no `JUDGES_DISAGREE`. This is the case that separates "disagree" from "declined".
- (b) `attempts` is the maximum over the judges, for example when one judge retried after a malformed reply.
- (c) Confidence and flags come from the primary.
- (d) `calibration/measure_judge.py`'s `combine()`, which produced the PR's headline 260, has no test at all.

Suggested additions:
```python
def test_both_decline_is_not_a_disagreement(self):
    verdict = UnanimousJudge([judge("qwen", reply(POOR)), judge("nemotron", reply(POOR))]).judge(item())
    assert not verdict.is_rewardable
    assert REASON_JUDGES_DISAGREE not in verdict.reason_codes

def test_attempts_is_the_most_any_judge_needed(self):
    verdict = UnanimousJudge([judge("qwen", "not json", reply(GOOD)), judge("nemotron", reply(GOOD))]).judge(item())
    assert verdict.attempts == 2
```
Add a small table test for `combine()` as well, covering HELD in either run, SCORED/SCORED disagreement, and mismatched case sets.

#### 🟢 Praise

`src/s2e_validator/judge/unanimous.py` / `class UnanimousJudge:` — Composition over inheritance done well. `UnanimousJudge` satisfies the same `Judge` protocol as `ModelJudge`, so the pipeline needs no change and the panel can contain any judges, stubs included (Open/Closed). Taking a sequence rather than exactly two keeps it open for a third model without a rewrite.

`src/s2e_validator/judge/unanimous.py` / `held = next((v for v in verdicts if v.status is JudgeStatus.HELD), None)` — A hold in any judge becomes a hold of the whole panel, and it is built through `JudgeVerdict.held`, so the contract's "a judge failure is never a score" invariant carries over to the panel rather than being re-implemented.

`src/s2e_validator/api/main.py` / `raise SettingsError("S2E_JUDGE_SECOND_URL and S2E_JUDGE_SECOND_MODEL go together")` — Failing at startup on a half-configured second model is the right call. A service that quietly ran with one judge would look healthy while missing the safety net it was configured to have.

`tests/test_unanimous.py` / `@pytest.mark.parametrize("held_first", [True, False])` — Testing holds and vetoes in both judge orders catches the easy-to-miss bug where only the primary's state is checked.

#### 🔵 Questions for the Author

**F-13 · `src/s2e_validator/judge/unanimous.py`**
`together, paying only when both would, on 260. A second model with a veto can`
Live, the pair scored 259/268, the same as Qwen alone, at the cost of a second model on the GPU, about double the latency, and twice the ways for a submission to be held. What is the case for enabling it: robustness to prompt injection that fools only one model, wrong payouts avoided on cases outside the labelled set, or something else? A line in the README saying when an operator should set the second model (and when not) would help deployments.

**F-14 · `src/s2e_validator/judge/unanimous.py`**
`is_rewardable=all(votes),`
A vetoed verdict keeps the primary's `quality_score`, which can be well above the pay line, while `is_rewardable` is `False`. Does every downstream consumer gate on `is_rewardable` and never on `quality_score` alone? That includes the score-to-Q mapping (#43, M1) and the reward-batch builder. If any of them derives eligibility from the score, the veto is bypassed. If you are unsure, a test through the settlement path with a vetoed high-score verdict would settle it.

**F-15 · `src/s2e_validator/judge/unanimous.py`**
`REASON_JUDGES_DISAGREE = "JUDGES_DISAGREE"`
`parse_reply` deliberately restricts reason codes to the rubric's vocabulary, and `JUDGES_DISAGREE` is outside it. Does anything downstream validate or map `reason_codes` against `rubric.reasons`, such as the audit log schema, the API response or settlement? If so, it will now see an unknown code. Would it be cleaner as a separate field, for example `vetoed_by: tuple[str, ...]` naming the dissenting models? Then the audit log records *who* vetoed, not only that someone did.

### Verdict
**Request Changes**
The service-side `UnanimousJudge` is correct and safe. The calibration path that produces the PR's evidence can silently measure the wrong configuration (F-01), and its pay-line figure ignores the veto (F-02). Both need small fixes before merge.

---

### GitHub Review Comment
> Becomes the summary body of the draft GitHub review (see `draft-payload.md`); also usable as-is in the GitHub "Leave a comment" box.

Nice work. A veto-only panel behind the existing `Judge` protocol is a clean, low-risk way to add a second model, and the "a hold in either judge holds the submission" handling keeps the never-a-score contract intact. The service side looks correct to me. My blockers are in the calibration script that produces the PR's numbers: `build_judge()` doesn't apply the service's both-or-neither check, so a half-set environment silently measures one model, and the held-out pay line recomputes payment from the primary's score, so it ignores the veto. I'd also like to see the env-to-judge logic shared between the service and the script, a separate key for the second endpoint, and an answer on whether anything downstream uses `quality_score` without checking `is_rewardable`. Requesting changes for the two calibration fixes.
