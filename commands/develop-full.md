---
name: develop-full
description: Full development workflow -- TDD, lint, test, self-review, commit organization. Provide a ticket ID or task description.
user-invocable: true
category: core
inputs:
  - issue_number
  - task_description
outputs:
  - files_changed
  - test_results
  - branch_name
  - pr_number
---

# Full Development Workflow

Develop a feature or fix end-to-end with TDD, testing, self-review, and clean commit history.

## Instructions

### Phase 0: Understand the Task

1. Get the task from `$ARGUMENTS` -- either a GitHub Issue number or a task description.
2. If it's an issue number, fetch details:
   ```bash
   gh issue view <ISSUE_NUMBER> --json number,title,body,labels,milestone
   ```
3. **Claim the issue** (GitHub Issues only):
   ```bash
   # Assign to self
   gh issue edit <ISSUE_NUMBER> --add-assignee @me
   ```
   If the project uses a GitHub Projects board, move the issue to "In Progress".
4. Read the project's CLAUDE.md and AGENTS.md for conventions and patterns.
5. Read agent memory files if they exist:
   - `.claude/agent-memory/MEMORY.md`
   - `.claude/agent-memory/cookbook.md`
6. **Check for spec** (issue numbers only): look for `.claude/specs/{issue-number}-*.md`.
   - If found with `Status: approved`: read it. Report: "Using approved spec: {filename}". The `/develop` step will follow this spec as its primary guide.
   - If found with `Status: draft`: warn the user: "Draft spec exists at {filename} but is not approved. Run `/spec {issue}` to review and approve it, or proceed without spec guidance." Wait for user confirmation before continuing.
   - If not found: continue silently. Specs are optional -- not every task needs one.
7. Identify which module(s) this work touches and read relevant source code.

### Phase 1: Develop (TDD)

Follow the `/develop` command workflow:

1. **Write tests first** -- define expected behavior before implementation.
2. **Implement the solution** -- follow existing codebase conventions.
3. **Scout check** -- for every file touched, fix pre-existing issues you notice (failing tests, lint warnings, dead code, obvious bugs). Keep fixes small and low-risk.
4. **Run the project's linter** (see CLAUDE.md for commands).
5. **Run the project's tests** (see CLAUDE.md for commands).
6. If tests fail, fix and re-run (up to 3 attempts).

### Phase 2: Self-Review

Follow the `/review` command workflow:

1. Review your own diff: `git diff`
2. Check for:
   - Bugs, edge cases, off-by-one errors
   - Missing test coverage
   - Security concerns
   - Code style consistency with the rest of the module
   - Unnecessary changes or leftover debug code
3. Address all findings (fix or acknowledge with justification).
4. Re-run tests after fixes.

### Phase 3: Organize Commits

Follow the `/rearrange-commits` command approach:

1. **Separate concerns** -- schema/model changes, core logic, tests, config in distinct commits.
2. **Each commit should be self-contained** and reviewable on its own.
3. **Commit message format**: `type(scope): description`
   - Types: feat, fix, refactor, test, docs, chore, perf
   - Example: `feat(auth): add token refresh endpoint`
4. **NEVER create a single monolithic commit** with all changes.
5. Commit messages should explain the 'why', not just the 'what'.

### Phase 4: Summary

Write a file at `.agent-summary.md` in the working directory with:

- **Big picture**: 3-5 sentence narrative explaining the problem, why it matters, and why this approach. Written for someone unfamiliar with the ticket.
- **What changed**: Brief description of all changes.
- **How it worked before / How it works now**: Behavior delta.
- **Manual test instructions**: Exact commands, expected outputs, edge cases, what success/failure looks like.
- **Files changed**: List with one-line descriptions.

## Workflow Chain

This command orchestrates:
1. `/develop` -- implement with TDD
2. `/test` -- verify linter + tests pass
3. `/review` -- self-review and auto-fix
4. `/rearrange-commits` -- organize into logical commits

After this command completes, run `/pr` to create the pull request.

## Rules

- Follow patterns from the project's existing code and CLAUDE.md/AGENTS.md
- NEVER use emojis in any output
