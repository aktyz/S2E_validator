# Peer PR Review

You are reviewing a colleague's pull request on the **Survey2Earn validator** repository — the off-chain quality validator, split out of the Survey2Earn monorepo into its own repo.
Act as a thoughtful, professional peer reviewer. Findings must name the rule or principle violated and include a concrete suggested fix — not just "this is wrong."

---

## Precedence — the CLAUDE.md Restrictions Win

If anything in this skill conflicts with the **Restrictions** section of `CLAUDE.md`, the Restrictions win, without exception. In practice: **you never run `git` (including read-only `git`), `find`, `ls`, `grep`, or any package-manager/build/test/lint command (`pip`, `pytest`, `python -m pytest`, etc.) yourself.** Any step below that needs the output of one of these commands is satisfied by *handing the command to the user and waiting for them to paste the output back* — never by running it, and never by silently substituting a different tool that does the same job.

**File reads work differently, and this is the one place the two mechanisms in this skill split apart.** `CLAUDE.md` permits `Read` on a file the user has named, or on a file you asked permission to read and the user approved (the batch-ask protocol: list the exact paths with a one-line justification each, then stop and wait for a yes). The fixed-path manifest this skill needs (Step 2a) is exactly that case: its path is known in advance, so instead of asking for a `cat` command and a pasted-back wall of text, **request permission to read it by path, wait for approval, then read it yourself with `Read`.** This is not "using `Read` to work around `cat`" — `cat` was never the requirement; the requirement is that you never read a file the user hasn't named or approved, and asking-then-`Read`ing satisfies that directly. It does **not** extend to `git diff`/`git log` (Steps 1/3) or any build/test/lint command — those have no `Read`-tool equivalent and stay on the hand-over-and-wait path.

**Every command you hand over, and every read you request permission for, must be explained. This is stated once here and applies throughout this skill** — it is not repeated at each step. For each command block:

1. Give the exact command in a fenced code block, written with **absolute paths** for the directory the user will actually run it from.
2. Explain in one or two sentences of plain English what the command does, so the user learns the shell.
3. Ask the user to run it and paste the output back — then **stop and wait**. Never guess what the output would have been.

For each file-read permission request:

1. List the exact absolute path(s), with a one-line justification per path — what you need from it and why the review can't proceed without it.
2. Ask for a yes — then **stop and wait**.
3. Once approved, call `Read` yourself. Do not re-ask for the same path later in the same review.

**This skill is repository-agnostic — there is no hard-coded checkout path.** Every command and output path below is written against `<repo-root>`, which means **the absolute path of the project directory this command was invoked from** (the primary working directory reported in your environment). Resolve it once, at the start of the review, and substitute the real absolute path into every command or read request *before* handing it over — never emit one that still contains the literal `<repo-root>` placeholder. If you are not certain what the absolute repo root is for this checkout, ask the user for it *before* emitting anything containing a path.

**The repo root is the validator package.** It uses a setuptools src-layout: `pyproject.toml` at the root, the package under `src/s2e_validator/`, tests under `tests/`. The on-chain Survey2Earn program is **not** in this repo; it lives in the separate Survey2Earn repository. If the Step 1 diff shows a different layout, map the steps onto the directories that actually exist rather than insisting on these names.

**WebFetch is not restricted and must not be skipped.** The Restrictions govern the shell and filesystem access. `WebFetch` does not shell out and does not touch the user's filesystem, so the live-docs fetching in Step 2 stays **mandatory and is performed by you directly, without asking**. Do not over-apply the restriction and skip the docs — a review written from version-frozen training data is precisely the failure this skill exists to prevent.

---

## Review Modes — Initial or Follow-up

The review file `<repo-root>/docs/reviews/PR_<number>_REVIEW.md` is **the only memory this skill has between sessions.** A follow-up is usually run in a brand-new session that has never seen the earlier review, so everything a follow-up needs — what was reviewed, at which commit, what was found, what is still open — must be recoverable from that file alone. That is why the file is stateful and append-only (see *Round Layout, State Header, and Finding IDs* under Output Format).

**`$ARGUMENTS` keywords.** After the PR number, an optional mode keyword may follow:

| `$ARGUMENTS` | Behaviour |
|---|---|
| `<PR>` | Auto-detect (below). |
| `<PR> followup` | Follow-up required: if the review file does not exist, stop and tell the user rather than silently starting an initial review. |
| `<PR> fresh` | Ignore any existing review and start over. This discards earlier rounds, so **ask the user to confirm before writing**, and say that the file may have no history to recover it from. |

**Auto-detect.** Put the review file's path in the Round 1 file-read permission request, marked *"read only if it exists"*, with the justification: "the previous review of this PR, if any — if present, this run is a follow-up and its findings are the checklist; if absent, this is an initial review." After approval, `Read` it. **Read succeeds → follow-up. File not found → initial review.** Do not ask the user which mode they want; the file's existence decides it. The mode is not known until that read happens, so the Round 1 message is identical in both modes.

A file that exists but has no `review-state` header is a **legacy file** (written before this skill kept state). It is still a follow-up; Step F1 says how to handle the missing state.

The follow-up procedure is *Follow-up Mode — Steps F1–F3*, after Step 5. Its output template is under Output Format.

---

## Request Rounds — Declare the Two-Stage Shape Up Front

This review needs the user to run commands and approve file reads. It cannot be done in a single batch, because **the list of source files worth reading is derived from the diff and does not exist until the diff does.** So the review runs in exactly **two request rounds**:

