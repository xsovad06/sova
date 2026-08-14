---
description: Merge an approved PR (squash), delete the branch, and run post-merge cleanup.
user-invocable: true
---

# Approve Merge

Merge a PR and perform post-merge cleanup. This command is the final step in the shipping pipeline -- it runs after the PR has been rebased, CI has passed, and the user has approved the merge from the dashboard.

```bash
# Benchmark logging (entry)
bash .claude/benchmark/log.sh "approve_merge_start" "" "" 2>/dev/null || true
```

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
- PR is mergeable (no conflicts)

Note: formal GitHub review approval is NOT required. The user triggering this command from the dashboard is the approval. Log the review status for the record, but proceed regardless.

If CI checks are still pending, poll in a loop. Replace `<PR_NUMBER>` with the actual PR number from context before executing the loop as a single Bash tool call. Requires gh CLI v2.32+ (for the `bucket` field).

```bash
# Set the PR number once at the top -- substitute from context
PR_NUM=<PR_NUMBER>
# Poll CI checks in a loop (30 iterations x 30s = 15 minutes max)
# Uses `bucket` (not `state`) -- bucket normalizes raw states into: pass, fail, pending, skipping, cancel
# Grace period: first 5 iterations (2.5 min) tolerate TOTAL=0 for checks to register after push
for i in $(seq 1 30); do
  echo "--- CI poll attempt $i/30 ---"
  CHECKS_JSON=$(gh pr checks "$PR_NUM" --json name,bucket 2>/dev/null || echo "[]")
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
- **CI PASSED**: proceed to merge (step 3)
- **NO_CHECKS** (no CI checks configured): proceed to merge (step 3). No checks means nothing to wait for.
- **CI FAILED**: write a failed handoff with the failing check names and stop -- do not merge with failing CI
- **CI TIMEOUT**: write a handoff with status `awaiting_action` and a "Wait for CI" next action (mode: `claude-command`, command: `agent-resume`, args: `{pr: <PR_NUMBER>, wait_for: "ci"}`), then stop

### 3. Merge

Read `sova.toml` to determine merge settings from the `[integration]` section:

- `merge_method`: "auto" (repo default), "squash", "rebase", or "merge"
- `delete_branch`: true/false (default true)
- `merge_queue_enabled`: "auto" (detect via GraphQL), "true", "false"
- `post_merge_state`: "done" (close issue) or "on_qa" (add label, keep open)

**Merge queue detection** (when `merge_queue_enabled = "auto"` or `"true"`):

Query the GraphQL API to check if a merge queue is configured on the base branch.

If merge queue is detected (or forced via config):
- Omit merge strategy flags, GitHub merge queues control the strategy
- Omit `--delete-branch` (branch deletion handled separately after queue processing)
- Run: `gh pr merge <PR_NUMBER> --repo <OWNER/REPO>`
- If output contains "already queued" or "added to merge queue":
  - Write a merge queue marker file so the dashboard can track the PR:
    ```bash
    mkdir -p .claude/agent-control
    python3 -c "import json; print(json.dumps({'pr_number': <PR_NUMBER>, 'repo': '<OWNER/REPO>', 'issue_number': '<ISSUE_NUMBER>', 'branch_name': '<HEAD_BRANCH>'}))" > .claude/agent-control/merge-queue-<PR_NUMBER>.json
    ```
  - Proceed to queue polling (step 3b)

If merge queue is NOT detected:
- Apply the configured merge method flag (or omit for "auto" to use repo default)
- Include `--delete-branch` if configured

```bash
gh pr merge <PR_NUMBER> --squash --delete-branch
```

If merge fails, write a failed handoff with the error and stop.

#### 3b. Merge Queue Polling (only if PR was enqueued)

Poll merge queue status via GraphQL until merged, ejected, or timeout.
Poll every `merge_queue_poll_interval` seconds (default 30), up to `merge_queue_timeout` seconds (default 1800).

- **MERGED**: proceed to cleanup. If `delete_branch = true`, delete the remote branch explicitly via GitHub API
- **UNMERGEABLE**: report ejection with position info and stop (do not retry)
- **TIMEOUT**: report that the PR is still enqueued. Stop, user must run `/after-merge` manually.

### 4. Post-Merge Issue State

Handle the issue based on `post_merge_state` from config.

**GitHub projects** (`task_source.type = "github"` or no `sova.toml`):

- **"done"** (default): close the linked issue (`gh issue close <ISSUE_NUMBER>`)
- **"on_qa"**: add `agent:on-qa` label, do NOT close the issue
- **Other value**: log a warning and skip the state transition

**Jira projects** (`task_source.type = "jira"`):

Read the Jira connection settings from `sova.toml` (`[task_source]` section). Use the Jira REST API to transition the issue:

- **"done"**: trigger a Jira workflow transition matching "Done", "Closed", "Resolved", or "Close"
- **"on_qa"**: trigger a Jira workflow transition matching "On QA", "QA", "Verification", or "Ready for QA". Also add the `agent:on-qa` label.
- Check `jira_state_transitions` in `sova.toml` for custom transition name overrides
- If no matching transition is available on the Jira board, log a warning and skip

### 5. Local Cleanup

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

### 6. Suggest Follow-Up

Check if there are review learnings worth capturing:

```bash
# Check if there were review comments
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments --jq 'length'
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --jq '[.[] | select(.state == "CHANGES_REQUESTED")] | length'
```

If there were substantive review comments (more than 2), include an "Ingest review feedback" action in the handoff.

### 7. Write Handoff

Write the completion handoff to `.claude/agent-control/handoff.json`:

- `source`: `"approve-merge"`
- `status`: `"completed"` (or `"failed"`)
- `summary`: what happened (merged, branches cleaned)
- `details.actions_taken`: full list of cleanup steps
- `next_actions`: empty for completed, or:
  - **Ingest review feedback** (style: `neutral`) -- if there were review comments worth learning from

### 8. Report

Output a summary including:
- Merge status
- Branches cleaned up
- Any suggested follow-up actions

## Rules

- Never merge if CI is failing -- write a handoff instead
- Use the merge method from `[integration]` config (default: auto)
- Handle merge queue when detected or configured
- Always write a handoff file, even on completion (with `status: completed`)
- NEVER use emojis in any output

```bash
# Benchmark logging (exit)
bash .claude/benchmark/log.sh "approve_merge_complete" "" "" 2>/dev/null || true
```
