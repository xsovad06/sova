---
name: status
description: Project health check -- git state, services, tests, and progress overview.
user-invocable: true
---

# Project Status Check

Perform a health check on the project and report current status.

**Focus area**: $ARGUMENTS

## Checks

### 1. Git Status
- Current branch and clean/dirty state
- Uncommitted changes summary
- Recent commits (last 5)
- Any stale feature branches: `git branch --merged main`

### 2. Services
- Check if project services are running (Docker, dev server, etc.)
- Verify the application responds (health check endpoint if available)

### 3. Database
- Check for unapplied migrations (see CLAUDE.md for the migration check command)
- Verify database is accessible

### 4. Dependencies
- Check for outdated or conflicting packages
- Verify lock file is in sync with dependency manifest

### 5. Test Suite
- Run the project's tests (see CLAUDE.md for commands)
- Report pass/fail count and coverage if available

### 6. Project Progress
- Read agent memory files if they exist (`.claude/agent-memory/MEMORY.md`)
- Check open issues: `gh issue list --assignee @me --state open --limit 10`
- Check open PRs: `gh pr list --author @me --state open`
- Reference project milestones: `gh issue list --state open --json milestone`

### 7. Report
Provide a concise status dashboard:
```
Branch:       main (clean)
Services:     app [running] | db [running]
Migrations:   up to date
Tests:        120 passed, 0 failed
Open PRs:     2
Open Issues:  5
Next up:      <suggested focus>
```

## Cross-References

- **Want to start working?** Run `/find-task` or `/standup`
- **Need to run tests?** Run `/test`

## Rules

- NEVER use emojis in any output