- **Round 1 — scope and versions.** One message containing: the diff commands and the PR-description fetch command (Step 1), the commit-log command (Step 3), and a single file-read permission request covering the manifest (Step 2a) and the previous review file if one exists (see Review Modes) — each path with its one-line justification. The PR-description command also returns the PR conversation (comments and reviews) in the same paste. Then wait.
- **Round 2 — source files.** One message containing a **single batch request** for the specific source files you need to read, each with a one-line justification: what you need from it, and why the review cannot be completed without it. In a **follow-up**, this message also carries the delta-diff commands (Step F1), the filtered review-thread replies command (see *Follow-up Mode*, Step 1), and the files needed to re-verify open findings. Then wait.

Before Round 1, announce the structure to the user, e.g.: *"This review runs in two request rounds — first scope and versions, then one batch request for the specific source files the diff points at."* If `$ARGUMENTS` includes `followup`, say so; otherwise add that the run becomes a follow-up if a previous review file for this PR turns out to exist.

**This declared two-stage structure is the intended shape of the review, not under-scoping.** The batch-ask protocol forbids drip-feeding requests you could have foreseen; it does not forbid a second round whose contents are genuinely unknowable until the first round returns — which is why the split is declared in advance rather than discovered halfway through. Within each round, ask for everything at once. A **third** round means you genuinely under-scoped, and you must say so plainly to the user when it happens.

**Before Round 1 can even be built, you need the PR number** (see Step 1) — the output filename depends on it. If `$ARGUMENTS` doesn't supply one, ask for it first and treat that as a round of its own, distinct from the two above.

---

## Your Role

Act as a **thoughtful peer reviewer**, not a linter. Your job is to:

- Understand what the change is trying to accomplish by reading the diff and commit messages
- Catch correctness bugs, logic errors, and security issues
- Flag design problems — SOLID violations, leaky abstractions, wrong layer of responsibility
- Note anything that will cause maintenance pain later
- Praise genuinely good decisions — positive reinforcement matters

---

## Review Process

Follow these steps in order. Do not skip any step.

### Step 1 — Resolve the PR Number and Diff Target, Request the Diff

`$ARGUMENTS` must begin with the **GitHub PR number** — the output filename depends on it. Strip any mode keyword (`followup` or `fresh`, see *Review Modes*) from the rest, then interpret what remains to build the diff command:

| `$ARGUMENTS` value | Command to hand to the user |
|--------------------|-----------------------------|
| PR number only | `git diff origin/main...HEAD` |
| PR number + one branch/commit | `git diff <arg>...HEAD` |
| PR number + two values | `git diff <arg1>...<arg2>` |
| No PR number present | **Stop.** Ask the user for the PR number before doing anything else. |

**Do not run this command.** Include it in the Round 1 message, together with the `--stat` form so you can see the shape of the change before reading the full body:

```bash
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
```

*Explain it roughly like this:* "The first command summarises which files changed and by how many lines each; the second prints the full text of the change. `origin/main...HEAD` means: compare this branch against the point where it last diverged from the remote `main`, so only this branch's own work is shown."

> **Why `origin/main` and not `main`?** A local `main` drifts from the remote the moment the user stops pulling, so diffing against it can silently review the PR against a stale base — hiding changes that are already upstream, or attributing upstream changes to this branch. **Fallback:** if the remote-tracking ref is not present (the user reports `fatal: ambiguous argument 'origin/main'` or similar), use `main...HEAD` instead **and state explicitly in the review** that the base may be stale because no remote-tracking ref was available.

> **Why three dots here?** For `git diff`, `A...B` diffs `B` against the **merge base** of `A` and `B` — exactly what a GitHub PR shows. Plain `A..B` compares the two endpoints directly, so commits that landed on `A` after the branch point appear inverted, as though this PR were reverting them. Note that Step 3 uses **two** dots for `git log`: the same syntax means something different for that command, and the reason is given there.

**Also include, in the same Round 1 message, a command to fetch the PR description from GitHub** — the diff and commit log tell you *what* changed, not what the author *says* they were trying to do:

```bash
gh pr view <PR-number> --json title,body,url,baseRefName,headRefName,headRefOid,comments,reviews
```

*Explain it roughly like this:* "Fetches the PR's title, description body, URL, base/head branch names, the full hash of the newest commit GitHub has on the PR branch, and its conversation (comments and submitted reviews) from GitHub via the `gh` CLI — it talks to GitHub's API, not to your local git history or working tree." The conversation matters most in a follow-up: it holds the author's replies to earlier questions and their reasons for declining a finding. In an initial review it still gives context the description leaves out.

Two of these fields feed the draft GitHub review written at the end (see *GitHub Draft Review Payload*): `url` (`https://github.com/<owner>/<repo>/pull/<number>`) supplies the `<owner>/<repo>` for the post command, and `headRefOid` is the commit the draft's inline comments attach to.

> **Why `gh` and not `WebFetch`?** `gh pr view` runs under your locally authenticated GitHub session, so it works the same for a private repo as a public one. `WebFetch` has no such session and would either fail outright on a private repo or return a stripped-down public page. `gh` is a separate CLI from `git` — it talks to GitHub's API rather than reading or mutating the local repository — but it still reaches outside data you haven't yet seen, so it gets the same treatment as every other command here: handed over, never run by the assistant, and the pasted-back output is what gets read, not a guess at what it would say.

If the user reports `gh` is missing, not authenticated, or the PR doesn't exist yet (e.g. pushed but not opened), record that in the Review Baseline and fall back to commit-message and diff inference alone for Step 3 — do not block the review on it.

Scan the diff and note:
- Which areas of the package are touched (e.g. `src/s2e_validator/prefilters/`, other modules under `src/s2e_validator/`, `tests/`, `pyproject.toml`)
- New files added and their purpose
- Files deleted and whether anything still depends on them
- Files whose names carry special meaning (e.g. `pyproject.toml`, `__init__.py`, `conftest.py`, a bundled model file such as `.onnx`)

