---
description: Produce a structured specification document for a task before development starts
argument-hint: "<task-description-or-issue-reference>"
example: "/sova:spec Design the retry policy for the webhook dispatcher"
---

## Name
sova:spec

## Synopsis
```
/sova:spec <task-description-or-issue-reference>
```

## Description

The `sova:spec` command produces a structured specification document for a task before any code is written. It shifts architectural, data-model, and UX decisions to the cheap planning phase instead of discovering them mid-implementation, which is especially valuable for tasks that are complex, ambiguous, or touch multiple modules.

The output is a written document the user reviews and approves (or requests changes to) before development begins. Once approved, a spec becomes the primary context for a subsequent `/sova:develop` (or equivalent) run: its steps, file references, and edge cases take precedence over ad-hoc exploration.

## Implementation

1. **Gather context**
   - Read the task description or referenced issue in full.
   - Read the project's own contributor documentation for architectural conventions.
   - Identify the modules, files, and existing patterns relevant to the task.

2. **Draft the specification**, covering:
   - **Summary**: what is being built and why, in a few sentences.
   - **Affected files**: which files will be created, modified, or deleted, and why.
   - **Data model changes**: any schema, migration, or persisted-state changes required.
   - **API changes**: any new or modified public interfaces, endpoints, or contracts.
   - **Dependencies**: existing code, libraries, or other in-flight work this task depends on.
   - **Edge cases**: inputs, states, or failure modes that need explicit handling.
   - **Suggested approach**: an ordered list of implementation steps.

3. **Flag open questions**
   - Where the task description is ambiguous or underspecified, list the open questions explicitly rather than guessing silently. A spec that hides an assumption is worse than one that surfaces it.

4. **Present for approval**
   - Output the specification for the user to review. Do not begin implementation until the spec is approved, since its purpose is to catch design problems before they become code.

## Return Value
- **Claude agent text / document**: a structured specification covering summary, affected files, data model changes, API changes, dependencies, edge cases, and suggested approach.

## Examples

1. **New feature**:
   ```
   /sova:spec Add per-user rate limiting to the public API
   ```

2. **Issue reference**:
   ```
   /sova:spec #217
   ```

## Arguments
- `$1` (required): a description of the task, or a reference to an issue/ticket to specify.

## See Also
- `/sova:develop` -- implement the task once the spec is approved
- `/sova:debug` -- for investigating an existing bug rather than planning new work
