---
name: integrate-pr
description: Full integration pipeline -- rebase, CI, merge, cleanup, learn. One click from approved PR to done.
user-invocable: true
category: pr
inputs:
  - pr_number
outputs:
  - merge_result
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
git worktree prune
git checkout <HEAD_BRANCH>
git rebase origin/<BASE_BRANCH>
```

If checkout fails with "already checked out", `sync_branch()` auto-resolves the conflict via `resolve_worktree_conflict()`. This handles stale worktree cleanup with PID liveness checks.

**Stop on merge conflicts** -- report which files conflict and stop. Do not attempt auto-resolution.

If the rebase changed nothing (already up to date), skip the push. Otherwise:

```bash
git push --force-with-lease
```

**Stop if push fails** (branch protection, permissions) -- report the error.

### Phase 3: Pre-Merge Documentation Updates

Run this phase on the feature branch BEFORE merge to avoid post-merge commits on main (which branch protection would block).

Only run if `.claude/agent-memory/` exists in the project.

1. **Capture review learnings**: fetch review data from the PR (`gh pr view`, `gh api repos/.../pulls/<N>/comments`). Analyze for actionable findings and update `.claude/agent-memory/cookbook.md` (no duplicates). Promote patterns confirmed in 2+ PRs to `.claude/rules/*.md`.

2. **Update documentation counts**: run verification commands (test count, service count, router count) and fix any drifted values in `AGENTS.md`, `README.md`, or `docs/VISION.md`.

3. **Commit and push** if any files changed:
   ```bash
   git add -A .claude/agent-memory/ AGENTS.md README.md docs/VISION.md .claude/rules/
   git commit -m "docs: update counts and capture learnings from PR #<PR_NUMBER>"
   git push --force-with-lease
   ```

### Phase 4: Wait for CI

If the repository has CI checks configured, poll in a loop using the following bash command. This includes external review bots (e.g., CodeRabbit) that appear as pending StatusContext checks.

Requires gh CLI v2.32+ (for the `bucket` field).

```bash
# Poll CI checks in a loop (30 iterations x 30s = 15 minutes max)
# Uses `bucket` (not `state`) -- bucket normalizes raw states into: pass, fail, pending, skipping, cancel
# Grace period: first 5 iterations (2.5 min) tolerate TOTAL=0 for checks to register after push
for i in $(seq 1 30); do
  echo "--- CI poll attempt $i/30 ---"
  CHECKS_JSON=$(gh pr checks <PR_NUMBER> --json name,bucket 2>/dev/null || echo "[]")
  echo "$CHECKS_JSON" | jq -r '.[] | "\(.bucket)\t\(.name)"'
  STATS=$(echo "$CHECKS_JSON" | jq -r '
    (length | tostring) + "\t" +
    ([.[] | select(.bucket == "pending")] | length | tostring) + "\t" +
    ([.[] | select(.bucket == "fail" or .bucket == "cancel")] | length | tostring)
  ' 2>/dev/null) || { echo "Failed to parse CI check status"; break; }
  IFS=$'\t' read -r TOTAL PENDING FAILED <<< "$STATS"
  if [ "$TOTAL" -eq 0 ]; then
    if [ "$i" -lt 5 ]; then
      echo "No checks registered yet (grace period $i/5)"
      sleep 30
      continue
    else
      echo "NO_CHECKS: no CI checks configured (grace period expired)"
      break
    fi
  fi
  if [ "$PENDING" -eq 0 ]; then
    if [ "$FAILED" -gt 0 ]; then
      echo "CI FAILED: $FAILED check(s) failed"
      break
    else
      echo "CI PASSED: all $TOTAL checks passed"
      break
    fi
  fi
  if [ "$i" -eq 30 ]; then
    echo "CI TIMEOUT: checks still pending after 15 minutes"
    break
  fi
  sleep 30
done
```

Act on the result:
- **CI PASSED**: also verify that no blocking `CHANGES_REQUESTED` review remains (`gh pr view <PR_NUMBER> --json reviewDecision` -- `gh pr checks` monitors CI status only, not review decisions). Then proceed to Phase 5.
- **NO_CHECKS** (no CI checks configured): proceed to Phase 5. No checks means nothing to wait for.
- **CI FAILED**: analyze the failure output briefly.
  - For infrastructure/flaky issues (network timeouts, resource limits, unrelated tests), post a retry comment and re-run the polling loop once more. On second failure, stop and report the diagnosis.
  - For real code issues, stop and report the diagnosis with failing check details.
- **CI TIMEOUT**: stop and report. The user can re-run `/integrate-pr` after CI completes.

### Phase 5: Merge

Try merge strategies in order until one succeeds (repo settings may restrict some):

```bash
gh pr merge <PR_NUMBER> --rebase --delete-branch
```

If rebase merge is not allowed, fall back to squash, then regular merge. If all fail, report the error.

**Stop if merge fails** -- report the error (usually merge conflicts, branch protection, or required reviews).

### Phase 6: Post-Merge Cleanup (incorporates `/after-merge`)

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

### Phase 7: Report

Note: review learnings and doc count updates were captured in Phase 3 (pre-merge). No post-merge git commits are needed.

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
