---
description: Merge an approved PR (squash), delete the branch, and run post-merge cleanup.
user-invocable: true
---

# Approve Merge

Merge a PR and perform post-merge cleanup. This command is the final step in the shipping pipeline -- it runs after the PR has been rebased, CI has passed, and the user has approved the merge from the dashboard.

PR: $ARGUMENTS

## Instructions

You are an autonomous agent handling the merge and cleanup of an approved PR.

### 1. Read Handoff Context

Check if a handoff file exists from a previous agent:

```bash
cat .claude/agent-control/handoff.json 2>/dev/null
```

If present, extract `pr_number`, `issue`, and `branch` from it. If not present, parse the arguments directly.

### 2. Pre-Merge Verification

```bash
# Verify PR is still approved and mergeable
gh pr view <PR_NUMBER> --json state,reviewDecision,mergeable,statusCheckRollup,headRefName,baseRefName

# Check CI status
gh pr checks <PR_NUMBER>
```

Verify:
- PR state is `OPEN`
- Review decision is `APPROVED`
- PR is mergeable (no conflicts)

If CI checks are still pending, wait up to 5 minutes (polling every 30 seconds). If they don't complete, write a handoff with "Wait for CI" action and stop.

If CI has failures, write a failed handoff and stop -- do not merge with failing CI.

### 3. Merge

```bash
gh pr merge <PR_NUMBER> --squash --delete-branch
```

If merge fails, write a failed handoff with the error and stop.

### 4. Local Cleanup

```bash
# Switch to base branch
git checkout <BASE_BRANCH>
git pull origin <BASE_BRANCH>

# Delete local branch if it exists
git branch -d <HEAD_BRANCH> 2>/dev/null || true

# Clean up worktrees for this issue
git worktree list
# Remove any worktrees matching the issue pattern
```

### 5. Suggest Follow-Up

Check if there are review learnings worth capturing:

```bash
# Check if there were review comments
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments --jq 'length'
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --jq '[.[] | select(.state == "CHANGES_REQUESTED")] | length'
```

If there were substantive review comments (more than 2), include an "Ingest review feedback" action in the handoff.

### 6. Write Handoff

Write the completion handoff to `.claude/agent-control/handoff.json`:

- `source`: `"approve-merge"`
- `status`: `"completed"` (or `"failed"`)
- `summary`: what happened (merged, branches cleaned)
- `details.actions_taken`: full list of cleanup steps
- `next_actions`: empty for completed, or:
  - **Ingest review feedback** (style: `neutral`) -- if there were review comments worth learning from

### 7. Report

Output a summary including:
- Merge status
- Branches cleaned up
- Any suggested follow-up actions

## Rules

- Never merge if CI is failing -- write a handoff instead
- Always use `--squash` merge to keep history clean
- Always use `--delete-branch` to clean up the remote branch
- Always write a handoff file, even on completion (with `status: completed`)
- NEVER use emojis in any output
