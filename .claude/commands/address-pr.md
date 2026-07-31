---
name: address-pr
description: "Address PR review comments (human and bot): score, fix or acknowledge, reply, resolve threads. Provide PR number."
user-invocable: true
category: pr
inputs:
  - pr_number
outputs:
  - files_changed
  - review_addressed
---

# Address PR Review Comments

Score each review comment, address all of them (fix or acknowledge with justification), and reply on GitHub. Handles both human reviewers and automated review bots (Sourcery, CodeRabbit, etc.).

```bash
# Benchmark logging (entry)
bash .claude/benchmark/log.sh "address_pr_start" "" "" 2>/dev/null || true
```

## CRITICAL: Complete ALL Steps

This command runs as a headless agent. You MUST execute every step below through to completion. In headless mode, producing a text-only summary without a tool call causes the process to exit immediately, so NEVER output a final summary without having completed steps 8-17 first (squash, rebase, push, reply, resolve, dismiss). If you discover that all findings are already addressed, you MUST still complete steps 9-10 (rebase and push) to resolve any merge conflicts, then skip to step 11 to handle thread resolution before the summary.

**Incomplete execution is worse than failure**: a run that fixes code but never commits/pushes wastes the cost and leaves the PR unchanged.

## Instructions

1. Get the PR number from `$ARGUMENTS`.

