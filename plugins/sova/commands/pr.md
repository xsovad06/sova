---
description: Create a pull request with the standard template, analyzing all commits and changes on the current branch.
argument-hint: "<issue_reference>"
example: "/sova:pr"
category: core
inputs:
  - issue_reference
outputs:
  - pr_number
  - pr_url
---

## Name
sova:pr

## Synopsis
```
/sova:pr <issue_reference>
```

## Description

Create a pull request with the standard template, analyzing all commits and changes on the current branch.

## Implementation

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
   If a PR already exists, **ASK THE USER** which workflow they want:
   - **Option A: AI feedback workflow**: reset and recreate all commits (soft reset to `origin/$BASE`, reorganize). Rebasing is safe here since step 10 force-pushes this option.
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
   - **Handle rebase conflicts** (if any): read each conflicted file, resolve the markers, `git add <file>`, then `git rebase --continue`. If a conflict cannot be resolved with confidence, run `git rebase --abort`, report the conflicting files to the user, and stop.

4. **Handle the base branch** (if the current branch is the base branch itself):
   - Create a new feature branch from changes
   - Generate a descriptive branch name (e.g., `fix/lint-errors`, `feat/add-caching`)
   - Continue PR process on the new branch

5. **Handle uncommitted changes** (if any exist, including a WIP commit from step 3):
   - If step 3 created a WIP commit, confirm `git log -1 --format=%s` prints `chore: WIP snapshot before rebase`, then undo it with `git reset --soft HEAD~1` so its contents are uncommitted again
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

   Determine the task source (GitHub, Jira, or other) from the project's own config or conventions.

   **JIRA** (`type = "jira"`):
   - Do NOT use `Closes #N`, `Fixes #N`, or `Resolves #N` (those are GitHub Issue syntax)
   - Add a `## JIRA` section at the bottom of the body with a link to the ticket:
     `[PROJECT_KEY-NUMBER](https://your-jira-instance/browse/PROJECT_KEY-NUMBER)`
   - The PR title MUST start with `[PROJECT_KEY-NUMBER]` followed by a human-readable description (NOT the branch name)

   **GitHub** (default):
   - If `$ARGUMENTS` contains a GitHub Issue number (e.g., #42), include `Closes #42` in the body

   **Save the body to a file** so step 9 can check it and step 11 can submit it unchanged. Note the printed path: later shell blocks are fresh shells and must re-assign `BODY_FILE` to it.
   ```bash
   BODY_FILE=$(mktemp)
   cat > "$BODY_FILE" <<'EOF'
   [THE BODY]
   EOF
   echo "PR body saved to $BODY_FILE"
   ```

8. **Run preflight CI checks locally** before pushing:
   Run `the project's CI-equivalent check command (see its Makefile, package.json scripts, or CI config)`. It is only as complete as the project configured it: if it is not the same target CI runs, also run the CI steps it omits (read the CI workflow file to list them). This must pass before any push. Fix any failures before proceeding.

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
    - Existing PR (AI feedback): `git push --force-with-lease origin $(git branch --show-current)`
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

12. **Trigger CodeRabbit review** (if configured):
    If the project uses an automated reviewer such as CodeRabbit and the PR was newly created (not an update), post a comment to trigger CodeRabbit:
    ```bash
    gh pr comment "$PR_NUM" --body "@coderabbitai review"
    ```

13. **Return the PR URL** to the user.

### Post-Push: CI + Self-Review

These phases run after the PR is created/updated. They enable autonomous operation (push-to-ready-to-merge in one command).

14. **Visual verification** (if applicable):
    If the project has a `/verify-local` command AND changes affect UI (templates, CSS, JS, views, but not test-only or migration-only), follow the `/verify-local` procedure. If verification reveals issues, fix them, commit the fix (amend into the relevant commit or add a new commit), and push (`git push --force-with-lease`) before proceeding to step 15: otherwise CI runs against the old commit and step 17 reviews a diff that doesn't contain the fix. Skip if no `/verify-local` command exists.

15. **Wait for CI pipeline**:
    Capture the commit CI must validate, then poll until a run for that exact
    commit appears and completes: `--limit 1` alone can return an unrelated
    or stale run (another workflow, or the new run not yet registered):
    ```bash
    HEAD_SHA=$(git rev-parse HEAD)
    gh run list --branch $(git branch --show-current) --json databaseId,status,conclusion,headSha,workflowName --jq --arg sha "$HEAD_SHA" '[.[] | select(.headSha == $sha)]'
    ```
    If `gh run watch` is available, use it against the matched run's `databaseId`. Otherwise poll every 30 seconds until a run matching `$HEAD_SHA` appears and completes. Max wait: 30 minutes (a full test suite routinely runs longer than 15).

    **Run each wait as a synchronous foreground command**, never as a background task: in headless mode nothing resumes you when a background task finishes, and ending a turn without a tool call ends the run. The Bash tool caps a single call at 10 minutes (600000 ms) and defaults to 2, so pass an explicit `timeout` of at least 540000 ms on every wait call, and keep each call to at most 16 polls of 30 seconds (8 minutes). A `gh run watch` call gets the same timeout. When a call ends with the run still pending, start another call the same way, until the run completes or the 30 minute total is used up. Re-assign `HEAD_SHA` at the top of each call, since each one is a fresh shell.

16. **Handle CI result**:
    - **CI passes**: continue to step 17.
    - **CI fails**: fetch logs (`gh run view <run_id> --log-failed`), analyze, fix, amend to the relevant commit, re-run step 9's checks, force push (`--force-with-lease`), go back to step 15 (max 3 CI retry cycles total). If still failing after 3 attempts, stop and ask the user.
    - **CI still pending after max wait**: report current status and the PR URL. Suggest the user check back later.

17. **Self-review the PR diff**:
    Review the actual diff that will be merged, as a senior engineer would:
    1. Fetch the PR number from the branch
    2. Fetch the diff, read every changed file, and analyse it deeply
    3. Post the review on GitHub
    4. If the verdict has no findings >= 3/10: skip step 18, go to step 19
    5. If there are findings >= 3/10: continue to step 18

18. **Address review findings**:
    Fix the findings:
    1. Score and address each finding (fix or acknowledge)
    2. Commit fixes
    3. Reply to review comments on GitHub
    4. Resolve threads
    5. Re-run step 9's checks, then force push: `git push --force-with-lease`
    6. Wait for CI again (go back to step 15 logic, max 3 total CI cycles across the entire run)

19. **Report**:
    ```
    ## PR Summary

    Branch: <branch>
    PR: <url>
    CI: passed (attempt N)
    Review: approved / N findings addressed
    Status: ready to merge
    ```

## Cross-References

- **Before this**: Run `/review` to catch issues before pushing
- **Full workflow**: `/develop` -> `/review` -> `/pr`
- **After merge**: merge the PR, delete the branch, and capture anything learned
- **Need to reorganize commits first?** Reorganize the branch's commits into clean, logical units

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

## Arguments

- `issue_reference`

## Examples

```
/sova:pr
```
