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

Merge a PR that is already ready, then clean up. Verifies mergeability, merges, cleans up branches/worktrees/stashes, closes the linked issue, and runs post-merge cleanup. Replaces the need to run `/after-merge` separately. Works for both manual invocation and autonomous agent use.

**This command does not push by default.** A push creates a new head SHA and spends a full CI cycle, which on a ready PR is pure waste and delays the merge by the length of the suite. The PR is expected to arrive here mergeable, with review learnings and documentation already folded in by `/address-pr`. Two things justify a push, both meaning another PR merged into the base branch since this one was last pushed: a real conflict (`mergeable: CONFLICTING`), or, under this repository's strict status-check policy, the branch simply being behind (`mergeStateStatus: BEHIND`) even with no conflicting lines. Phase 2 resolves either case and is explicit about the cost.

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

### Phase 2: Assess Mergeability (push only when the PR cannot merge as it stands)

The default path through this command is: do not touch the branch, do not push,
do not re-run CI. Every push here creates a new head SHA and costs a full CI
cycle, so a push must be justified by GitHub refusing to merge, never by
routine hygiene.

Reset the push-tracking state file first. It is a file rather than a shell
variable because Phases 2 through 4 run as separate command invocations, and
it lives in the PRIMARY checkout specifically: the Update subroutine below may
`cd` into a per-issue worktree, and `.claude/agent-control/` is not mirrored
into worktrees, so a bare relative path would read and write a different file
depending on which directory happens to be current when each phase runs.
Resolve the primary checkout explicitly rather than assuming the current
directory is it:

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in
  /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;;   # linked worktree
  *)  PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;;  # already primary
