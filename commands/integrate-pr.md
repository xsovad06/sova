---
name: integrate-pr
description: Full integration pipeline -- rebase, CI, merge, cleanup, learn. One click from approved PR to done.
user-invocable: true
category: pr
---

# Integrate PR

Full integration pipeline for a PR. Rebases onto the base branch, waits for CI, merges, cleans up branches/worktrees/stashes, closes the linked issue, captures review learnings, promotes confirmed patterns to project knowledge, and updates agent memory. Replaces the need to run `/after-merge` and `/extract-knowledge` separately. Works for both manual invocation and autonomous agent use.

PR: $ARGUMENTS

## Instructions

### Phase 1: Identify the PR

Determine the PR number from available context, in priority order:

1. **From arguments** (`$ARGUMENTS`): use directly if a number is provided
2. **From current branch**: query for an open PR on the current branch:
   ```bash
   gh pr view --json number,title,body,state,baseRefName,headRefName,statusCheckRollup,reviewDecision,commits,mergeable
   ```
3. **From recent PRs**: if on the base branch, list recent open PRs authored by the current user:
   ```bash
   gh pr list --author @me --state open --limit 10
   ```
   Ask the user which one to integrate (unless running autonomously, in which case stop and report ambiguity).

If no PR can be identified, stop and report clearly.

Once identified, fetch full PR metadata:

```bash
gh pr view <PR_NUMBER> --json number,title,body,state,baseRefName,headRefName,statusCheckRollup,reviewDecision,commits,mergeable
```

**Stop if**:
- PR state is `CLOSED` -- report and stop.
- PR state is `MERGED` -- skip to Phase 5 (cleanup only).

Extract the linked issue number from the PR body (patterns: `Closes #N`, `Fixes #N`, `Resolves #N`) or title (`#N`). This is optional -- the pipeline works without a linked issue.

Log the review decision status (APPROVED, CHANGES_REQUESTED, etc.) but do NOT require formal approval to proceed. The user invoking this command is the approval.

### Phase 2: Rebase and Push

Ensure you are on the PR's head branch:

```bash
git fetch origin
git checkout <HEAD_BRANCH>
git rebase origin/<BASE_BRANCH>
```

**Stop on merge conflicts** -- report which files conflict and stop. Do not attempt auto-resolution.

If the rebase changed nothing (already up to date), skip the push. Otherwise:

```bash
git push --force-with-lease
```

**Stop if push fails** (branch protection, permissions) -- report the error.

### Phase 3: Wait for CI

If the repository has CI checks configured, poll until complete:

```bash
gh pr checks <PR_NUMBER>
```

Poll every 30 seconds, up to 15 minutes.

- **CI passes**: proceed to Phase 4.
- **No CI checks configured**: proceed to Phase 4 immediately.
- **CI fails**: analyze the failure output briefly.
  - If failures look like infrastructure/flaky issues (network timeouts, resource limits, unrelated tests), post a retry comment and wait once more. If it fails again, stop and report the diagnosis.
  - If any failure looks like a real code issue, stop and report the diagnosis with the failing check details.
- **CI times out** (15 minutes elapsed): stop and report. The user can re-run the command after CI completes.

### Phase 4: Merge

Try merge strategies in order until one succeeds (repo settings may restrict some):

```bash
gh pr merge <PR_NUMBER> --rebase --delete-branch
```

If rebase merge is not allowed, fall back to squash, then regular merge. If all fail, report the error.

**Stop if merge fails** -- report the error (usually merge conflicts, branch protection, or required reviews).

### Phase 5: Post-Merge Cleanup (incorporates `/after-merge`)

```bash
git checkout <BASE_BRANCH>
git pull origin <BASE_BRANCH>

# Delete local branch if it still exists
git branch -d <HEAD_BRANCH> 2>/dev/null || true
```

Clean up any worktrees associated with this PR or issue:

```bash
git worktree list
# Remove matching worktrees
git worktree remove <WORKTREE_PATH> --force 2>/dev/null || true
```

Close the linked issue if one was found and it was not auto-closed:

```bash
gh issue close <ISSUE_NUMBER> 2>/dev/null || true
```

Check for stale stashes that belong to the merged branch:

```bash
git stash list
```

If any stash entries reference the merged branch name, report them to the user (do not drop without confirmation).

### Phase 6: Capture Review Learnings (incorporates `/ingest-review`)

Only run this phase if `.claude/agent-memory/` exists in the project.

Check if there were review comments worth learning from:

```bash
OWNER_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
INLINE_COUNT=$(gh api "repos/${OWNER_REPO}/pulls/<PR_NUMBER>/comments" --jq 'length')
REVIEW_COUNT=$(gh api "repos/${OWNER_REPO}/pulls/<PR_NUMBER>/reviews" --jq '[.[] | select(.state == "CHANGES_REQUESTED" or .body != "")] | length')
```

If there were substantive review comments (2+ inline comments or any CHANGES_REQUESTED reviews):

1. Fetch full review data:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title
   gh api "repos/${OWNER_REPO}/pulls/<PR_NUMBER>/comments"
   ```

2. Analyze and extract:
   - **Patterns to follow** -- things reviewers praised or explicitly requested
   - **Mistakes to avoid** -- bugs caught, missing edge cases, style violations
   - **Test coverage gaps** -- missing assertions, untested scenarios

3. Update the agent memory files (only if they exist):
   - Append new findings to `review-feedback.md` (skip duplicates)
   - Add recurring mistakes to `common-mistakes.md`
   - Log the PR in `task-history.md` with date, summary, and outcome

If there were no substantive review comments, still log the PR in `task-history.md` (if it exists).

### Phase 7: Extract and Promote Knowledge (incorporates `/extract-knowledge`)

Only run this phase if `.claude/agent-memory/` exists in the project.

1. **Update test count** in `.claude/agent-memory/MEMORY.md` if the PR added or removed tests. Also update any other files that track test counts (README.md, AGENTS.md, etc.).

2. **Promote confirmed patterns to project knowledge**: Review the findings captured in Phase 6. If any pattern was:
   - Flagged in 2+ PRs, OR
   - A security/correctness issue with clear prevention rule
   
   Then add it to the appropriate project knowledge file (the project's rules, guidelines, or conventions docs).

3. **Add session learnings**: If new framework gotchas, testing patterns, or development insights emerged during this PR's development, append them to `.claude/agent-memory/learnings.md`.

4. **Check file sizes**: Ensure agent memory files stay within limits:
   - `MEMORY.md` -- under 80 lines
   - `learnings.md` -- under 150 lines (prune oldest if needed)
   - `review-feedback.md` -- under 150 lines (prune oldest if needed)

### Phase 8: Report

Output a concise summary covering:

- PR number, title, and base branch it was merged into
- Whether rebase was needed
- CI status (passed, retried, or skipped)
- Branches cleaned up (local + remote)
- Issue closed (or no linked issue)
- Learnings captured (count, or "skipped" if no agent memory)
- Patterns promoted to project knowledge (count, or "none")
- Stale stashes found (if any)

## Error Recovery

When the pipeline stops at any phase, report clearly:

- Which phase failed
- The specific error
- What to do next (resolve conflicts, fix CI, retry, etc.)

The user can fix the issue and re-run `/integrate-pr <PR_NUMBER>` to resume. The command is idempotent -- it detects the current state and picks up from where it left off (e.g., if already rebased, it skips to CI; if already merged, it skips to cleanup).

## Cross-References

- **Replaces**: `/after-merge` (cleanup) + `/extract-knowledge` (learning) -- both are now built into this pipeline
- **Before this**: `/review-full` or `/address-pr` to prepare the PR
- **Next**: `/find-task` or `/standup` to pick up the next task

## Rules

- Never stop between phases unless there is a hard failure
- Try `--rebase` merge first, fall back to `--squash`, then `--merge`
- Always use `--delete-branch` to clean up the remote branch
- Use `--force-with-lease` for pushes, never `--force`
- Only record actionable, specific lessons in memory -- not generic advice
- Do not duplicate existing memory entries
- NEVER use emojis in any output
