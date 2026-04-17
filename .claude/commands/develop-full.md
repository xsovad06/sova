---
name: develop-full
description: Full development workflow -- TDD, lint, test, self-review, commit organization. Provide a ticket ID or task description.
user-invocable: true
---

# Full Development Workflow

Develop a feature or fix end-to-end with TDD, testing, self-review, and clean commit history.

## Instructions

### Phase 0: Understand the Task

1. Get the task from `$ARGUMENTS` -- either a GitHub Issue number or a task description.
2. If it's a GitHub Issue, fetch details:
   ```bash
   gh issue view <ISSUE_NUMBER>
   ```
3. Read the project's CLAUDE.md for conventions and patterns.
4. Read agent memory files if they exist:
   - `.claude/agent-memory/MEMORY.md`
   - `.claude/agent-memory/learnings.md`
   - `.claude/agent-memory/review-feedback.md`
   - `.claude/agent-memory/common-mistakes.md`
5. Identify which module(s) this work touches and read relevant source code.

### Phase 1: Develop (TDD)

1. **Write tests first** -- define expected behavior before implementation.
2. **Implement the solution** -- follow existing codebase conventions.
3. **Run the project's linter** (see CLAUDE.md for commands).
4. **Run the project's tests** (see CLAUDE.md for commands).
5. If tests fail, fix and re-run (up to 3 attempts).

### Phase 2: Self-Review

1. Review your own diff: `git diff`
2. Check for:
   - Bugs, edge cases, off-by-one errors
   - Missing test coverage
   - Security concerns
   - Code style consistency with the rest of the module
   - Unnecessary changes or leftover debug code
3. Auto-fix any findings scored >= 3/10.
4. Re-run tests after fixes.

### Phase 3: Organize Commits

1. **Separate concerns** -- schema/model changes, core logic, tests, config in distinct commits.
2. **Each commit should be self-contained** and reviewable on its own.
3. **Commit message format**: `type(scope): description`
   - Types: feat, fix, refactor, test, docs, chore, perf
   - Scopes: agent, dashboard, commands, personas, invariants, knowledge, cli, docs
   - Example: `feat(agent): add handle-pr mode to orchestrator`
4. **NEVER create a single monolithic commit** with all changes.
5. Commit messages should explain the 'why', not just the 'what'.

### Phase 4: Summary

Write a file at `.agent-summary.md` in the working directory with:

- **Big picture**: 3-5 sentence narrative explaining the problem, why it matters, and why this approach. Written for someone unfamiliar with the ticket.
- **What changed**: Brief description of all changes.
- **How it worked before / How it works now**: Behavior delta.
- **Manual test instructions**: Exact commands, expected outputs, edge cases, what success/failure looks like.
- **Files changed**: List with one-line descriptions.

## Rules

- Follow patterns from the project's existing code and CLAUDE.md
- NEVER use emojis in any output
