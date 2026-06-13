---
description: Full integration pipeline -- rebase, CI, merge, cleanup, learn. One click from approved PR to done.
user-invocable: true
---

# Integrate PR

Full autonomous integration pipeline for an approved PR. Chains the ship-pr, approve-merge, after-merge, extract-knowledge, and ingest-review workflows into a single uninterrupted run. Stops only on hard failures (conflicts, CI failures, merge rejection).

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

If there are merge conflicts, attempt to resolve them before giving up:

1. Identify the conflicting files (`git diff --name-only --diff-filter=U`).
2. For each conflicted file, read the full file, understand both sides of each conflict marker (`<<<<<<<` / `=======` / `>>>>>>>`), and choose the correct resolution (or merge both sides). Write the resolved content back and stage with `git add`.
3. After resolving all files in the current rebase step, continue the rebase:
   ```bash
   GIT_EDITOR=true git rebase --continue
   ```
4. If more conflicts appear on subsequent commits, repeat steps 1-3 (up to 3 rebase steps total).
5. After successful resolution, verify no conflict markers remain in the resolved files:
   ```bash
   grep -rn "<<<<<<< " <resolved_files>
   ```
   If any markers remain, abort the rebase (`git rebase --abort`) and write a failed handoff.

If conflict resolution fails after 3 attempts, or if the conflicts are too complex to resolve confidently:
- Run `git rebase --abort` to restore a clean state
- Write a failed handoff listing the conflicting files and stop

If rebase was a no-op (already up to date), skip the push. Otherwise:

```bash
git push --force-with-lease
```

**Hard stop** if push fails (branch protection, permissions).

### Phase 3: Pre-Merge Documentation Updates

Run this phase on the feature branch BEFORE merge. This avoids post-merge commits on main (which branch protection would block).

Only run if `.claude/agent-memory/` exists in the project.

1. **Capture review learnings**: fetch all review data from the PR:
   ```bash
   OWNER_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
   gh pr view <PR_NUMBER> --json comments,reviews,body,title
   gh api "repos/${OWNER_REPO}/pulls/<PR_NUMBER>/comments"
   gh api "repos/${OWNER_REPO}/pulls/<PR_NUMBER>/reviews"
   ```
   Analyze ALL review comments (human and bot) for actionable findings:
   - Patterns to follow, mistakes to avoid, test coverage gaps, bot suggestions worth adopting
   - Skip only if there are truly zero review comments. A bot approval with suggestions still counts.
   - Update `.claude/agent-memory/cookbook.md` (append new findings, no duplicates).
   - Promote confirmed patterns (flagged in 2+ PRs, or security/correctness) to `.claude/rules/*.md`.

2. **Update documentation counts**: run verification commands and fix any drifted counts:
   ```bash
   pytest tests/ --collect-only -q 2>/dev/null | tail -1   # test count
   ls sova/dashboard/services/*.py | grep -v __init__ | wc -l  # service count
   ls sova/dashboard/routers/*.py | grep -v __init__ | wc -l   # router count
   ```
   If any count in `AGENTS.md`, `README.md`, or `docs/VISION.md` has drifted, update it.

3. **Add session learnings**: scan the PR description, commit messages, and this conversation for new framework gotchas, testing patterns, or operational issues. Append to `.claude/agent-memory/cookbook.md`.

4. **Check file sizes**: `cookbook.md` under 200 lines (prune oldest `[confirmed: 0]` if needed).

5. **Commit and push** if any files changed:
   ```bash
   git add -A .claude/agent-memory/ AGENTS.md README.md docs/VISION.md .claude/rules/
   git commit -m "docs: update counts and capture learnings from PR #<PR_NUMBER>"
   git push --force-with-lease
   ```

### Phase 4: Wait for CI

After pushing, CI needs to re-run. Poll until complete:

```bash
gh pr checks <PR_NUMBER>
```

Poll every 30 seconds, up to 15 minutes total.

**If CI passes**: proceed to Phase 5.

**If CI fails**: analyze the failures briefly.
- If all failures look like infrastructure/flaky (network timeouts, resource limits), post a `/retest` comment and retry once. If it fails again, write a failed handoff and stop.
- If any failure looks like a real code issue, write a failed handoff with diagnosis and stop.

**If CI times out** (15 minutes): write an `awaiting_action` handoff with "Wait for CI" and "Abort" actions, then stop.

### Phase 5: Merge

Try merge strategies in order until one succeeds (repo settings may restrict some):

```bash
gh pr merge <PR_NUMBER> --rebase --delete-branch
```

If rebase merge is not allowed, fall back to `--squash`, then `--merge`. If all fail, write a failed handoff and stop.

**Hard stop** if merge fails -- write a failed handoff with the error.

### Phase 6: Post-Merge Cleanup (incorporates `/after-merge`)

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

Check for stale stashes that belong to the merged branch:

```bash
git stash list
```

If any stash entries reference the merged branch name, log them in the handoff. In autonomous mode, do not drop stashes -- report them. In interactive mode, ask the user.

### Phase 7: Write Completion Handoff

Note: review learnings and doc count updates were already captured in Phase 3 (pre-merge). This phase only writes the completion handoff -- no git commits on main.

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
  - `patterns_promoted`: number of patterns promoted to project knowledge (0 if none)
  - `stale_stashes`: list of stash descriptions found (empty if none)
  - `ci_status`: `"passed"`
- `next_actions`: empty (pipeline is complete)

### Phase 8: Report

Output a summary:
- PR merged into base branch (merge strategy used)
- Branches cleaned up (local + remote)
- Issue closed
- Learnings captured (count, or "no substantive review comments")
- Patterns promoted to project knowledge (count, or "none")
- Stale stashes found (if any)
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
Note: Phase 3 (doc updates) runs after rebase, Phase 4 (CI), Phase 5 (merge).
1. "Resolve Conflicts" (style: `neutral`) -- manual resolution needed
2. "Retry Integration" (style: `neutral`) -- mode: `claude-command`, command: `/integrate-pr`, args: `{pr}` -- re-run after main changes
3. "Abort" (style: `danger`) -- clear handoff

**CI failures (Phase 4)**:
1. "Retry CI" (style: `neutral`) -- post `/retest` and wait
2. "Investigate CI" (style: `neutral`) -- mode: `claude-command`, command: `/agent-resume`, args: `{pr, investigate_ci: true}`
3. "Abort" (style: `danger`)

**CI timeout (Phase 4)**:
1. "Wait for CI" (style: `neutral`) -- mode: `claude-command`, command: `/agent-resume`, args: `{pr, wait_for: "ci"}`
2. "Merge Now" (style: `approve`) -- mode: `claude-command`, command: `/approve-merge`, args: `{pr}`
3. "Abort" (style: `danger`)

**Merge failure (Phase 5)**:
1. "Retry Merge" (style: `neutral`) -- mode: `claude-command`, command: `/approve-merge`, args: `{pr}`
2. "Abort" (style: `danger`)

## Cross-References

- **Replaces**: `/after-merge` (cleanup) + `/extract-knowledge` (learning) -- both are now built into this pipeline
- **Before this**: `/review-full` or `/address-pr` to prepare the PR
- **Next**: `/find-task` or `/standup` to pick up the next task

## Rules

- Never stop between phases unless there is a hard failure
- Try `--rebase` merge first, fall back to `--squash`, then `--merge`
- Always use `--delete-branch` to clean up the remote branch
- Use `--force-with-lease` for pushes, never `--force`
- Always write a handoff file, even on success (with `status: completed`)
- Only record actionable, specific lessons in memory -- not generic advice
- Do not duplicate existing memory entries
- NEVER use emojis in any output
