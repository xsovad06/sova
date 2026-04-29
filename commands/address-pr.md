---
name: address-pr
description: Address PR review comments (human and bot) -- score, fix or decline, reply, resolve threads. Provide PR number.
user-invocable: true
category: pr
---

# Address PR Review Comments

Score each review comment, fix valuable ones, politely decline low-value ones, and reply on GitHub. Handles both human reviewers and automated review bots (Sourcery, CodeRabbit, etc.).

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
   - 1-2: purely subjective preferences -- naming style, comment wording, formatting not caught by linter
   - 3-5: minor improvements -- DRY violations, missing edge cases, error handling, code removal
   - 6-8: meaningful -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

   Bump to 3+ (not 1-2) if the finding removes code, improves error handling, reduces duplication, or fixes doc inconsistencies. Reserve 1-2 only for truly subjective style preferences.

   For bot suggestions: evaluate critically -- don't blindly accept. Bots lack project context and may suggest changes that conflict with project conventions.

6. **For comments scoring 3+**: Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.

7. **For comments scoring 1-2**: Auto-fix if the change removes code, improves error handling, or reduces duplication. Decline only purely subjective preferences.

8. **After all fixes**:
   Run the project's linter and tests (see CLAUDE.md for commands).
   Stage and commit with a message following the project's commit conventions (see AGENTS.md).
   The commit type is `fix` and the description must summarize WHAT was fixed, not just reference the issue/PR.
   ```
   fix(<scope>): address review findings from PR #<PR_NUMBER>
   ```
   Example: `fix(ai): address review findings from PR #82`
   The scope comes from the area of code changed. Do NOT use generic messages like `feat: issue #N`.

9. **Reply to each comment on GitHub** -- keep replies SHORT and DIRECT:
   - Addressed (3+): `Fixed: [what changed].` or `Added [what].`
   - Declined (<3): brief explanation of why current code is sufficient.
   - No filler words, no 'Great catch!', no emojis.

10. **Resolve each thread** using GraphQL:
    ```bash
    gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
    ```

11. **Request bot re-review** (if bot comments were addressed):
    ```bash
    # Sourcery AI
    gh pr comment <PR_NUMBER> --body "@sourcery-ai review"
    # CodeRabbit
    gh pr comment <PR_NUMBER> --body "@coderabbitai review"
    ```
    Only request re-review for bots whose comments were actually addressed.

12. **Update memory**: Append lessons from comments scored 3+ to `.claude/agent-memory/review-feedback.md`.

13. **Print summary**:
    - Table: | Comment | Source | Score | Action |
    - Source column distinguishes human vs bot reviewers
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- **Don't blindly accept bot suggestions** -- evaluate each one against project conventions
- NEVER use emojis in any output
