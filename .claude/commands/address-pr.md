---
name: address-pr
description: Address PR review comments -- score, fix or acknowledge, reply, resolve threads. Provide PR number.
user-invocable: true
---

# Address PR Review Comments

Score each review comment, address all of them (fix or acknowledge with justification), and reply on GitHub.

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

4. **Check for SonarCloud quality gate failure** (only when SonarCloud is configured for this repo):
   ```bash
   # Check if SonarCloud checks exist and their status
   gh pr checks <PR_NUMBER> 2>/dev/null | grep -i 'sonar'
   ```

   If any SonarCloud check shows `fail`:
   a. Parse the SonarCloud bot comment (user `sonarqubecloud` or `sonarcloud[bot]`) from the PR comments fetched in step 3 for specific failed conditions (e.g., "50.0% Coverage on New Code (required >= 80%)").
   b. For **coverage failures** (the most common):
      - Identify which Python files this PR changed:
        ```bash
        gh pr diff <PR_NUMBER> --name-only | grep '\.py$'
        ```
      - Run coverage locally scoped to the changed files to find uncovered lines:
        ```bash
        pytest tests/ --cov=sova --cov-report=term-missing -q 2>/dev/null | grep -E '^sova/' | grep -v '100%'
        ```
      - Cross-reference the uncovered lines with the PR diff to focus on **new** uncovered code (not pre-existing gaps).
      - Write tests for the uncovered new code. Place tests in the existing test file for that module, or create a new test file following the project's test conventions.
      - Score: 6/10 (blocks mergeability via quality gate).
   c. For **duplication failures**: identify and refactor duplicated code blocks. Score: 4/10.
   d. For **new issue failures**: follow the SonarCloud link to identify specific issues, fix them. Score: 5-8/10 depending on severity.
   e. If SonarCloud is NOT present in `gh pr checks` output, skip this step entirely (the project does not use SonarCloud).

5. **Merge all sources**: combine handoff findings, PR review comments, and SonarCloud findings.
   Deduplicate by file+description. Handoff findings take priority (they have pre-scored severity).

6. Find the working directory -- check for an existing worktree or create one:
   ```bash
   git worktree list
   ```

7. **Score each comment 1-10** (skip for handoff findings which are pre-scored):
   - 1-2: low-priority -- style preference, minor naming, formatting
   - 3-5: moderate -- DRY violations, missing edge cases, error handling
   - 6-8: important -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

   Scoring determines priority order, not whether to fix. All findings are addressed.

8. **Address all findings** (regardless of score): Fix the issue in the code.
   - If you CANNOT fix it (needs architectural decision, unclear requirements): add to NEEDS_HUMAN_INPUT list.
   - If a finding is a false positive, not applicable in context, or requires a human decision: acknowledge it with a one-line justification instead of fixing. Do not skip findings based on score alone.

9. **Scout check**: while fixing findings, scan each touched file for pre-existing issues -- failing tests, lint warnings, dead imports, obvious bugs adjacent to your changes. Fix them alongside the review findings. Keep scout fixes small and low-risk.

10. **After all fixes**:
    Run the project's linter and tests (see CLAUDE.md for commands).
    Stage and commit with a message following the project's commit conventions (see AGENTS.md).
    The commit type is `fix` and the description must summarize WHAT was fixed, not just reference the issue/PR.
    ```
    fix(<scope>): address review findings from PR #<PR_NUMBER>
    ```
    Example: `fix(ai): address review findings from PR #82`
    The scope comes from the area of code changed. Do NOT use generic messages like `feat: issue #N`.

11. **Rearrange commits**: run the `/rearrange-commits` workflow to squash the fix commit into the original feature commits. The PR should read as clean feature development with no review-fix artifacts.

12. **Rebase onto base branch** to ensure the PR is mergeable after fixes:
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

13. **Push and wait for CI**:
    ```bash
    git push --force-with-lease
    ```
    After pushing, poll CI until it completes (max 10 minutes):
    ```bash
    gh run list --branch $(git branch --show-current) --limit 1 --json databaseId,status,conclusion
    ```
    If CI fails, analyze the failure, fix, amend the commit, and re-push (max 2 retries).
    If CI passes, continue. If still pending after max wait, report status and continue anyway.

14. **Reply to each comment on GitHub** -- keep replies SHORT and DIRECT:
    - Fixed: `Fixed: [what changed].` or `Added [what].`
    - Acknowledged (not fixed): `Acknowledged -- [justification: false positive / not applicable / needs human input].`
    - No filler words, no 'Great catch!', no emojis.

15. **Resolve each thread** using GraphQL (for PR review comments only):
    ```bash
    gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
    ```

16. **Dismiss stale bot reviews** that block mergeability. After addressing all findings, check for `CHANGES_REQUESTED` reviews from bots (CodeRabbit, etc.) and dismiss them:
    ```bash
    # List reviews with CHANGES_REQUESTED state from bots
    gh api repos/<REPO>/pulls/<PR_NUMBER>/reviews --jq '.[] | select(.state == "CHANGES_REQUESTED") | select(.user.type == "Bot") | "\(.id) | \(.user.login)"'

    # Dismiss each one with a summary of what was addressed
    gh api -X PUT repos/<REPO>/pulls/<PR_NUMBER>/reviews/<REVIEW_ID>/dismissals \
      -f message="Findings addressed: [brief summary]."
    ```
    Only dismiss bot reviews whose findings have been addressed or acknowledged. Never dismiss human reviews.

17. **Clear the handoff** after addressing all findings:
    ```bash
    rm -f .claude/agent-control/handoff.json
    ```

18. **Update memory**: Append lessons learned to `.claude/agent-memory/cookbook.md` (under matching domain section).

19. **Print summary**:
    - Table: | Source | File | Score | Action |
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- NEVER use emojis in any output
- Handoff findings are the PRIMARY source -- always check the handoff file first
- SonarCloud quality gate failures are BLOCKING -- they prevent PR mergeability. Treat coverage gaps on new code as real findings (score 6/10), not informational notes.
- If no findings from any source (handoff, reviews, SonarCloud), report "No review comments to address" and exit
