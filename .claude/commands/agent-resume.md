---
description: Read the current handoff state and continue the workflow based on what is needed.
user-invocable: true
---

# Agent Resume

Read the current handoff state and determine what to do next. This is the "smart router" command -- it inspects the current state of a PR or issue and autonomously decides the next action.

ARGS: $ARGUMENTS

## Instructions

You are an autonomous agent that picks up where a previous agent left off. Your job is to assess the current situation and take the appropriate next step.

### 1. Read Current State

Read all available state:

```bash
# Handoff from previous agent
cat .claude/agent-control/handoff.json 2>/dev/null

# Agent control status
cat .claude/agent-control/status.json 2>/dev/null
```

Parse the arguments -- they may include:
- `pr=<NUMBER>` -- a specific PR to work on
- `issue=<NUMBER>` -- a GitHub Issue to work on
- `wait_for=ci` -- wait for CI checks to pass
- `investigate_ci=true` -- investigate CI failures

### 2. Determine Situation

If a handoff exists, check its `status`:
- `awaiting_action` -- previous agent finished but needs human decision. Assess if any action can be taken automatically based on the args.
- `completed` -- previous workflow is done. Check if there is follow-up work.
- `failed` -- previous agent failed. Diagnose and suggest recovery.

If no handoff exists, use the PR number or issue to assess the current state:

```bash
# Get PR state
gh pr view <PR_NUMBER> --json state,reviewDecision,statusCheckRollup,headRefName,baseRefName,mergeable,commits

# Check if branch is behind base
gh pr view <PR_NUMBER> --json mergeStateStatus --jq '.mergeStateStatus'

# Check CI
gh pr checks <PR_NUMBER>

# Check for review comments needing attention
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments --jq '[.[] | select(.in_reply_to_id == null)] | length'
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --jq '.[] | "\(.user.login): \(.state)"'
```

### 3. Act Based on Situation

**PR is behind base branch** -- rebase and push:
- Fetch latest base, rebase the PR branch, push with --force-with-lease
- If there are merge conflicts, attempt to resolve them (read each conflicted file, understand both sides of each conflict marker, resolve, `git add`, `git rebase --continue` -- up to 3 rebase steps)
- If resolution fails, run `git rebase --abort` and write a failed handoff listing conflicting files
- Write handoff with merge options

**CI is pending** (and `wait_for=ci`):

Poll CI in a loop using the following bash command. The loop uses 30 iterations x 30s = 15 minutes (matching default `ci.max_wait=900s`). Adjust iteration count if `ci.max_wait` or `ci.poll_interval` are customized in `sova.toml`. Requires gh CLI v2.32+ (for the `bucket` field).

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
- **CI PASSED**: write handoff with "Merge" action
- **CI FAILED**: write handoff with "Retry" and "Investigate" actions
- **NO_CHECKS** (no CI checks configured): proceed to next action (treat as passed) -- write handoff with "Merge" action
- **CI TIMEOUT**: write handoff with "Wait for CI" action so the dashboard can re-trigger later

**CI has failed** (and `investigate_ci=true`):
- Fetch the CI check details
- Determine if failures are flaky (infrastructure) or real (code)
- For flaky failures: post `/retest` comment on PR, write handoff with "Wait for CI" action
- For real failures: write handoff with diagnosis and "Fix CI" action

**PR has unaddressed review comments**:
- Count and summarize the comments
- Write handoff with "Address comments" action pointing to `address-pr` mode

**PR is approved, CI green, branch is up to date**:
- Write handoff with "Merge" as the primary action
- This is the "ready to ship" state

**PR is merged** (state is `MERGED`):
- Run after-merge cleanup (local branches, worktrees)
- Write completion handoff

### 4. Write Handoff

Always write a handoff file reflecting the current state and available next actions. Follow the handoff protocol schema.

Include:
- `source`: `"agent-resume"`
- Current `status` based on assessment
- `summary`: clear description of the current state
- `next_actions`: appropriate options based on the situation

### 5. Report

Output a brief status report:
- What state was found
- What action was taken (if any)
- What the user can do next from the dashboard

## Rules

- This command is a router, not a doer. For complex operations (rebase, merge, address comments), prefer writing a handoff that points to the specialized command rather than doing everything inline.
- Exception: simple operations (posting `/retest`, waiting for CI) can be done inline.
- Always write a handoff file so the dashboard stays updated.
- Never merge without explicit user action (either via handoff "Merge" button or direct instruction).
- When waiting for CI, respect the timeout (15 minutes max) and write a handoff if it exceeds.
- NEVER use emojis in any output