esac
mkdir -p "$PRIMARY_ROOT/.claude/agent-control"
echo 0 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"
```

Ask GitHub whether the PR can merge:

```bash
gh pr view <PR_NUMBER> --json mergeable,mergeStateStatus,baseRefName,headRefName
```

Act on `mergeable` first, then, when it is clean, on `mergeStateStatus`:

- **`mergeable: CONFLICTING`**: a PR merged into the base branch since this one
  was last pushed and the two diverge on the same lines. Run the update
  subroutine below; expect it to hit real conflicts.

- **`mergeable: UNKNOWN`**: GitHub is still computing mergeability. Re-query up
  to 5 times, 5 seconds apart. If it is still `UNKNOWN`, treat it as
  `MERGEABLE` and let the merge attempt in Phase 5 surface the real answer. Do
  not rebase speculatively.

- **`mergeable: MERGEABLE` and `mergeStateStatus: BEHIND`**: no conflicting
  lines, but the branch has not incorporated everything merged into the base
  branch since it was last pushed. This repository's `main-protection` ruleset
  sets `strict_required_status_checks_policy: true`, so GitHub will refuse to
  merge until the branch is updated, regardless of how green its existing
  checks are. Run the update subroutine below; expect it to complete cleanly
  with no conflicts, since `mergeable` already ruled those out.

  This is a real, unavoidable CI cost, not a routine-hygiene push: skipping it
  would leave the branch permanently unmergeable under this ruleset. Confirm
  the policy before assuming it applies in another project:
  ```bash
  gh api repos/<OWNER>/<REPO>/rulesets --jq '.[] | select(.target == "branch") | .id'
  gh api repos/<OWNER>/<REPO>/rulesets/<ID> \
    --jq '.rules[] | select(.type == "required_status_checks")
          | .parameters.strict_required_status_checks_policy'
  ```
  If that returns `false`, a `BEHIND` state is not a merge blocker there and
  this step may be skipped, matching the `MERGEABLE`/not-`BEHIND` case below.

- **`mergeable: MERGEABLE` and anything else** (typically `CLEAN`): nothing to
  do. Do NOT rebase, do NOT push. Go to Phase 3.

#### Update subroutine

Handles both a real conflict (`mergeable: CONFLICTING`) and a clean-but-stale
branch (`mergeStateStatus: BEHIND`). The rebase step is identical either way;
only step 3 (conflict resolution) has anything to do when there are no
conflicts to resolve.

1. **Get onto the PR branch safely.** Never run `git checkout <HEAD_BRANCH>` in
   the primary checkout: if a worktree already holds the branch, the checkout
   fails or, worse, leaves uncommitted work that bleeds onto the base branch
   later. Resolve the worktree first:
   ```bash
   git fetch origin
   git worktree prune
   HEAD_BRANCH_REF="branch refs/heads/<HEAD_BRANCH>"
   WORKTREE_PATH=$(git worktree list --porcelain | grep -F -B2 "$HEAD_BRANCH_REF" \
     | grep "^worktree " | head -1 | sed 's/^worktree //')
   ```
   If `WORKTREE_PATH` is non-empty, `cd` into it and verify
   `git branch --show-current` equals `<HEAD_BRANCH>`. If it is empty and the
   current branch already matches `<HEAD_BRANCH>`, you are in the right place.
   If neither holds, STOP and report: there is no safe place to rebase.

2. **Rebase**. Save the pre-rebase commit first: once the rebase completes,
   `git rebase --abort` reports no rebase is in progress and cannot undo it,
   so a validation failure discovered afterward (step 6) needs this to restore
   the branch.
   ```bash
   ORIGINAL_HEAD=$(git rev-parse HEAD)
   git rebase origin/<BASE_BRANCH>
   ```

3. **Resolve only conflicts you fully understand.** Bounded: at most 5
   conflicting commits, at most 3 attempts each.

   Safe to resolve autonomously:
   - both sides added entries to the same list, import block, or registry
   - both sides edited the same file in non-overlapping places
   - the base branch renamed or moved something this PR also touches, where the
     intent of each side is unambiguous
   - a lockfile or generated file that can be regenerated

   STOP and hand off instead of guessing:
   - both sides changed the same function's behaviour or signature
   - the conflict spans a semantic decision (which of two implementations wins)
   - resolving would silently drop a change from either side

4. After each resolution, `git add <file>` and `git rebase --continue`, then
   verify nothing is left behind:
   ```bash
   grep -rn "<<<<<<<\|>>>>>>>" <resolved_files>
   ```
   This must produce no output. `git add` clears a file's unmerged index entry
   whether or not the markers were removed, so the index alone does not prove
   the conflict was resolved.

5. When all commits are replayed, run the project's linter and tests (see
   CLAUDE.md). The rebase pulled in new base-branch code, so a green result
   from before the rebase means nothing.

6. **On any failure**:
   - **Unresolvable conflict or attempt budget exhausted** (rebase still in
     progress):
     ```bash
     git rebase --abort
     ```
   - **Tests failing after the rebase completed** (step 5, no rebase in
     progress: `git rebase --abort` here reports nothing to abort and leaves
     the rebased commits in place):
     ```bash
     git reset --hard "$ORIGINAL_HEAD"
     ```
   Stop and report the conflicting files, pointing the user at
   `/address-pr <PR_NUMBER>`. Never leave the worktree mid-rebase.

7. **Before pushing, fold in any stale documentation for free.** This push is
   already required (a real conflict or a stale branch), so capturing
   documentation now costs nothing extra: run the Phase 3 check (steps 1-2
   below) right here, before this push goes out. Doing it after the push, as a
   separate step, is too late: the push has already happened by the time a
   later phase runs, so amending afterward only rewrites the local commit and
   silently never reaches the remote PR head that Phase 5 actually merges.

   If the check finds nothing stale, skip straight to the push. If it finds
   something stale, stage it now (do not commit or push it separately):
   ```bash
   git add -A .claude/agent-memory/ AGENTS.md README.md docs/ .claude/rules/
   ```

   Then amend anything staged into this commit and push once:
   ```bash
   if ! git diff --cached --quiet; then
     git commit --amend --no-edit
   fi
   git push --force-with-lease && echo 1 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"
   ```
   **Stop if the push fails** (branch protection, permissions) and report.

   **Phase 3 is then a no-op**: its state-file check below sees `1` and skips
   entirely, since the check already ran here, before this push.

8. **Return to the primary checkout** before continuing, whether or not step 1
   `cd`'d into a worktree. Every later phase (post-merge cleanup checks out the
   base branch) assumes it is running from the primary checkout, and normally
   is; the Update subroutine is the one place in this command that changes
   that:
   ```bash
   cd "$PRIMARY_ROOT"
   ```

### Phase 3: Documentation and Knowledge (verify; do not push on its own)

Knowledge capture and documentation freshness belong to `/address-pr` and
`/review`, which run while the branch is already being pushed. This phase only
verifies that they did their job. It must never create a head SHA of its own:
a documentation amend on an otherwise-ready PR buys nothing and costs a full
CI cycle plus the wait before the merge.

**Skip this phase entirely if Phase 2 already pushed** (the state file reads
`1`): the Update subroutine's step 7 already ran this exact check, before that
push, and folded in anything it found. Running it again here would find
nothing new (the branch already carries the fix) and risks a confusing second
amend after the push has already gone out. This is a separate command
invocation from Phase 2, so resolve the primary checkout again rather than
assuming it carried over, then read the state file from there:
```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in
  /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;;   # linked worktree
  *)  PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;;  # already primary
