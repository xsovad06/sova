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

1. **Update main branch and rebase**:
   - **Stash any uncommitted changes first** (if any): `git stash push -m "Pre-rebase stash"`
   - **Fetch latest**: `git fetch origin`
   - **Update local main**: `git checkout main && git pull origin main` (adjust branch name if project uses `master`)
   - **Check for local-only main commits**: `git rev-list --count origin/main..main` -- if non-zero, warn the user: "local main is N commits ahead of origin/main -- these will leak into PR scope. Run `git reset --hard origin/main` to fix."
   - **Switch back to feature branch**: `git checkout -`
   - **Rebase onto updated main**: `git rebase main`
   - **Handle rebase conflicts** (if any): Inform the user and help resolve them
   - **Pop the stashed changes** (if any): `git stash pop`

2. **Gather branch information** by running these commands in parallel:
   - `git status` -- check for uncommitted changes
   - `git branch --show-current` -- get current branch name
   - `git log origin/main..HEAD --oneline` -- list all commits on this branch
   - `git diff origin/main...HEAD --stat` -- summary of all committed changes
   - `git diff --stat` -- summary of uncommitted changes
   - `gh pr list --head $(git branch --show-current) --json number,url,state` -- check if PR already exists

3. **Handle existing PR**:
   If a PR already exists, **ASK THE USER** which workflow they want:
   - **Option A: AI feedback workflow** -- reset and recreate all commits (soft reset to main, reorganize)
   - **Option B: Incremental workflow** -- add new commits on top

4. **Handle main branch** (if current branch is main/master):
   - Create a new feature branch from changes
   - Generate a descriptive branch name (e.g., `fix/lint-errors`, `feat/add-caching`)
   - Continue PR process on the new branch

5. **Handle uncommitted changes** (if any exist):
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
   - Read the full diff: `git diff origin/main...HEAD`
   - Identify the type of change (feature, bugfix, refactor, etc.)
   - Look for migration files, API spec changes, test coverage

7. **Generate PR content**:

   **Title**: Concise, descriptive title based on the changes.

   **Body**: Use the project's PR template if one exists (check `.github/PULL_REQUEST_TEMPLATE.md`). Otherwise use:

   ```markdown
   ## Summary
   In 1-3 bullet points, describe what changed and why.

   ## Changes
   Brief description of each logical change grouped by area.

   ## Review guidance
   What should a reviewer focus on? Any trade-offs or shortcuts?

   ## Test plan
   How were these changes verified?
   ```

   Do not include preamble or commentary before the first heading. Do not use emojis.

   **Link the issue in the PR body** based on the task source:

   Read `sova.toml` to check `[task_source] type` if it exists.

   **JIRA** (`type = "jira"`):
   - Do NOT use `Closes #N`, `Fixes #N`, or `Resolves #N` (those are GitHub Issue syntax)
   - Add a `## JIRA` section at the bottom of the body with a link to the ticket:
     `[PROJECT_KEY-NUMBER](https://your-jira-instance/browse/PROJECT_KEY-NUMBER)`
   - The PR title MUST start with `[PROJECT_KEY-NUMBER]` followed by a human-readable description (NOT the branch name)

   **GitHub** (default):
   - If `$ARGUMENTS` contains a GitHub Issue number (e.g., #42), include `Closes #42` in the body

8. **Run preflight CI checks locally** before pushing:
   Run the full CI-equivalent checks: `{{ check_cmd }}`. This must pass before any push -- it covers linting, tests, formatting, invariants, and any other checks the CI pipeline enforces. Fix any failures before proceeding.

9. **Push the branch**:
   - New PR: `git push -u origin $(git branch --show-current)`
   - Existing PR (AI feedback): `git push --force-with-lease origin $(git branch --show-current)`
   - Existing PR (incremental): `git push origin $(git branch --show-current)`

10. **Create or update PR**:
    ```bash
    # New PR (--assignee @me ensures creator is always assigned)
    gh pr create --assignee @me --title "THE_TITLE" --body "$(cat <<'EOF'
    [THE BODY]
    EOF
    )"

    # Update existing PR
    gh pr edit <PR_NUMBER> --title "THE_TITLE" --body "$(cat <<'EOF'
    [THE BODY]
    EOF
    )"
    ```

11. **Return the PR URL** to the user.

### Post-Push: CI + Self-Review

These phases run after the PR is created/updated. They enable autonomous operation (push-to-ready-to-merge in one command).

12. **Visual verification** (if applicable):
    If the project has a `/verify-local` command AND changes affect UI (templates, CSS, JS, views -- not test-only or migration-only), follow the `/verify-local` procedure. If verification reveals issues, fix them, re-run CI, and retry. Skip if no `/verify-local` command exists.

13. **Wait for CI pipeline**:
    Poll CI status until it completes:
    ```bash
    gh run list --branch $(git branch --show-current) --limit 1 --json databaseId,status,conclusion
    ```
    If `gh run watch` is available, use it. Otherwise poll every 30 seconds. Max wait: 15 minutes.

14. **Handle CI result**:
    - **CI passes**: continue to step 15.
    - **CI fails**: fetch logs (`gh run view <run_id> --log-failed`), analyze, fix, amend to the relevant commit, force push (`--force-with-lease`), go back to step 13 (max 3 CI retry cycles total). If still failing after 3 attempts, stop and ask the user.
    - **CI still pending after max wait**: report current status and the PR URL. Suggest the user check back later.

15. **Self-review the PR diff**:
    Run the `/review-pr` workflow against this PR to review the actual diff that will be merged:
    1. Fetch the PR number from the branch
    2. Execute the full `/review-pr` analysis (fetch diff, read files, deep analysis)
    3. Post the review on GitHub
    4. If the verdict has no findings >= 3/10: skip step 16, go to step 17
    5. If there are findings >= 3/10: continue to step 16

16. **Address review findings**:
    Run the `/address-pr` workflow to fix the findings:
    1. Score and address each finding (fix or acknowledge)
    2. Commit fixes
    3. Reply to review comments on GitHub
    4. Resolve threads
    5. Force push: `git push --force-with-lease`
    6. Wait for CI again (go back to step 13 logic, max 3 total CI cycles across the entire run)

17. **Report**:
    ```
    ## PR Summary

    Branch: <branch>
    PR: <url>
    CI: passed (attempt N)
    Review: approved / N findings addressed
    Status: ready for /integrate-pr
    ```

## Cross-References

- **Before this**: Run `/review` or `/review-full` to catch issues before pushing
- **Full workflow**: `/develop-full` -> `/review-full` -> `/pr` -> `/integrate-pr`
- **After merge**: Run `/integrate-pr` for merge, cleanup, and knowledge extraction
- **Need to reorganize commits first?** Run `/rearrange-commits`

## Rules

- If the branch has no commits ahead of main and no uncommitted changes, inform the user
- All commits on the branch will be analyzed to generate the PR description
- NEVER merge the PR -- that happens via `/integrate-pr`
- Use `--force-with-lease` for force pushes, never `--force`
- NEVER skip CI checks or use `--no-verify`
- If CI fails 3 times, stop and ask the user for guidance
- Do NOT ask for or request reviewers -- the user handles reviews themselves
- NEVER include AI references in commits or PRs
- NEVER use emojis in any output
