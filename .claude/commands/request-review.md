---
description: Draft a Slack message to request PR review from the team.
---

Draft a Slack message for requesting a PR review. Can be used with a PR number argument or auto-detect from the current branch.

## Instructions

1. **Identify the PR**:
   - If a PR number is provided as argument, use it: `gh pr view $ARGUMENTS --json title,url,body`
   - Otherwise, detect from current branch: `gh pr view --json title,url,body`
   - If no PR found, inform the user

2. **Extract key info**:
   - PR title and URL
   - One-line summary of what the PR does (from the title or body, in plain language)

3. **Draft the Slack message**:
   - Format: casual, very short — tag the team's review group (for example `@reviewers`), a few words from the PR title, PR link
   - Vary the greeting naturally (Hi, Hey, Hola, etc.)
   - No emojis, no bullet points, no formatting — keep it under 20 words (excluding the link)
   - Examples of good messages:
     - `Hi @reviewers, PR for review — add search indexes: <link>`
     - `Hola @reviewers, PR for review — audit logging for settings changes: <link>`
     - `Hey @reviewers, PR for review — retry flaky webhook delivery: <link>`

4. **Move JIRA tickets to Code Review**:
   - Extract JIRA ticket numbers from the PR title/body (format: `[PROJ-XXXXX]` or JIRA links)
   - Move each ticket: `jira issue move PROJ-XXXXX "Code Review"`
   - Report which tickets were moved

5. **Output**:
   - Print the draft message in a code block so the user can copy-paste it directly
   - Report which JIRA tickets were moved to Code Review
