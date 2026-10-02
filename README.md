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

| Issue | Filter | State |
|---|---|---|
| #13 | Pipeline frame, `PreFilter` interface, shadow/enforce modes | Merged |
| #14 | `TextFormatFilter`, `ImageFormatFilter`, structural checks | Merged |
| #15 | `ImageExactDuplicateFilter`, exact-duplicate photos | Merged |
| #16 | `ImageNearDuplicateFilter`, `TextNearDuplicateFilter`, near-duplicates | Merged |
| #23 | Orchestrator: config-built stage, commit phase | Merged |
| #24 | `StageMetrics`: per-filter counts, latency, shadow visibility | Merged |
| #21 | `FaceDetectionFilter`: the campaign's no-faces rule, YuNet | Merged |
| #20 | `SubmissionVelocityFilter`: per-contributor submission rate | This PR |

## Run the tests

```bash
cd validator
python -m pip install -e ".[dev]"   # or just: pip install pytest
python -m pytest
python -m pytest -m slow            # latency budgets, opt-in
```

The `slow` tests assert on wall-clock time, which flakes on a shared CI runner, so they are left out of the default run and out of CI. Run them deliberately, on an idle machine, when a change could affect the hot path.

Requires Python 3.11+. Runtime dependencies: **Pillow** (decoding photos, perceptual hashing) and **opencv-python-headless** with **numpy** (face detection). The YuNet model ships in `models/`, so there is no download step; see `models/README.md`.

## Layout

