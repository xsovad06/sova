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
   - **Fetch latest**: `git fetch origin`
   - **Update local main**: `git checkout main && git pull origin main`
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

4. **Handle uncommitted changes** (if any exist):
   - Analyze and group related changes logically
   - **Create small, atomic commits with detailed explanations**:
     * Each commit = ONE logical change
     * Use conventional commit format: `type(scope): subject`
     * Scopes: agent, dashboard, commands, personas, invariants, knowledge, cli, docs

5. **Analyze all changes** (existing + new commits):
   - Read the full diff: `git diff origin/main...HEAD`
   - Identify the type of change (feature, bugfix, refactor, etc.)

6. **Generate PR content**:

   **Title**: Concise, descriptive title based on the changes.

   **Body**:
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

7. **Run preflight checks locally** before pushing:
   ```bash
   shellcheck pak agent/*.sh agent/adapters/*.sh invariants/*.sh
   ```
   Fix any failures before proceeding.

8. **Push the branch**:
   - New PR: `git push -u origin $(git branch --show-current)`
   - Existing PR (AI feedback): `git push --force-with-lease origin $(git branch --show-current)`
   - Existing PR (incremental): `git push origin $(git branch --show-current)`

9. **Create or update PR**:
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

10. **Assign the PR** to the current user:
    ```bash
    gh pr edit <PR_NUMBER> --add-assignee "$(gh api user --jq '.login')"
    ```

11. **Return the PR URL** to the user.

## Cross-References

- **Before this**: Run `/review` to catch issues before pushing
- **After merge**: Run `/after-merge` for cleanup
- **Need to reorganize commits first?** Run `/rearrange-commits`

## Rules

- If the branch has no commits ahead of main and no uncommitted changes, inform the user
- All commits on the branch will be analyzed to generate the PR description
- NEVER use emojis in any output