Do not form conclusions yet — Step 2 fetches the docs needed to review the diff correctly.

---

### Step 2 — Resolve Versions and Fetch Live Docs

**This step is mandatory and must run before forming any findings. Training-data knowledge of library conventions is version-frozen and will produce wrong findings.**

Two different mechanisms are in play here, and you must not confuse them:

- **File/version extraction** reads a file on the user's disk at a known, fixed path: request permission by path (with justification), wait for the user's yes, then read it yourself with `Read`. See *Precedence* above for why this differs from the `git`/build/test/lint commands elsewhere in this skill.
- **Doc fetching** uses `WebFetch`, which is unrestricted. You perform it yourself, directly, without asking.

---

#### 2a — Manifest and Live Docs (always run)

1. You need the validator's dependency manifest. Include this path in the **Round 1** file-read permission request:

   ```
   <repo-root>/pyproject.toml
   ```

   *Justification:* "Declares the minimum Python version, runtime dependencies, optional extras, and pytest configuration for the validator." Skip this read if the diff already adds or fully rewrites `pyproject.toml` — its complete contents are already in the Step 1 diff, so a separate read would be redundant.

   From the content, extract the Python version floor, the runtime dependencies with their floor/cap ranges, the optional extras (`api`, `solana`, `dev`, or whatever exists now), and the pytest markers. If the diff adds a runtime dependency, adds a package to an extra, or moves a version cap, that is a deliberate architectural decision and must be reviewed as one (see Validator-Specific Rules), not treated as routine.

2. For each library or concept touched by the diff, fetch the matching reference:

   | Diff touches | Fetch |
   |---|---|
   | Version-sensitive stdlib features (e.g. `tomllib`, structural pattern matching, `typing.Self`, exception groups), or the Python floor changes | `https://docs.python.org/3/whatsnew/` — confirm the feature's minimum Python version against the floor |
   | Image decoding / Pillow | `https://pillow.readthedocs.io/en/stable/` |
   | OpenCV (e.g. `cv2.FaceDetectorYN`) | `https://docs.opencv.org/4.x/` |
   | numpy | `https://numpy.org/doc/stable/` |
   | The HTTP service (`api` extra: FastAPI, multipart uploads, uvicorn) | `https://fastapi.tiangolo.com/` |
   | Signing / transaction encoding (`solana` extra: solders) | `https://kevinheavey.github.io/solders/` |
   | Tests under `tests/` | `https://docs.pytest.org/en/stable/` — fixtures, parametrize, markers |

---

#### 2b — Record and Override

After fetching, state the following at the top of your review output (before the findings):

```
### Review Baseline
| Item            | Key Versions                                   | Docs loaded       |
|-----------------|-------------------------------------------------|-------------------|
| PR description  | found / not found / gh unavailable              | —                 |
| Validator       | Python X.Y, <deps/extras touched + ranges>, pytest X.Y | [list pages used] |
```

If any fetched doc contradicts the static rules in this skill, **the fetched content wins**. Call out the contradiction explicitly so the skill can be updated.

---

### Step 3 — Derive Goal and Scope

The PR description fetched in Step 1 is the **primary source of stated intent** — read its `title` and `body` first. Commit messages and the diff corroborate it, fill in what the description leaves vague, and become the **sole** source if the description came back empty, was never opened, or `gh` wasn't available:

1. You need the commit messages. **Do not run this** — put it in the **Round 1** message alongside the Step 1 diff and PR-description commands, using the same base resolved there:

   ```bash
   git log origin/main..HEAD --oneline
   ```

   *"List every commit that is on this branch but not on the remote `main`, one short line each — subject line plus abbreviated hash."*

   > **Why two dots here, when Step 1 used three?** The syntax means different things to the two commands. For `git log`, `A...B` is the **symmetric difference** — commits on either side that the other lacks — so it would also list commits that exist on the base and not on the branch, which are not part of this PR. The PR's own commit list is `A..B`, two dots. For `git diff`, three dots is still correct (see Step 1). The mismatch is deliberate, not a typo.

2. From the PR description, the commit messages, and the changed filenames, state explicitly:
   - **Goal** — what problem or feature is this change addressing? Lead with what the PR description claims; if the diff doesn't actually deliver what the description says, that gap is itself a finding (see Step 4), not something to quietly paper over by re-deriving a goal that matches the code.
   - **Scope** — every area touched (e.g. `src/s2e_validator/prefilters/`, `tests/`)

These become the benchmark for the Pull request overview and the Verdict. Do not skip this step — the overview section cannot be written without it.

### Step 4 — Verify Goals Against Code

For each goal derived in Step 3, answer:
- Is it fully implemented?
- Is it implemented at the right layer? Off-chain validation and reward-batch preparation belong in this repo; deterministic settlement, treasury vaulting, and lifecycle state belong to the on-chain program in the separate Survey2Earn repo (see Validator-Specific Rules).
- Does it work correctly, or only in the happy path?

Any rule from **Project Standards to Enforce** that is violated by the diff must appear as a finding in the appropriate classification (🔴 / 🟠 / 🟡) — do not list violations separately outside the Findings section.

Flag any gap between the apparent intent and what is actually delivered.

### Step 5 — Classify Each Finding

| Label | Meaning |
|-------|---------|
| 🔴 **Bug** | Will break at runtime or produces wrong behaviour |
| 🟠 **Design** | Violates SOLID, leaky abstraction, wrong layer, over-engineered |
| 🟡 **Style** | Naming, readability, comment quality, dead code |
| 🟢 **Praise** | A genuinely good decision worth reinforcing |
| 🔵 **Question** | Unclear intent — ask the author before assuming it is wrong |

