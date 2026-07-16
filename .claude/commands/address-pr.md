---
name: address-pr
description: Address PR review comments (human and bot) -- score, fix or acknowledge, reply, resolve threads. Provide PR number.
user-invocable: true
category: pr
inputs:
  - pr_number
outputs:
  - files_changed
  - review_addressed
---

# Address PR Review Comments

Score each review comment, address all of them (fix or acknowledge with justification), and reply on GitHub. Handles both human reviewers and automated review bots (Sourcery, CodeRabbit, etc.).

## CRITICAL: Complete ALL Steps

This command runs as a headless agent. You MUST execute every step below through to completion. In headless mode, producing a text-only summary without a tool call causes the process to exit immediately -- so NEVER output a final summary without having completed steps 8-15 first (squash, rebase, push, reply, resolve). If you discover that all findings are already addressed, you MUST still complete steps 9-10 (rebase and push) to resolve any merge conflicts, then skip to step 15 for the summary.

**Incomplete execution is worse than failure** -- a run that fixes code but never commits/pushes wastes the cost and leaves the PR unchanged.

## Instructions

1. Get the PR number from `$ARGUMENTS`.

2. Fetch review data:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title,headRefName
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews
   ```

3. Find the working directory using the PR's branch name (from `headRefName` in step 2), not the issue number. This prevents cross-PR contamination when worktrees from unrelated issues exist:
   ```bash
   git worktree prune
   git checkout <HEAD_BRANCH>
   ```
   If checkout fails with "already checked out", `sync_branch()` in `sova/git/branch.py` auto-resolves the conflict via `resolve_worktree_conflict()`. For manual use, the worktree path can be found with:
   ```bash
   WORKTREE_PATH=$(git worktree list --porcelain | grep -F -B2 "branch refs/heads/<HEAD_BRANCH>" | grep "^worktree " | sed 's/^worktree //')
   ```
   Never resolve the worktree by issue number alone -- always use the branch name from the PR metadata.

4. **Classify comment sources**: Separate human reviewer comments from bot comments (e.g., `sourcery-ai[bot]`, `coderabbitai[bot]`). Both get scored the same way, but bot comments are addressed in bulk and may warrant a re-review request.

5. **Score each comment 1-10**:
   - 1-2: low-priority -- style preference, minor naming, formatting
   - 3-5: moderate -- DRY violations, missing edge cases, error handling
   - 6-8: important -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

   Scoring determines priority order, not whether to fix. All findings are addressed.

   For bot suggestions: evaluate critically -- don't blindly accept. Bots lack project context and may suggest changes that conflict with project conventions.

6. **Address all findings** (regardless of score): Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.
   - If a finding is a false positive, not applicable in context, or requires a human decision: acknowledge it with a one-line justification instead of fixing. Do not skip findings based on score alone.

7. **Scout check**: while fixing findings, scan each touched file for pre-existing issues -- failing tests, lint warnings, dead imports, obvious bugs adjacent to your changes. Fix them alongside the review findings. Keep scout fixes small and low-risk.

8. **Squash fixes into original commits** (MANDATORY -- no fix-on-fix commits):

   The PR must read as clean feature development. Do NOT create a separate "address review findings" commit -- fold each fix into the commit that introduced the code being fixed. The final history should look as if the code was written correctly from the start.

   a. Run the project's linter and tests (see CLAUDE.md for commands). All must pass before proceeding.

   b. Record the original commit messages for replay:
      ```bash
      git log origin/$BASE..HEAD --reverse --format="%H %s" > /tmp/pr-commits.txt
      ```

   c. Create a backup branch, then soft-reset:
      ```bash
      git branch backup-pre-squash
      git reset --soft origin/$BASE
      git reset HEAD .
      ```
      All changes are now unstaged in the working tree.

   d. Re-commit in the original logical groups. For each original commit (from the list in step b):
      - Stage the files that belong to that logical unit
      - When a file (e.g., a test file) has changes belonging to multiple commits, save the final version aside, apply the intermediate version for earlier commits, then restore the final version for the last commit that touches it
      - Use the original commit message (preserving type, scope, and description)
      - Fold review fixes into whichever commit introduced the code being fixed

   e. **Single-commit shortcut**: if the branch had only one commit before fixes, skip the reset-and-recommit flow. Instead, stage all fixes and `git commit --amend --no-edit`.

   f. Verify the tree is identical to the backup:
      ```bash
      git diff backup-pre-squash
      ```
      This MUST produce no output. If it does, something was lost -- investigate before continuing.

   g. Clean up: `git branch -D backup-pre-squash`

9. **Rebase onto base branch** to ensure the PR is mergeable after fixes:
    ```bash
    BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq '.baseRefName')
    git fetch origin
    git rebase origin/$BASE
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

10. **Push and wait for CI** (MANDATORY -- do not skip):
   ```bash
   git push --force-with-lease
   ```
   After pushing, poll CI checks on the PR until ALL required checks complete (max 15 minutes, poll every 30s):
   ```bash
   gh pr checks <PR_NUMBER> --json name,bucket --jq '.[] | "\(.bucket)\t\(.name)"'
   ```
   Wait until every check shows `pass`, `fail`, or `skipping` -- no `pending` remaining.

   **If any check fails**: fetch the failure logs, analyze, fix the code, re-push, and re-poll (max 2 retries):
   ```bash
   gh run view <RUN_ID> --log-failed
   ```
   Common CI failures: commit-format invariant (wrong scope/type), test timeouts, lint errors, SonarCloud coverage.

   **Only continue to step 11 when all required checks pass.** If checks are still failing after 2 retries, report the specific failures and stop -- do not proceed to reply/resolve steps with a red CI.

11. **Reply to each comment on GitHub** -- keep replies SHORT and DIRECT:
   - Fixed: `Fixed: [what changed].` or `Added [what].`
   - Acknowledged (not fixed): `Acknowledged -- [justification: false positive / not applicable / needs human input].`
   - No filler words, no 'Great catch!', no emojis.

12. **Resolve each thread** using GraphQL:
    ```bash
    gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
    ```

13. **Request bot re-review** (if bot comments were addressed):
    ```bash
    # Sourcery AI
    gh pr comment <PR_NUMBER> --body "@sourcery-ai review"
    # CodeRabbit
    gh pr comment <PR_NUMBER> --body "@coderabbitai review"
    ```
    Only request re-review for bots whose comments were actually addressed.

14. **Update memory**: Append lessons learned to `.claude/agent-memory/cookbook.md` (under matching domain section).

15. **Print summary**:
    - Table: | Comment | Source | Score | Action |
    - Source column distinguishes human vs bot reviewers
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge

## Rules

- **Don't blindly accept bot suggestions** -- evaluate each one against project conventions
- NEVER use emojis in any output
