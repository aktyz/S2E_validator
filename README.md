# Survey2Earn Validator

Off-chain service that decides whether a contributor submission is rewardable and produces the quality score (`Q_i`) that `execute_reward_batch` settles on-chain.

```
Submission (image + text + metadata)
        │
        ▼
Stage 1: deterministic pre-filters   ← this package, no paid or remote calls
        │  REJECT (enforced) → stop, with reason_code
        ▼  PASS (+ flags)
Stage 2: LLM judge                   ← not built yet
        │
        ▼
Reward batch → programs/survey2earn (execute_reward_batch)
```

## Status

Issue #13: pre-filter pipeline architecture & interface. No real filters yet; `NoOpFilter` proves the wiring.

## Run the tests

```bash
cd validator
python -m pip install -e ".[dev]"   # or just: pip install pytest
python -m pytest
```

Requires Python 3.11+. No runtime dependencies so far.

## Layout

| File | Purpose |
|---|---|
| `src/s2e_validator/prefilters/interaction.py` | `Interaction`: the submission every filter receives. The image is either `NewImage` (uploaded bytes) or `ExistingImage` (reference to a photo already in the pool). |
| `src/s2e_validator/prefilters/result.py` | `PreFilterResult` and `Verdict` (PASS / FLAG / REJECT) |
| `src/s2e_validator/prefilters/base.py` | `PreFilter` interface every check implements, plus `NoOpFilter` |
| `src/s2e_validator/prefilters/config.py` | `FilterMode` (off / shadow / enforce) and `PipelineConfig` |
| `src/s2e_validator/prefilters/pipeline.py` | `PreFilterPipeline.run_pre_filters(interaction)` |

## Adding a filter

```python
class LengthFormatFilter(PreFilter):
    name = "length_format"
    cost_rank = 10

    def check(self, interaction: Interaction) -> PreFilterResult:
        if len(interaction.text.strip()) < 3:
            return PreFilterResult.rejected("TEXT_TOO_SHORT", confidence=1.0, length=len(interaction.text))
        return PreFilterResult.passed()
```

Then register it in the pipeline and give it a mode in config. Until you do, it runs in **shadow** mode.

## Decisions (issue #13)

| Decision | Choice | Why |
|---|---|---|
| Runtime | **Python 3.11+**, stdlib only for the pipeline core | The ML tooling the later stages need (perceptual hashing, face detection, judge SDKs, evaluation) is Python-first. The on-chain boundary is a signed Solana transaction carrying `quality_scores: Vec<u64>`, so the service language doesn't have to match the program's. |
| Result type | `PreFilterResult { verdict, reason_code, confidence?, details }` | As specified. Construction fails if a REJECT or FLAG has no `reason_code`, so a rejection without a reason cannot exist. |
| Verdicts | **PASS / FLAG / REJECT** | Adds FLAG to the issue's PASS/REJECT. Language (#17), velocity (#20) and stolen-image signals (#22) are soft flags that must reach the judge as context without stopping the submission. Adding the verdict later would change the interface every filter implements. |
| Execution order | Ascending `cost_rank` declared on each filter | Order is explicit and testable rather than depending on registration order. Guide: 10 structural, 20 hashing, 30 stateful lookups, 40 local models. |
| Fail-fast vs run-all | **Fail fast** on the first *enforced* REJECT | Later, more expensive filters never run on obvious junk. Shadow rejects and flags never stop the pipeline. Trade-off: shadow filters ranked after an enforced reject don't run for that submission, so their shadow metrics undercount on already-rejected traffic. |
| Modes | Per filter: **off / shadow / enforce**, default **shadow** | Every filter ships log-only first. A filter missing from config can never start rejecting real contributors by accident. |
| Filter crashes | **Fail open**: record the error, continue | A false reject costs a real contributor money; a false accept costs one judge call. A broken filter must not reject anyone. |
| Return type | `PipelineResult` instead of a bare `PreFilterResult` | Orchestration (#23) and metrics (#24) need to know which filter decided, what shadow filters would have rejected, and which flags go to the judge. `verdict` and `reason_code` are still at the top level. |

## Open questions

- **REJECT → on-chain** (#12, #28): does a rejected submission get no batch entry at all, or an entry with `Q_i = 0`? This decides whether rejections leave an on-chain trace; it doesn't change this interface.
- **Existing-photo descriptions**: how contributors pick an existing photo, how many descriptions a photo can receive, and which ones are paid. This affects near-duplicate scoping (#16), not the interaction shape.