For every 🔴 and 🟠 finding, include:
- The file and the **anchor line** — the verbatim line of code where the comment would best be placed (see *Comment Anchors* under Output Format). Do not cite line numbers.
- What principle or rule is violated and **why** that matters
- A short concrete suggestion or code snippet showing the fix

### Follow-up Mode — Steps F1–F3

Run these **instead of Steps 3–5** when Review Modes selected a follow-up. Step 5's classification labels still apply to new findings. Steps 1 and 2 run as written, with these differences:

- **Step 1** — the PR-description command also returns the conversation. Read it for the author's replies to earlier questions and their stated reasons for declining a finding. **`gh pr view` does not include the text of replies in inline review threads**: they show up only as `reviews` entries with an empty `body`. So in a follow-up's Round 2 message, always hand over this filtered command as well:

  ```bash
  gh api --paginate repos/<owner>/<repo>/pulls/<number>/comments | jq -rs 'add | INDEX(.id) as $by | .[] | select(.in_reply_to_id) | "--- \(.user.login) replying to: \($by[(.in_reply_to_id|tostring)].body | split("\n")[0])\n\(.body)\n"'
  ```

  *Explain it roughly like this:* "Fetches every inline review comment on the PR and prints only the replies: who wrote each one, the first line of the comment it answers (e.g. `**F-03 · 🟡 Style**`), and the reply text. `-s` with `add` joins the pages `--paginate` returns, so a reply on one page can still find its parent on another." `<owner>/<repo>` comes from the Step 1 `url`. **Never ask for the raw `pulls/<number>/comments` output, or redirect it to a file for you to `Read`.** Each comment carries its full `diff_hunk`, so the raw JSON is one line of hundreds of KB that `Read` can neither load nor page through. Asking for it forces a third request round.
- **Step 2a** — fetch live docs only for concepts the *delta* touches, and skip any page the previous round's Review Baseline already lists unless `pyproject.toml` changed in the delta. Restate the baseline only for rows that changed and write "Unchanged since round N-1." for the rest.

#### F1 — Load the previous state and size the delta

From the review file take: `round`, `reviewed_head`, `last_finding_id`, the goal stated in Round 1, and every finding still open (see F2). Then compare `reviewed_head` with the hashes in the `git log origin/main..HEAD --oneline` output (match on prefix, at least 7 characters):

| Situation | What to do |
|---|---|
| `reviewed_head` is in the log, with newer commits above it | Those commits are the delta. In the Round 2 message hand over `git diff --stat <reviewed_head>..HEAD` and `git diff <reviewed_head>..HEAD`, explained like the Step 1 diff. *Two dots compare exactly those two commits — "what changed since the last review". Three dots would use their merge base, which after a rebase can hide changes.* |
| `reviewed_head` is the newest commit and the conversation has nothing newer than the last round's `updated` date | Nothing to review. **Do not touch the file.** Reply with the one-line "nothing new" message from *Delivering the Review*. |
| `reviewed_head` is the newest commit, but there are newer comments | No delta diff. Run F2 using the author's replies only. |
| `reviewed_head` is not in the log (rebase or force-push), or the file has no header | History was rewritten or is unknown. Skip the delta, re-verify every open finding against the full Step 1 diff, and say in the round's summary that the delta could not be computed. |

**Legacy file (no header).** Treat it as a completed earlier round. Its findings carry no IDs, so assign `F-01`, `F-02`, … in document order in the status list of your new round (the old text stays untouched; the status list defines the mapping), and count `round` from 2.

#### F2 — Re-verify every open finding by its anchor

**Open** means: the finding, in the newest round that mentions it, is *Still open* or *Partially addressed*; or it has never been through a follow-up. Praise is never tracked. For each open finding, find its verbatim anchor line in the current diff or in a source file you were given, then decide:

| Result | When |
|---|---|
| ✅ **Resolved** | The code the finding pointed at is fixed. Judge against the finding's *problem*, not against the suggested snippet; a different fix that works counts. |
| ◐ **Partially addressed** | Some of it is fixed. Say exactly what remains. |
| 🔁 **Still open** | The anchor is present and the surrounding code is unchanged, or the change does not touch the problem. |
| 💬 **Won't fix** | The author declined, with a reason in the conversation. Say whether the reason holds. If it does not, keep the finding **Still open** and explain why; do not re-litigate a sound decision. |
| ↩ **Withdrawn** | You retract it: you were wrong, or a later change made it moot. Say why. |

- **An anchor that has disappeared is not proof of a fix.** Locate the replacement in the delta (or full diff) and judge it. If you cannot find it and lack the file, ask for it in the Round 2 batch.
- A 🔵 question the author has now answered becomes ✅ Resolved when the answer settles it, and turns into a finding if the answer reveals a problem.
- Evidence is a function name or a verbatim line — never a line number.

#### F3 — Review the delta for new findings

Apply Steps 3–5's standards to the delta only: code changed since `reviewed_head`, plus anything the fixes now interact with. Regressions introduced by a fix are findings. Do not re-review unchanged code that the previous round already covered. If the delta could not be computed, review the full diff for anything the earlier rounds missed and say so.

New findings continue the ID sequence from `last_finding_id` and use the anchor rules under Output Format. The finished round follows the follow-up template there.

---

## Project Standards to Enforce

### Universal Rules
- **SOLID** — Single Responsibility, Open/Closed, Liskov Substitution, Interface Segregation, Dependency Inversion
- **Composition over inheritance**
- **Functions must be small and single-purpose** — if a function does two things, it should be two functions

### General Practices

Carried over from the Survey2Earn monorepo's architecture rules, keeping only what applies to off-chain Python:

- **Modular design, clear responsibility boundaries, minimal coupling** — a module should have one reason to change, and should not reach into another module's internals.
- **File size** — 300 lines recommended, 500 maximum. A file past 500 lines should be split by responsibility; flag it (🟠) rather than accepting "it's all related."
- **Helpers stay pure** — shared utility functions take inputs and return outputs; they do not mutate module-level or caller-owned state.
- **Validate inputs at the boundary** — every public entry point (API handler, pipeline entry, CLI) validates its arguments before doing work, rather than trusting the caller.
- **Explicit, descriptive errors** — errors are raised as named exception types with messages that say what failed and why, not bare `Exception` or silent `None` returns.
- **No hallucinated APIs** — any library call introduced must be verifiable against the docs fetched in Step 2a. Flag anything that doesn't match the fetched reference rather than assuming it's correct.
- **Extend rather than rewrite** — changes to an existing module should be backward compatible, or the PR should justify the break. New behaviour that doesn't fit an existing module goes in a new one (Open/Closed).
- **Mind the cost** — flag unnecessary repeated work, such as decoding the same image twice or reloading a model per request, and loops that scale with untrusted input size.

### Validator Stack

| Concern | Tool |
|---------|------|
| Language | Python 3.11+ |
| Runtime dependencies | `pillow>=11,<13`, `opencv-python-headless>=4.9,<5`, `numpy>=1.26` |
| Optional extras | `api`: FastAPI, python-multipart, uvicorn · `solana`: solders · `dev`: pytest, httpx |
| Testing | pytest 8+; wall-clock latency tests marked `slow` and excluded by default (`addopts = "-m 'not slow'"`) |
| Packaging | setuptools ≥68, src-layout (`src/s2e_validator/`), tests in `tests/` |
| Linting/Formatting | None configured in `pyproject.toml` as of the last check |

### Validator-Specific Rules

> ⚠️ These rules reflect the stack at the time this skill was written. They are **defaults only**.
> Step 2a reads the live `pyproject.toml` and fetches live docs where relevant — if either contradicts anything here, it wins and this list is stale.

- **Dependency discipline** — every entry in `pyproject.toml` carries a floor, a cap where a major release could change behaviour, and a comment saying why it exists and what version it was tested against. A new dependency or a lifted cap is an architectural decision: raise it as a 🔵 Question if the PR doesn't explain it. A missing cap or comment is a 🟡. A dependency that parses untrusted input (uploads, multipart bodies) needs a maintained floor that includes known security fixes.
- **Core vs. extras** — the core pipeline needs none of the optional extras. A module on the core import path that imports an extras-only package (e.g. `fastapi`, `solders`) breaks a plain install: 🔴.
- **Untrusted uploads** — contributor photos and form bodies are untrusted. Check size and format before decoding, and handle decoder failures as validation results, not crashes.
- **Prefilter pipeline boundaries** — each prefilter under `src/s2e_validator/prefilters/` should be a small, single-purpose, independently testable unit implementing a shared explicit interface (ABC/`Protocol`), not duck-typed. New prefilters should extend the pipeline without modifying its runner (Open/Closed).
- **No settlement logic off-chain** — the validator may prepare, encode and sign reward batches for submission, but reward settlement, treasury vaulting, and SurveyRun lifecycle state belong to the on-chain program in the separate Survey2Earn repo. A validator module that computes final payouts as the authority, or tracks on-chain-equivalent accounting state of its own, is a layer violation (🟠 at minimum, 🔴 if it duplicates or bypasses the on-chain settlement engine).
- **No hardcoded cluster/program identifiers** — RPC URLs, program IDs, keypair/wallet paths and secrets must be configurable/injected, not embedded in validator source.
- **Test coverage** — a new prefilter or module without a corresponding test under `tests/` is a gap; flag it. A test that asserts on wall-clock time must carry the `slow` marker.

---

## Output Format

This block describes the **contents of `PR_<number>_REVIEW.md`** — it is not a template for a terminal message. Everything below goes in the file and only in the file (see *Delivering the Review*). The one other file this skill writes is the draft-review payload described under *GitHub Draft Review Payload*: a machine-readable projection of the same round for posting to GitHub, not a second copy of the review.

The block below is the file for an **initial review**: the state header, then Round 1. A follow-up adds a round above it using the template under *Round Layout, State Header, and Finding IDs*. Every finding except 🟢 Praise starts with its ID (`F-01`, `F-02`, … in order of appearance across 🔴, 🟠, 🟡, 🔵).

