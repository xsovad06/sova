---
name: pr
description: Create a pull request with the standard template, analyzing all commits and changes on the current branch.
user-invocable: true
category: core
inputs:
  - issue_reference
outputs:
  - pr_number
  - pr_url
---

# Create Pull Request

Create a pull request for the current branch using the project's standard PR template.

## Instructions

1. **Resolve the base branch and fetch** (do not rebase yet: step 3 decides whether rebasing is safe for this PR):
   Resolve the base branch at runtime rather than assuming `main`. Every shell block in this command runs in a fresh shell, so each block that uses `BASE` re-assigns it first. Fall back to the repo default only when `gh pr view` confirms there is no PR for this branch yet; abort on any other failure (auth, network, API) instead of guessing:
   ```bash
   BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
   [ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
   git fetch origin || { echo "Fetch failed: STOP, origin/$BASE may be stale" >&2; exit 1; }
   echo "Base branch: $BASE"
   ```
   - **Never check out the base branch**: in a linked worktree it is usually checked out elsewhere, so `git checkout "$BASE"` fails. Rebase onto the remote-tracking ref instead.
   - **Verify the GitHub account**: `gh auth status` must show the account the project configures as its GitHub user (for SOVA projects, `sova config get github_user` prints it). Otherwise `--assignee @me` in step 11 assigns the wrong user and later thread writes fail with FORBIDDEN. If it differs, run `gh auth switch --user <github_user>`.

2. **Gather branch information**. These commands do not need `$BASE` and can run in parallel:
   - `git status`: check for uncommitted changes
   - `git branch --show-current`: get current branch name
   - `git diff --stat`: summary of uncommitted changes
   - `gh pr list --head $(git branch --show-current) --json number,url,state`: check if PR already exists

   The two commands below need `$BASE`, resolved fresh in this shell (same fail-closed resolution as step 1):
   ```bash
   BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
   [ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
   git log "origin/$BASE"..HEAD --oneline
   git diff "origin/$BASE"...HEAD --stat
   ```

3. **Handle existing PR, then rebase only when it is safe**:
   If a PR already exists, **ASK THE USER** which workflow they want (when running headless, with no user to ask, use Option B: it never rewrites history the remote already has):
   - **Option A: AI feedback workflow**: reset and recreate all commits (back up the branch, soft reset to the fork point, reorganize, verify: see step 5). Rebasing is safe here since step 10 force-pushes this option.
   - **Option B: Incremental workflow**: add new commits on top of the existing remote history. Do NOT rebase: step 10 pushes this option with a plain `git push`, which fails as non-fast-forward if the rebase rewrote commit IDs the remote already has.

   For a new PR (no existing PR yet) or Option A, rebase now, before adding any commits:
   - **Never use `git stash`**: the stash stack is shared by every worktree and session of this repository, so a pop can restore someone else's changes. If `git status --porcelain` prints anything, review it first (nothing that must not be committed, such as secrets or scratch files), then set the work aside as a WIP commit; step 5 turns it into real commits:
     ```bash
     git add -A && git commit -m "chore: WIP snapshot before rebase" \
       || { echo "WIP commit failed (hook, signing or staging error): STOP, nothing was reset" >&2; exit 1; }
     [ -z "$(git status --porcelain)" ] || { echo "Uncommitted changes remain after the WIP commit: STOP before rebasing" >&2; exit 1; }
     ```
   - **Rebase**:
     ```bash
     BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
     [ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
     git rebase "origin/$BASE"
     ```
   - **Handle rebase conflicts** (if any): read each conflicted file, resolve the markers, `git add <file>`, then `git rebase --continue`. If a conflict cannot be resolved with confidence, run `git rebase --abort`, then undo the WIP commit if one was made (`git log -1 --format=%s` prints `chore: WIP snapshot before rebase`: run `git reset --soft HEAD~1`), report the conflicting files to the user, and stop.

4. **Handle the base branch** (if the current branch is the base branch itself):
   - Create a new feature branch from changes
   - Generate a descriptive branch name (e.g., `fix/lint-errors`, `feat/add-caching`)
   - Continue PR process on the new branch

