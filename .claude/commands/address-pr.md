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

4. **Fetch external review tool findings** (if configured):
   Read `sova.toml` to check if `[external_reviews]` is present and `enabled = true`. If absent or disabled, skip this step.

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
   Save thread IDs for resolution in step 15.

5. **Merge all sources**: combine handoff findings, PR review comments, and external tool findings.
   Deduplicate by source+file+line across all sources. If two sources flag the same line with the same issue, keep the one with the higher score. Handoff findings take priority (they have pre-scored severity).
   External tool findings use the tool name (CodeRabbit, SonarCloud) as the source label.

6. Find the working directory -- check for an existing worktree or create one:
   ```bash
   git worktree list
   ```

7. **Score each comment 1-10** (skip for handoff findings and external tool findings which are pre-scored):
   - 1-2: low-priority -- style preference, minor naming, formatting
   - 3-5: moderate -- DRY violations, missing edge cases, error handling
   - 6-8: important -- potential bugs, missing validation, test gaps
   - 9-10: critical -- security, data loss, correctness bugs

   Scoring determines priority order, not whether to fix. All findings are addressed.

8. **Address all findings** (regardless of score) -- FIX FIRST, REPLY LATER:
   For each finding:
   a. **Read the file** at the indicated location to understand the full context.
   b. **Evaluate**: is it a real issue, false positive, or needs human input?
   c. **If real**: apply the fix in the code. Then **verify the fix** by re-reading the changed lines to confirm the issue is resolved. Do NOT claim "Fixed" without verifying.
   d. **If false positive**: note the justification (will be used in the reply).
   e. **If cannot fix**: add to NEEDS_HUMAN_INPUT list.

   CRITICAL: Do NOT reply to comments or resolve threads until the code is fixed, tested, and pushed. Replying "Fixed" before the fix exists is the primary cause of review loops -- external reviewers re-scan the code after push and will re-open findings that weren't actually fixed.

9. **Scout check**: while fixing findings, scan each touched file for pre-existing issues -- failing tests, lint warnings, dead imports, obvious bugs adjacent to your changes. Fix them alongside the review findings. Keep scout fixes small and low-risk.

10. **After all fixes -- verify before proceeding**:
    Run the project's linter and tests (see CLAUDE.md for commands).
    If tests or linter fail, fix them before continuing. Do NOT proceed with broken code.
    Stage and commit with a message following the project's commit conventions (see AGENTS.md).
    The commit type is `fix` and the description must summarize WHAT was fixed, not just reference the issue/PR.
    ```
    fix(<scope>): address review findings from PR #<PR_NUMBER>
    ```
    Example: `fix(ai): address review findings from PR #82`
    The scope comes from the area of code changed. Do NOT use generic messages like `feat: issue #N`.

11. **Rearrange commits**: run the `/rearrange-commits` workflow to squash the fix commit into the original feature commits. The PR should read as clean feature development with no review-fix artifacts.

12. **Push and wait for CI**:
    ```bash
    git push --force-with-lease
    ```
    After pushing, poll CI until it completes (max 10 minutes):
    ```bash
    gh run list --branch $(git branch --show-current) --limit 1 --json databaseId,status,conclusion
    ```
    If CI fails, analyze the failure, fix, amend the commit, and re-push (max 2 retries).
    If CI passes, continue. If still pending after max wait, report status and continue anyway.

13. **Verify GitHub auth** before replying or resolving threads:
    ```bash
    gh auth status
    ```
    Confirm the active account matches `github_user` in `sova.toml`. If not, switch with `gh auth switch --user <target>`. The `resolveReviewThread` mutation returns FORBIDDEN if the wrong account is active.

14. **Reply to each comment on GitHub ONLY AFTER push succeeds** -- keep replies SHORT and DIRECT:
    - Fixed: `Fixed: [what changed].` or `Added [what].`
    - Acknowledged (not fixed): `Acknowledged -- [justification: false positive / not applicable / needs human input].`
    - No filler words, no 'Great catch!', no emojis.
    - For CodeRabbit threads, reply directly to the thread:
      ```bash
      gh api graphql -f query='mutation($body: String!, $tid: ID!) {
        addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $tid, body: $body}) {
          comment { id }
        }
      }' -f "body=Fixed: <description>" -f "tid=<THREAD_ID>"
      ```

15. **Resolve each thread** using GraphQL (for PR review comments and CodeRabbit threads):
    ```bash
    gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
    ```
    Only resolve threads for findings that were actually fixed AND verified in the pushed code.
    Do NOT resolve threads for acknowledged findings -- let the reviewer decide.
    SonarCloud issues auto-resolve on the next scan after the fix is pushed -- no manual resolution needed.

16. **Clear the handoff** after addressing all findings:
    ```bash
    rm -f .claude/agent-control/handoff.json
    ```

17. **Update memory**: Append lessons learned to `.claude/agent-memory/cookbook.md` (under matching domain section).

18. **Print summary**:
    - Table: | Source | File | Score | Action | -- where Source is one of: SOVA reviewer, human reviewer, CodeRabbit, SonarCloud
    - Status: `ALL_RESOLVED` or `NEEDS_HUMAN_INPUT` (with bullet list of items needing input)

## Cross-References

- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge
- **Need to reorganize commits after fixes?** Run `/rearrange-commits`

## Rules

- NEVER use emojis in any output
- Handoff findings are the PRIMARY source -- always check the handoff file first
- If no findings from any source, report "No review comments to address" and exit
- NEVER reply "Fixed" to a comment without first applying AND verifying the code change
- NEVER resolve a thread before the fix is pushed -- external reviewers re-scan after push
- Treat CodeRabbit and SonarCloud findings with the same rigor as human reviewer findings -- read the code, understand the issue, fix it properly
- If a finding seems wrong, verify against the actual code before dismissing as false positive