```
<!-- review-state
pr: <number>
round: 1
reviewed_head: <newest hash from the git log output, as printed>
base: origin/main
verdict: <Approve | Request Changes | Needs Discussion>
last_finding_id: F-<nn>
updated: <YYYY-MM-DD>
-->

# Round 1 — Initial review · reviewed <hash> · <date>

## PR Review: <title inferred from commits/diff>

## Pull request overview

<One-paragraph narrative: what problem this PR solves and why it matters. Ground this in the fetched PR description's title/body where one was found; fall back to commit messages and the diff when it wasn't.>

### Changes:
1. <Area / concern>:
   - <Specific change>
   - <Specific change>
2. <Area / concern>:
   - <Specific change>
   - <Specific change>
<... one numbered item per logical group of changes ...>

### Impact
- <What improves or becomes possible as a result>
- <Who or what is affected: dev experience, runtime behaviour, reliability, etc.>

---

### Review Baseline
| Item            | Key Versions           | Docs loaded         |
|-----------------|-------------------------|---------------------|
| ...             | ...                     | ...                 |

### Summary
One paragraph: does this PR achieve its stated goals? Any critical blockers before merge?

### Findings

Every finding in the sections below uses the same shape — ID, file path, anchor line, then the text (see *Comment Anchors* after this block). 🟢 Praise takes no ID:

    **F-<nn> · `<file path>`**
    `<anchor line, verbatim>`
    <the finding text>

#### 🔴 Bugs
<file path> / <anchor line> — description, why it matters, suggested fix

#### 🟠 Design Issues
<file path> / <anchor line> — description, principle violated, suggested fix

#### 🟡 Style / Readability
<file path> / <anchor line> — description, suggested fix

#### 🟢 Praise
<file path> / <anchor line> — what is good and why

#### 🔵 Questions for the Author
<file path> / <anchor line> — what is unclear, what you need answered

### Verdict
**Approve** / **Request Changes** / **Needs Discussion**
One sentence explaining the verdict.

---

### GitHub Review Comment
> Becomes the summary body of the draft GitHub review (see *GitHub Draft Review Payload*); also usable as-is in the GitHub "Leave a comment" box.

<2–4 sentences in a professional, collegial tone. Cover: overall impression, the one or two most important points (blocker bugs or key praise), and a clear statement of the verdict. Do not repeat every finding — this is the message a human reviewer would type, not a list.>
```

### Comment Anchors

