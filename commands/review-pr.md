---
name: review-pr
description: Review another person's pull request -- fetch, analyze, and post structured review on GitHub. Provide PR number.
user-invocable: true
category: pr
---

# Review PR

Act as a senior engineer reviewing a teammate's pull request. Provide a thorough, honest, constructive review that catches real problems and acknowledges good work. You are a domain expert in the project's tech stack and patterns (see AGENTS.md).

PR: $ARGUMENTS

## 1. Fetch PR State

Gather all PR data in parallel:

```bash
# Metadata
gh pr view <PR_NUMBER> --json title,body,author,state,additions,deletions,files,commits,reviewRequests,labels,baseRefName,headRefName,statusCheckRollup

# Full diff
gh pr diff <PR_NUMBER>

# Commits
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/commits --jq '.[] | "\(.sha) \(.commit.message)"'

# Top-level comments
gh pr view <PR_NUMBER> --json comments --jq '.comments[] | "---\n\(.author.login) (\(.createdAt)):\n\(.body)\n"'

# Inline review comments
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments --jq '.[] | "---\n\(.user.login) on \(.path):\(.line // .original_line) (\(.created_at)):\n\(.body)\n"'

# Reviews
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --jq '.[] | "\(.user.login) (\(.submitted_at)): \(.state)\n\(.body)\n"'

# CI checks
gh pr checks <PR_NUMBER>
```

Extract: author, linked issue, whether AI-generated (bot prefixes, agent comments).

**CI failures do NOT block the review.** If CI checks are failing, note the failures briefly in the review summary (what failed, likely cause if obvious) but proceed with the full code review. CI issues are a separate concern -- the review's job is to evaluate code quality, correctness, and design. A PR with failing CI still needs its code reviewed.

## 2. Cross-Reference Comment Threads vs Actual Code

For AI-generated PRs where agents may claim to have pushed fixes that never landed:

For each thread where someone said "Fixed in commit X":
1. Check if commit X exists in the current commit list
2. Verify the actual diff reflects the claimed change
3. Build a **ghost commit table** of any claimed-but-missing fixes

If ghost commits are found, this is a **blocking finding**.

## 3. Read Changed Files in Full

For every file touched in the diff:
- Read the **entire file** on the PR branch to understand surrounding context
- Identify the module's role in the architecture
- Note related files that interact with the changed code

Read related files as needed -- review with full understanding, not in isolation.

## 4. Deep Analysis

Review across these dimensions, in priority order. Reference `AGENTS.md` and `docs/*-guidelines.md` for project-specific rules.

### Security (Critical)
- Auth/permission checks correct?
- Tenant/scope isolation -- no cross-tenant data leaks?
- Input validation on all user-provided data?
- No injection risks?

### Correctness (Critical)
- Does the logic solve the stated problem?
- Edge cases: empty inputs, missing params, boundary values?
- Backward compatibility -- existing behavior still works?
- Error paths handled?

### Consistency (High)
- New code follows the same patterns as existing code?
- Similar operations handled the same way?
- Error messages consistent with existing format?

### Performance (High)
- N+1 query patterns?
- Queries inside loops?
- Large datasets without pagination?

### Test Coverage (Medium)
- New code paths covered?
- Edge cases and error paths tested?
- Tests assert meaningful behavior?

### Code Quality (Low)
- Business logic in the right layer?
- DRY -- duplicated logic?
- Dead code, unused imports?

## 5. Check Scope

- Does the PR include unrelated changes? Flag them.
- Is the PR too large? Suggest splitting if >500 lines of non-spec/non-test changes.
- Are all changes covered by the ticket scope?

## 6. Present Findings

### PR Summary
One paragraph: what the PR does, who authored it, how many commits/files.

### Ghost Commits (if any)
Table of claimed-but-missing fixes.

### Findings

Rank by impact (highest first):

```
[SEVERITY] Category -- Short title
Location: file_path:line_number
Problem: What is wrong and why it matters.
Suggestion: How to fix it, with code if helpful.
```

Severity: **CRITICAL** / **HIGH** / **MEDIUM** / **LOW**

Scoring guidance -- bump to 3+ (not 1-2) if the finding:
- Removes code or reduces duplication (less code = fewer bugs)
- Improves error handling (catches specific exceptions, removes silent failures)
- Fixes a doc inconsistency that misleads contributors or agents
- Eliminates dead code or unused imports

Reserve 1-2 only for purely subjective preferences: naming style, comment wording, formatting not caught by linter.

### Verdict

- **Approve** -- no blockers, findings are minor
- **Request changes** -- issues that must be resolved (list them)
- **Comment only** -- observations, no blocking opinion

### What's Done Well
Call out 2-3 things the code does well. Reinforce good patterns.

## 7. Post Review on GitHub

**Ask the user before posting.** Show the draft review first.

When approved:
```bash
# Comment or approve
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews \
  -f event=COMMENT \
  -f body="$(cat <<'EOF'
[REVIEW BODY]
EOF
)"
```

Report the review URL after posting.

## Cross-References

- **Reviewing your own code?** Use `/review` instead (self-review with auto-fix)
- **Need to address review comments on your PR?** Use `/address-pr`

## Rules

- Be constructive and specific. Every finding must have a concrete suggestion.
- Do not nitpick style if the code passes the project's linter.
- Do not invent problems. If the code is solid, say so.
- Do not review generated files (migrations, lock files) unless they look wrong.
- Respect the author's approach -- suggest alternatives only when there's a concrete problem.
- Keep the review concise.
- NEVER use emojis or icons in the review output.
