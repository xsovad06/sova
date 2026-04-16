---
name: develop-full
description: Full development workflow for GWYM — TDD, lint, test, self-review, commit organization.
user-invocable: false
---

# Full Development Workflow (GWYM Agent)

Develop a feature or fix end-to-end with TDD, testing, self-review, and clean commit history.

## Instructions

### Phase 0: Understand the Task

1. Get the task from `$ARGUMENTS` — a GitHub issue number or task description.
2. If it's an issue number, fetch details:
   ```bash
   gh issue view <NUMBER> --json title,body,labels,milestone
   ```
3. Read the project's CLAUDE.md for conventions and workflow.
4. Read `.claude/rules/` knowledge files for patterns and gotchas:
   - `architecture.md` — app structure, key paths, architectural decisions
   - `patterns.md` — Django/Python/QuerySet/Git gotchas
   - `ui-patterns.md` — Chart.js, CSS/theme, HTMX patterns (if UI work)
   - `testing.md` — fixtures, factories, testing conventions
   - `models.md` — model-specific patterns and components (if model work)
5. Read agent memory files if they exist:
   - `.claude/agent-memory/MEMORY.md`
   - `.claude/agent-memory/learnings.md`
   - `.claude/agent-memory/review-feedback.md`
   - `.claude/agent-memory/common-mistakes.md`
6. For domain-specific deep dives, load relevant skills:
   - `/architecture-overview` — full data model, request flow, URL namespace
   - `/import-patterns` — import wizards, classification, adapters (if import work)
7. Identify which app(s) this work touches and read relevant source code.

### Phase 1: Develop (TDD)

1. **Write tests first** — define expected behavior before implementation.
   - Tests in `apps/<app>/tests/test_<module>.py`
   - Use `factory_boy` factories from `apps/<app>/tests/factories.py`
2. **Implement the solution** — follow existing codebase conventions.
   - Models: `apps/<app>/models.py`
   - Business logic: `apps/<app>/services.py`
   - API: `apps/<app>/api.py` (Django Ninja)
   - Views: `apps/<app>/views.py`
3. **Run the linter**:
   ```bash
   make lint
   ```
4. **Run tests**:
   ```bash
   make test
   ```
5. If tests fail, fix and re-run (up to 3 attempts).

### Phase 2: Self-Review

1. Review your own diff: `git diff`
2. Check for:
   - Bugs, edge cases, off-by-one errors
   - Missing test coverage (>= 80% required)
   - Security: user scoping on all queries, no injection risks
   - N+1 queries: missing select_related/prefetch_related
   - Template .count in loops (use annotate instead)
   - Decimal for money (never float)
   - Type hints on all functions
   - Code style consistency with the rest of the codebase
   - Unnecessary changes or leftover debug code
3. Fix any issues found and re-run tests.

### Phase 3: Organize Commits

Organize changes into logical, atomic commits:

1. **Separate concerns** — model changes, core logic, tests, config in distinct commits.
2. **Each commit should be self-contained** and reviewable on its own.
3. **Commit message format**: `type(scope): description`
   - Types: feat, fix, refactor, test, docs, chore
   - Example: `feat(imports): add CSV validation for bank transactions`
4. **NEVER create a single monolithic commit** with all changes.
5. **NEVER add Co-Authored-By or any AI/Claude reference** in commits.
6. Commit messages should explain the 'why', not just the 'what'.

### Phase 4: Summary

Write a file at `.agent-summary.md` in the working directory with:

- **Big picture**: 3-5 sentence narrative explaining the problem, why it matters, and why this approach.
- **What changed**: Brief description of all changes.
- **How it worked before / How it works now**: Behavior delta.
- **Manual test instructions**: Exact commands, expected outputs, edge cases.
- **Files changed**: List with one-line descriptions.

## Important

- Follow patterns from CLAUDE.md and docs/python-standards.md.
- Use Decimal for monetary values, never float.
- All querysets filtered by request.user.
- Type hints required on all function signatures.
- Line length: 140 chars max.
- f-strings only (no % or .format).
- NEVER use emojis in any output.
