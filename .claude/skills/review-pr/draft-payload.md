# GitHub Draft Review Payload — review-pr supporting file

Read this file before writing a round's draft-review JSON, and again when the user pastes back an error from posting it. The rest of the review flow is in `SKILL.md`.

---

## GitHub Draft Review Payload

Every round that writes to the review file also writes a JSON payload, which the user posts to GitHub as a **pending (draft) review**. A pending review is visible only to its author until they submit it from the web app, so the user can check, edit or delete each comment before anyone else sees it.

> **Why `gh api` and not `gh pr review`?** `gh pr review` with `--comment`, `--approve` or `--request-changes` always *submits* the review immediately; it has no draft option. GitHub's REST endpoint `POST /repos/<owner>/<repo>/pulls/<number>/reviews` creates a **pending** review when the request has no `event` field. Do not "simplify" this back to `gh pr review`.

**Path:** `<repo-root>/docs/reviews/PR_<number>_R<round>_GITHUB_DRAFT.json`, one file per round (e.g. `PR_42_R1_GITHUB_DRAFT.json`, `PR_42_R2_GITHUB_DRAFT.json`). It is a posting artifact, not the review's memory: the append-only rules apply to the `.md` file only, and the payload may be rewritten when posting fails (see *Posting Failures* below).

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
2. **`commit_id`** is `headRefOid` from the Step 1 `gh pr view` output, and it must start with `reviewed_head`. If it does not (the local branch is ahead of or behind what GitHub has), or `gh` was unavailable, omit both `commit_id` and `comments`, put every finding in `body`, and say why in the handover (see *Delivering the Review* in `SKILL.md`). Line numbers counted from the local diff cannot be trusted against a different commit.
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
6. **`path`** is the same repo-relative path used in the Markdown review (*Comment Anchors* rule 8 in `SKILL.md`).
7. **Each comment's `body`** starts with `**<ID> · <emoji> <label>**` (Praise: `**🟢 Praise**`), followed by the finding text and suggested fix exactly as written in the Markdown review, minus the file path and anchor line, since GitHub shows those itself. In a follow-up, only **new** findings go inline; earlier findings are covered by the status list in `body`.
8. **Valid JSON.** The file is written by hand with `Write`, so escape it by hand: newlines as `\n`, double quotes as `\"`, backslashes as `\\`, tabs as `\t`. No raw line breaks inside strings, no trailing commas, no comments. Emoji and other UTF-8 characters can stay as they are. Code fences inside a comment are fine once escaped.
9. **The payload must agree with the Markdown review:** same IDs, same text. It is a projection of the round, never a place to add, soften or drop findings. The verdict is not sent; the user chooses Comment, Approve or Request changes when submitting in the web app.

---

## Posting Failures

**If the user pastes back a failure**, fix the payload, never the review file:

| Error | Fix |
|---|---|
| `422` mentioning `line`, `path`, `position`, or "could not be resolved" | Move every inline comment into `body` under **Not attached to a line**, remove `comments`, keep `commit_id`, rewrite this round's JSON with `Write`, and hand the same command over again. |
| `422` saying you can only have one pending review | Change nothing. Tell the user to submit or delete their existing draft on the PR page, then rerun the same command. |
| `422` about `commit_id` | Remove `commit_id` and `comments`, move the inline comments into `body`, rewrite the JSON, and hand the command over again. |
| `404` | Check `<owner>/<repo>` and the PR number against the Step 1 `url`. If they match, the `gh` account likely lacks access to the repo. |
