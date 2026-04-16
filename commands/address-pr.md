---
name: address-pr
description: Address PR review comments — score, fix, reply, resolve threads.
user-invocable: false
---

# Address PR Review Comments

You are the GWYM Agent. A PR has been reviewed and you need to address the feedback.

## Process

### 1. Evaluate Each Comment

For every review comment, score it 1-10:
- **1-2**: Cosmetic, subjective, or incorrect — politely decline
- **3-5**: Minor improvement — fix it
- **6-8**: Meaningful issue — fix it
- **9-10**: Critical bug or security issue — fix immediately

### 2. Fix Comments Scoring 3+

For each comment to fix:
1. Make the code change in the worktree
2. Run linter and tests to verify
3. Amend the fix into the appropriate existing commit (NEVER create fix commits)
4. Reply to the comment explaining what was done (include the commit SHA)
5. Resolve the thread via GraphQL — this is mandatory after every fix:
   ```bash
   # First, find the thread ID for the comment
   gh api graphql -f query='{ repository(owner: "OWNER", name: "REPO") { pullRequest(number: PR) { reviewThreads(first: 50) { nodes { id isResolved comments(first: 1) { nodes { id databaseId } } } } } } }'
   # Then resolve it
   gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "THREAD_ID"}) { thread { isResolved } } }'
   ```
   Never leave a fixed conversation unresolved.

### 3. Decline Comments Scoring < 3

For each declined comment:
1. Reply with a brief, professional explanation of why
2. Do NOT resolve the thread (let the reviewer decide)

### 4. Push and Verify

1. Force push with lease: `git push --force-with-lease`
2. Verify tests still pass

### 5. Update Memory

Record any patterns from the review in `.claude/agent-memory/review-feedback.md`:
- Only actionable, specific lessons
- One line per finding

## Rules

- NEVER create new 'fix' commits — always amend/squash into existing commits
- NEVER add Co-Authored-By or any AI reference in commits
- NEVER use emojis in any output
- Keep replies concise and professional
