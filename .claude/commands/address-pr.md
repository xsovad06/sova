---
name: address-pr
description: Address PR review comments -- score, fix or decline, reply, resolve threads. Provide PR number.
user-invocable: true
---

# Address PR Review Comments

Score each review comment, fix valuable ones, politely decline low-value ones, and reply on GitHub.

## Instructions

1. Get the PR number from `$ARGUMENTS`.

2. **Check for SOVA reviewer handoff first** (this is the primary source of review findings):
   ```bash
   cat .claude/agent-control/handoff.json 2>/dev/null
   ```
   If the handoff exists and has `pending_findings`, use those as the review comments to address.
   Each finding has: `file`, `line`, `severity`, `category`, `description`, `suggestion`.
   The severity is already scored (1-10), so use it directly instead of re-scoring.

3. **Also fetch PR-level review data** (from human reviewers or GitHub review API):
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title,headRefName
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews
   ```

4. **Also check issue comments** for SOVA review comments (they start with `## Code Review for PR`):
   ```bash
   # Find the linked issue number from the PR body (look for "Closes #N")
   gh pr view <PR_NUMBER> --json body --jq '.body' | grep -oP 'Closes #\K\d+'
   # Then fetch issue comments
   gh api repos/<OWNER>/<REPO>/issues/<ISSUE_NUMBER>/comments --jq '.[] | select(.body | startswith("## Code Review")) | .body'
   ```

5. **Merge all sources**: combine handoff findings, PR review comments, and issue review comments.
   Deduplicate by file+description. Handoff findings take priority (they have pre-scored severity).

6. Find the working directory -- check for an existing worktree or create one:
   ```bash
   git worktree list
   ```

7. **Score each comment 1-10** (skip for handoff findings which are pre-scored):
   - 1-2: cosmetic nitpicks, subjective style, trivial renames
   - 3-5: minor improvements -- DRY violations, missing edge cases, readability
   - 6-8: meaningful -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

8. **For comments scoring 3+**: Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.

9. **For comments scoring below 3**: Decline politely.
   - Acknowledge the suggestion briefly.
   - Explain why the current code is sufficient.
   - Keep tone respectful but firm.

10. **After all fixes**:
    Run the project's linter and tests (see CLAUDE.md for commands).
    ```bash
    git add -A && git commit --amend --no-edit
    git push --force-with-lease
    ```
    NEVER create new 'fix' commits -- always amend/squash into existing commits.

11. **Reply to each comment on GitHub** -- keep replies SHORT and DIRECT:
    - Addressed (3+): `Fixed: [what changed].` or `Added [what].`
    - Declined (<3): brief explanation of why current code is sufficient.
    - No filler words, no 'Great catch!', no emojis.

12. **Resolve each thread** using GraphQL (for PR review comments only):
    ```bash
    gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
    ```

13. **Clear the handoff** after addressing all findings:
    ```bash
    rm -f .claude/agent-control/handoff.json
    ```

14. **Update memory**: Append lessons from comments scored 3+ to `.claude/agent-memory/review-feedback.md`.

15. **Print summary**:
    - Table: | Source | File | Score | Action |
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- NEVER use emojis in any output
- Handoff findings are the PRIMARY source -- always check the handoff file first
- If no findings from any source, report "No review comments to address" and exit
