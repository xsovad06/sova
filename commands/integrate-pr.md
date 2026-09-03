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

If the rebase changed nothing (already up to date), skip the push. Track whether a push happened so Phase 4 can decide whether CI must re-run. The flag is written to a state file (not a shell variable) so it survives across the separate command invocations of Phases 2 through 4:

```bash
# Reset the push-tracking state file at the start of the run.
mkdir -p .claude/agent-control
echo 0 > .claude/agent-control/integrate-pushed
# ... only if the rebase rewrote history:
git push --force-with-lease && echo 1 > .claude/agent-control/integrate-pushed
```

**Stop if push fails** (branch protection, permissions) -- report the error.

### Phase 3: Pre-Merge Documentation Updates

Run this phase on the feature branch BEFORE merge to avoid post-merge commits on main (which branch protection would block).

Only run if `.claude/agent-memory/` exists in the project.

1. **Capture review learnings**: fetch review data from the PR (`gh pr view`, `gh api repos/.../pulls/<N>/comments`). Analyze for actionable findings and update `.claude/agent-memory/cookbook.md` (no duplicates). Promote patterns confirmed in 2+ PRs to `.claude/rules/*.md`.

2. **Update documentation counts**: run verification commands (test count, service count, router count) and fix any drifted values in `AGENTS.md`, `README.md`, or `docs/VISION.md`.

3. **Amend into the last commit and push** if any files changed (never create a standalone docs commit):
   ```bash
   git add -A .claude/agent-memory/ AGENTS.md README.md docs/VISION.md .claude/rules/
   # Only amend and push when something actually changed. An amend rewrites
   # the head SHA and re-triggers CI, so a no-op amend wastes a full CI cycle.
   if ! git diff --cached --quiet; then
     git commit --amend --no-edit
     git push --force-with-lease && echo 1 > .claude/agent-control/integrate-pushed
   fi
   ```

   **CI-cost note**: the files staged here are Markdown only (`AGENTS.md`,
   `README.md`, `docs/**/*.md`, `.claude/rules/*.md`, `.claude/agent-memory/*.md`).
   CI classifies a change as docs-only iff every changed path matches `*.md`, so a
   markdown-only push does NOT re-run the expensive test/scan jobs (they report
   success immediately), and this amend no longer blocks the merge on a
   15-minute re-run. Note that `docs/` and `.claude/` also hold non-md code
   (scripts, manifests, HTML), so staging a non-md file here would re-run the full
   suite. The push-tracking state file still lets Phase 4 skip polling entirely
   when nothing was re-pushed at all.

### Phase 4: Wait for CI

**Fast path (skip the wait entirely).** If neither Phase 2 (rebase) nor Phase 3
(docs amend) pushed anything (the push-tracking state file still reads `0`), the
PR head SHA is unchanged, so any CI that already ran is still valid. Confirm the
existing checks are green and, if so, proceed straight to Phase 5 without polling:

```bash
# Default to 0 if the state file is missing (defensive: never assume a push).
PUSHED=$(cat .claude/agent-control/integrate-pushed 2>/dev/null || echo 0)
if [ "$PUSHED" -eq 0 ]; then
  # Capture the JSON and exit status separately. `gh pr checks` returns a
  # non-zero status (exit 8) when checks are pending while still writing valid
  # JSON, so a `|| echo "[]"` fallback would append a second JSON document and
  # break the numeric jq counts. Keep the real output; treat empty/invalid JSON
  # as non-green and fall through to the poll.
  CHECKS_JSON=$(gh pr checks <PR_NUMBER> --json name,bucket 2>/dev/null)
  TOTAL=$(echo "$CHECKS_JSON" | jq 'length' 2>/dev/null || echo 0)
  PENDING=$(echo "$CHECKS_JSON" | jq '[.[] | select(.bucket == "pending")] | length' 2>/dev/null || echo 1)
  FAILED=$(echo "$CHECKS_JSON" | jq '[.[] | select(.bucket == "fail" or .bucket == "cancel")] | length' 2>/dev/null || echo 1)
  # Require at least one check: zero checks is not a green fast path, poll instead.
  if [ "$TOTAL" -gt 0 ] && [ "$PENDING" -eq 0 ] && [ "$FAILED" -eq 0 ]; then
    echo "No re-push: existing CI is complete and green. Skipping the poll."
    # proceed to Phase 5
  fi
  # If checks are still pending/failed (or absent) despite no push, fall through
  # to the poll below.
fi
```