5. **Recreate history (Option A) and handle uncommitted changes**. For Option A, the first bullet and the verification after the commit format below always run, even when nothing is uncommitted. The remaining bullets apply whenever uncommitted changes exist, including a WIP commit from step 3:
   - **Option A only**: recreate every commit. Back up the branch, then soft-reset to the fork point; this replaces the WIP undo in the next bullet, since the reset uncommits the WIP snapshot too. Never reset to `origin/$BASE` by name: the step 3 rebase made the two equal, but a later fetch can move `origin/$BASE`, and recommitting this branch's tree on top of the newer base silently reverts everything the base gained. Note the printed backup name: the verification below and step 10 re-assign `BACKUP` to it.
     ```bash
     [ -z "$(git status --porcelain)" ] || { echo "Uncommitted changes remain: STOP before creating the backup" >&2; exit 1; }
     BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
     [ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
     FORK_POINT=$(git merge-base HEAD "origin/$BASE") || { echo "Cannot resolve the fork point: STOP before resetting" >&2; exit 1; }
     BACKUP="backup/pr-$(date +%s)-$$-$RANDOM"
     git branch "$BACKUP" || { echo "Failed to create backup branch $BACKUP: STOP before resetting" >&2; exit 1; }
     echo "Backup branch: $BACKUP"
     git reset --soft "$FORK_POINT" && git reset HEAD .
     ```
   - Otherwise (a new PR), if step 3 created a WIP commit, confirm `git log -1 --format=%s` prints `chore: WIP snapshot before rebase`, then undo it with `git reset --soft HEAD~1` so its contents are uncommitted again
   - Analyze and group related changes logically
   - **Create small, atomic commits with detailed explanations**:
     * Each commit = ONE logical change
     * Use conventional commit format: `type(scope): subject`
     * Write multi-line commit messages explaining WHAT, WHY, and HOW

   **Commit message format**:
   ```
   type(scope): short description (max 50 chars)

   Detailed explanation of what this commit does and why.
   Include before/after behavior if applicable.
   ```

   **Option A only: verify the recreated history.** Both commands below must print nothing: the recreated branch must have exactly the tree it had before.
   ```bash
   BACKUP=<backup branch printed in step 5>
   git status --porcelain
   git diff "$BACKUP"
   ```
   Then confirm the recreated history kept what the base gained. The two checks above cannot see this: a reset that wrongly targeted `origin/$BASE` leaves the tree identical, so `git diff "$BACKUP"` stays empty while the branch's diff now reverts everything the base gained since the fork point. The block must end by printing `File-set check passed`.
   ```bash
   BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
   [ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
   BACKUP=<backup branch printed in step 5>
   OLD_FORK=$(git merge-base "$BACKUP" "origin/$BASE") || { echo "Cannot resolve the backup's fork point: STOP" >&2; exit 1; }
   NEW_FORK=$(git merge-base HEAD "origin/$BASE") || { echo "Cannot resolve HEAD's fork point: STOP" >&2; exit 1; }
   BEFORE=$(git diff --name-only "$OLD_FORK" "$BACKUP") || { echo "git diff failed: STOP" >&2; exit 1; }
   AFTER=$(git diff --name-only "$NEW_FORK" HEAD) || { echo "git diff failed: STOP" >&2; exit 1; }
   BASE_CHANGED=$(git diff --name-only "$OLD_FORK" "origin/$BASE") || { echo "git diff failed: STOP" >&2; exit 1; }
   # A file the squash newly brought into the branch's diff that the base changed since the fork is a reverted base change.
   REVERTED=$(comm -12 <(comm -13 <(sort <<<"$BEFORE") <(sort <<<"$AFTER")) <(sort <<<"$BASE_CHANGED"))
   if [ -n "$REVERTED" ]; then printf 'Squash reverted base changes in:\n%s\nSTOP\n' "$REVERTED" >&2; exit 1; fi
   echo "File-set check passed"
   ```

   **If any check fails**, first preserve anything uncommitted: `git status --porcelain` output means tracked or untracked edits exist only in the working tree, made during commit recreation, and are not in `$BACKUP` or any commit on this branch, so the recovery reset below would silently discard them. Commit them to a rescue branch before resetting, then redo Option A's reset from the top of this step:
   ```bash
   BACKUP=<backup branch printed in step 5>
   if [ -n "$(git status --porcelain)" ]; then
     git add -A && git commit -m "chore: WIP rescue before re-reset" \
       || { echo "Rescue commit failed: STOP, do not reset over uninspected changes" >&2; exit 1; }
     RESCUE="rescue/squash-$(date +%s)-$$-$RANDOM"
     git branch "$RESCUE" || { echo "Failed to create rescue branch $RESCUE: STOP" >&2; exit 1; }
     echo "Rescue branch: $RESCUE"
   fi
   git reset --hard "$BACKUP"
   ```

