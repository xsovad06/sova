---
description: Rebase an approved PR onto the base branch, push, and wait for CI. Autonomous -- no approval gates.
user-invocable: true
---

# Ship PR

Prepare an approved PR for merging by rebasing it onto the latest base branch and pushing. This command is autonomous and writes a handoff file when done so the dashboard can present merge options.

PR: $ARGUMENTS

## Instructions

You are an autonomous agent handling the "ship" phase of a PR. The PR is already approved by a human reviewer. Your job is to get it ready for merge.

### 1. Gather PR State

```bash
# PR metadata
gh pr view <PR_NUMBER> --json title,body,state,baseRefName,headRefName,statusCheckRollup,reviewDecision,commits,mergeable

# Current CI status
gh pr checks <PR_NUMBER>
```

Verify:
- PR state is `OPEN`
- Note the base branch (usually `main`) and head branch

Note: formal GitHub review approval is NOT required. The user triggering this command is the approval. Log the review status for the record, but proceed regardless.

### 2. Sync and Rebase

```bash
# Fetch latest
git fetch origin

# Check out the PR branch
git checkout <HEAD_BRANCH>

# Rebase onto base branch
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
- Write a handoff with status `failed`, listing the conflicting files
- Include "Resolve conflicts" and "Retry" actions
- Stop

### 3. Push

```bash
git push --force-with-lease
```

If push fails (e.g., branch protection, permissions), write a failed handoff and stop.

### 4. Poll CI

After pushing, CI checks need time to start and complete. Poll in a loop using the following bash command:

```bash
# Poll CI checks in a loop (30 iterations x 30s = 15 minutes max)
# Uses `bucket` (not `state`) -- bucket normalizes raw states into: pass, fail, pending, skipping, cancel
for i in $(seq 1 30); do
  echo "--- CI poll attempt $i/30 ---"
  CHECKS_JSON=$(gh pr checks <PR_NUMBER> --json name,bucket 2>/dev/null || echo "[]")
  echo "$CHECKS_JSON" | jq -r '.[] | "\(.bucket)\t\(.name)"'
  IFS=$'\t' read -r TOTAL PENDING FAILED < <(echo "$CHECKS_JSON" | jq -r '
    (length | tostring) + "\t" +
    ([.[] | select(.bucket == "pending")] | length | tostring) + "\t" +
    ([.[] | select(.bucket == "fail" or .bucket == "cancel")] | length | tostring)
  ')
  if [ "$TOTAL" -gt 0 ] && [ "$PENDING" -eq 0 ]; then
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

Classify the result for the handoff:
- **CI PASSED**: write handoff with merge option as primary action
- **CI TIMEOUT**: write handoff with "Wait for CI" and "Merge now" options
- **CI FAILED**: analyze failures. If known flaky checks, write handoff noting this. If real failures, write failed handoff.

### 5. Write Handoff

Write the handoff file to `.claude/agent-control/handoff.json`. Ensure the directory exists first.

```bash
mkdir -p .claude/agent-control
```

The handoff must follow the handoff protocol schema. Include:
- `source`: `"ship-pr"`
- `issue`: extracted from PR title or branch name (pattern: `#NNN` or issue number)
- `pr_number`: the PR number
- `branch`: the head branch name
- `status`: `"awaiting_action"` (or `"failed"` if something went wrong)
- `summary`: what happened (rebased, pushed, CI status)
- `details.actions_taken`: list of steps completed
- `details.ci_status`: current CI status (`passed`, `pending`, `failed`)

**Next actions to include:**

If CI passed or pending:
1. **Merge PR** (style: `approve`) -- mode: `claude-command`, command: `approve-merge`, args: `{pr, issue}`
2. **Wait for CI** (style: `neutral`) -- mode: `claude-command`, command: `agent-resume`, args: `{pr, wait_for: "ci"}`
3. **Abort** (style: `danger`) -- mode: `shell`, clears the handoff file

If CI failed:
1. **Retry CI** (style: `neutral`) -- mode: `shell`, command: posts `/retest` comment
2. **Investigate** (style: `neutral`) -- mode: `claude-command`, command: `agent-resume`, args: `{pr, investigate_ci: true}`
3. **Abort** (style: `danger`)

### 6. Report

Output a brief summary of what was done and what the user can do next from the dashboard.

## Rules

- Never merge the PR -- that is the next agent's job (approve-merge)
- Never modify code -- this command only rebases and pushes
- Always write a handoff file, even on failure
- Use `--force-with-lease` for pushes, never `--force`
- Extract the issue number from the PR title (look for patterns like `#NNN` or `Closes #NNN`)
- NEVER use emojis in any output