Line numbers drift (the reviewer's checkout, rebases, later commits) and are never exact, so **findings are located by content, not by number.** Each finding names the file and quotes the one line of code where a review comment would be most appropriately placed, so the reader can paste it into their editor's search and land on the spot.

Example — instead of:

    **`prefilters/text_format.py` ~L89–94** (control-character count)

write:

    **F-04 · `src/s2e_validator/prefilters/text_format.py`**
    `return _WHITESPACE_RUN.sub(" ", text).strip()`
    Control characters are counted after normalisation, so …

Rules for the anchor line:

1. **One physical line, copied verbatim** from the diff or from a source file you were given — never retyped from memory, paraphrased, or "cleaned up." Strip only the leading indentation and the diff's `+`/`-` marker. If you don't have the exact text in front of you, don't invent an anchor; ask for the file in the Round 2 batch.
2. **Pick where the comment belongs, not where the range starts.** For a multi-line problem, choose the single line a human reviewer would click to comment on: the line that *causes* the bug, the call that misuses the API, the `def`/`class` line for a whole-function design issue, the `return`/`raise` that shows the wrong behaviour. Not the first line of the range by default, and not a blank line, a lone brace, a comment, or an `import` unless the finding is about that.
3. **Prefer a line added by the diff** (a `+` line), since GitHub can only attach inline comments to lines in the diff. Use an unchanged context line only when the finding is about code the PR left untouched but now depends on, and say so.
4. **Make it findable.** It should be distinctive enough to hit once, or close to it, when searched in that file. If the best line is generic (`return None`, `pass`), pick the nearest distinctive line that still fits rule 2. If it genuinely can't be unique, add a short tag after the code span, e.g. *(inside `_count_control_chars`)*.
5. **Missing-code findings** (missing test, missing validation, missing guard) anchor on the line where the missing code *should be* attached — typically the `def` line, the call site that lacks the check, or the last line of the block. Say in the text that it is an absence.
6. **Deleted code** anchors on the `-` line text as it appeared before the PR; note *(removed line)* after the code span.
7. **Wrap the anchor in a Markdown code span.** If the line itself contains backticks, use a wider delimiter (` `` … `` `).
8. **File path** is written relative to `<repo-root>` — the project directory this command was invoked from (e.g. `/root/S2E_validator`), resolved as described at the top of this skill — in bold code formatting, so it is searchable as well. That means the full path from the repo root, e.g. `src/s2e_validator/prefilters/text_format.py` or `tests/test_text_format.py`. Never shorten it to the package or a subdirectory such as `prefilters/`, and never make it absolute. This is the same path git prints in diff headers, minus the `a/` / `b/` prefix.
9. **A finding spanning several files** is one finding per file: give each file its own ID, path and anchor rather than listing several in one heading.
10. **No line numbers, `~L` ranges, or `file:line` references anywhere in the review** — including the Summary, Verdict, and GitHub Review Comment. When referring back to a finding elsewhere in the file, use its ID (`F-07`); to point at code, name the function or quote the anchor. The one exception is the `line` field of the draft-review JSON, which GitHub's API requires: it is computed from the diff (see *GitHub Draft Review Payload*) and never appears in the Markdown review.

Omit any section that has no findings — do not write "None."
The GitHub Review Comment is always required — even for a clean Approve, write a short positive note.

### Round Layout, State Header, and Finding IDs

`PR_<number>_REVIEW.md` is **append-only**: one file, newest round first, older rounds never rewritten. Because a follow-up may run in a new session with no memory of the earlier one, the file is the only carrier of state.

```
<!-- review-state … -->        ← always the first thing in the file; describes the NEWEST round

# Round <N> — Follow-up review · …        ← newest round
…
---
# Round 1 — Initial review · …            ← the initial review, exactly as first written
```

**State header.** The `review-state` HTML comment (fields in the initial-review template above) reflects the newest round and is updated in place by every follow-up. `reviewed_head` is the newest commit in the `git log origin/main..HEAD --oneline` output at the time of that round. Do not add fields; a follow-up must be able to rely on exactly these.

**Finding IDs.** `F-01`, `F-02`, … assigned in order of appearance, **never renumbered or reused**, continuing across rounds from `last_finding_id`. The ID is the stable handle: a finding keeps it for its whole life, and every later status line refers to it. 🟢 Praise takes no ID and is never tracked.

**Follow-up round template.** This is the section inserted above the previous newest round:

```
# Round <N> — Follow-up review · reviewed <hash> · <k> new commits since <previous hash> · <date>

## Follow-up summary
<One paragraph: how much of the previous round was addressed, what still blocks, and anything new that matters. State plainly if the delta could not be computed (rebase, force-push, legacy file) and how you compensated.>

### Review Baseline (changes since round <N-1>)
<Only rows that changed, or "Unchanged since round <N-1>.">

### Status of earlier findings
- **F-01** · <short title> — ✅ Resolved — <evidence: the function or verbatim line that now fixes it>
- **F-02** · <short title> — 🔁 Still open — <what is unchanged>
- **F-03** · <short title> — ◐ Partially addressed — <what remains>
- **F-04** · <short title> — 💬 Won't fix — <the author's reason, and whether it holds>
- **F-05** · <short title> — ↩ Withdrawn — <why you retract it>

### New findings
#### 🔴 Bugs
**F-<next> · `<file path>`**
`<anchor line, verbatim>`
<description, why it matters, suggested fix>

<… 🟠 Design Issues, 🟡 Style / Readability, 🟢 Praise, 🔵 Questions for the Author — same shapes as the initial review, omitting any section with nothing in it …>

### Verdict
**Approve** / **Request Changes** / **Needs Discussion**
One sentence.

---

### GitHub Review Comment
> Becomes the summary body of the draft GitHub review (see *GitHub Draft Review Payload*); also usable as-is in the GitHub "Leave a comment" box.

<2–4 sentences about this round: what was addressed, what remains, the verdict. Do not repeat every finding.>
```

**Carry-forward rule.** The status list covers every finding that was open at the end of the previous round. ✅ Resolved, 💬 Won't fix (accepted) and ↩ Withdrawn are listed once, in the round where they close, and not repeated. 🔁 Still open and ◐ Partially addressed carry into the next round's list. So the newest round's status list plus its new findings is always the complete open set; older rounds are history.

**A 💬 Won't fix you do not accept** stays 🔁 Still open, with the reason you disagree.

### GitHub Draft Review Payload

Every round that writes to the review file also writes a JSON payload, which the user posts to GitHub as a **pending (draft) review**. A pending review is visible only to its author until they submit it from the web app, so the user can check, edit or delete each comment before anyone else sees it.

> **Why `gh api` and not `gh pr review`?** `gh pr review` with `--comment`, `--approve` or `--request-changes` always *submits* the review immediately; it has no draft option. GitHub's REST endpoint `POST /repos/<owner>/<repo>/pulls/<number>/reviews` creates a **pending** review when the request has no `event` field. Do not "simplify" this back to `gh pr review`.

**Path:** `<repo-root>/docs/reviews/PR_<number>_R<round>_GITHUB_DRAFT.json`, one file per round (e.g. `PR_42_R1_GITHUB_DRAFT.json`, `PR_42_R2_GITHUB_DRAFT.json`). It is a posting artifact, not the review's memory: the append-only rules apply to the `.md` file only, and the payload may be rewritten when posting fails (see *Delivering the Review*).

**Shape:**

```json
{
  "commit_id": "<headRefOid from Step 1, full 40-character hash>",
  "body": "<summary body, see rule 3>",
  "comments": [
    {
      "path": "src/s2e_validator/prefilters/text_format.py",
      "line": 57,
      "side": "RIGHT",
      "body": "**F-04 · 🔴 Bug**\n\nControl characters are counted after normalisation, so …\n\n**Suggested fix:**\n```python\n…\n```"
    }
  ]
}
```

Rules:

1. **Never include an `event` key.** Its absence is what keeps the review a draft. Any `event` value, including `"COMMENT"`, publishes the review immediately.
2. **`commit_id`** is `headRefOid` from the Step 1 `gh pr view` output, and it must start with `reviewed_head`. If it does not (the local branch is ahead of or behind what GitHub has), or `gh` was unavailable, omit both `commit_id` and `comments`, put every finding in `body`, and say why in the handover (see *Delivering the Review*). Line numbers counted from the local diff cannot be trusted against a different commit.
3. **`body`** holds, in this order:
   - the round's *GitHub Review Comment* text;
   - in a follow-up, the round's *Status of earlier findings* list;
   - a `**Not attached to a line**` section with every finding that could not be placed inline (rule 4), each in the Markdown review's shape: ID and label, file path, anchor line in a code span, then the finding text.

   It never contains the `review-state` header, the Review Baseline, or the Pull request overview; those belong to the local file.
4. **`comments`** gets one entry per finding (🟢 Praise included, without an ID) **whose anchor line is part of the diff GitHub shows for this PR**:
   - Anchor is a `+` line → `"side": "RIGHT"`, and `line` is its line number in the new file.
   - Anchor is a `-` line from the **full Step 1 diff** → `"side": "LEFT"`, and `line` is its line number in the old file.
   - Anchor is an unchanged context line printed inside a hunk of the **full Step 1 diff** → `"side": "RIGHT"`, new-file line number.
   - Anything else goes to `body` instead: an anchor outside every hunk, a line from a file the diff doesn't touch, or a `-` or context line that appears only in a follow-up's delta diff. GitHub rejects the **whole** request if a single comment points at a line it can't resolve.
5. **Computing `line`: count it, never estimate it.** In the diff for that file, find the nearest `@@ -<a>,<b> +<c>,<d> @@` header above the anchor. The first line after that header is old-file line `<a>` and new-file line `<c>`. Walk down to the anchor: a context line (leading space) advances both counters, a `+` line advances only the new counter, a `-` line advances only the old counter, and `\ No newline at end of file` advances neither. If you cannot count it with certainty (for example, the anchor text occurs twice in the hunk and you can't tell which is meant), send the finding to `body`.
6. **`path`** is the same repo-relative path used in the Markdown review (Comment Anchors rule 8).
7. **Each comment's `body`** starts with `**<ID> · <emoji> <label>**` (Praise: `**🟢 Praise**`), followed by the finding text and suggested fix exactly as written in the Markdown review, minus the file path and anchor line, since GitHub shows those itself. In a follow-up, only **new** findings go inline; earlier findings are covered by the status list in `body`.
8. **Valid JSON.** The file is written by hand with `Write`, so escape it by hand: newlines as `\n`, double quotes as `\"`, backslashes as `\\`, tabs as `\t`. No raw line breaks inside strings, no trailing commas, no comments. Emoji and other UTF-8 characters can stay as they are. Code fences inside a comment are fine once escaped.
9. **The payload must agree with the Markdown review:** same IDs, same text. It is a projection of the round, never a place to add, soften or drop findings. The verdict is not sent; the user chooses Comment, Approve or Request changes when submitting in the web app.

---

## Delivering the Review — Files Only, Never the Terminal

The finished review is written to **`<repo-root>/docs/reviews/PR_<number>_REVIEW.md`**, and its draft-review projection to **`<repo-root>/docs/reviews/PR_<number>_R<round>_GITHUB_DRAFT.json`** (see *GitHub Draft Review Payload*), where `<number>` is the GitHub PR number resolved in Step 1 and `<repo-root>` is resolved to a real absolute path exactly as described at the top of this skill. Do not print either file, preview it, quote findings from it, or paste any part of it into your reply. Emitting the review twice — once as a chat message and once as a file — doubles the generated output for zero added information, and that duplication is exactly what this rule exists to prevent.

**Writing both files under `<repo-root>/docs/reviews/` is pre-authorized.** Do not ask "where would you like this?" — the question is already answered, permanently. **The review file is not a regenerated artifact — it is the review's memory.** If it does not exist, create it. If it exists, **never overwrite or regenerate it**: append a round instead (see *Round Layout, State Header, and Finding IDs*). The only exception is `fresh` mode, and only after the user confirms. The only permitted substitution is a different path the user names explicitly in `$ARGUMENTS` or in conversation; in that case write there instead, still without printing, and put the draft payload in the same directory.

Once the review is ready:

1. **Initial review** (file does not exist): call the `Write` tool **once**, with the complete file — state header, `# Round 1` heading, and the review in the format above — as full Markdown source, no wrapper fence, no truncation, no "…remaining findings omitted for brevity."
   **Follow-up review**: make exactly **two `Edit` calls** — one replacing the old `review-state` header with the updated one, one inserting the new round (followed by `---`) immediately above the previous newest round's `# Round` heading. Do not `Write` the file again and do not reprint older rounds. For a **legacy file** with no header, insert the header, the new round, `---`, and a `# Round 1 — Initial review (legacy file, reviewed commit unknown)` heading above the file's first line, leaving the old text untouched.
2. **Write the draft payload** with one `Write` call to this round's `_GITHUB_DRAFT.json` path, following *GitHub Draft Review Payload*.
3. Reply with **exactly the following and nothing else** — no verdict, no findings tally, no recap. First the two file lines:

   ```text
   Review written to /absolute/path/to/repo/docs/reviews/PR_<number>_REVIEW.md
   Draft GitHub review written to /absolute/path/to/repo/docs/reviews/PR_<number>_R<round>_GITHUB_DRAFT.json
   ```

   For a **follow-up**, the first line instead reads `Follow-up review (round <N>) appended to /absolute/path/to/repo/docs/reviews/PR_<number>_REVIEW.md`.

   Then the post command, handed over like every other command in this skill (see *Precedence*). `<owner>/<repo>` comes from the `url` fetched in Step 1; if `gh` was unavailable, ask the user for it rather than guessing:

   ```bash
   gh api --method POST repos/<owner>/<repo>/pulls/<number>/reviews --input /absolute/path/to/repo/docs/reviews/PR_<number>_R<round>_GITHUB_DRAFT.json
   ```

   *Explain it roughly like this:* "Sends the review to GitHub as a pending draft: the summary plus <k> inline comments, visible only to you. Nothing is published yet. Open the PR on GitHub, go to **Files changed**, check or edit the comments, then submit the review there and choose Comment, Approve or Request changes. GitHub allows only one pending review per person per PR, so submit or delete any earlier draft first. If the command fails, paste the error back." If rule 2 of *GitHub Draft Review Payload* moved everything into `body`, add one sentence saying so and why.

   When Step F1 found nothing new, the whole reply is instead the single line `No new commits or comments since round <N-1>; /absolute/path/to/repo/docs/reviews/PR_<number>_REVIEW.md left unchanged, no draft payload written.`
4. Then stop. Do not run the command yourself. The user reads the file and posts the draft.

**If the user pastes back a failure**, fix the payload, never the review file:

| Error | Fix |
|---|---|
| `422` mentioning `line`, `path`, `position`, or "could not be resolved" | Move every inline comment into `body` under **Not attached to a line**, remove `comments`, keep `commit_id`, rewrite this round's JSON with `Write`, and hand the same command over again. |
| `422` saying you can only have one pending review | Change nothing. Tell the user to submit or delete their existing draft on the PR page, then rerun the same command. |
| `422` about `commit_id` | Remove `commit_id` and `comments`, move the inline comments into `body`, rewrite the JSON, and hand the command over again. |
| `404` | Check `<owner>/<repo>` and the PR number against the Step 1 `url`. If they match, the `gh` account likely lacks access to the repo. |

This applies only to the finished review. The two request rounds above still happen in the terminal as normal — they gather the input, they are not the output.