6. **Analyze all changes** (existing + new commits):
   - Read the full diff: `git diff "origin/$BASE"...HEAD` (re-assign `BASE` as in step 1)
   - Identify the type of change (feature, bugfix, refactor, etc.)
   - Look for migration files, API spec changes, test coverage

7. **Generate PR content**:

   **Title**: Concise, descriptive title based on the changes.

   **Body**: Use the project's PR template if one exists (check `.github/PULL_REQUEST_TEMPLATE.md`). Otherwise use:

   ```markdown
   ## Summary
   3-5 bullet points: WHAT changed and WHY, folding in the file-by-file
   breakdown (which files/areas each bullet touches) rather than repeating it
   in a separate section.

   ## Review guidance
   What should a reviewer focus on? Any trade-offs or shortcuts?

   ## Test plan
   Name the specific test file(s) (and test names, where relevant) that cover
   this change, and how they were run. Do not write a generic statement like
   "tests were run" without naming which ones.
   ```

   Do not include preamble or commentary before the first heading. Do not use emojis.

   **Link the issue in the PR body** based on the task source:

   Determine the task source by running `sova config` and reading the `task_source` row (configuration lives in `.claude/sova.db`, not in a file).

   **JIRA** (`type = "jira"`):
   - Do NOT use `Closes #N`, `Fixes #N`, or `Resolves #N` (those are GitHub Issue syntax)
   - Add a `## JIRA` section at the bottom of the body with a link to the ticket:
     `[PROJECT_KEY-NUMBER](https://your-jira-instance/browse/PROJECT_KEY-NUMBER)`
   - The PR title MUST start with `[PROJECT_KEY-NUMBER]` followed by a human-readable description (NOT the branch name)

   **GitHub** (default):
   - If `{{ arguments }}` contains a GitHub Issue number (e.g., #42), include `Closes #42` in the body

   **Save the body to a file** so step 9 can check it and step 11 can submit it unchanged. Note the printed path: later shell blocks are fresh shells and must re-assign `BODY_FILE` to it.
   ```bash
   BODY_FILE=$(mktemp)
   cat > "$BODY_FILE" <<'EOF'
   [THE BODY]
   EOF
   echo "PR body saved to $BODY_FILE"
   ```

8. **Run preflight CI checks locally** before pushing:
   Run the full checks, skipping only when an identical check already passed earlier in this autonomous pipeline cycle for this exact worktree state (a no-op outside the autonomous pipeline: `sova checks should-skip` only ever reports a skip there):
   ```bash
   if sova checks should-skip "{{ check_cmd }}"; then
     echo "Full check already passed for this worktree state earlier in the pipeline cycle; skipping re-run."
   else
     {{ check_cmd }} && { sova checks record "{{ check_cmd }}" --passed || true; }
   fi
   ```
   It is only as complete as the project configured it: if it is not the same target CI runs, also run the CI steps it omits (read the CI workflow file to list them). This must pass before any push. Fix any failures before proceeding.

   If the project has pre-push invariant scripts (`invariants/*.sh`), run every one against the base (`for s in invariants/*.sh; do bash "$s" . "$BASE" || echo "FAIL: $s"; done`, with `BASE` resolved as in step 1) and fix every failure: the pre-push hook runs the same scripts, and a push it blocks is fixed by correcting the reported problem, never by `--no-verify`.

   If the project has an executable `.claude/scripts/prepush-check.sh`, run it too. It holds project-specific mechanical checks (deploy config drift, dependency downgrades after a rebase, spec regeneration) that generic commands cannot know about. Treat every `FAIL` line as blocking and every `WARN` line as something to confirm before pushing.

9. **Verify authorship and text** before pushing. Each check must print nothing; fix any hit (reword the commit, correct the author, or edit the body file) and re-run all of them:
   ```bash
   BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
   [ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
   BODY_FILE=<path printed in step 7>
   # No AI attribution: co-author trailers, "generated with" lines, or an AI author/committer identity.
   # Match attribution only; a body that merely mentions a `.claude/` path or CLAUDE.md is fine.
   git log "origin/$BASE"..HEAD --format='%an <%ae>%n%cn <%ce>%n%b' \
     | grep -iE '^co-authored-by:|generated with claude|noreply@anthropic\.com|^claude <'
   # Every commit is authored and committed by the configured git identity
   git log "origin/$BASE"..HEAD --format='%ae%n%ce' | sort -u | grep -vxF "$(git config user.email)"
   # No " -- " used as punctuation in commit messages or the PR body
   git log "origin/$BASE"..HEAD --format='%s%n%b' | grep -nF ' -- '
   grep -nF ' -- ' "$BODY_FILE"
   ```
   A hit inside a fenced code sample in the body (a literal end-of-options separator) is acceptable; a hit in prose is not: use a colon, a comma, or a single hyphen instead.

10. **Push the branch**:
    - New PR: `git push -u origin $(git branch --show-current)`
    - Existing PR (AI feedback): push, then delete the step 5 backup only once the push has succeeded. If the push fails, keep the backup and report its name:
      ```bash
      BACKUP=<backup branch printed in step 5>
      git push --force-with-lease origin "$(git branch --show-current)" && git branch -D "$BACKUP"
      ```
    - Existing PR (incremental): `git push origin $(git branch --show-current)`

11. **Create or update PR**:
    ```bash
    BODY_FILE=<path printed in step 7>
    # New PR (--assignee @me ensures creator is always assigned)
    gh pr create --assignee @me --title "THE_TITLE" --body-file "$BODY_FILE"

    # Update existing PR
    gh pr edit <PR_NUMBER> --title "THE_TITLE" --body-file "$BODY_FILE"

    # Once the PR is created or updated, remove the temporary body file
    rm -f "$BODY_FILE"
    ```

12. **Trigger CodeRabbit review** (if configured): do this only once the code it should review is final. When steps 14-18 will run (they push again: visual fixes, CI fixes, self-review fixes), defer this step until step 19, after the last push; a review of pre-fix code spends the bot's limited quota and its findings go stale on the next push.
    This key is not in the `sova config` table, so resolve it through the config loader:
    ```bash
    python3 -c "from pathlib import Path; from sova.config.loader import load_config; \
print(load_config(Path('.')).external_reviews.coderabbit.trigger_review)"
    ```
    If it prints `True` and the PR was newly created (not an update), post a comment to trigger CodeRabbit. This is a fresh shell, so resolve the PR number in the same block:
    ```bash
    PR_NUM=$(gh pr view --json number --jq .number) || { echo "Unable to resolve the PR number" >&2; exit 1; }
    gh pr comment "$PR_NUM" --body "@coderabbitai review"
    ```

13. **Return the PR URL** to the user.

### Post-Push: CI + Self-Review

These phases run after the PR is created/updated. They enable autonomous operation (push-to-ready-to-merge in one command).

14. **Visual verification** (if applicable):
    If the project has a `/verify-local` command AND changes affect UI (templates, CSS, JS, views, but not test-only or migration-only), follow the `/verify-local` procedure. If verification reveals issues, fix them, commit the fix (amend into the relevant commit or add a new commit), and push (`git push --force-with-lease`) before proceeding to step 15: otherwise CI runs against the old commit and step 17 reviews a diff that doesn't contain the fix. Skip if no `/verify-local` command exists.

15. **Wait for CI pipeline**:
    Wait for every required check on the PR, not just one matched workflow
    run: `gh run list` only sees runs it triggered directly, so a check that
    arrives as a mirrored commit status (e.g. SonarCloud, which posts via
    `.github/workflows/sonarcloud.yml` after CI's coverage artifact lands)
    or from any other separate workflow can still be pending, or can later
    fail, after the one run `gh run list` matched already passed. `gh pr
    checks --required` is scoped to the PR's current head by GitHub itself,
    so it needs no SHA matching and cannot return a stale or unrelated run:
    ```bash
    PR_NUM=$(gh pr view --json number --jq .number) || { echo "Unable to resolve the PR number" >&2; exit 1; }
    gh pr checks "$PR_NUM" --required --json name,bucket,link
    ```
    Poll every 30 seconds until every required check's `bucket` is `pass`, `fail`, `cancel`, or `skipping` (none left `pending`). Max wait: 30 minutes (a full test suite routinely runs longer than 15).

    **Run each wait as a synchronous foreground command**, never as a background task: in headless mode nothing resumes you when a background task finishes, and ending a turn without a tool call ends the run. The Bash tool caps a single call at 10 minutes (600000 ms) and defaults to 2, so pass an explicit `timeout` of at least 540000 ms on every poll call, and keep each call to at most 16 polls of 30 seconds (8 minutes). When a call ends with any required check still pending, start another call the same way, until every required check completes or the 30 minute total is used up. Re-resolve `PR_NUM` at the top of each call, since each one is a fresh shell.

16. **Handle CI result**:
    - **All required checks have bucket `pass` or `skipping`**: continue to step 17 (a `skipping` required check, e.g. a path-filtered workflow, is treated as satisfied).
    - **Any required check fails**: fetch logs for each failing check (`gh run view <run_id> --log-failed` when `link` points at a GitHub Actions run, otherwise open `link`), analyze, fix, amend to the relevant commit, re-run step 9's checks, force push (`--force-with-lease`), go back to step 15 (max 3 CI retry cycles total). If still failing after 3 attempts, stop and ask the user.
    - **Any required check is canceled**: treat as a non-success state, not a pass. Report it and re-run step 15 to trigger it again (e.g. re-push or `gh run rerun`) before continuing; do not proceed to step 17 with a canceled required check.
    - **Any required check still pending after max wait**: report the current statuses and the PR URL. Suggest the user check back later.

17. **Self-review the PR diff**:
    Run the `review-pr` workflow against this PR to review the actual diff that will be merged:
    1. Fetch the PR number from the branch
    2. Execute the `review-pr` workflow's analysis in full (fetch diff, read files, deep analysis)
    3. Post the review on GitHub
    4. If the verdict has no findings >= 3/10: skip step 18, go to step 19
    5. If there are findings >= 3/10: continue to step 18

18. **Address review findings**:
    Run the `address-pr` workflow to fix the findings:
    1. Score and address each finding (fix or acknowledge)
    2. Commit fixes
    3. Reply to review comments on GitHub
    4. Resolve threads
    5. Re-run step 9's checks, then force push: `git push --force-with-lease`
    6. Wait for CI again (go back to step 15 logic, max 3 total CI cycles across the entire run)

19. **Trigger CodeRabbit now** if step 12 was deferred, using step 12's block unchanged. Do the same before stopping early at step 16 (CI still pending, or the retry limit reached), so a deferred trigger is never dropped.

20. **Report**:
    ```
    ## PR Summary

    Branch: <branch>
    PR: <url>
    CI: passed (attempt N)
    Review: approved / N findings addressed
    Status: ready for /integrate-pr
    ```

## Cross-References

- **Before this**: Run the `review` workflow or the `review-full` workflow to catch issues before pushing
- **Full workflow**: the `develop-full` workflow -> the `review-full` workflow -> the `pr` workflow -> the `integrate-pr` workflow
- **After merge**: Run the `integrate-pr` workflow for merge, cleanup, and knowledge extraction
- **Need to reorganize commits first?** Run the `rearrange-commits` workflow

## Rules

- If the branch has no commits ahead of the base branch and no uncommitted changes, inform the user
- All commits on the branch will be analyzed to generate the PR description
- NEVER merge the PR yourself unless the user explicitly asks
- Use `--force-with-lease` for force pushes, never `--force`
- NEVER skip CI checks or use `--no-verify`
- If CI fails 3 times, stop and ask the user for guidance
- Do NOT ask for or request reviewers: the user handles reviews themselves
- NEVER include AI references in commits or PRs
- NEVER use emojis in any output
