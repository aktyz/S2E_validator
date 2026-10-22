# Follow-up Mode — review-pr supporting file

Read this file when *Review Modes* in `SKILL.md` selects a follow-up. It holds the follow-up procedure (Steps F1–F3) and the review file's round layout, state header and finding-ID rules.

---

## Follow-up Mode — Steps F1–F3

Run these **instead of Steps 3–5** when Review Modes selected a follow-up. Step 5's classification labels still apply to new findings. Steps 1 and 2 run as written, with these differences:

- **Step 1** — the PR-description command also returns the conversation. Read it for the author's replies to earlier questions and their stated reasons for declining a finding. **`gh pr view` does not include the text of replies in inline review threads**: they show up only as `reviews` entries with an empty `body`. So in a follow-up's Round 2 message, always hand over this filtered command as well:

  ```bash
  gh api --paginate repos/<owner>/<repo>/pulls/<number>/comments | jq -rs 'add | INDEX(.id) as $by | .[] | select(.in_reply_to_id) | "--- \(.user.login) replying to: \($by[(.in_reply_to_id|tostring)].body | split("\n")[0])\n\(.body)\n"'
  ```

  *Explain it roughly like this:* "Fetches every inline review comment on the PR and prints only the replies: who wrote each one, the first line of the comment it answers (e.g. `**F-03 · 🟡 Style**`), and the reply text. `-s` with `add` joins the pages `--paginate` returns, so a reply on one page can still find its parent on another." `<owner>/<repo>` comes from the Step 1 `url`. **Never ask for the raw `pulls/<number>/comments` output, or redirect it to a file for you to `Read`.** Each comment carries its full `diff_hunk`, so the raw JSON is one line of hundreds of KB that `Read` can neither load nor page through. Asking for it forces a third request round.
- **Step 2a** — fetch live docs only for concepts the *delta* touches, and skip any page the previous round's Review Baseline already lists unless `pyproject.toml` changed in the delta. Restate the baseline only for rows that changed and write "Unchanged since round N-1." for the rest.

### F1 — Load the previous state and size the delta

From the review file take: `round`, `reviewed_head`, `base`, `last_finding_id`, the goal stated in Round 1, and every finding still open (see F2).

**The base comes from the header.** A follow-up compares against the same base as the earlier rounds, not against whatever Step 1 defaulted to. If the header's `base` differs from the base used in the Round 1 `git log`, hand over `git log <base>..HEAD --oneline` with the header's `base` in the Round 2 message (this is part of Round 2, not a third round) and use that output below. The only exception is a base the user passed explicitly in the arguments for this run: use it, and say in the round's summary that the base changed. A legacy file with no header falls back to the base Step 1 resolved.

Then compare `reviewed_head` with the hashes in that `git log <base>..HEAD --oneline` output (match on prefix, at least 7 characters):

| Situation | What to do |
|---|---|
| `reviewed_head` is in the log, with newer commits above it | Those commits are the delta. In the Round 2 message hand over `git diff --stat <reviewed_head>..HEAD` and `git diff <reviewed_head>..HEAD`, explained like the Step 1 diff. *Two dots compare exactly those two commits — "what changed since the last review". Three dots would use their merge base, which after a rebase can hide changes.* |
| `reviewed_head` is the newest commit and the conversation has nothing newer than the last round's `updated` date | Nothing to review. **Do not touch the file.** Reply with the one-line "nothing new" message from *Delivering the Review*. |
| `reviewed_head` is the newest commit, but there are newer comments | No delta diff. Run F2 using the author's replies only. |
| `reviewed_head` is not in the log (rebase or force-push), or the file has no header | History was rewritten or is unknown. Skip the delta, re-verify every open finding against the full Step 1 diff, and say in the round's summary that the delta could not be computed. |

**Legacy file (no header).** Treat it as a completed earlier round. Its findings carry no IDs, so assign `F-01`, `F-02`, … in document order in the status list of your new round (the old text stays untouched; the status list defines the mapping), and count `round` from 2.

### F2 — Re-verify every open finding by its anchor

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

### F3 — Review the delta for new findings

Apply Steps 3–5's standards to the delta only: code changed since `reviewed_head`, plus anything the fixes now interact with. Regressions introduced by a fix are findings. Do not re-review unchanged code that the previous round already covered. If the delta could not be computed, review the full diff for anything the earlier rounds missed and say so.

New findings continue the ID sequence from `last_finding_id` and use the anchor rules under *Comment Anchors* in `SKILL.md`. The finished round follows the follow-up template below.

---

## Round Layout, State Header, and Finding IDs

`PR_<number>_REVIEW.md` is **append-only**: one file, newest round first, older rounds never rewritten. Because a follow-up may run in a new session with no memory of the earlier one, the file is the only carrier of state.

```
<!-- review-state … -->        ← always the first thing in the file; describes the NEWEST round

# Round <N> — Follow-up review · …        ← newest round
…
---
# Round 1 — Initial review · …            ← the initial review, exactly as first written
```

**State header.** The `review-state` HTML comment (fields in the initial-review template in `SKILL.md`) reflects the newest round and is updated in place by every follow-up. `reviewed_head` is the newest commit in the `git log <base>..HEAD --oneline` output at the time of that round, and `base` is the base that log was computed against (see F1). Do not add fields; a follow-up must be able to rely on exactly these.

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
> Becomes the summary body of the draft GitHub review (see `draft-payload.md`); also usable as-is in the GitHub "Leave a comment" box.

<2–4 sentences about this round: what was addressed, what remains, the verdict. Do not repeat every finding.>
```

**Carry-forward rule.** The status list covers every finding that was open at the end of the previous round. ✅ Resolved, 💬 Won't fix (accepted) and ↩ Withdrawn are listed once, in the round where they close, and not repeated. 🔁 Still open and ◐ Partially addressed carry into the next round's list. So the newest round's status list plus its new findings is always the complete open set; older rounds are history.

**A 💬 Won't fix you do not accept** stays 🔁 Still open, with the reason you disagree.