esac
PUSHED=$(cat "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed" 2>/dev/null || echo 0)
```

Only run the rest of this phase if `.claude/agent-memory/` exists in the
project AND `PUSHED` is `0`.

1. **Check for uncaptured review learnings**: fetch review data from the PR
   (`gh pr view`, `gh api repos/.../pulls/<N>/comments`,
   `gh api repos/.../pulls/<N>/reviews`). Enumerate every review thread from
   the whole PR history, resolved and unresolved, and check whether the
   actionable ones are already reflected in `.claude/agent-memory/cookbook.md`.

2. **Check documentation counts**: run the verification commands (test count,
   service count, router count) and compare against `AGENTS.md`, `README.md`,
   and `docs/VISION.md`.

3. **Act on the result**:

   - **Nothing stale**: continue to Phase 4. This is the expected outcome when
     `/address-pr` ran properly.

   - **Stale**: Phase 2 did not push, so there is no push to ride for free and
     amending here would be a pure-waste push on an otherwise-ready PR. Do NOT
     amend and do NOT push. Queue the content for the next branch instead, in
     the PRIMARY checkout so it survives this PR's worktree cleanup, using the
     `$PRIMARY_ROOT` already resolved above:
     ```bash
     mkdir -p "$PRIMARY_ROOT/.claude/agent-control"
     ```
     Append the following block to
     `$PRIMARY_ROOT/.claude/agent-control/pending-docs.md` using a file-editing
     tool, not a shell heredoc: the queued content is agent-generated and may
     itself contain a line that reads exactly `EOF`, which would close a
     heredoc early and let the remainder of the block execute as shell input.
     ```
     ## From PR #<PR_NUMBER> (<DATE>)
     <the cookbook entries, rule promotions and count corrections, verbatim>
     ```
     `/address-pr` drains this queue at the start of its next run, resolving
     the primary checkout the same way, and folds the content into that
     branch's own commits, which are pushed anyway. Report the queued items in
     Phase 7.

   - **Override**: if `$ARGUMENTS` contains `--with-docs`, the user has
     explicitly accepted the extra CI cycle. Amend and push directly:
     ```bash
     git add -A .claude/agent-memory/ AGENTS.md README.md docs/ .claude/rules/
     if ! git diff --cached --quiet; then
       git commit --amend --no-edit
       git push --force-with-lease && echo 1 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"
     fi
     ```

### Phase 4: Wait for CI

**Fast path (skip the wait entirely).** This is the expected path. Nothing was
pushed unless Phase 2 had to resolve a conflict, so the PR head SHA is unchanged
and any CI that already ran is still valid. When the push-tracking state file
still reads `0`, confirm the existing checks are green and proceed straight to
Phase 5 without polling:

```bash
# A separate command invocation from Phases 2 and 3: resolve the primary
# checkout again rather than assuming it carried over.
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in
  /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;;   # linked worktree
  *)  PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;;  # already primary
esac
# Default to 0 if the state file is missing (defensive: never assume a push).
PUSHED=$(cat "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed" 2>/dev/null || echo 0)
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

