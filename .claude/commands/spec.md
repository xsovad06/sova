---
name: spec
description: Produce a structured specification document for an issue before development starts.
user-invocable: true
category: core
inputs:
  - issue_number
  - task_description
outputs:
  - spec_document
---

# Specification

Produce a structured specification document for a task before development starts. Shifts architectural and UX decisions to the cheap planning phase instead of the expensive coding phase.

```bash
# Benchmark logging (entry)
bash .claude/benchmark/log.sh "spec_start" "" "" 2>/dev/null || true
```

**Task**: $ARGUMENTS

## Instructions

### Step 1: Fetch the Task

If `$ARGUMENTS` is a text description (not a number or ticket key), use it directly as the problem statement and skip to Step 2.

Determine the task source by reading `sova.toml` (if it exists) and checking `[task_source] type`.

**GitHub** (default, or no sova.toml):
```bash
gh issue view $ARGUMENTS --json number,title,body,labels,milestone
```

**JIRA** (`task_source.type = "jira"`):
```bash
jira issue view $ARGUMENTS --plain
```
Extract: title, description, status, linked/blocked tickets, components.

Save the original description verbatim -- it will be preserved in the spec.

**Early exit -- already implemented**: if the issue body contains a Research section that concludes the issue is already fully implemented (look for phrases like "already implemented", "already complete", "no remaining work"), write a minimal spec file to `.claude/specs/{issue-number}-already-implemented.md` with the following content, then stop -- do not explore the codebase or write a full spec:

```markdown
# Spec: {Issue title}

**Issue**: #{number}
**Status**: approved
**Created**: {YYYY-MM-DD}
**Complexity**: trivial

## Problem

Already implemented per research findings.

## Solution

No changes needed. The research step confirmed this issue is already fully implemented.
```

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
5. Find the closest precedent -- which prior task or file implements something similar? Note specific file paths and line numbers as the pattern reference.

Be thorough here. The spec's value comes from grounding decisions in the actual code, not from generic planning.

**Verification requirement**: For every function, model field, class, or constant referenced in the spec, open the source file and confirm it exists with the stated signature and return type. Do not state "X has field Y" without having read the model. Do not state "function F returns Z" without having read the function body.

### Pre-Write Checklist

Before writing the spec, verify each of the following:

- Every function referenced has been read; signature and return type confirmed
- Every model field referenced has been verified in the model file
- Every external dependency (library function, service call) has been checked for actual behavior
- No item in the planned Open Questions section can be answered by reading code

If any item fails this checklist, go back to Step 3 and read the relevant file before continuing.

### Step 4: Write the Spec

Create the directory if needed:
```bash
mkdir -p .claude/specs
```

Generate a slug from the issue title (lowercase, hyphens, max 40 chars). Write the spec to `.claude/specs/{issue-number}-{slug}.md`:

```markdown
# Spec: {Issue title}

**Issue**: #{number} (or JIRA key)
**Status**: draft
**Created**: {YYYY-MM-DD}
**Complexity**: simple | moderate | complex

## Problem

What problem does this solve? 1-3 sentences from the user's perspective.

## Solution

High-level approach in 2-4 sentences. What changes and why.

## Pattern Reference

Which existing implementation to follow as a model:
- Ticket/PR that implemented something similar
- File paths with line numbers for the reference implementation
- Key classes, methods, or patterns to reuse

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

(Omit this section for backend-only changes.)

## Implementation Plan

Ordered steps. Each step should be one commit-sized unit of work.
Reference specific files to create/modify.

1. Step one -- what and where
2. Step two -- what and where
3. ...

## Design Decisions

Pre-answered questions for every ambiguity found during exploration.
The implementing agent should not need to make architectural choices.

1. **Question?** -- Answer with rationale.
2. **Question?** -- Answer with rationale.

(Omit if no ambiguities exist.)

## Scope Boundaries

Explicit limits to prevent over-engineering.

- Do NOT {thing that seems related but is out of scope}
- Out of scope: {related concern for a future task}

## Edge Cases

Things the implementation must handle that aren't obvious from the issue.

## Testing Strategy

What to test: key scenarios, edge cases, integration points.
Reference existing test patterns or fixtures to reuse.

## Dependencies

Existing services, utilities, or patterns to reuse.
Reference specific functions/classes with file paths.

## Open Questions

Items that require user input before development can start: business rules, scope boundaries, UX preferences, or external system behavior that cannot be determined from the codebase. Never put code facts here. If uncertain about a code fact, read the relevant file; if it cannot be found, document the assumption in Design Decisions instead.

(Omit this section if all uncertainties can be resolved by reading code.)
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

### Step 8: Write Back to Tracker (optional)

After approval, ask: "Write the spec back to the issue/ticket description?"

If the user agrees, update the tracker with the spec appended to the original description:

**GitHub**:
```bash
echo "<updated body>" > /tmp/issue_body.md
gh issue edit $ARGUMENTS --body-file /tmp/issue_body.md
```

**JIRA** (requires `jira-cli` by ankitpokhrel):
```bash
jira issue edit $ARGUMENTS -b "<updated body>" --no-input
```

Format the updated body as:
```markdown
## Original Description

{original description preserved verbatim}

---

## Implementation Spec

{spec content from Step 4, without the frontmatter header}
```

If the user declines, skip this step -- the spec file in `.claude/specs/` is the primary artifact.

## Cross-References

- **Before spec**: Run `/find-task` to pick the next issue
- **After spec**: Run `/develop {issue-number}` or `/develop-full {issue-number}` to implement

## Rules

- This command plans only -- do NOT write any implementation code
- Spec files are committed to git as provenance records (they accumulate design decisions across the pipeline)
- NEVER use emojis in any output
- Omit spec sections that don't apply (e.g., no "Data Model" for a pure UI change)
- Ground every recommendation in actual code found during Step 3
- If the issue lacks enough detail to spec, list concrete open questions instead of guessing
- Never populate Open Questions with implementation details findable by reading the codebase. If all uncertainties can be resolved by reading code, the Open Questions section should be omitted entirely.

```bash
# Benchmark logging (exit)
bash .claude/benchmark/log.sh "spec_complete" "" "" 2>/dev/null || true
```
