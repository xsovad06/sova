---
name: coding-standards
description: Review current changes against the project's coding conventions (from AGENTS.md).
user-invocable: true
---

# Coding Standards Review

Review the current changes against the project's coding conventions.
Reference: AGENTS.md and any `docs/*-guidelines.md` files for the full standard.

**Scope**: $ARGUMENTS

## Review Checklist

### 1. Gather Changes
- Run `git diff` to see unstaged changes
- Run `git diff --cached` to see staged changes
- If scope specifies files/modules, focus on those

### 2. Language and Framework Standards
Check each changed file against conventions from AGENTS.md:
- [ ] **Line length** within project limit
- [ ] **Type annotations** on function signatures (if project requires them)
- [ ] **Naming conventions** match project style (snake_case, camelCase, PascalCase as applicable)
- [ ] **Business logic placement** in the correct architectural layer (services, not views/controllers)
- [ ] **No hardcoded secrets** -- sensitive values in env vars / settings
- [ ] **Proper imports** organized per project convention
- [ ] **No unused imports** or dead code
- [ ] **No debug code** or print statements left behind

### 3. Security
- [ ] No SQL injection risks (use ORM or parameterized queries)
- [ ] No XSS risks (proper escaping of user content)
- [ ] CSRF protection on state-changing endpoints
- [ ] File uploads validated (type, size) before storage
- [ ] No credentials or secrets in code

### 4. Database (if applicable)
- [ ] Migrations generated for model changes
- [ ] No destructive migrations without explicit intent
- [ ] Indexes on fields used in frequent lookups/filters
- [ ] Proper eager loading to avoid N+1 queries

### 5. Testing
- [ ] New functionality has corresponding tests
- [ ] Tests follow project conventions (see AGENTS.md)
- [ ] Edge cases and error paths covered

### 6. Report
Provide a summary:
- List of issues found (with file:line references)
- Severity: **critical** (security/data), **warning** (quality), **info** (style)
- Suggested fixes for each issue
- Apply fixes automatically for non-controversial issues (style, imports)
- Ask before applying fixes that change behavior

## Cross-References

- **Full code review?** Run `/review` for a comprehensive pre-push review
- **Need to fix tests?** Run `/test` to iterate

## Rules

- NEVER use emojis in any output
