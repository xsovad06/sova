---
name: spec
description: Produce a structured specification document for an issue before development starts.
user-invocable: true
---

# Specification

Produce a structured specification document for a task before development starts. Shifts architectural and UX decisions to the cheap planning phase instead of the expensive coding phase.

**Task**: $ARGUMENTS

## Instructions

### Step 1: Fetch the Issue

If `$ARGUMENTS` is a number, fetch the issue:
```bash
gh issue view $ARGUMENTS --json number,title,body,labels,milestone
```

If `$ARGUMENTS` is a text description (not a number), use it directly as the problem statement.

### Step 2: Read Project Context

Read whatever exists -- skip files that are missing:
- `CLAUDE.md` and `AGENTS.md` for project conventions
- `.claude/rules/architecture.md` for component overview and design decisions
- `.claude/agent-memory/cookbook.md` for known patterns and past mistakes

### Step 3: Explore the Codebase

Based on the issue, identify the affected areas:
1. Find the modules, files, and functions that will need changes
2. Read existing patterns in those areas -- match them, don't invent new ones
3. Identify reusable components, utilities, and test fixtures
4. Check for related prior work (similar features, relevant tests)

Be thorough here. The spec's value comes from grounding decisions in the actual code, not from generic planning.

### Step 4: Write the Spec

Create the directory if needed:
```bash
mkdir -p .claude/specs
```

Generate a slug from the issue title (lowercase, hyphens, max 40 chars). Write the spec to `.claude/specs/{issue-number}-{slug}.md`:

```markdown
# Spec: {Issue title}

**Issue**: #{number}
**Status**: draft
**Created**: {YYYY-MM-DD}
**Complexity**: simple | moderate | complex

## Problem

What problem does this solve? 1-3 sentences from the user's perspective.

## Solution

High-level approach in 2-4 sentences. What changes and why.

## Data Model

New or modified models, fields, constraints.
Include migration notes if relevant.

(Omit this section if no DB changes.)

## API Changes

New or modified endpoints with request/response shapes.

(Omit this section if no API changes.)

## User Interface

For UI features, include:
- User flow: what the user does step by step
- ASCII mockup of the layout/component
- Which existing templates/components to reuse
- Responsive behavior (mobile considerations)

Example ASCII mockup format:
+----------------------------------+
| Card Title            [?] [Edit] |
|                                  |
| Label        Value               |
| Label        Value               |
|                                  |
| [Cancel]              [Save]     |
+----------------------------------+

(Omit this section for backend-only changes.)

## Implementation Plan

Ordered steps. Each step should be one commit-sized unit of work.
Reference specific files to create/modify.

1. Step one -- what and where
2. Step two -- what and where
3. ...

## Edge Cases

Things the implementation must handle that aren't obvious from the issue.

## Testing Strategy

What to test: key scenarios, edge cases, integration points.
Reference existing test patterns or fixtures to reuse.

## Dependencies

Existing services, utilities, or patterns to reuse.
Reference specific functions/classes with file paths.

## Open Questions

Anything that needs user input before development starts.

(Omit this section if there are no open questions.)
```

**Guidelines for the spec content**:
- Reference specific file paths and function names discovered in Step 3
- Keep the implementation plan concrete -- "add X to Y" not "implement the feature"
- Each implementation step should map to roughly one commit
- Complexity rating: simple (1-2 files, <50 lines), moderate (3-6 files, 50-200 lines), complex (7+ files or >200 lines)
- Prefer reusing existing patterns over introducing new abstractions
- If the issue has acceptance criteria, every criterion must be covered by at least one implementation step

### Step 5: Present for Review

Show the full spec to the user. Ask:

> Spec written to `.claude/specs/{filename}`. Review the plan above -- anything to change, add, or remove? Say "approved" to mark it ready for `/develop`.

### Step 6: Iterate

If the user gives feedback:
1. Update the spec file with the requested changes
2. Show the updated sections (not the full spec again unless asked)
3. Ask for approval again

### Step 7: Mark Approved

When the user approves, update the spec:
- Change `**Status**: draft` to `**Status**: approved`
- Confirm: "Spec approved. Run `/develop {issue-number}` to start implementation."

## Cross-References

- **Before spec**: Run `/find-task` to pick the next issue
- **After spec**: Run `/develop {issue-number}` or `/develop-full {issue-number}` to implement
- **Researcher role**: produces a brief assessment on the issue itself; the spec is a detailed implementation plan in a separate file. They complement each other.

## Rules

- This command plans only -- do NOT write any implementation code
- Do NOT commit the spec file (specs are working documents, not tracked artifacts)
- NEVER use emojis in any output
- Omit spec sections that don't apply (e.g., no "Data Model" for a pure UI change)
- Ground every recommendation in actual code found during Step 3
- If the issue lacks enough detail to spec, list concrete open questions instead of guessing