**Run this loop as a single synchronous foreground command and wait for it to
finish.** Do NOT launch it as a background task and then stop to "wait" for a
notification: headless mode has no mechanism to resume you when a
background task completes, and ending your turn without a tool call
terminates the run immediately, leaving the PR unmerged with no further
action taken. The loop's worst case (below) exceeds the Bash tool's 2 minute
default timeout, so pass an explicit `timeout` of at least 540000 (9 minutes,
under the tool's 600000ms/10 minute cap) on this call. Block on this command
until it prints one of the terminal outcomes below, then follow the "Act on
the result" rules further down to decide the next step.

```bash
# Poll CI checks in a loop (16 iterations x 30s = 8 minutes max, safely under
# the Bash tool's 600000ms/10 minute timeout cap)
# Uses `bucket` (not `state`) -- bucket normalizes raw states into: pass, fail, pending, skipping, cancel
# Grace period: first 5 iterations (2.5 min) tolerate TOTAL=0 for checks to register after push
for i in $(seq 1 16); do
  echo "--- CI poll attempt $i/16 ---"
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
  if [ "$i" -eq 16 ]; then
    echo "CI TIMEOUT: checks still pending after 8 minutes"
    break
  fi
  sleep 30
done
```

Act on the result:
- **CI PASSED**: also verify that no blocking `CHANGES_REQUESTED` review remains (`gh pr view <PR_NUMBER> --json reviewDecision`; `gh pr checks` monitors CI status only, not review decisions).
  - If `reviewDecision` is `CHANGES_REQUESTED`: stop. Never merge while a review (bot or human, including CodeRabbit) is requesting changes, treat it as a real reviewer with its own address-review cycle. Write a handoff pointing at `/address-pr <PR_NUMBER>` and report that integration is blocked on unaddressed review feedback.
  - Otherwise, proceed to Phase 5.
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

Return to the primary checkout first: the Update subroutine in Phase 2 may
have `cd`'d into a per-issue worktree, and `git checkout <BASE_BRANCH>` below
fails there (the base branch is normally already checked out in the primary
checkout, and git refuses to check out a branch that is checked out
elsewhere), stopping this phase before any cleanup runs.

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in
  /*) cd "${COMMON_DIR%/.git}" ;;   # linked worktree: return to primary checkout
  *)  ;;                            # already primary
esac

git checkout <BASE_BRANCH>
git pull origin <BASE_BRANCH>

# Delete local branch if it still exists
git branch -d <HEAD_BRANCH> 2>/dev/null || true

# Delete remote branch if delete_branch = true (fallback -- Phase 5 may have
# already handled this via --delete-branch, but covers merge-queue path and
# already-merged PRs where Phase 5 is skipped). Skip for a fork PR: HEAD_BRANCH
# names a branch, not a repository, and `origin` is the base repository, so
# deleting <HEAD_BRANCH> there could remove an unrelated branch that happens
# to share the fork contributor's branch name.
IS_FORK=$(gh pr view <PR_NUMBER> --json isCrossRepository --jq '.isCrossRepository')
if [ "$IS_FORK" != "true" ]; then
  git push origin --delete <HEAD_BRANCH> 2>/dev/null || true
fi
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

Run the full issue-aware GC to clean up any remaining stale worktrees and branches across the project:

```bash
sova cleanup --all --project <PROJECT_DIR>
```

`sova cleanup --all` only removes a worktree once its own issue is confirmed closed on GitHub, no agent is actively using it, and its working tree is clean, so it will not touch other issues' in-progress work.

### Phase 7: Report

Note: this command makes no post-merge commits. Documentation and knowledge are
captured by `/address-pr` on the branch; anything Phase 3 found missing was
queued for the next branch rather than pushed.

Output a concise summary covering:

- PR number, title, and base branch it was merged into
- Whether a rebase was needed, and if so which conflicts were resolved
- Whether anything was pushed, and why (state the reason explicitly: a push
  means a CI cycle was spent, so it must be accounted for)
- CI status (passed, retried, or skipped because nothing was re-pushed)
- Branches cleaned up (local + remote)
- Issue closed (or no linked issue)
- Documentation and knowledge: "already captured" or the items queued to
  `.claude/agent-control/pending-docs.md` for the next branch
- Stale stashes found (if any)

## Error Recovery

When the pipeline stops at any phase, report clearly:

- Which phase failed
- The specific error
- What to do next (resolve conflicts, fix CI, retry, etc.)

The user can fix the issue and re-run `/integrate-pr <PR_NUMBER>` to resume. The command is idempotent -- it detects the current state and picks up from where it left off (e.g., if already rebased, it skips to CI; if already merged, it skips to cleanup).

## Cross-References

- **Replaces**: `/after-merge` (cleanup), built into Phase 6
- **Knowledge capture happens upstream**: `/address-pr` folds review learnings and documentation into the branch's own commits. This command only verifies and, if something was missed, queues it for the next branch.
- **Before this**: `/review-full` or `/address-pr` to prepare the PR
- **Next**: `/find-task` or `/standup` to pick up the next task

## Rules

- Never stop between phases unless there is a hard failure
- **Never push unless GitHub refuses to merge the PR as it stands.** A push
  creates a new head SHA and spends a full CI cycle. The only justification is
  `mergeable: CONFLICTING` (or a `BEHIND` state under a strict status-check
  policy). Documentation, knowledge, and doc-count drift are never a reason to
  push from this command: queue them for the next branch instead.
- Never background the CI-poll loop and stop to wait for a notification (see the Phase 4 note above for why and how to pass an extended timeout instead)
- Use the merge method from `[integration]` config (default: auto, uses GitHub repo default)
- Handle merge queue when detected or configured
- Use `--force-with-lease` for pushes, never `--force`
- Only record actionable, specific lessons in memory -- not generic advice
- Do not duplicate existing memory entries
- NEVER use emojis in any output
