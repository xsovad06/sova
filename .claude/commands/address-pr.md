---
name: address-pr
description: Address PR review comments -- score, fix or decline, reply, resolve threads. Provide PR number.
user-invocable: true
---

# Address PR Review Comments

Score each review comment, fix valuable ones, politely decline low-value ones, and reply on GitHub.

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

4. **Score each comment 1-10**:
   - 1-2: cosmetic nitpicks, subjective style, trivial renames
   - 3-5: minor improvements -- DRY violations, missing edge cases, readability
   - 6-8: meaningful -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

5. **For comments scoring 3+**: Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.

6. **For comments scoring below 3**: Decline politely.
   - Acknowledge the suggestion briefly.
   - Explain why the current code is sufficient.
   - Keep tone respectful but firm.

7. **After all fixes**:
   Run the project's linter and tests (see CLAUDE.md for commands).
   ```bash
   git add -A && git commit --amend --no-edit
   git push --force-with-lease
   ```
   NEVER create new 'fix' commits -- always amend/squash into existing commits.

8. **Reply to each comment on GitHub** -- keep replies SHORT and DIRECT:
   - Addressed (3+): `Fixed: [what changed].` or `Added [what].`
   - Declined (<3): brief explanation of why current code is sufficient.
   - No filler words, no 'Great catch!', no emojis.

9. **Resolve each thread** using GraphQL:
   ```bash
   gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
   ```

10. **Update memory**: Append lessons from comments scored 3+ to `.claude/agent-memory/review-feedback.md`.

11. **Print summary**:
    - Table: | Comment | Source | Score | Action |
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- NEVER use emojis in any output
