---
name: integrate-pr
description: Full integration pipeline -- rebase, CI, merge, cleanup, learn. One click from approved PR to done.
user-invocable: true
---

# Integrate PR

Full autonomous integration pipeline for an approved PR. Chains the ship-pr, approve-merge, after-merge, and ingest-review workflows into a single uninterrupted run. Stops only on hard failures (conflicts, CI failures, merge rejection).

PR: $ARGUMENTS

## Instructions

You are an autonomous agent running the full integration pipeline. Your job is to take an approved PR from its current state all the way to merged, cleaned up, and learnings captured -- without human intervention unless something breaks.

### Phase 1: Gather State

Read the handoff file if one exists:

```bash
cat .claude/agent-control/handoff.json 2>/dev/null
```

If present, extract `pr_number`, `issue`, and `branch`. Otherwise parse from arguments.

Get PR metadata:

```bash
gh pr view <PR_NUMBER> --json title,body,state,baseRefName,headRefName,statusCheckRollup,reviewDecision,commits,mergeable,number
```

**Hard stop** if:
- PR state is not `OPEN` (if `MERGED`, skip to Phase 5 cleanup)

Note: review approval is NOT required. When this command is triggered from the dashboard, the user clicking "Integrate PR" is the approval. Log whether the PR has a formal GitHub review approval, but proceed regardless.

Extract the issue number from the PR title (pattern `#NNN` or `Closes #NNN` in the body).

### Phase 2: Rebase and Push

```bash
git fetch origin
git checkout <HEAD_BRANCH>
git rebase origin/<BASE_BRANCH>
```

**Hard stop** on merge conflicts -- write a failed handoff listing conflicting files and stop. Do not attempt auto-resolution.

If rebase was a no-op (already up to date), skip the push. Otherwise:

```bash
git push --force-with-lease
```

**Hard stop** if push fails (branch protection, permissions).

### Phase 3: Wait for CI

After pushing, CI needs to re-run. Poll until complete:

```bash
gh pr checks <PR_NUMBER>
```

Poll every 30 seconds, up to 15 minutes total.

**If CI passes**: proceed to Phase 4.

**If CI fails**: analyze the failures briefly.
- If all failures look like infrastructure/flaky (network timeouts, resource limits), post a `/retest` comment and retry once. If it fails again, write a failed handoff and stop.
- If any failure looks like a real code issue, write a failed handoff with diagnosis and stop.

**If CI times out** (15 minutes): write an `awaiting_action` handoff with "Wait for CI" and "Abort" actions, then stop.

### Phase 4: Merge

```bash
gh pr merge <PR_NUMBER> --squash --delete-branch
```

**Hard stop** if merge fails -- write a failed handoff with the error.

### Phase 5: Post-Merge Cleanup

```bash
# Switch to base branch and sync
git checkout <BASE_BRANCH>
git pull origin <BASE_BRANCH>

# Delete local branch
git branch -d <HEAD_BRANCH> 2>/dev/null || true

# Clean up worktrees for this issue
git worktree list
# Remove any worktrees matching the issue number pattern
git worktree remove .claude/worktrees/<ISSUE_PATTERN> --force 2>/dev/null || true
```

Close the linked issue if not auto-closed:

```bash
gh issue close <ISSUE_NUMBER> 2>/dev/null || true
```

### Phase 6: Ingest Review Feedback

Check if there were review comments worth learning from:

```bash
OWNER_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
gh api repos/${OWNER_REPO}/pulls/<PR_NUMBER>/comments --jq 'length'
gh api repos/${OWNER_REPO}/pulls/<PR_NUMBER>/reviews --jq '[.[] | select(.state == "CHANGES_REQUESTED" or .body != "")] | length'
```

If there were substantive review comments (2+ inline comments or any CHANGES_REQUESTED reviews):

1. Fetch full review data:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title
   gh api repos/${OWNER_REPO}/pulls/<PR_NUMBER>/comments
   ```

2. Analyze and extract:
   - **Patterns to follow** -- things reviewers praised or explicitly requested
   - **Mistakes to avoid** -- bugs caught, missing edge cases, style violations
   - **Test coverage gaps** -- missing assertions, untested scenarios

3. Read existing memory files:
   - `.claude/agent-memory/review-feedback.md`
   - `.claude/agent-memory/common-mistakes.md`
   - `.claude/agent-memory/task-history.md`

4. Update memory files:
   - Append new findings to `review-feedback.md` (no duplicates)
   - Add recurring mistakes to `common-mistakes.md`
   - Log the PR in `task-history.md` with ticket, date, summary, outcome

If there were no substantive review comments, skip the learning extraction but still log the PR in `task-history.md`.

### Phase 7: Write Completion Handoff

Write the final handoff to `.claude/agent-control/handoff.json`:

```bash
mkdir -p .claude/agent-control
```

- `source`: `"integrate-pr"`
- `status`: `"completed"`
- `issue`: the issue number
- `pr_number`: the PR number
- `summary`: concise summary of what happened (merged, cleaned, learnings captured)
- `details`:
  - `actions_taken`: list of all phases completed
  - `learnings_captured`: number of new findings ingested (0 if none)
  - `ci_status`: `"passed"`
- `next_actions`: empty (pipeline is complete)

### Phase 8: Report

Output a summary:
- PR merged (squash) into base branch
- Branches cleaned up (local + remote)
- Issue closed
- Learnings captured (count, or "no substantive review comments")
- Total pipeline duration

## Failure Handoffs

When writing a failed handoff at any phase, include:

- `source`: `"integrate-pr"`
- `status`: `"failed"`
- `summary`: what failed and at which phase
- `details.failed_phase`: which phase failed (1-6)
- `details.error`: the error message
- `next_actions` based on failure type:

**Merge conflicts (Phase 2)**:
1. "Resolve Conflicts" (style: `neutral`) -- manual resolution needed
2. "Abort" (style: `danger`) -- clear handoff

**CI failures (Phase 3)**:
1. "Retry CI" (style: `neutral`) -- post `/retest` and wait
2. "Investigate CI" (style: `neutral`) -- mode: `claude-command`, command: `/agent-resume`, args: `{pr, investigate_ci: true}`
3. "Abort" (style: `danger`)

**CI timeout (Phase 3)**:
1. "Wait for CI" (style: `neutral`) -- mode: `claude-command`, command: `/agent-resume`, args: `{pr, wait_for: "ci"}`
2. "Merge Now" (style: `approve`) -- mode: `claude-command`, command: `/approve-merge`, args: `{pr}`
3. "Abort" (style: `danger`)

**Merge failure (Phase 4)**:
1. "Retry Merge" (style: `neutral`) -- mode: `claude-command`, command: `/approve-merge`, args: `{pr}`
2. "Abort" (style: `danger`)

## Rules

- Never stop between phases unless there is a hard failure
- Always use `--squash` merge to keep history clean
- Always use `--delete-branch` to clean up the remote branch
- Use `--force-with-lease` for pushes, never `--force`
- Always write a handoff file, even on success (with `status: completed`)
- Only record actionable, specific lessons in memory -- not generic advice
- Do not duplicate existing memory entries
- NEVER use emojis in any output
