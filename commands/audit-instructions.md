---
name: audit-instructions
description: Audit CLAUDE.md and project rules for completeness, contradictions, and autonomous-agent readiness.
user-invocable: true
category: meta
inputs:
  - focus
outputs:
  - audit_report
  - improvement_suggestions
---

Audit this project's AI instruction system for completeness, contradictions, implicit assumptions, and autonomous-agent readiness. $ARGUMENTS

## Phase 1: Collect All Instruction Sources

Read every instruction file -- do not skip any.

```bash
cat CLAUDE.md 2>/dev/null || echo "MISSING: CLAUDE.md"
```

```bash
cat AGENTS.md 2>/dev/null || echo "No AGENTS.md"
```

```bash
for f in .claude/rules/*.md; do [ -f "$f" ] && echo "=== $f ===" && cat "$f"; done 2>/dev/null || echo "No rules"
```

```bash
ls .claude/agent-memory/*.md 2>/dev/null || echo "No agent memory"
```

```bash
ls .claude/skills/*/SKILL.md 2>/dev/null || echo "No skills"
```

## Phase 2: Analyze Project Reality

Understand the actual project to compare against instructions:

```bash
find . -maxdepth 3 -type f \( -name "*.py" -o -name "*.ts" -o -name "*.js" -o -name "*.go" -o -name "*.rs" -o -name "*.java" \) | head -50
```

```bash
git log --oneline -20 2>/dev/null || echo "Not a git repo"
```

```bash
ls pyproject.toml package.json Cargo.toml go.mod Makefile Dockerfile 2>/dev/null
```

```bash
ls -d tests/ test/ __tests__/ spec/ 2>/dev/null && find tests/ test/ __tests__/ spec/ -maxdepth 1 -name "*.py" -o -name "*.ts" -o -name "*.js" 2>/dev/null | head -20
```

## Phase 3: Gap Analysis

For each category, assess completeness (0-10) and list specific gaps:

| Category | What to check |
|----------|---------------|
| Build commands | Are test_cmd, lint_cmd, format_cmd documented and correct? Do they match actual tooling? |
| Architecture | Does CLAUDE.md explain the module structure? Can an agent navigate the codebase without guessing? |
| Conventions | Are naming conventions, file organization, and code style rules stated explicitly? |
| Error handling | Are error handling patterns documented? |
| Testing | Are test patterns, fixtures, mocks, and conventions documented? |
| Dependencies | Are dependency management conventions documented (add/remove/upgrade)? |
| Security | Are security-sensitive areas called out (credentials, external inputs, subprocess)? |
| Agent boundaries | Are there clear "always do" / "ask first" / "never do" boundaries? |
| Contradictions | Do any rules conflict with each other or with actual project practice? |
| Implicit assumptions | What does the project assume that is not written down? |

## Phase 4: Report

Present findings in this structure:

**Instruction Completeness Score**: X/100

| Category | Score | Key Gap |
|----------|-------|---------|
| Build commands | X/10 | ... |
| Architecture | X/10 | ... |
| Conventions | X/10 | ... |
| Error handling | X/10 | ... |
| Testing | X/10 | ... |
| Dependencies | X/10 | ... |
| Security | X/10 | ... |
| Agent boundaries | X/10 | ... |
| Contradictions | X/10 | ... |
| Implicit assumptions | X/10 | ... |

**Critical Gaps** (will cause agent failures):
- List each with a specific, actionable recommendation

**Contradictions Found**:
- List any conflicting instructions with file locations

**Implicit Assumptions** (undocumented knowledge required to work in this project):
- List things the project assumes but never states

## Phase 5: Propose Improvements

For each gap found, propose a specific addition or edit. Group by target file:
- CLAUDE.md additions
- New rule files (.claude/rules/)
- Existing rule file updates

Present the improvements and ask the user if they want them applied.

## Rules

- Do NOT invent preferences the project has not demonstrated -- only document what exists
- Preserve the existing voice and style of instructions
- Where unsure about a convention, ask the user rather than guessing
- Focus on what an autonomous agent needs to succeed without human guidance
- Use `make test` and `make lint` for project-specific commands in any templates
