---
name: sova-address-pr
description: "Address PR review comments (human and bot): score, fix or acknowledge, reply, resolve threads. Provide PR number."
---

# Address PR Review Comments

Score each review comment, address all of them (fix or acknowledge with justification), and reply on GitHub. Handles both human reviewers and automated review bots (Sourcery, CodeRabbit, etc.).

## CRITICAL: Complete ALL Steps

This command runs as a headless agent. You MUST execute every step below through to completion. In headless mode, producing a text-only summary without a tool call causes the process to exit immediately, so NEVER output a final summary without having completed steps 8-17 first (capture knowledge, squash, rebase, push, reply, resolve, dismiss). If you discover that all findings are already addressed, you MUST still complete steps 8-11 (capture knowledge, squash, rebase and push) to fold in pending documentation and resolve any merge conflicts, then skip to step 12 to handle thread resolution before the summary.

**Incomplete execution is worse than failure**: a run that fixes code but never commits/pushes wastes the cost and leaves the PR unchanged.

## Instructions

1. Get the PR number from `the arguments provided when this skill is invoked`.

2. Fetch review data and the PR's branch names:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title,headRefName,baseRefName
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews
   BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq .baseRefName)
   HEAD_BRANCH=$(gh pr view <PR_NUMBER> --json headRefName --jq .headRefName)
   echo "Base branch: $BASE, head branch: $HEAD_BRANCH"
   ```
   Every shell block runs in a fresh shell, so variables do not carry over between blocks: each later block that uses `BASE` or `HEAD_BRANCH` re-assigns them first with the same two `gh pr view` lines (or the literal values printed here).

3. **Verify you are in the correct working directory**: this is the most critical safety check. Working in the wrong directory means modifying the wrong branch and leaving garbage uncommitted changes on main.

   a. Check the current branch:
      ```bash
      git branch --show-current
      ```

   b. If the output matches `headRefName` from step 2, you are in the right place. Continue to 3e (the sync and account checks below apply on every path, including this one, so do not skip ahead to step 4).

   c. If they differ, find the worktree for the PR branch:
      ```bash
      HEAD_BRANCH=$(gh pr view <PR_NUMBER> --json headRefName --jq .headRefName)
      git worktree prune
      HEAD_BRANCH_REF="branch refs/heads/${HEAD_BRANCH}"
      WORKTREE_PATH=$(git worktree list --porcelain | grep -Fx -B2 "$HEAD_BRANCH_REF" | grep "^worktree " | head -1 | sed 's/^worktree //')
      echo "Worktree: $WORKTREE_PATH"
      ```
      If `WORKTREE_PATH` is non-empty, navigate into it, verify, and then continue to 3e:
      ```bash
      HEAD_BRANCH=$(gh pr view <PR_NUMBER> --json headRefName --jq .headRefName)
      cd "<WORKTREE_PATH printed above>"
      VERIFIED=$(git branch --show-current)
      echo "Verified branch: $VERIFIED"
      [ "$VERIFIED" = "$HEAD_BRANCH" ] || { echo "MISMATCH: worktree is on $VERIFIED, expected $HEAD_BRANCH" >&2; exit 1; }
      ```
      If `VERIFIED` does not equal `HEAD_BRANCH`, STOP and report: the worktree exists but is on the wrong branch. If your shell does not keep its working directory between calls, start every later shell block with the same `cd`.

   d. **STOP if the worktree cannot be found**: if `WORKTREE_PATH` is empty and the current branch is not `headRefName`, report the mismatch and exit. Do NOT use `git checkout <HEAD_BRANCH>` to fix this: doing so switches the main repo's working tree to the PR branch, causing any uncommitted changes to bleed onto main after cleanup.

   Never resolve the worktree by issue number alone: always use the branch name from the PR metadata.

   e. **Sync to the PR head before editing anything a review cites.** A local branch behind its remote means fixing code the reviewer never saw, and the later force-push would discard the remote commits:
      ```bash
      HEAD_BRANCH=$(gh pr view <PR_NUMBER> --json headRefName --jq .headRefName)
      git fetch origin "$HEAD_BRANCH" || { echo "Fetch failed: STOP" >&2; exit 1; }
      BEHIND=$(git rev-list --count "HEAD..origin/$HEAD_BRANCH")
      AHEAD=$(git rev-list --count "origin/$HEAD_BRANCH..HEAD")
      echo "Behind: $BEHIND, ahead: $AHEAD"
      ```
      Behind and not ahead: `git merge --ff-only "origin/$HEAD_BRANCH"`. Both behind and ahead: STOP and report the divergence rather than guessing which side wins. If work you expected seems missing, check `git stash list` and `git reflog` before concluding it was lost.

   f. **Verify the GitHub account** before any thread or review write: `gh auth status` must show the account the project configures as its GitHub user (for SOVA projects, `sova config get github_user` prints it). Resolving threads or dismissing reviews as the wrong account fails with FORBIDDEN after the code is already pushed. If it differs, run `gh auth switch --user <github_user>`.

4. **Classify comment sources**: Separate human reviewer comments from bot comments (e.g., `sourcery-ai[bot]`, `coderabbitai[bot]`). Both get scored the same way, but bot comments are addressed in bulk and may warrant a re-review request.

5. **Score each comment 1-10**:
   - 1-2: low-priority: style preference, minor naming, formatting
   - 3-5: moderate: DRY violations, missing edge cases, error handling
   - 6-8: important: potential bugs, missing validation, test gaps
   - 9-10: critical: security, data loss, correctness bugs

   Scoring determines priority order, not whether to fix. All findings are addressed.

   For bot suggestions: evaluate critically and don't blindly accept. Bots lack project context and may suggest changes that conflict with project conventions.

6. **Address all findings** (regardless of score): Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.
   - If a finding is a false positive, not applicable in context, or requires a human decision: acknowledge it with a one-line justification instead of fixing. Do not skip findings based on score alone.

7. **Scout check**: while fixing findings, scan each touched file for pre-existing issues (failing tests, lint warnings, dead imports, obvious bugs adjacent to your changes). Fix them alongside the review findings. Keep scout fixes small and low-risk.

8. **Capture knowledge and documentation updates NOW, before the squash** (MANDATORY):

   This is the only place in the PR lifecycle where documentation and knowledge
   can be written for free. The branch is about to be squashed and pushed
   anyway, so tracked documentation folded in here rides that push (the agent
   memory in step b may be local-only and then never does). Anything left until the
   `sova-integrate-pr` skill would need a push of its own on an otherwise-ready
   PR, which costs a full CI cycle and delays the merge by the length of the
   suite.

   a. **Drain the pending queue**: `.claude/agent-control/pending-docs.md` in
      the PRIMARY checkout, if it exists, holds documentation and knowledge
      that a previous run of the `sova-integrate-pr` skill found missing and
      deferred rather than pushing. This step usually runs inside a per-issue
      worktree (per step 3 above), not the main checkout, and `.claude/agent-control/` is
      not mirrored into worktrees, so resolve the primary checkout explicitly
      rather than checking a bare relative path:
      ```bash
      COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
      case "$COMMON_DIR" in
        /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;;   # linked worktree
        *)  PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;;  # already primary
      esac
      QUEUE="$PRIMARY_ROOT/.claude/agent-control/pending-docs.md"
      ```
      If `$QUEUE` exists, apply each entry to its destination file, then clear
      the queue:
      ```bash
      rm -f "$QUEUE"
      ```

   b. **Record this round's learnings**: append actionable lessons from the
      review findings to the cookbook in the PRIMARY checkout, never the
      worktree's copy: `.claude/agent-memory/` may be gitignored, and a
      worktree's copy is deleted with the worktree. The cookbook is
      `$PRIMARY_ROOT/.claude/agent-memory/cookbook/<domain>.md` (domains listed
      in that directory's `README.md`) when that directory exists, otherwise
      `$PRIMARY_ROOT/.claude/agent-memory/cookbook.md` under the matching
      section. Enumerate every review thread from the whole PR history,
      resolved and unresolved, not only what came up in this round. Skip
      duplicates and generic advice. Promote a pattern confirmed in 2 or more
      PRs to `.claude/rules/*.md` (tracked, so it rides the push).

   c. **Fix documentation drift**: run the project's verification commands
      (test count, service count, router count) and correct any drifted values
      in `AGENTS.md`, `README.md`, and the project's vision or roadmap doc, if
      `AGENTS.md` or `README.md` links one. Update any docs the code changes in
      this PR made stale.

   These edits are part of the change, not a separate concern: fold them into
   the relevant commit in the next step. Never create a standalone docs commit.

9. **Squash fixes into original commits** (MANDATORY, no fix-on-fix commits):

   The PR must read as clean feature development. Do NOT create a separate "address review findings" commit: fold each fix into the commit that introduced the code being fixed. The final history should look as if the code was written correctly from the start.

   **Before starting**: run `git fetch origin || { echo "Fetch failed: STOP" >&2; exit 1; }`, then verify `git branch --show-current` equals `headRefName`. If not, STOP: never reset commits while on the wrong branch.

   a. Run the project's linter and tests (see CLAUDE.md for commands). All must pass before proceeding. If the project has pre-push invariant scripts (`invariants/*.sh`), run them too, re-assigning `BASE` first as in step 2 (`for s in invariants/*.sh; do bash "$s" . "$BASE" || echo "FAIL: $s"; done`) and fix every failure: a commit-format or prose invariant that fails only at push time costs a full squash cycle. Never bypass a failing hook with `--no-verify`.

   b. Record the fork point and the original commits for replay, before anything else touches history. The reset below targets the fork point (`git merge-base`), never `origin/$BASE` itself: if the base branch moved since this branch was last rebased, re-committing the branch's tree on top of the newer base would silently revert everything the base gained in between. Step 10 rebases afterwards.
      ```bash
      BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq .baseRefName)
      FORK_POINT=$(git merge-base HEAD "origin/$BASE")
      COMMITS_FILE=$(mktemp)
      git log "$FORK_POINT"..HEAD --reverse --format="%H %s" > "$COMMITS_FILE"
      echo "Fork point: $FORK_POINT"
      echo "Original commits saved to $COMMITS_FILE"
      cat "$COMMITS_FILE"
      ```

   c. Snapshot everything, review fixes included, into a uniquely named backup branch. Note the printed values: later blocks re-assign `FORK_POINT` and `BACKUP` to them.
      ```bash
      if [ -n "$(git status --porcelain)" ]; then
        git add -A && git commit -m "chore: WIP review fixes before squash" \
          || { echo "WIP commit failed (hook, signing or staging error): STOP, nothing was reset" >&2; exit 1; }
      fi
      # Fail closed: the backup must hold the review fixes, so the tree must be fully committed first.
      [ -z "$(git status --porcelain)" ] || { echo "Uncommitted changes remain: STOP before creating the backup" >&2; exit 1; }
      BACKUP="backup/pr-<PR_NUMBER>-$(date +%s)-$$-$RANDOM"
      git branch "$BACKUP" || { echo "Failed to create backup branch $BACKUP: STOP before resetting" >&2; exit 1; }
      echo "Backup branch: $BACKUP"
      ```

   d. Soft-reset to the fork point. All changes, original work and review fixes alike, are now unstaged in the working tree:
      ```bash
      FORK_POINT=<fork point printed in step b>
      git reset --soft "$FORK_POINT"
      git reset HEAD .
      ```

   e. Re-commit in the original logical groups. For each original commit (from the list in step b):
      - Stage the files that belong to that logical unit
      - When a file (e.g., a test file) has changes belonging to multiple commits, save the final version aside, apply the intermediate version for earlier commits, then restore the final version for the last commit that touches it
      - Reuse the original commit message (preserving type, scope, and description): `git commit -C <original SHA>` copies it verbatim
      - Fold review fixes into whichever commit introduced the code being fixed

      **Single-commit shortcut**: if the branch had only one commit, stage everything and run `git commit -C <its SHA>`.

   f. Verify nothing was lost or left behind:
      ```bash
      BACKUP=<backup branch printed in step c>
      git status --porcelain
      git diff "$BACKUP"
      ```
      Both commands MUST print nothing. If either does, something was lost or left uncommitted: restore the pre-squash state (this keeps the review fixes, which are in the backup's WIP commit), then start again from step d:
      ```bash
      BACKUP=<backup branch printed in step c>
      git reset --hard "$BACKUP"
      ```

   g. Keep the backup branch until the push in step 11 succeeds; it is deleted there, not here.

10. **Rebase onto base branch** to ensure the PR is mergeable after fixes:
    ```bash
    BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq .baseRefName)
    git fetch origin || { echo "Fetch failed: STOP, origin/$BASE may be stale" >&2; exit 1; }
    git rebase "origin/$BASE"
    ```

    If there are merge conflicts:
    1. Read each conflicted file, understand both sides of each conflict marker
    2. Resolve conflicts by editing the file to remove markers, keeping the correct code
    3. `git add <resolved_file>` and `git rebase --continue`
    4. Repeat for subsequent commits (up to 3 rebase steps)
    5. After each resolution, verify no conflict markers remain: `grep -rn "<<<<<<<" <file>`

    If resolution fails after 3 attempts:
    - Run `git rebase --abort` to restore a clean state
    - Report the conflicting files and stop

    If rebase was a no-op (already up to date), continue to the next step.

    After a rebase that moved the branch, confirm it did not silently revert the base: `git diff "origin/$BASE"...HEAD --stat` must list only this PR's files, and `bash invariants/silent-revert-guard.sh . "$BASE"` must pass when the project has it. On a failure, restore from the step 9c backup (`git reset --hard "$BACKUP"`) and redo the rebase.

11. **Push and wait for CI** (MANDATORY, do not skip):
   ```bash
   BACKUP=<backup branch printed in step 9c>
   git push --force-with-lease && git branch -D "$BACKUP"
   ```
   The backup branch is deleted only once the push has succeeded. If the push fails, keep it and report its name.

   After pushing, poll CI checks on the PR until ALL required checks complete (max 30 minutes, poll every 30s; a full test suite routinely runs longer than 15 minutes):
   ```bash
   gh pr checks <PR_NUMBER> --json name,bucket --jq '.[] | "\(.bucket)\t\(.name)"'
   ```
   Wait until every check shows `pass`, `fail`, or `skipping`, with no `pending` remaining.

   **Run each poll loop as a synchronous foreground command**, never as a background task: in headless mode nothing resumes you when a background task finishes, and ending a turn without a tool call ends the run. The Bash tool caps a single call at 10 minutes (600000 ms) and defaults to 2, so pass an explicit `timeout` of at least 540000 ms on every poll call, and keep each call to at most 16 polls of 30 seconds (8 minutes). When a call ends with checks still pending, start another call the same way, until every check completes or the 30 minute total is used up.

   **If checks are still pending when the 30 minutes are used up**, that is not a failure: continue to step 12. The fixes are pushed, so the review threads and the address summary must still be written; stopping here would leave every fixed thread open on the PR. Record the head SHA and the names of the checks still pending, state in the step 14 summary that CI was still running on that commit, and report it in step 17. The `sova-integrate-pr` skill re-checks CI before merging, so an unfinished run is never merged unverified.

   **If any check fails**: fetch the failure logs, analyze, fix the code, re-push, and re-poll (max 2 retries):
   ```bash
   gh run view <RUN_ID> --log-failed
   ```
   Common CI failures: commit-format invariant (wrong scope/type), test timeouts, lint errors, SonarCloud coverage.

   **Only a failing check blocks step 12.** Continue when all required checks pass, or when the 30 minute wait ran out with checks still pending (see above). If checks are still failing after 2 retries, report the specific failures and stop. Do not proceed to reply/resolve steps with a red CI.

12. **Fetch all unresolved review threads** using GraphQL (`--paginate` follows `endCursor`, so a PR with more than 100 threads is not silently truncated):
    ```bash
    gh api graphql --paginate -f owner='<OWNER>' -f repo='<REPO>' -F pr=<PR_NUMBER> -f query='
      query($owner: String!, $repo: String!, $pr: Int!, $endCursor: String) {
        repository(owner: $owner, name: $repo) {
          pullRequest(number: $pr) {
            reviewThreads(first: 100, after: $endCursor) {
              pageInfo { hasNextPage endCursor }
              nodes {
                id
                isResolved
                isOutdated
                path
                line
                originalLine
                comments(first: 1) { totalCount nodes { body author { login __typename } } }
              }
            }
          }
        }
      }'
    ```
    Keep every thread with `isResolved: false`, **including outdated ones**. GitHub marks a thread outdated as soon as a push changes the lines it is anchored to, which is exactly what fixing it does, so after step 11's push most of the threads you just fixed are outdated; dropping them leaves them open on the PR. Match each thread to a finding by `path` plus `line`, falling back to `originalLine` when `line` is null (it always is on an outdated thread), and by the comment body when two threads share a location. A reviewer can post the same finding as more than one thread: every duplicate still needs its own reply and resolve in step 13, and the same reply text is fine. Also keep every thread with `isResolved: true` and `comments.totalCount` of 1 that matches one of this PR's findings: someone resolved it without a reply, so step 13 still posts the reply (it needs no re-resolve). Use `author.__typename` to classify thread authors: `Bot` for automated reviewers, `User` for humans. If `__typename` is absent, fall back to checking whether `login` ends with `[bot]`.

13. **Reply to each thread, then resolve all of them** (MANDATORY: this is what unblocks the PR):

    For each unresolved thread, post an inline reply explaining the disposition, then resolve it, regardless of author (bot or human) or disposition (Fixed or Acknowledged). Replying is always required; skipping replies leaves reviewers unclear on what was done. The PR must end with zero unresolved conversations so it reads as ready to merge.

    a. **Reply** with a short, direct explanation:
       ```bash
       gh api graphql \
         -f query='mutation($threadId: ID!, $body: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) { comment { id } } }' \
         -f threadId="THREAD_ID" \
         -f body="Fixed in <SHA>: <what changed>."
       ```
       For acknowledged findings: `"Acknowledged: <justification>."`

    b. **Resolve** the thread:
       ```bash
       gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
       ```

    Reply to every thread, then resolve every thread: bot and human reviewers alike, Fixed and Acknowledged findings alike. A PR must show zero unresolved conversations before merge; leaving "Acknowledged" human threads open only creates unnecessary back-and-forth for the reviewer.

14. **Post the address summary as a COMMENT review** on the PR with all dispositions in one table. The first line MUST be the `sova-addressed` marker carrying the pushed head commit: the dashboard reads it from the PR's review list to learn that the standing SOVA verdict has been addressed. A plain `gh pr comment` is invisible to that check and leaves the PR stuck at "SOVA Changes Requested".

    ```bash
    HEAD_SHA=$(git rev-parse HEAD)
    # Round number is computed, never guessed: one more than the address
    # summaries already posted on this PR (a fourth round once got labelled
    # "Round 1" when the count was left to judgement). --paginate is required:
    # per_page=100 alone still caps the count at 100 reviews on a long-lived PR.
    # The marker list is captured into a variable first so a failed `gh api`
    # call is caught explicitly instead of silently flowing through the pipe
    # as zero matches. `grep -c .` (not `wc -l`) counts non-blank lines, which
    # stays correct even though command substitution strips the trailing
    # newline `jq` puts after the last match.
    if ! MARKERS=$(gh api --paginate "repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews?per_page=100" \
      --jq '.[] | select((.body // "") | startswith("<!-- sova-addressed")) | .id'); then
      echo "Unable to count prior address reviews; aborting" >&2
      exit 1
    fi
    MARKER_COUNT=0
    [ -n "$MARKERS" ] && MARKER_COUNT=$(printf '%s\n' "$MARKERS" | grep -c .)
    ROUND=$((MARKER_COUNT + 1))
    gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews \
      -f event=COMMENT \
      -f body="$(printf '<!-- sova-addressed: sha=%s -->\n\n## Address Review: Round %s\n\n' "$HEAD_SHA" "$ROUND"; cat <<'EOF'
    | # | Finding | Action |
    |---|---------|--------|
    | 1 | `file:line`: short description | Fixed: what changed. |
    | 2 | `file:line`: short description | Acknowledged: justification. |
    EOF
    )"
    ```

    Keep the Action column SHORT and DIRECT. No filler words, no emojis.

15. **Dismiss bot CHANGES_REQUESTED reviews** (mandatory when any bot review is in CHANGES_REQUESTED state after addressing findings):

    Fetch bot reviews that are still CHANGES_REQUESTED:
    ```bash
    gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews \
      --jq '.[] | select(.state == "CHANGES_REQUESTED") | select(.user.type == "Bot") | {id: .id, user: .user.login}'
    ```
    For each such review:
    ```bash
    gh api -X PUT repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews/<REVIEW_ID>/dismissals \
      -f message="Findings addressed or acknowledged. See the Address Review summary."
    ```

    **Never dismiss human reviews**: only bot reviews (`user.type == "Bot"`) whose findings have been addressed.

16. **Request bot re-review** (only if bot comments were addressed AND the cycle cap allows it). Every re-review trigger produces a fresh round of bot findings and a new address run, so an uncapped loop burns tokens on both sides. The cap is the same `pipeline.max_address_review_cycles` that bounds the autonomous address-review pipeline (default 3; 0 means unlimited). Read it from the primary checkout's SOVA database (this command usually runs inside a worktree).

    This block is self-contained: each bash invocation is a fresh shell, so it
    recomputes the round rather than reusing step 14's variable. Step 14 has
    already posted this round's summary, so the marker count now equals the
    round just completed. Both values are forced to digits before any numeric
    comparison, since the setting is stored as JSON (a string value would
    arrive quoted) and `sqlite3` may not be installed at all. Only the JSON
    quotes and whitespace are stripped: deleting every non-digit would turn a
    malformed value such as `3.5` into `35` instead of falling back to the
    default, so anything that is not a plain number falls back to 3.

    ```bash
    SOVA_ROOT=$(dirname "$(git rev-parse --git-common-dir)")
    RAW=$(sqlite3 "$SOVA_ROOT/.claude/sova.db" \
      "SELECT value FROM project_settings WHERE key='pipeline.max_address_review_cycles';" 2>/dev/null || true)
    RAW=$(printf '%s' "$RAW" | tr -d '"[:space:]')
    case "$RAW" in
      [0-9]|[1-9][0-9]) MAX_CYCLES="$RAW" ;;
      *) MAX_CYCLES=3 ;;
    esac
    # The marker list is captured first so a failed `gh api` call is caught
    # explicitly rather than silently counting as zero matches, which would
    # under-report ROUND and let the cap be bypassed. `grep -c .` counts
    # non-blank lines correctly even after command substitution strips the
    # trailing newline `jq` puts after the last match.
    COUNT_FAILED=0
    if MARKERS=$(gh api --paginate "repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews?per_page=100" \
      --jq '.[] | select((.body // "") | startswith("<!-- sova-addressed")) | .id'); then
      ROUND=0
      [ -n "$MARKERS" ] && ROUND=$(printf '%s\n' "$MARKERS" | grep -c .)
    else
      COUNT_FAILED=1
    fi
    if [ "$COUNT_FAILED" = "1" ]; then
      # Fail closed: without a trustworthy count, requesting a re-review
      # risks silently exceeding the cap, so skip it this round instead.
      echo "Bot re-review skipped: unable to verify the address cycle count" >&2
    elif [ "$MAX_CYCLES" -eq 0 ] || [ "$ROUND" -lt "$MAX_CYCLES" ]; then
      # Gate each mention independently: only ping a bot whose own findings
      # were actually addressed this round. Requesting a review from a bot
      # that posted nothing just spends one of its hourly review quota slots
      # for no reason.
      if [ "$SOURCERY_ADDRESSED" = "1" ]; then
        gh pr comment <PR_NUMBER> --body "@sourcery-ai review"
      fi
      if [ "$CODERABBIT_ADDRESSED" = "1" ]; then
        gh pr comment <PR_NUMBER> --body "@coderabbitai review"
      fi
    else
      echo "Bot re-review skipped: address cycle cap reached ($ROUND of $MAX_CYCLES)"
    fi
    ```
    Set `SOURCERY_ADDRESSED=1` / `CODERABBIT_ADDRESSED=1` based on whether step 4-6 addressed at least one finding from that bot on this PR; leave a variable unset (or `0`) when the bot posted no comments this round. Only request re-review for bots whose comments were actually addressed. When the cap skips the trigger, say so in the step 17 summary so the next reviewer knows the cap ended the loop, not a clean pass.

17. **Print summary**:
    - Table: | Comment | Source | Score | Action |
    - Source column distinguishes human vs bot reviewers
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)
    - When step 11's wait ran out with checks still pending, say so: the head SHA and the pending check names

## Cross-References

- **Want to learn from the feedback?** Run the `sova-ingest-review` skill with the PR number after merge

## Rules

- **Don't blindly accept bot suggestions**: evaluate each one against project conventions
- NEVER use emojis in any output
