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

4. **Merge all sources**: combine handoff findings and PR review comments.
   Deduplicate by file+description. Handoff findings take priority (they have pre-scored severity).

5. Find the working directory -- check for an existing worktree or create one:
   ```bash
   git worktree list
   ```

6. **Score each comment 1-10** (skip for handoff findings which are pre-scored):
   - 1-2: purely subjective preferences -- naming style, comment wording, formatting not caught by linter
   - 3-5: minor improvements -- DRY violations, missing edge cases, error handling, code removal
   - 6-8: meaningful -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

   Bump to 3+ (not 1-2) if the finding removes code, improves error handling, reduces duplication, or fixes doc inconsistencies. Reserve 1-2 only for truly subjective style preferences.

7. **For comments scoring 3+**: Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.

8. **For comments scoring 1-2**: Auto-fix if the change removes code, improves error handling, or reduces duplication. Decline only purely subjective preferences.

9. **After all fixes**:
   Run the project's linter and tests (see CLAUDE.md for commands).
   Stage and commit with a message following the project's commit conventions (see AGENTS.md).
   The commit type is `fix` and the description must summarize WHAT was fixed, not just reference the issue/PR.
   ```
   fix(<scope>): address review findings from PR #<PR_NUMBER>
   ```
   Example: `fix(ai): address review findings from PR #82`
   The scope comes from the area of code changed. Do NOT use generic messages like `feat: issue #N`.

10. **Reply to each comment on GitHub** -- keep replies SHORT and DIRECT:
    - Addressed (3+): `Fixed: [what changed].` or `Added [what].`
    - Declined (<3): brief explanation of why current code is sufficient.
    - No filler words, no 'Great catch!', no emojis.

11. **Resolve each thread** using GraphQL (for PR review comments only):
    ```bash
    gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
    ```

12. **Clear the handoff** after addressing all findings:
    ```bash
    rm -f .claude/agent-control/handoff.json
    ```

13. **Update memory**: Append lessons from comments scored 3+ to `.claude/agent-memory/review-feedback.md`.

14. **Print summary**:
    - Table: | Source | File | Score | Action |
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- NEVER use emojis in any output
- Handoff findings are the PRIMARY source -- always check the handoff file first
- If no findings from any source, report "No review comments to address" and exit
