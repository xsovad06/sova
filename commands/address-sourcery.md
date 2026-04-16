---
name: address-sourcery
description: Address Sourcery AI review comments and request another round of review.
user-invocable: true
---

# Address Sourcery AI Comments

Address all Sourcery AI code review comments on the current PR, then request a new review.

## Instructions

### Step 1: Get PR Information

```bash
gh pr view --json number,url,headRefName
```

### Step 2: Fetch Sourcery AI Review Comments

```bash
gh pr view --comments
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments
```

Look for comments from `sourcery-ai` or `sourcery-ai[bot]`.

### Step 3: Analyze Each Comment

For each Sourcery AI suggestion:
1. **Read the suggestion carefully**
2. **Understand the reasoning** behind it
3. **Decide**: Accept, modify, or skip with justification

### Step 4: Address Comments

For each suggestion, do ONE of the following:

#### A. Accept and Implement
If the suggestion improves the code:
- Make the suggested change
- Ensure it doesn't break tests

#### B. Implement with Modification
If the suggestion is good but needs adjustment:
- Implement a modified version
- Document why you modified it

#### C. Skip with Justification
If the suggestion doesn't apply:
- Document why it was skipped
- Valid reasons: project conventions differ, context-specific, would break functionality, false positive

### Step 5: Run Tests

After making changes, run the project's linter and tests (see CLAUDE.md for commands).
Fix any issues before proceeding.

### Step 6: Commit Changes

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: address Sourcery AI review suggestions

Changes made:
- [List each change made]

Suggestions skipped (with justification):
- [List any skipped suggestions and why]
EOF
)"
```

### Step 7: Push Changes

```bash
git push origin $(git branch --show-current)
```

### Step 8: Request New Review

```bash
gh pr comment --body "@sourcery-ai review"
```

### Step 9: Report Summary

- Number of suggestions addressed
- Number of suggestions skipped (with reasons)
- Link to the updated PR
- Confirmation that new review was requested

## Cross-References

- **Human review comments too?** Use `/address-pr` for human reviewer comments
- **Want to learn from the feedback?** Run `/ingest-review <PR_NUMBER>` after merge

## Rules

- **Don't blindly accept all suggestions** -- Evaluate each one critically
- **Maintain code consistency** -- Follow existing project patterns
- **Keep tests passing** -- Never push broken code
- **Document skipped suggestions** -- Explain why in the commit message
- NEVER use emojis in any output