If a push DID happen (or the fast-path checks were not all green), poll in a
loop using the following bash command. This includes external review bots
(e.g., CodeRabbit) that appear as pending StatusContext checks.

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

Read `sova.toml` to determine merge settings from the `[integration]` section:

- `merge_method`: "auto" (repo default), "squash", "rebase", or "merge"
- `delete_branch`: true/false (default true)
- `merge_queue_enabled`: "auto" (detect via GraphQL), "true", "false"
- `post_merge_state`: "done" (close issue) or "on_qa" (add label, keep open)

First, query the PR to determine the base branch for queue detection:
```bash
gh pr view <PR_NUMBER> --json baseRefName --jq '.baseRefName'
```

**Merge queue detection**: using the base branch from above, query the GraphQL API to check if a merge queue is configured.

If merge queue is detected:
- Omit merge strategy flags (queue controls strategy)
- Omit `--delete-branch` (handled after queue processing)
- Run: `gh pr merge <PR_NUMBER>`
- If enqueued, write a merge queue marker file so the dashboard can track the PR:
  ```bash
  mkdir -p .claude/agent-control
  python3 -c "import json; print(json.dumps({'pr_number': <PR_NUMBER>, 'repo': '<OWNER/REPO>', 'issue_number': '<ISSUE_NUMBER>', 'branch_name': '<HEAD_BRANCH>'}))" > .claude/agent-control/merge-queue-<PR_NUMBER>.json
  ```
- Then poll merge queue status via GraphQL every `merge_queue_poll_interval` seconds (default 30)
- On MERGED: proceed to Phase 6. If `delete_branch = true`, delete remote branch via GitHub API
- On UNMERGEABLE: report ejection and stop
- On TIMEOUT: report the PR is still enqueued, stop

If merge queue is NOT detected:

```bash
gh pr merge <PR_NUMBER> [--squash|--rebase|--merge] [--delete-branch]
```

If `merge_method` is "auto", omit strategy flags to use the GitHub repo default. Otherwise use the configured method. Only include `--delete-branch` when `delete_branch = true`.

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

Handle the linked issue based on `post_merge_state` from `[integration]` config.

**GitHub projects** (`task_source.type = "github"` or no `sova.toml`):

- **"done"** (default): close the issue (`gh issue close <ISSUE_NUMBER>`)
- **"on_qa"**: add `agent:on-qa` label, keep the issue open
- **Other value**: log a warning and skip the state transition

**Jira projects** (`task_source.type = "jira"`):

Read the Jira connection settings from `sova.toml` (`[task_source]` section: `jira_base_url`, `jira_email`, `jira_api_token`, `jira_project_key`). Use the Jira REST API to transition the issue:

- **"done"**: trigger a Jira workflow transition matching "Done", "Closed", "Resolved", or "Close"
- **"on_qa"**: trigger a Jira workflow transition matching "On QA", "QA", "Verification", or "Ready for QA". Also add the `agent:on-qa` label.
- Check `jira_state_transitions` in `sova.toml` for custom transition name overrides (e.g., `on_qa = "Move to QA"` takes priority over the defaults)
- If no matching transition is available on the Jira board, log a warning and skip

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
- Use the merge method from `[integration]` config (default: auto, uses GitHub repo default)
- Handle merge queue when detected or configured
- Use `--force-with-lease` for pushes, never `--force`
- Only record actionable, specific lessons in memory -- not generic advice
- Do not duplicate existing memory entries
- NEVER use emojis in any output
