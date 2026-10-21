# Reply to a PR Review

You are helping the **author** of a pull request on the **Survey2Earn validator** repository (the off-chain quality validator, split out of the Survey2Earn monorepo) answer the review comments they received. The result is a script that adds one reply under every review thread as a **pending (draft) review**, so nothing is visible to anyone until the author checks the replies and submits them from the GitHub web app.

`$ARGUMENTS`: `<PR number> [<review id> …]`. With review ids, only threads from those reviews are answered; without, every open thread is (see Step 2). If the PR number is missing, ask for it before doing anything else.

---

## Precedence — the CLAUDE.md Restrictions Win

If anything in this skill conflicts with the **Restrictions** section of `CLAUDE.md`, the Restrictions win, without exception. You never run `git` (read-only included), `gh`, `find`, `ls`, `grep`, or any build/test/lint command yourself. Every command below is **handed to the user**, who runs it and pastes the output back. You never run the finished script either.

Every command you hand over follows the same shape, stated once here and applied throughout:

1. The exact command in a fenced code block, with **absolute paths** for the directory the user will run it from.
2. One or two sentences of plain English on what it does.
3. Ask the user to run it and paste the output back, then **stop and wait**. Never guess the output.

File reads follow the batch-ask protocol: list every absolute path with a one-line justification, ask once, wait for a yes, then `Read`. A path that appears in a review comment is **referenced, not named**: it still needs this permission.

`<repo-root>` means the absolute path of the project directory this command was invoked from (the primary working directory in your environment). Substitute the real path into every command before handing it over; never emit the literal placeholder. If unsure, ask.

**The repo root is the validator package**: `pyproject.toml` at the root, the package under `src/s2e_validator/`, tests under `tests/`. The on-chain Survey2Earn program is **not** in this repo; it lives in the separate Survey2Earn repository.

**Never suggest mutating git commands** (`add`, `commit`, `push`, `rebase`, …), not even in a code block. If fixes still have to reach GitHub, say so in words.

---

## Request Rounds

The skill runs in exactly **two request rounds**, then writes its output.

### Round 1 — the review and the branch state

One message with these three commands, then wait.

```bash
cd <repo-root> && gh pr view <PR> --json number,title,url,author,headRefName,headRefOid,body,reviews
```
*Prints the PR's title, URL, author, head branch and commit, description, and every submitted review with its id, author, state and summary body.*

```bash
cd <repo-root> && gh api "repos/{owner}/{repo}/pulls/<PR>/comments" --paginate --jq '.[] | {id, node_id, pull_request_review_id, in_reply_to_id, user: .user.login, path, line, original_line, created_at, body}'
```
*Lists every inline review comment on the PR, one JSON object each: its ids, the review it belongs to, the comment it replies to (if any), the author, the file and line, and the text. `{owner}` and `{repo}` are filled in by `gh` from the repository's GitHub remote.*

```bash
git -C <repo-root> rev-parse HEAD && git -C <repo-root> status --short
```
*Prints the commit your local branch points at, then any uncommitted changes. This tells us whether the fixes the replies describe are already on GitHub.*

### Step 2 — pick the threads and triage them

From the Round 1 output:

1. **Threads to answer.** A thread is identified by its **root comment**, the one with `in_reply_to_id: null`. Answer a root comment when:
   - its author is not the PR author;
   - no later comment in the same thread (same `in_reply_to_id` chain) was written by the PR author;
   - if review ids were given in `$ARGUMENTS`, its `pull_request_review_id` is one of them.
2. **Review summaries.** Every review `body` in scope that asks a question or requests something not tied to a line gets one answer. All these answers go into a single **summary reply** (see Step 4), because a pending review can only carry one body.
3. **Classify each item**:

   | Class | Meaning | What the reply says |
   |---|---|---|
   | **Done** | The change is in the code | What changed, naming functions, files or tests, never line numbers (they drift). |
   | **Superseded** | The code the comment was about no longer exists, or the design changed | What replaced it, and why the concern is covered or no longer applies. |
   | **Deferred** | Valid, but handled elsewhere, including concerns that belong to the on-chain program in the Survey2Earn repo (settlement, treasury vaulting, SurveyRun lifecycle) | The issue number, branch or repo that handles it, and what that will do. |
   | **Won't fix** | The author disagrees | The reason, stated plainly. |
   | **Answer** | A question | The direct answer first, then the reasoning. |
   | **Decision needed** | Only the author can say: product intent, trust model, deployment facts, … | Never guessed. Goes into the Round 2 decision list. |