| File | Purpose |
|---|---|
| `src/s2e_validator/prefilters/interaction.py` | `Interaction`: the submission every filter receives. The image is either `NewImage` (uploaded bytes) or `ExistingImage` (reference to a photo already in the pool). |
| `src/s2e_validator/prefilters/result.py` | `PreFilterResult` and `Verdict` (PASS / FLAG / REJECT) |
| `src/s2e_validator/prefilters/base.py` | `PreFilter` interface every check implements, plus `NoOpFilter` |
| `src/s2e_validator/prefilters/config.py` | `FilterMode` (off / shadow / enforce), `PipelineConfig`, `settings_from_dict` |
| `src/s2e_validator/prefilters/pipeline.py` | `PreFilterPipeline.run_pre_filters(interaction)` |
| `src/s2e_validator/prefilters/text_format.py` | `TextFormatFilter` (#14) |
| `src/s2e_validator/prefilters/image_format.py` | `ImageFormatFilter` (#14) |
| `src/s2e_validator/prefilters/hashing.py` | `image_identity()` (SHA-256 / `ref:<id>`, doubles as the `uniqueness_key`), `dhash()`, `simhash()`, `hamming()` |
| `src/s2e_validator/prefilters/stores.py` | `ImageKeyStore` + `InMemoryImageKeyStore` (TTL, lock-guarded atomic claims); near-duplicate windows |
| `src/s2e_validator/prefilters/suspicion.py` | `SuspicionLog`: append-only record of cross-user photo matches |
| `src/s2e_validator/prefilters/image_duplicate.py` | `ImageExactDuplicateFilter` (#15) |
| `src/s2e_validator/prefilters/near_duplicate.py` | `ImageNearDuplicateFilter`, `TextNearDuplicateFilter` (#16) |
| `src/s2e_validator/prefilters/windows.py` | `HashWindow`: bucketed, TTL-bounded fingerprint window (#16) |
| `src/s2e_validator/prefilters/text.py` | `normalize()` and `canonical()`, shared by every filter that reads a description |
| `src/s2e_validator/prefilters/registry.py` | `build_pipeline()`, `load_config()`: the stage assembled from config (#23) |
| `src/s2e_validator/prefilters/face_detection.py` | `FaceDetectionFilter` (#21), YuNet detector |
| `src/s2e_validator/prefilters/velocity.py` | `SubmissionVelocityFilter` (#20), `SubmissionTimesStore` |
| `src/s2e_validator/prefilters/metrics.py` | `StageMetrics`: per-filter counts, latency, reject rate, judge calls saved (#24) |

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

## Exact-duplicate detection (issue #15)

Anchored on the **photo**, never on the text: two honest contributors describing the same landmark write similar captions.

| Situation | Verdict |
|---|---|
| Same user re-uploads their own photo | **REJECT** `DUPLICATE_IMAGE_SAME_USER` |
| Another user uploads a photo already claimed — first time | **FLAG** `DUPLICATE_IMAGE_TEXT_ONLY` |
| Another user uploads it **again** | **REJECT** `DUPLICATE_IMAGE_REFERENCE` |
| Many users describe the same pooled photo | **PASS** |
| Same user describes one pooled photo twice | **REJECT** `DUPLICATE_IMAGE_REFERENCE` |

Keys: an uploaded photo is `SHA-256` of the raw bytes, held globally; a pooled photo is `ref:<image_id>`. Every submission also claims a per-user key, `<image key>|user:<id>`.

Pooled-photo keys are scoped per user on purpose: many contributors describing one photo is what the campaign asks for, so only the *same* user repeating it is farming.

**A second person's identical photo is not rejected.** They are describing a photo someone else already owns, which is exactly what a pooled-photo submission is, so the contribution is downgraded to **text-only** with a FLAG and the judge prices it accordingly. Only same-user repeats are unambiguous farming and stay hard rejects.

**Text-only is capped at one per user per photo.** The downgrade claims a per-user key, so a second attempt at the same photo is a REJECT rather than another priced contribution — the same cap a pooled-photo description has. Without it, one account could farm another's photo indefinitely, a flag at a time.

**A text-only contribution is keyed on its text, not the photo.** The photo's key belongs to the original submitter, and settlement uses that key to stop one photo being paid for twice; sharing it would block the copy and the original together. The result carries `uniqueness_key` (`text:<hash>`) and `photo_key` for traceability.

**First claim wins.** The original submitter is never affected by a later duplicate. Results carry `first_submission_id` and `first_seen_at` so an appeal can be traced.

**Cross-user matches go to the suspicion log.** `suspicion.py` keeps an append-only record of two accounts holding a byte-identical photo — evidence for investigating stolen images across campaigns, deliberately separate from both `PreFilterResult` and `ImageKeyStore`. It never changes a verdict. `InMemorySuspicionLog` is a placeholder for the durable fraud repository.

The match is recorded in `commit` only, never in `check`: a submission the stage goes on to reject leaves no fraud record against the account, and a corrected resubmission carries a new `submission_id`, so logging at detection would count one photo twice. If the stage rejects *after* the match was recorded (a claim race lost at commit time), `rollback` retracts it. `SuspicionLog.record` is still **required to be idempotent** on `(image_key, duplicate_submission_id)`: one submission duplicating one photo is a single fact, however many times it is reported or a crashed `commit` is retried. The Protocol says so, so a durable implementation inherits the rule.

**`check` reads, `commit` writes.** The filter never claims a key during `check`. The orchestrator commits once the whole pipeline passes (#23), so a submission rejected by a *later* filter — a face, say — doesn't lock its own photo out of a corrected resubmission. Re-validating the same `submission_id` is never a duplicate of itself, so retries are safe.

**Claim races are decided at commit.** Two uploads of one photo can both pass `check` before either commits. `ImageKeyStore.add` is atomic and returns `DuplicateClaimResult(seen, claimed)`, and `commit` returns a *revised* verdict when this submission lost — downgrading the loser to text-only rather than paying twice for one photo. `commit` and `uniqueness_key` are on the `PreFilter` interface with no-op defaults, so the orchestrator calls them on every filter without special-casing.

### Store choice and retention

`InMemoryImageKeyStore`: a process-local dict with a **30-day** default time-to-live, expired entries dropped from the front on each lookup.

- **Why in-memory for v1:** no infrastructure to run, and a campaign runs in one validator process.
- **Concurrency:** nothing processes submissions in parallel yet, but the service in front of this will, so `find`/`add`/`purge_expired` are guarded by a re-entrant lock and claiming is atomic. The lock is held only around dict work.
- **Known limit:** it does not survive a restart, and previously seen photos become submittable again. Per #12, this store is the *only* defence against repeated cross-wallet payouts for one photo, so a durable store (Redis or a database) behind the same `ImageKeyStore` protocol is required before mainnet. Swapping it needs no filter changes.
- **Why 30 days:** longer than a run's reward window, so a photo can't be re-submitted inside one campaign. Revisit once campaign length is fixed.

### Measured (Python 3.11.9, Windows laptop)

Re-measured after the lock, the suspicion log and the second (per-user) key claim:

| Measure | Result |
|---|---|
| `check` + `commit` per submission | ~20 µs |
| 50,000 submissions loaded | ~1.0 s |
| Single lookup against a 50k store | ~0.02 ms |

At Bielik's 1M-photo target the store holds roughly 100 MB of keys, which is the point at which the durable store matters more than speed. `tests/test_image_duplicate_volume.py` re-checks the order of magnitude on every run.

### Open: releasing a claim when a submission is rejected later

A photo's claim is made when Stage 1 passes, but the judge (#stage-2) or settlement can still reject the submission afterwards. The original then holds the photo's key without ever being paid, and a second contributor is capped at text-only on a photo nobody owns.

Nothing is lost today — no judge exists yet — but before the campaign runs, `ImageKeyStore` needs a `release(key, submission_id)` and the judge stage needs to call it on rejection. Worth its own issue rather than guessing the shape here, since it depends on what settlement does with a rejected entry (#12).

## Length & format (issue #14)

Two filters, because text and photo fail in unrelated ways and a campaign may want one enforced before the other.

### `text_format` (cost_rank 10)

| Check | Reason code | Default |
|---|---|---|
| Lone surrogates or `U+FFFD` — text that never decoded intact | `TEXT_ENCODING_INVALID` | — |
| Control characters beyond `\n\r\t` | `TEXT_CONTROL_CHARACTERS` | `max_control_characters: 0` |
| Empty or whitespace-only | `TEXT_EMPTY` | — |
| Too short / too long, measured after collapsing whitespace | `TEXT_TOO_SHORT` / `TEXT_TOO_LONG` | `min_chars: 5`, `max_chars: 2000` |
| The app's own placeholder pasted back | `TEXT_INSTRUCTION_ECHO` | `placeholders: ()` |
| Repeated **letter** run: `aaaaaaaa` | `TEXT_REPEATED_CHARACTERS` | `max_repeated_run: 5` |
| Repeating unit: `asdfasdfasdf` | `TEXT_REPEATED_PATTERN` | `min_pattern_repeats: 3`, `min_pattern_coverage: 0.6` |
| Single token, few distinct characters: `asdfasdf` | `TEXT_LOW_CHARACTER_VARIETY` - **FLAG** | `variety_min_length: 8`, `min_character_variety: 0.5` |
| Ends mid-word on a hyphen | `TEXT_LOOKS_TRUNCATED` — **FLAG** | `flag_truncated: true` |

`min_chars: 5` is a sanity floor, not a copy of the campaign's rule. Obywatel Bielik requires 100 characters, so anything shorter cannot arrive through their UI; this only catches what comes another way.

**Only category Cc counts as a control character.** Category Cf includes U+200D, which joins every multi-part emoji, plus zero-width spaces and the BOM that ride along with pasted text. Counting those rejected "Zamek w Malborku 👩‍💻" under the default of zero, which is exactly the false reject this stage is meant to avoid.

**Runs count letters only.** "1000000" and "Wow!!!!!!" are odd, not structurally invalid, and the judge can weigh them.

**Low character variety is a flag, not a reject.** "Mississippi" scores 0.36, the same as `asdfasdf`. A structural check cannot separate them, so it hands both to the judge with the observation attached.

**`placeholders` is empty until Bielik's real app strings are known** (#28). The check is exact-match on the normalised text, so a description that merely starts with the placeholder wording still passes.

### `image_format` (cost_rank 12)

| Check | Reason code | Default |
|---|---|---|
| File size bounds, checked before decoding | `IMAGE_FILE_TOO_SMALL` / `IMAGE_FILE_TOO_LARGE` | `4 KiB` … `25 MiB` |
| Decodes at all | `IMAGE_UNDECODABLE` | — |
| Pixel count sanity | `IMAGE_DECOMPRESSION_BOMB` | Pillow's `MAX_IMAGE_PIXELS` |
| Allowed format | `IMAGE_FORMAT_NOT_ALLOWED` | `JPEG, PNG, WEBP` |
| Minimum resolution | `IMAGE_RESOLUTION_TOO_LOW` | `256 × 256` |

Submissions describing a photo already in the pool carry no bytes, so this filter passes them through — the pooled photo was checked when it arrived.

The filter calls Pillow's `load()`, not `verify()`. `verify()` only walks the file structure and lets a truncated JPEG through, which would then blow up inside perceptual hashing (#16) or face detection (#21). **Known limit:** a photo whose pixel data is corrupted but still decodable passes here; whether the picture shows anything is the judge's call.

**Everything cheap is decided before decoding.** `Image.open()` is lazy, so file size, format and dimensions are all known before a pixel is read: a 25 MiB GIF is rejected on its format without ever reaching a decoder, which also keeps untrusted bytes away from most of the decoders Pillow ships.

**The pixel bound is ours, not Pillow's.** Pillow only *warns* between 1x and 2x `MAX_IMAGE_PIXELS` and raises only above 2x, so a highly compressible file inside that window would be decoded in full. `max_pixels` is checked explicitly after `open()` and before `load()`, and does not depend on a process-wide global that another filter can change. Pillow's own error is kept as a backstop.

**Anything raised while decoding means undecodable.** Pillow's decoders raise widely (`struct.error`, `zlib.error` and others), and letting one escape would make the pipeline fail open and send a broken file to the judge.

### Tuning without a redeploy

`PipelineConfig` carries thresholds next to modes, and each filter parses its own:

```json
{
  "default_mode": "shadow",
  "filters": {
    "image_format": "enforce",
    "text_format": {"mode": "enforce", "settings": {"min_chars": 8, "placeholders": ["Opisz zdjęcie"]}}
  }
}
```

`settings_from_dict` rejects unknown keys, and each settings class checks its own values: a list field given a bare string, a threshold given `"5"` instead of `5`, or a ratio outside 0..1 all fail when the config loads rather than once a submission reaches the filter. Without that, a filter would construct fine, then raise inside `check()` on every submission, and the pipeline's fail-open rule would quietly mean it never worked.

**Where the wiring lives.** This PR parses config into settings; turning a config file into filter instances (and checking that its filter names are real) is the orchestrator's job in #23. Until that lands, a caller builds `TextFormatFilter(TextFormatSettings.from_dict(...))` itself.

## Near-duplicate detection (issue #16)

Catches what exact hashing misses: a photo re-saved or resized, and a caption reused with small edits. No embeddings: a model call would break the cost premise. dHash and SimHash are deterministic, local and bounded.

### Scenarios

| Scenario | Verdict |
|---|---|
| Same user, same photo re-encoded or resized | **REJECT** `NEAR_DUPLICATE_IMAGE_SAME_USER` |
| Another user, same photo lightly edited, first time | **FLAG** `NEAR_DUPLICATE_IMAGE_TEXT_ONLY` + suspicion log |
| That user edits the same photo again | **REJECT** `NEAR_DUPLICATE_IMAGE_SAME_USER` |
| Same user, near-identical caption on a *different* photo | **REJECT** `NEAR_DUPLICATE_TEXT_TEMPLATE` |
| Another user, long near-identical caption | **REJECT** `NEAR_DUPLICATE_TEXT_ACROSS_USERS` |
| Different users, different photos, similar captions (same landmark) | **PASS** |
| Identical *short* captions, same or different user ("Pierogi ruskie") | **PASS** |
| Caption too long to compare whole | **FLAG** `NEAR_DUPLICATE_TEXT_UNCOMPARABLE` |
| Same caption on the same photo | **PASS** here; the image filters own it |

### Strictest question first

Each rule is its own lookup, asked in order: "does *this user* already have something this close?" (reject) before "does *anyone else*?" (flag or reject). Asking only "what is nearest?" made the verdict depend on whose fingerprint happened to be closest, which let a user collect unlimited text-only flags by editing someone else's photo repeatedly.

**Text-only is capped at one per user per photo**, as with exact duplicates. The cap attaches to the *photo copied*, not to the user's edit of it: two edits can sit further from each other than either sits from the original, so when a submission is downgraded, the original's fingerprint is recorded in that user's bucket too.

**Only originals enter the global bucket.** First claim wins, so a later duplicate is always attributed to the copier.

### Bounded input

A filter upstream may be in shadow mode, which never stops a submission, so these functions bound themselves rather than trusting `image_format` or `text_format`:

- **Photos:** dimensions are read from the lazy `open()` and anything above `max_pixels` (50 MP, the same bound as `image_format`) is not decoded. For JPEG, `draft()` asks the decoder to downscale while decoding, so a 12 MP photo never exists at full size: **152 ms to 35 ms** on a 12 MP JPEG. A photo that cannot be fingerprinted passes with `fingerprint: skipped` and a reason, rather than raising.
- **Text:** a caption is compared whole or not at all. Over 8,000 raw characters, or over 2,000 once invisible characters are removed, it is **FLAG** `NEAR_DUPLICATE_TEXT_UNCOMPARABLE` for the judge and nothing is recorded. Comparing only a prefix was defeatable: invisible padding in front of a copy canonicalised to an empty string, and a unique preamble could push the copied part outside the prefix.

### Invisible edits

Fingerprints and text-only keys are built on a canonical form: NFKC, Unicode category Cf removed, whitespace collapsed, case-folded. Zero-width characters inserted between letters, or Polish letters typed in decomposed form, render identically but used to move the hash far past the threshold. `text_format` deliberately lets Cf characters through because emoji depend on them, so this is where they are neutralised.

### Concurrency

`commit` decides and inserts as one step under the window's lock, so two simultaneous near-identical uploads cannot both win. Hashing happens first and outside the lock, so one worker's SimHash or SHA-256 never blocks another's commit. Tested with 16 threads racing, for photos and for captions: exactly one keeps the photo.

Fingerprints are cached by a **digest** of the input, never the input itself: keyed on content, so a corrected resubmission under the same id is judged afresh, and bounded, so the cache holds kilobytes rather than up to 256 photos of 25 MiB.

### Thresholds, and what they rest on

| Text edit | Distance (of 64 bits) |
|---|---|
| Punctuation only | 2 |
| Typo | 5 |
| Words reordered | 9 |
| One word swapped | 11 |
| Diacritics stripped | 13 |
| **One honest pair describing one landmark** | **16** |

| Photo edit, with `draft` | Textured test photo | 12 MP phone-style photo |
|---|---|---|
| Resize to 50% and recompress | 8 | 1 |
| Recompress at quality 10 | 12 | 0 |
| **Different photos** | **24 to 34** | **29 to 35** |

`same_user_max_distance: 10`, `cross_user_max_distance: 4`, image `max_distance: 10`.

**These are synthetic measurements, and the honest-pair floor rests on a single pair.** Noise photos have independent hashes by construction, so they say little about two honest photos of the same landmark from the same viewpoint, which is the campaign's normal case and can land much closer. A false positive there is not free: cross-user it downgrades an honest contributor to text-only. Recalibration against real Bielik submissions is #27, and nothing here should be enforced before it.

### Measured latency (Python 3.11.9, Windows laptop)

| Step | Time |
|---|---|
| Image `check`, 12 MP JPEG, cold decode, 2k-photo window | ~25 ms |
| Image `commit`, fingerprint cached | ~5 ms |
| Text `check`, worst case: 10k captions in the user's bucket and 10k in the global one | ~16 ms |
| Text `check`, 2,000-character caption | ~17 ms |

**Scale ceiling.** Lookup is a linear scan, about 0.1 microseconds per entry, under one lock. That is fine into the tens of thousands of entries per bucket, which is what is measured. The global bucket holds every photo in the retention window, so at hundreds of thousands of photos a lookup reaches tens of milliseconds and serialises behind the lock. Multi-index hashing is the next step if the metrics show the need; at a 10-bit threshold it narrows candidates by only about six times, so it is not free either. Buckets expire lazily, one at a time as they are touched, with an occasional full sweep.

**Known gaps for v1, accepted:** heavy crops and rotations defeat dHash, and deep semantic paraphrase and stripped diacritics defeat SimHash. Both are what the judge is for.

## The stage, wired together (issue #23)

```python
from s2e_validator.prefilters import build_pipeline, load_config

pipeline = build_pipeline(load_config("config/example_campaign.json"))
outcome = pipeline.run_pre_filters(interaction)

if outcome.proceeds_to_judge:
    judge(interaction, flags=outcome.flags)   # stage 2, not built yet
else:
    reject(outcome.reason_code, outcome.decided_by)
```

Filters run in `cost_rank` order — text checks, image checks, exact hashing, then the two near-duplicate scans:

| Order | Filter | Cost |
|---|---|---|
| 10 | `text_format` | string work |
| 12 | `image_format` | decodes the photo |
| 15 | `submission_velocity` | dict lookup |
| 20 | `image_exact_duplicate` | SHA-256 + dict lookup |
| 30 | `image_near_duplicate` | dHash + window scan |
| 32 | `text_near_duplicate` | SimHash + window scan |

**Check, then commit.** Every filter is checked first; only if the stage passes does each filter `commit` — claiming a photo hash, recording a fingerprint. So a submission rejected by *any* filter claims nothing and can be fixed and resubmitted. A `commit` may return a revised verdict when a contested claim was lost, and an enforced REJECT there still stops the submission.

**Rejected at commit time, too.** A commit-time REJECT arrives after earlier filters have already claimed their keys, so the stage calls `rollback` on each of them: `ImageKeyStore.release(key, submission_id)` gives a key back only to the submission that holds it, the near-duplicate windows drop that submission's fingerprints, and `SuspicionLog.retract` withdraws any cross-user match it recorded, so the corrected resubmission is not counted as a second copy of the same photo. Without it the loser of a claim race would be locked out by its own abandoned claim. Rollback is fail-open like `check`: a filter that cannot undo its claim logs it rather than crashing the stage.

**Later filters' check results are kept.** When a commit rejects, the filters after it never commit, but their `check` already ran, and those records stay in the result. Shadow mode exists to collect exactly that evidence.

**What reaches Stage 2.** Only PASS proceeds. Flags travel with it as context (`outcome.flags`), so the judge knows a submission is, say, text-only because someone else owns the photo. A REJECT never becomes a batch entry at all, so it leaves no on-chain trace — the mapping raised in #12 and #28 is still open, and switching a reject to `Q_i = 0` instead would be a change here, not in the filters.

### Everything is config

`build_pipeline` constructs every known filter from `FILTER_BUILDERS`; config alone decides which run, which can reject, and with what thresholds. A config naming an unknown filter, or an unknown key, fails loudly rather than silently doing nothing. That rule leaves no room for comment keys, so the notes live here instead of in the file.

`config/example_campaign.json` is the worked example: every filter not listed runs in shadow mode, so a new filter can never start rejecting contributors by accident. Promote a filter to `enforce` only once its shadow numbers look right. A test loads that exact file, so the example cannot rot.

**Reuse one `FilterDependencies` across reloads.** Retuning a threshold means calling `build_pipeline` again, which builds new filter objects. The key store, the suspicion log and the fingerprint windows all live on `FilterDependencies`, so passing the same instance carries that state across the reload; a fresh one would hand the new filters empty windows and quietly disable duplicate detection for a whole retention period. Each of them is injectable, so a durable or shared implementation can replace the in-memory default.

| Mode | Effect |
|---|---|
| `off` | The filter does not run at all |
| `shadow` | Runs and logs what it *would* reject; never blocks. **The default for anything not listed.** |
| `enforce` | A REJECT stops the stage |

## Submission velocity (issue #20)

The only check in the stage that is not content-based. A script can produce submissions that each look fine; what it cannot do is slow down.

| Submissions in the window | Verdict |
|---|---|
| Under `flag_threshold` | PASS |
| `flag_threshold` or more | **FLAG** `SUBMISSION_RATE_ELEVATED` |
| `reject_threshold` or more | **REJECT** `SUBMISSION_RATE_AUTOMATED` |

Defaults: a 600 second window, a flag at 12 and a rejection at 30. `reject_threshold` must be above `flag_threshold`, or the flag could never fire.

**Why those numbers.** The campaign asks for at least 100 characters of description per photo, so a person needs roughly half a minute per submission even when hurrying. A flag at 12 in ten minutes is above one a minute sustained; a rejection at 30 is three a minute, which nobody is typing. Someone emptying a holiday camera roll in one sitting stays well under both, and that case is a test. These are reasoned starting points, not measurements: recalibrate on real submission times (#27).

**Counted from the submission's own timestamp**, not wall clock, so a backlog replayed after an outage is not read as a burst. `submitted_at` must therefore be the time the platform received the submission: a timestamp the contributor could set would let a script spread itself out on paper.

**`check` reads, `commit` records and counts again.** Several submissions from one contributor can pass `check` together, each seeing the count before the others are recorded. So `commit` records and counts in one atomic step, and its verdict is the authoritative one. Tested with three arriving at once, and with 16 threads recording 400 submissions.

**Only submissions that pass the whole stage are counted.** One rejected by any filter never happened, rate-wise: a `check`-time reject is never committed, and a `commit`-time reject is rolled back, including this filter's own. A retry is the same event, not a faster contributor, and a re-validated submission does not count itself twice. The record belongs to the submission id, not to one validation run: if an accepted submission is validated again and the stage now rejects it, its record goes, the same rule `ImageKeyStore.release` follows.

**A throughput cap, not a ban.** Because rejected attempts are not counted, a script that keeps trying is held to just under `reject_threshold` accepted submissions per window (29 per ten minutes at the defaults), each flagged for the judge from the 12th. It is slowed, not stopped. That is accepted for v1: `check` must not change state, so blocking a caught script outright needs a separate attempts counter and cooldown written by the stage on an enforced reject. Recalibrating the thresholds on real submission times is #27.

**State.** `SubmissionTimesStore` lives on `FilterDependencies`, like the key store and the fingerprint windows, so the rate survives a config reload. Each contributor's history is kept sorted by time and indexed by submission id, so counting is a binary search and nothing scans: the cost of one submission does not grow with the volume a script sends (about 0.02 ms per `check` and `commit` with 430,000 entries retained for one user, against 108 ms for a linear scan). The shared default store remembers one hour, deliberately short, because in shadow mode a script's submissions are all recorded and retention is what it can make the store hold. It trims a contributor's entries as they are touched, correctly even when a replayed backlog arrives out of order, and sweeps out contributors who never came back. A store that remembers less than the window is refused when the filter is built, since it would undercount silently; a window over an hour needs a store built with a longer retention. It is not durable yet: a restart forgets the last window, which only delays a flag by one window.

## Face detection (issue #21)

Faces are excluded by the campaign's own rules, so this filter enforces a **client rule**, not a quality judgement.

| Setting | Default | Why |
|---|---|---|
| `action` | `reject` | The campaign bans faces outright. Set `flag` to send the decision to the judge instead. |
| `score_threshold` | `0.6` | YuNet confidence below this is ignored |
| `min_area_ratio` | `0.0` | Catches every face, including a distant bystander. Raise it if only the photo's subject matters. |
| `max_side` | `1024` | Photos are downscaled first; cost scales with pixels |
| `max_pixels` | `50,000,000` | Never decoded above this, read from the header alone. Same bound as `image_format`, which may be in shadow mode and so cannot be relied on to stop a decompression bomb. JPEGs are also decoded at reduced size from the start (`draft`). |

**Detector:** OpenCV's YuNet, 227 KB, MIT licensed, committed to `models/` so the stage needs no download and behaviour cannot shift under us mid-campaign. OpenCV is capped below 5, because this model file is the one documented for OpenCV 4.

**Fail at startup, not quietly all campaign.** The model loads lazily, so one bad submission cannot stop the stage. But a model missing from the deployment would then fail open on *every* submission, and no face would ever be caught. So whatever serves the stage must call `pipeline.preflight()` once before serving (the API, #43, will; until then, call it yourself): each enabled filter checks it can run (face detection loads the model and runs it on a blank image), and `PreflightFailed` names the filter that cannot. Filters that are off are not checked.

**Photos are checked upright.** Phones store portrait photos sideways with an orientation tag, which Pillow does not apply by itself, and the detector is not rotation-invariant. The tag is applied before detection; a tag that cannot be read leaves the photo checked as stored, never unchecked. A very thin image is scaled to at least 1 pixel each way rather than failing, and running out of memory surfaces as an error, never as a pass.

**Measured:** about 63 ms per photo at `max_side` 1024, 47 ms at 640, 22 ms at 320. It is the most expensive filter by an order of magnitude, hence `cost_rank` 40: last, after everything cheap has had a chance to reject.

**Privacy:** the result records that a face was found, how many, and the largest one's relative size. It never records where, because this filter exists to protect people in photos.

**Blur-and-pass is not implemented.** This stage inspects submissions and never rewrites them, so blurring would be a transform step, not a filter. That is the client decision still open in #28: reject, or blur and keep. Switching to `"action": "flag"` is a config change; blurring would need new machinery.

**Honest limit on the tests.** The positive test uses a *drawn* face, which YuNet detects reliably, alongside checks that the suite's ordinary photos are never flagged. Real photographs are the only real validation, and they arrive with the Bielik data: re-measure then, and check whether the client's existing automatic check already covers this before enforcing (#28).

## Metrics (issue #24)

```python
metrics = StageMetrics()
pipeline = build_pipeline(load_config("config/example_campaign.json"), metrics=metrics)
...
print(render_snapshot(metrics.snapshot()))
```

```
filter                   checked   pass   flag  reject  shadow  err  mean ms   max ms
-----------------------------------------------------------------------------------
text_format                 1204   1131      0      73       0    0     0.04     0.31
image_format                1131   1128      0       3       0    0     3.10    11.87
image_exact_duplicate       1128   1109     12       7       0    0     0.09     0.44
image_near_duplicate        1121   1106      9       0       6    0     3.44    12.02
text_near_duplicate         1121   1108      0       0      13    0     2.18     9.51

submissions=1204 passed=1121 rejected=83 flagged=21 reject_rate=6.9%
judge_calls_saved=83 (~$0.33 est.) mean_stage=6.2ms
```

*(the shape of the output; the numbers are illustrative until real traffic runs through it, but they follow the pipeline's rules: only an enforced reject removes a submission, so flags and shadow rejections still reach every later filter)*

| Column | Meaning |
|---|---|
| `pass` | Nothing found. Per filter this **excludes** flagged submissions; the stage's `passed` counts everything that went on to the judge, flagged or not |
| `reject` | Rejections that actually stopped a submission: enforcing filters only |
| `shadow` | **What a shadow filter would have rejected.** The number to read before promoting a filter to `enforce` |
| `flag` | Soft signals passed to the judge as context |
| `err` | The filter crashed; the submission passed anyway (fail open) |

A record can be an error *and* a verdict: when `commit` crashes after `check` returned one, both count, so a row can add up to more than `checked`.

Latency covers both halves of a filter's work, `check` and `commit`, so a filter whose cost sits in its claim step is not reported as free.

**Reason codes are counted by what they did**, as `(outcome, code)` with outcome `reject`, `shadow` or `flag`, so "what are we rejecting, and what would we reject?" is answerable per campaign. They are counts, not a trail: tracing one contributor's appeal needs the submission's own `PipelineResult`, which the metrics do not keep.

**Snapshots are copies.** `snapshot()` returns frozen data the collector never touches again, so a snapshot taken earlier never changes and can be read or serialised on any thread.

**Judge calls saved.** Every enforced rejection is a judge call not made. The dollar figure multiplies by `DEFAULT_JUDGE_CALL_USD` (0.004): an operations estimate for reading the table, a placeholder until a judge model is chosen, and not an input to any budget, fee or reward.

**Alerting, two monitors.** Each `RateMonitor` watches one rate over a rolling window and logs one warning when it crosses its ceiling and one when it recovers, never one per submission:

| Monitor | Counts a submission when | Default | Why |
|---|---|---|---|
| `reject_rate` | an enforced filter rejected it **or a shadow filter would have** | above 50% of the last 200 | A spike usually means a broken filter, not more farming. Counting shadow rejections is what makes it work in the all-shadow default |
| `error_rate` | any filter crashed on it | above 5% of the last 200 | A crash fails open into a PASS, so it pushes the reject rate *down*; only a separate monitor sees it |

`AnomalySettings` refuses combinations that could never fire, such as a minimum larger than the window or a ceiling of 100%. Monitors log only after every lock is released, so a log handler can safely read the metrics, for example to attach the current table to an alert. A metrics sink can never break the stage: if `observe` raises, the error is logged and the result is still returned, because by then every filter has committed its claims.

`snapshot().to_dict()` is JSON-ready for a dashboard; `render_snapshot(snapshot)` is the table above.

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
