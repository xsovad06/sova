---
name: pr
description: Create a pull request with the standard template, analyzing all commits and changes on the current branch.
user-invocable: true
---

# Create Pull Request

Create a pull request for the current branch using the project's standard PR template.

## Instructions

1. **Update main branch and rebase**:
   - **Stash any uncommitted changes first** (if any): `git stash push -m "Pre-rebase stash"`
   - **Fetch latest changes**: `git fetch origin`
   - **Update local main**: `git checkout main && git pull origin main`
   - **Switch back to feature branch**: `git checkout -` (or use the branch name)
   - **Rebase onto updated main**: `git rebase main`
   - **Handle rebase conflicts** (if any): Inform the user and help resolve them
   - **Pop the stashed changes** (if any were stashed): `git stash pop`
   - If stash pop has conflicts, help the user resolve them before proceeding

2. **Gather branch information** by running these commands in parallel:
   - `git status` - check for uncommitted changes (staged and unstaged)
   - `git branch --show-current` - get current branch name
   - `git log origin/main..HEAD --oneline` - list all commits on this branch
   - `git diff origin/main...HEAD --stat` - get summary of all committed changes
   - `git diff --stat` - get summary of uncommitted changes
   - `gh pr list --head $(git branch --show-current) --json number,url,state` - check if PR already exists for this branch

3. **Handle existing PR**:
   If a PR already exists for this branch, **ASK THE USER** which workflow they want:
   - Option A: **AI feedback workflow** (reset and recreate all commits)
   - Option B: **Incremental workflow** (add new commits on top)

   **3a. AI feedback workflow** (user chose Option A):
   This resets all commits and recreates them fresh -- useful when AI-generated code needs complete reorganization:
   - Inform the user that existing PR will be updated with reorganized commits
   - **Stash any uncommitted changes first** (if any): `git stash push -m "PR update stash"`
   - **Soft reset all commits on the branch back to origin/main**: `git reset --soft origin/main`
   - **Pop the stashed changes** (if any were stashed): `git stash pop`
   - Now all changes (both previously committed AND any uncommitted changes) are staged/unstaged
   - Continue to step 5 to create fresh, well-organized commits
   - Use force push in step 9, then update the existing PR (step 10)

   **3b. Incremental workflow** (user chose Option B):
   This preserves existing commits and adds new ones on top -- useful for addressing review feedback:
   - Inform the user that new commits will be added to the existing PR
   - Keep all existing commits as-is
   - Continue to step 5 to commit only the uncommitted changes as new commits
   - Use normal push in step 9, then update the existing PR description (step 10) if needed

4. **Handle main branch** (if current branch is main/master):
   - If on main/master with uncommitted or staged changes, create a new feature branch before proceeding
   - Generate a descriptive branch name based on the changes (e.g., `fix/lint-errors`, `feat/add-caching`)
   - Create and switch to the new branch: `git checkout -b <branch-name>`
   - Continue with the PR process on the new branch

5. **Handle uncommitted changes** (if any exist):
   - Analyze the uncommitted changes: `git diff` and `git diff --cached`
   - Group related changes logically (e.g., by feature, by file type, by purpose)
   - **Create small, atomic commits with detailed explanations**:
     * Each commit should represent ONE single logical change
     * Use conventional commit format: `type(scope): subject`
     * Scopes: agent, dashboard, commands, personas, invariants, knowledge, cli, docs
     * Write multi-line commit messages with detailed body explaining:
       - WHAT: The specific change being made
       - WHY: The motivation/reasoning behind it
       - HOW: The approach taken (if not obvious)

   **Commit message format**:
   ```
   type(scope): short description (max 50 chars)

   Detailed explanation of what this commit does and why.
   Include before/after behavior if applicable.

   Example usage or impact (when helpful).
   ```

   Types: feat, fix, refactor, test, docs, chore, perf

6. **Analyze all changes** (existing commits + newly created commits):
   - Read the full diff: `git diff origin/main...HEAD`
   - Understand what files were modified/added/deleted
   - Identify the type of change (feature, bugfix, refactor, etc.)
   - Look for test coverage, API changes

7. **Generate PR content**:

   **Title**: Concise, descriptive title based on the changes.

   **Body**: Use the project's PR template if one exists (check `.github/PULL_REQUEST_TEMPLATE.md`). Otherwise use:

   ```markdown
   ## Summary
   [WHAT was changed and WHY]

   ## Changes
   [Brief description of each logical change]

   ## Testing
   [How to verify the changes work]

   ## Checklist
   - [ ] ShellCheck passes on changed bash scripts
   - [ ] Dashboard tests pass (if applicable)
   - [ ] Documentation updated (if applicable)
   ```

   If `$ARGUMENTS` contains a GitHub Issue number (e.g., #42), include `Closes #42` in the body.

8. **Run preflight CI checks locally** before pushing:
   - Determine which checks apply based on changed files (`git diff origin/main...HEAD --stat`):
     * If any `.sh` files changed: `shellcheck invariants/*.sh`
     * Run the project's lint and test commands (see CLAUDE.md for commands)
   - **Fix any failures before proceeding.** Do NOT push code that fails CI.
   - If fixes are needed, commit them and re-run the failing checks.

9. **Push the branch**:
   - New PR: `git push -u origin $(git branch --show-current)`
   - Existing PR (AI feedback workflow): `git push --force-with-lease origin $(git branch --show-current)`
   - Existing PR (incremental): `git push origin $(git branch --show-current)`

10. **Create or update PR**:
    ```bash
    # New PR
    gh pr create --title "THE_TITLE" --body "$(cat <<'EOF'
    [THE BODY]
    EOF
    )"

    # Update existing PR
    gh pr edit <PR_NUMBER> --title "THE_TITLE" --body "$(cat <<'EOF'
    [THE BODY]
    EOF
    )"
    ```

11. **Assign the PR** to the current user:
    ```bash
    gh pr edit <PR_NUMBER> --add-assignee "$(gh api user --jq '.login')"
    ```

12. **Return the PR URL** to the user.

## Rules

- If the branch has no commits ahead of main and no uncommitted changes, inform the user
- All commits on the branch (both existing and newly created) will be analyzed to generate the PR description
- NEVER use emojis in any output