4. **Branch state.** Compare `headRefOid` with the local `HEAD`, and look at the `status --short` output. If they differ, or there are uncommitted changes, **Done** replies describe code the reviewer cannot see yet. Say so in the handover. The author must get the fixes onto GitHub before submitting the review; say this in words, with no git command.

### Round 2 — files and decisions

One message with both parts, then wait.

- **File reads.** A single batch request for the files needed to verify every **Done** and **Superseded** claim: the files the comments point at, plus any file the fix moved code into. Justify each path in one line. Skip files already read in this session.
- **Decisions.** A numbered list of every **Decision needed** item, each with the reviewer's question in one line and your recommended answer. Accept "ok" as agreement.

A claim you cannot verify from the files is not written as **Done**. Ask about it, or phrase it as what the author told you.

---

## Writing the Replies

- **Answer first.** Open with "Done.", "Fixed with …", "Good catch.", "By design.", "Deferred to #N." or the direct answer. No "Thanks for the comment", except once in the summary reply.
- **Concrete over general.** Name the function, file, test or issue. Quote a constraint or signature in backticks when it *is* the fix.
- **One reply per thread, one topic per reply.** When one change answers several threads, say it fully once and point to it from the others ("see the reply on `_count_control_chars`").
- **No line numbers, no commit hashes.** They go stale before the review is read.
- **Use the project's own terms**, matching the codebase and any terminology feedback in memory.
- **Short.** Two to six sentences, or a short bullet list when several tests or changes are listed.
- **Nothing the author did not confirm.** No invented issue numbers, branch names, test counts or deployment facts.
- **The summary reply**: one short paragraph. Thank the reviewer, say every inline comment has a reply, then answer the summary's questions.

---

## The Output Script

**Path:** `<repo-root>/docs/reviews/PR_<number>_REPLIES_<YYYYMMDD>.sh`. Writing under `<repo-root>/docs/reviews/` is pre-authorized. If the file exists, add a suffix (`_2`, …) and never overwrite.

> **Why GraphQL and not REST?** `POST /repos/<owner>/<repo>/pulls/<number>/comments/<id>/replies` always **publishes** the reply immediately. REST `POST …/pulls/<number>/reviews` can create a pending review, but its `comments` cannot reply to an existing thread. The GraphQL mutation `addPullRequestReviewThreadReply` can add a reply to an existing thread **inside a pending review**, so that is what the script uses. Do not "simplify" it back to REST.

Write it with one `Write` call, from this template. Fill in `OWNER`, `NAME` and `PR` from the Round 1 `url`, and add one `reply` call per root comment id, in the order the comments appear in the review:

````bash
#!/usr/bin/env bash
# Adds replies to the review threads of PR #<number> as PENDING review comments.
# Nothing is visible to others until you submit the review on GitHub
# ("Files changed" → "Finish your review"). Paste the summary printed at the end
# into the review body when you submit.
# Submit only after the fixes are on GitHub, so each "done" matches the pushed code.
# Re-running adds every reply a second time to the same pending review.
set -euo pipefail

OWNER="<owner>"
NAME="<repo>"
PR=<number>

# PR node id and your own pending review on it, if one exists (only yours is visible)
read -r PR_ID PENDING_ID < <(gh api graphql -f owner="$OWNER" -f name="$NAME" -F pr="$PR" -f query='
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      id
      reviews(states: PENDING, first: 1) { nodes { id } }
    }
  }
}' --jq '.data.repository.pullRequest | "\(.id) \(.reviews.nodes[0].id // "")"')

