---
name: ingest-review
description: Ingest PR review feedback into agent memory for continuous learning. Provide PR number.
user-invocable: true
category: learning
---

# Ingest PR Review Feedback

Process review comments from a merged PR and update agent memory.

## Instructions

1. Get the PR number from `$ARGUMENTS`. If empty, ask the user.

2. Fetch PR data:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
   ```

3. **Fetch external review tool findings** (if the project has `[external_reviews]` configured in `sova.toml` with `enabled = true`):

   **SonarCloud** (if `"sonarcloud"` in `tools` and `project_key` is set):
   ```bash
   curl -sf -H "Authorization: Bearer $SONAR_TOKEN" \
     "https://sonarcloud.io/api/issues/search?componentKeys=<PROJECT_KEY>&pullRequest=<PR_NUMBER>&resolved=true&ps=500"
   ```
   Note: use `resolved=true` here (not false) because we're ingesting from an already-merged PR.
   These are the issues that were found AND fixed during the PR lifecycle.

   **CodeRabbit** (if `"coderabbit"` in `tools`):
   Fetch all review threads (both resolved and unresolved) to see the full picture:
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
   Filter for threads authored by `coderabbitai`, `coderabbitai[bot]`, or `coderabbit[bot]`.

4. Analyze review comments and extract lessons:
   - **Patterns to always follow** -- things reviewers praised or requested
   - **Mistakes to avoid** -- bugs caught, missing edge cases, style violations
   - **Style preferences** -- formatting, naming, structural preferences
   - **Test coverage gaps** -- missing assertions, untested scenarios
   - **External tool patterns** (if external findings were fetched):
     - Group findings by tool (CodeRabbit vs SonarCloud) and category (security, style, correctness, performance)
     - Identify file hotspots -- files/directories with 3+ findings are candidates for `.coderabbit.yaml` `path_instructions`
     - Check for recurrence -- has this rule/pattern appeared in previous PRs?
     - For SonarCloud, the rule key (e.g., `python:S8415`) identifies the pattern
     - For CodeRabbit, look for repeated phrases in finding bodies across PRs
     - Rules that fired 2+ times -> candidate for cookbook entry
     - New rule categories not seen before -> new cookbook entry under matching domain

5. Read existing memory file:
   - `.claude/agent-memory/cookbook.md`

6. Update `.claude/agent-memory/cookbook.md`:
   - Append new findings under the matching domain section (no duplicates)
   - If a mistake has appeared before, add it to the "Common Mistakes" section with `[Nx]` count
   - If a finding is high-impact, add it to `MEMORY.md`
   - For external tool findings (if fetched), use the "External Review Tools" section if it exists, otherwise create one. Include the tool name and rule key for traceability:
     `- **<Pattern description>** -- <explanation>. SonarCloud rule: <rule_key> / CodeRabbit finding on PR #<N>. [confirmed: 0]`

7. Report what was learned and which files were updated.

## Cross-References

- **Run automatically after merge**: `/after-merge` includes this step
- **Extract broader knowledge**: Run `/extract-knowledge` for session-wide lessons

## Rules

- Only record actionable, specific lessons -- not generic advice
- NEVER use emojis in any output