2. Fetch review data:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title,headRefName
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews
   ```

3. **Verify you are in the correct working directory**: this is the most critical safety check. Working in the wrong directory means modifying the wrong branch and leaving garbage uncommitted changes on main.

   a. Check the current branch:
      ```bash
      git branch --show-current
      ```

   b. If the output matches `headRefName` from step 2, you are in the right place. Continue to step 4.

   c. If they differ, find the worktree for the PR branch:
      ```bash
      git worktree prune
      HEAD_BRANCH_REF="branch refs/heads/${HEAD_BRANCH}"
      WORKTREE_PATH=$(git worktree list --porcelain | grep -F -B2 "$HEAD_BRANCH_REF" | grep "^worktree " | head -1 | sed 's/^worktree //')
      ```
      If `WORKTREE_PATH` is non-empty, navigate into it and verify:
      ```bash
      cd "$WORKTREE_PATH"
      VERIFIED=$(git branch --show-current)
      echo "Verified branch: $VERIFIED"
      ```
      If `VERIFIED` does not equal `HEAD_BRANCH`, STOP and report: the worktree exists but is on the wrong branch.

   d. **STOP if the worktree cannot be found**: if `WORKTREE_PATH` is empty and the current branch is not `headRefName`, report the mismatch and exit. Do NOT use `git checkout <HEAD_BRANCH>` to fix this: doing so switches the main repo's working tree to the PR branch, causing any uncommitted changes to bleed onto main after cleanup.

   Never resolve the worktree by issue number alone -- always use the branch name from the PR metadata.

4. **Classify comment sources**: Separate human reviewer comments from bot comments (e.g., `sourcery-ai[bot]`, `coderabbitai[bot]`). Both get scored the same way, but bot comments are addressed in bulk and may warrant a re-review request.

5. **Score each comment 1-10**:
   - 1-2: low-priority -- style preference, minor naming, formatting
   - 3-5: moderate -- DRY violations, missing edge cases, error handling
   - 6-8: important -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

   Scoring determines priority order, not whether to fix. All findings are addressed.

   For bot suggestions: evaluate critically -- don't blindly accept. Bots lack project context and may suggest changes that conflict with project conventions.

6. **Address all findings** (regardless of score): Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.
   - If a finding is a false positive, not applicable in context, or requires a human decision: acknowledge it with a one-line justification instead of fixing. Do not skip findings based on score alone.

7. **Scout check**: while fixing findings, scan each touched file for pre-existing issues -- failing tests, lint warnings, dead imports, obvious bugs adjacent to your changes. Fix them alongside the review findings. Keep scout fixes small and low-risk.

8. **Squash fixes into original commits** (MANDATORY -- no fix-on-fix commits):

   The PR must read as clean feature development. Do NOT create a separate "address review findings" commit -- fold each fix into the commit that introduced the code being fixed. The final history should look as if the code was written correctly from the start.

   **Before starting**: verify `git branch --show-current` equals `headRefName`. If not, STOP: never reset commits while on the wrong branch.

   a. Run the project's linter and tests (see CLAUDE.md for commands). All must pass before proceeding.

   b. Record the original commit messages for replay:
      ```bash
      git log origin/$BASE..HEAD --reverse --format="%H %s" > /tmp/pr-commits.txt
      ```

   c. Create a backup branch, then soft-reset:
      ```bash
      git branch backup-pre-squash
      git reset --soft origin/$BASE
      git reset HEAD .
      ```
      All changes are now unstaged in the working tree.

   d. Re-commit in the original logical groups. For each original commit (from the list in step b):
      - Stage the files that belong to that logical unit
      - When a file (e.g., a test file) has changes belonging to multiple commits, save the final version aside, apply the intermediate version for earlier commits, then restore the final version for the last commit that touches it
      - Use the original commit message (preserving type, scope, and description)
      - Fold review fixes into whichever commit introduced the code being fixed

   e. **Single-commit shortcut**: if the branch had only one commit before fixes, skip the reset-and-recommit flow. Instead, stage all fixes and `git commit --amend --no-edit`.

   f. Verify the tree is identical to the backup:
      ```bash
      git diff backup-pre-squash
      ```
      This MUST produce no output. If it does, something was lost. Investigate before continuing.

   g. Clean up: `git branch -D backup-pre-squash`

9. **Rebase onto base branch** to ensure the PR is mergeable after fixes:
    ```bash
    BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq '.baseRefName')
    git fetch origin
    git rebase origin/$BASE
    ```

    If there are merge conflicts:
    1. Read each conflicted file, understand both sides of each conflict marker
    2. Resolve conflicts by editing the file to remove markers, keeping the correct code
    3. `git add <resolved_file>` and `git rebase --continue`
    4. Repeat for subsequent commits (up to 3 rebase steps)
    5. After each resolution, verify no conflict markers remain: `grep -rn "<<<<<<<" <file>`

    If resolution fails after 3 attempts:
    - Run `git rebase --abort` to restore a clean state
    - Report the conflicting files and stop

    If rebase was a no-op (already up to date), continue to the next step.

10. **Push and wait for CI** (MANDATORY -- do not skip):
   ```bash
   git push --force-with-lease
   ```
   After pushing, poll CI checks on the PR until ALL required checks complete (max 15 minutes, poll every 30s):
   ```bash
   gh pr checks <PR_NUMBER> --json name,bucket --jq '.[] | "\(.bucket)\t\(.name)"'
   ```
   Wait until every check shows `pass`, `fail`, or `skipping` -- no `pending` remaining.

   **If any check fails**: fetch the failure logs, analyze, fix the code, re-push, and re-poll (max 2 retries):
   ```bash
   gh run view <RUN_ID> --log-failed
   ```
   Common CI failures: commit-format invariant (wrong scope/type), test timeouts, lint errors, SonarCloud coverage.

   **Only continue to step 11 when all required checks pass.** If checks are still failing after 2 retries, report the specific failures and stop -- do not proceed to reply/resolve steps with a red CI.

11. **Fetch all unresolved review threads** using GraphQL:
    ```bash
    gh api graphql -f query='{
      repository(owner: "<OWNER>", name: "<REPO>") {
        pullRequest(number: <PR_NUMBER>) {
          reviewThreads(first: 50) {
            nodes {
              id
              isResolved
              isOutdated
              comments(first: 1) { nodes { body path line author { login __typename } } }
            }
          }
        }
      }
    }'
    ```
    Filter to unresolved, non-outdated threads. Match each thread to a finding using the thread's `path` and `line` fields. Use `author.__typename` to classify thread authors: `Bot` for automated reviewers, `User` for humans. If `__typename` is absent, fall back to checking whether `login` ends with `[bot]`.

12. **Reply to each thread, then conditionally resolve** (MANDATORY: this is what unblocks the PR):

    For each unresolved thread, post an inline reply explaining the disposition. Then resolve conditionally: resolve all bot threads (both Fixed and Acknowledged), and human threads only when the finding was Fixed. Leave Acknowledged human threads unresolved so the reviewer can confirm. Replying is always required; skipping replies leaves reviewers unclear on what was done.

    a. **Reply** with a short, direct explanation:
       ```bash
       gh api graphql \
         -f query='mutation($threadId: ID!, $body: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) { comment { id } } }' \
         -f threadId="THREAD_ID" \
         -f body="Fixed in <SHA>: <what changed>."
       ```
       For acknowledged findings: `"Acknowledged: <justification>."`

    b. **Resolve** the thread:
       ```bash
       gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
       ```

    Thread resolution depends on who posted it:
    - **Bot reviewers** (CodeRabbit, Sourcery, etc.): reply + resolve threads for both Fixed AND Acknowledged findings. Bot threads do not require human confirmation.
    - **Human reviewers**: reply to all threads. Resolve only Fixed threads. Leave Acknowledged threads unresolved so the reviewer can confirm the justification.

13. **Post a summary comment** on the PR with all dispositions in one table:

    ```markdown
    ## Address Review: Round N

    | # | Finding | Action |
    |---|---------|--------|
    | 1 | `file:line`: short description | Fixed: what changed. |
    | 2 | `file:line`: short description | Acknowledged: justification. |
    ```

    Keep the Action column SHORT and DIRECT. No filler words, no emojis.

14. **Dismiss bot CHANGES_REQUESTED reviews** (mandatory when any bot review is in CHANGES_REQUESTED state after addressing findings):

    Fetch bot reviews that are still CHANGES_REQUESTED:
    ```bash
    gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews \
      --jq '.[] | select(.state == "CHANGES_REQUESTED") | select(.user.type == "Bot") | {id: .id, user: .user.login}'
    ```
    For each such review:
    ```bash
    gh api -X PUT repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews/<REVIEW_ID>/dismissals \
      -f message="Findings addressed or acknowledged. See Address Review comment."
    ```

    **Never dismiss human reviews**: only bot reviews (`user.type == "Bot"`) whose findings have been addressed.

15. **Request bot re-review** (if bot comments were addressed):
    ```bash
    # Sourcery AI
    gh pr comment <PR_NUMBER> --body "@sourcery-ai review"
    # CodeRabbit
    gh pr comment <PR_NUMBER> --body "@coderabbitai review"
    ```
    Only request re-review for bots whose comments were actually addressed.

16. **Update memory**: Append lessons learned to `.claude/agent-memory/cookbook.md` (under matching domain section).

17. **Print summary**:
    - Table: | Comment | Source | Score | Action |
    - Source column distinguishes human vs bot reviewers
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge

## Rules

- **Don't blindly accept bot suggestions** -- evaluate each one against project conventions
- NEVER use emojis in any output

```bash
# Benchmark logging (exit)
bash .claude/benchmark/log.sh "address_pr_complete" "" "" 2>/dev/null || true
```