if [[ -z "${PENDING_ID}" ]]; then
  PENDING_ID=$(gh api graphql -f prId="$PR_ID" -f query='
mutation($prId: ID!) {
  addPullRequestReview(input: { pullRequestId: $prId }) { pullRequestReview { id } }
}' --jq '.data.addPullRequestReview.pullRequestReview.id')
  echo "started a pending review"
else
  echo "reusing your existing pending review (its earlier comments stay in it)"
fi

# "<root comment REST id> <thread node id>" for every review thread
THREADS=$(gh api graphql --paginate -f owner="$OWNER" -f name="$NAME" -F pr="$PR" -f query='
query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | "\(.comments.nodes[0].databaseId) \(.id)"')

reply() {
  local thread
  thread=$(awk -v c="$1" '$1 == c { print $2 }' <<< "$THREADS")
  if [[ -z "$thread" ]]; then
    echo "no review thread found for comment $1 (is it a thread's root comment?)" >&2
    exit 1
  fi
  gh api graphql -f reviewId="$PENDING_ID" -f threadId="$thread" -f body="$2" -f query='
mutation($reviewId: ID!, $threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewId: $reviewId, pullRequestReviewThreadId: $threadId, body: $body
  }) { comment { id } }
}' > /dev/null
  echo "pending reply added to comment $1"
}

# 1. <file> — <one-line topic of the reviewer's comment>
reply <root comment id> "$(cat <<'REPLY_EOF'
<reply text, GitHub Markdown>
REPLY_EOF
)"

# … one block per thread …

echo
echo "All replies are pending. Open https://github.com/${OWNER}/${NAME}/pull/${PR}/files,"
echo "check them, then use 'Finish your review' with this text as the review body:"
echo "----------------------------------------------------------------------"
cat <<'REPLY_EOF'
<summary reply>
REPLY_EOF
echo "----------------------------------------------------------------------"
````

Rules for filling it in:

1. **Root comment ids only.** `reply` takes the id of the thread's first comment (`in_reply_to_id: null`), because that is how the script finds the thread.
2. **Quoted heredocs, delimiter `REPLY_EOF`.** `<<'REPLY_EOF'` keeps backticks, `$` and `\` literal, so reply text needs no escaping. A reply must never contain a line that is exactly `REPLY_EOF`.
3. **One comment line above each block**, naming the file and the reviewer's point, so the author can find a reply quickly.
4. **No summary questions?** Keep the printed block anyway, with just the thank-you sentence.
5. **The script agrees with the triage**: every thread picked in Step 2 has exactly one block. None is added, softened or dropped.

---

## Delivering

1. Write the script with one `Write` call. Do not print it or quote replies from it in your message: the author reads the file.
2. Reply with the path, then the run command, handed over as usual:

   ```bash
   bash <repo-root>/docs/reviews/PR_<number>_REPLIES_<YYYYMMDD>.sh
   ```
   *Adds every reply to your pending review on PR #<number>, printing one line per reply, then prints the summary text to paste when you submit. Nothing is published until you click "Finish your review" in the PR's Files tab.*

3. Add, only when they apply:
   - the branch-state warning from Step 2 (fixes not on GitHub yet);
   - that an existing pending review will be reused, with its earlier comments;
   - replies that promise a change the author still has to make.
4. Then stop. Do not run the script.

**If the user pastes back a failure**, fix the script, never the replies' content:

| Error | Fix |
|---|---|
| `no review thread found for comment <id>` | The id is not a thread's root comment. Replace it with the root id (follow `in_reply_to_id` up) and hand the command over again. Delete the pending review first, or remove the blocks that already ran, to avoid duplicates. |
| GraphQL error naming `pullRequestReviewId` or `addPullRequestReviewThreadReply` (schema changed) | Switch `reply` to `addPullRequestReviewComment(input: { pullRequestReviewId: $reviewId, inReplyTo: $commentNodeId, body: $body })`, passing the comment's `node_id` from Round 1 instead of the thread lookup. |
| `Could not resolve to a node` | Check `OWNER`, `NAME` and `PR` against the Round 1 `url`. |
| `HTTP 401` / `403` | The `gh` account lacks access. Ask the user to check `gh auth status`. |
