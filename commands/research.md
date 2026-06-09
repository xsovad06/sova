---
name: research
description: Investigate a task and produce a structured research assessment for autonomous development.
user-invocable: true
category: core
---

# Research

Investigate a task's codebase impact and produce a structured research assessment. Designed for autonomous execution -- always writes findings back to the tracker. For interactive pre-development planning, use `/spec` instead.

**Task**: $ARGUMENTS

## Instructions

### Step 1: Fetch the Task

Get the issue number or ticket key from `$ARGUMENTS`. If empty, stop with an error.

Determine the task source by reading `sova.toml` (if it exists) and checking `[task_source] type`.

**GitHub** (default, or no sova.toml):
```bash
gh issue view $ARGUMENTS --json number,title,body,labels,milestone
```

**JIRA** (`task_source.type = "jira"`):
```bash
jira issue view <TICKET_KEY> --plain
```

Save the original description verbatim.

If the issue has no description or body, report "Issue has no description; needs specification before research" and stop.

If the issue body already contains a `## Research` section, report "Issue already has a research section; ready for development" and stop.

### Step 2: Read Project Context

Read whatever exists -- skip files that are missing:
- `CLAUDE.md` and `AGENTS.md` for project conventions
- `.claude/rules/architecture.md` for component overview and design decisions
- `.claude/agent-memory/cookbook.md` for known patterns and past mistakes

### Step 3: Explore the Codebase

Based on the issue, investigate the affected areas. Use file reads, grep, and search -- do not guess from file names alone.

1. **Identify affected files**: find every file that needs to be modified, created, or deleted. Read the actual source to confirm.
2. **Find the pattern**: locate the closest existing implementation to follow. Note specific file paths, class names, and method signatures.
3. **Check for data model changes**: determine if DB models, schemas, or migrations are needed.
4. **Check for API changes**: identify new or modified endpoints with request/response shapes.
5. **Find reusable code**: identify utilities, base classes, test fixtures, and patterns that the implementation should use. Reference specific functions and classes.
6. **Anticipate edge cases**: based on reading the actual code, identify failure modes and edge cases not obvious from the issue description.
7. **Check for UI implications**: templates, components, user-facing behavior changes.
8. **Design implementation approach**: produce a concrete 3-6 step plan referencing specific files.

Be thorough and concrete. Reference actual file paths, function names, and line numbers from your exploration.

### Step 4: Estimate Complexity

Based on your findings, rate complexity:
- **trivial**: single file, <20 lines
- **simple**: 1-2 files, <50 lines
- **moderate**: 3-6 files, 50-200 lines
- **complex**: 7+ files or >200 lines
- **epic**: cross-cutting, multiple subsystems

### Step 5: Write Research Back to Tracker

Append the research assessment to the issue/ticket body. Format:

```
## Research

**Issue**: {title}
**Complexity**: {complexity rating}

{One paragraph summary of findings and recommended approach}

### Affected Files

- `path/to/file.py` (modify): What needs to change and why
- `path/to/new_file.py` (create): Purpose of the new file

### Pattern Reference

Follow the implementation in `path/to/reference.py` (class/method name).
Similar work was done in {ticket/PR reference if found}.

### Data Model Changes

{Description of schema/model changes, or "None required."}

### API Changes

{Description of endpoint changes, or "None required."}

### Dependencies

- `path/to/utility.py` -- FunctionName: how to reuse it
- `path/to/base_class.py` -- ClassName: extend for this implementation

### Edge Cases

- Edge case 1
- Edge case 2

### Suggested Approach

1. Step one -- what and where
2. Step two -- what and where
3. Step three -- what and where

### UI Notes

{UI implications, or omit this section if none.}
```

Write the updated body back to the tracker:

**GitHub**:
```bash
gh issue edit <NUMBER> --body "<original body + research section>"
```

**JIRA**:
```bash
jira issue edit <TICKET_KEY> -b "<original body + research section>" --no-input
```

### Step 6: Report

Output a brief summary of what was found:
```
Research completed for #{number}: {title}
Complexity: {rating}
Affected files: {count}
Ready for development.
```

## Cross-References

- **Before research**: Issue should be triaged (has labels, is in TRIAGED state)
- **After research**: Run `/develop {issue-number}` or `/develop-full {issue-number}` to implement
- **Interactive alternative**: Use `/spec {issue-number}` for human-in-the-loop planning

## Rules

- This command investigates only -- do NOT write any implementation code
- Do NOT create branches or modify project files
- Always write research back to the tracker -- this is autonomous, not interactive
- Ground every recommendation in actual code found during Step 3
- If the issue lacks enough detail, report "needs specification" and stop rather than guessing
- NEVER use emojis in any output
