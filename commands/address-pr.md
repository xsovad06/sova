---
name: address-pr
description: Address PR review comments (human and bot) -- score, fix or acknowledge, reply, resolve threads. Provide PR number.
user-invocable: true
category: pr
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

5. **Fetch external review tool findings** (if the project has `[external_reviews]` configured in `sova.toml` with `enabled = true`):

   **SonarCloud** (if `"sonarcloud"` in `tools` and `project_key` is set):
   ```bash
   curl -sf -H "Authorization: Bearer $SONAR_TOKEN" \
     "https://sonarcloud.io/api/issues/search?componentKeys=<PROJECT_KEY>&pullRequest=<PR_NUMBER>&resolved=false&ps=500"
   ```
   Parse the JSON response. For each issue in `.issues[]`:
   - `file_path`: split `.component` on `":"` and take the second part
   - `line`: `.line`
   - `severity`: `.severity` (BLOCKER/CRITICAL/MAJOR/MINOR/INFO)
   - `message`: `.message`
   - `tool_id`: `.key`

   Map SonarCloud severity to the 1-10 score: BLOCKER=10, CRITICAL=8, MAJOR=6, MINOR=3, INFO=1.

   **CodeRabbit** (if `"coderabbit"` in `tools`):
   ```bash
   gh api graphql -f query='
   query($owner: String!, $name: String!, $pr: Int!) {
     repository(owner: $owner, name: $name) {
       pullRequest(number: $pr) {
         reviewThreads(first: 100) {
           pageInfo { hasNextPage endCursor }
           nodes {
             id
             isResolved
             path
             line
             comments(first: 1) {
               nodes {
                 body
                 author { login }
               }
             }
           }
         }
       }
     }
   }' -F owner=<OWNER> -F name=<REPO> -F pr=<PR_NUMBER>
   ```
   If `pageInfo.hasNextPage` is true, re-run the query with `reviewThreads(first: 100, after: "<endCursor>")` until all threads are fetched.
   Filter for: `isResolved == false` AND `author.login` in (`coderabbitai`, `coderabbitai[bot]`, `coderabbit[bot]`).
   Extract: path, line, comment body (truncate to 500 chars), thread ID.
   Score by parsing the CodeRabbit header format (e.g., `_Potential issue_ | _Major_`): "Critical" = 8, "Major" = 6, "Minor" = 3, otherwise = 4. Use the header label, not keyword search in the body.
   Save thread IDs for resolution in step 14.

   Merge external findings with the PR review comments. Deduplicate by source+file+line across all sources. If two sources flag the same line with the same issue, keep the one with the higher score. External tool findings use the tool name (CodeRabbit, SonarCloud) as the source label.

6. **Score each comment 1-10** (skip for external tool findings which are pre-scored in step 5):
   - 1-2: low-priority -- style preference, minor naming, formatting
   - 3-5: moderate -- DRY violations, missing edge cases, error handling
   - 6-8: important -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

   Scoring determines priority order, not whether to fix. All findings are addressed.

   For bot suggestions: evaluate critically -- don't blindly accept. Bots lack project context and may suggest changes that conflict with project conventions.

7. **Address all findings** (regardless of score) -- FIX FIRST, REPLY LATER:
   For each finding:
   a. **Read the file** at the indicated location to understand the full context.
   b. **Evaluate**: is it a real issue, false positive, or needs human input?
   c. **If real**: apply the fix in the code. Then **verify the fix** by re-reading the changed lines to confirm the issue is resolved. Do NOT claim "Fixed" without verifying.
   d. **If false positive**: note the justification (will be used in the reply).
   e. **If cannot fix**: add to NEEDS_HUMAN_INPUT list.

   CRITICAL: Do NOT reply to comments or resolve threads until the code is fixed, tested, and pushed. Replying "Fixed" before the fix exists is the primary cause of review loops -- external reviewers re-scan the code after push and will re-open findings that weren't actually fixed.

