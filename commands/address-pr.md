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

## Instructions

1. Get the PR number from `$ARGUMENTS`.

2. Fetch review data:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title,headRefName
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews
   ```

3. Find the working directory -- check for an existing worktree or create one:
   ```bash
   git worktree list
   ```

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

8. **After all fixes**:
   Run the project's linter and tests (see CLAUDE.md for commands).
   Stage and commit with a message following the project's commit conventions (see AGENTS.md).
   The commit type is `fix` and the description must summarize WHAT was fixed, not just reference the issue/PR.
   ```
   fix(<scope>): address review findings from PR #<PR_NUMBER>
   ```
   Example: `fix(ai): address review findings from PR #82`
   The scope comes from the area of code changed. Do NOT use generic messages like `feat: issue #N`.

9. **Rearrange commits**: run the `/rearrange-commits` workflow to squash the fix commit into the original feature commits. The PR should read as clean feature development with no review-fix artifacts.

10. **Push and wait for CI**:
   ```bash
   git push --force-with-lease
   ```
   After pushing, poll CI until it completes (max 10 minutes):
   ```bash
   gh run list --branch $(git branch --show-current) --limit 1 --json databaseId,status,conclusion
   ```
   If CI fails, analyze the failure, fix, amend the commit, and re-push (max 2 retries).
   If CI passes, continue. If still pending after max wait, report status and continue anyway.

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
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- **Don't blindly accept bot suggestions** -- evaluate each one against project conventions
- NEVER use emojis in any output