8. **Scout check**: while fixing findings, scan each touched file for pre-existing issues -- failing tests, lint warnings, dead imports, obvious bugs adjacent to your changes. Fix them alongside the review findings. Keep scout fixes small and low-risk.

9. **After all fixes -- verify before proceeding**:
   Run the project's linter and tests (see CLAUDE.md for commands).
   If tests or linter fail, fix them before continuing. Do NOT proceed with broken code.
   Stage and commit with a message following the project's commit conventions (see AGENTS.md).
   The commit type is `fix` and the description must summarize WHAT was fixed, not just reference the issue/PR.
   ```
   fix(<scope>): address review findings from PR #<PR_NUMBER>
   ```
   Example: `fix(ai): address review findings from PR #82`
   The scope comes from the area of code changed. Do NOT use generic messages like `feat: issue #N`.

10. **Rearrange commits**: run the `/rearrange-commits` workflow to squash the fix commit into the original feature commits. The PR should read as clean feature development with no review-fix artifacts.

11. **Push and wait for CI**:
    ```bash
    git push --force-with-lease
    ```
    After pushing, poll CI until it completes (max 10 minutes):
    ```bash
    gh run list --branch $(git branch --show-current) --limit 1 --json databaseId,status,conclusion
    ```
    If CI fails, analyze the failure, fix, amend the commit, and re-push (max 2 retries).
    If CI passes, continue. If still pending after max wait, report status and continue anyway.

12. **Verify GitHub auth** before replying or resolving threads:
    ```bash
    gh auth status
    ```
    Confirm the active account has write access to the repo. If multiple accounts are configured, switch with `gh auth switch --user <target>`. The `resolveReviewThread` mutation returns FORBIDDEN if the wrong account is active.

13. **Reply to each comment on GitHub ONLY AFTER push succeeds** -- keep replies SHORT and DIRECT:
    - Fixed: `Fixed: [what changed].` or `Added [what].`
    - Acknowledged (not fixed): `Acknowledged -- [justification: false positive / not applicable / needs human input].`
    - No filler words, no 'Great catch!', no emojis.
    - For CodeRabbit threads (if external reviews are configured), reply directly to the thread:
      ```bash
      gh api graphql -f query='mutation($body: String!, $tid: ID!) {
        addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $tid, body: $body}) {
          comment { id }
        }
      }' -f "body=Fixed: <description>" -f "tid=<THREAD_ID>"
      ```

14. **Resolve each thread** using GraphQL:
    ```bash
    gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
    ```
    Only resolve threads for findings that were actually fixed AND verified in the pushed code.
    Do NOT resolve threads for acknowledged findings -- let the reviewer decide.
    SonarCloud issues auto-resolve on the next scan after the fix is pushed -- no manual resolution needed.

15. **Request bot re-review** (if bot comments were addressed):
    ```bash
    # Sourcery AI
    gh pr comment <PR_NUMBER> --body "@sourcery-ai review"
    # CodeRabbit
    gh pr comment <PR_NUMBER> --body "@coderabbitai review"
    ```
    Only request re-review for bots whose comments were actually addressed.

16. **Update memory**: Append lessons learned to `.claude/agent-memory/cookbook.md` (under matching domain section).

17. **Print summary**:
    - Table: | Comment | Source | Score | Action | -- where Source is one of: human reviewer, Sourcery, CodeRabbit, SonarCloud
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- **Don't blindly accept bot suggestions** -- evaluate each one against project conventions
- NEVER use emojis in any output
- NEVER reply "Fixed" to a comment without first applying AND verifying the code change
- NEVER resolve a thread before the fix is pushed -- external reviewers re-scan after push
- Treat bot reviewer findings with the same rigor as human reviewer findings -- read the code, understand the issue, fix it properly
- If a finding seems wrong, verify against the actual code before dismissing as false positive
