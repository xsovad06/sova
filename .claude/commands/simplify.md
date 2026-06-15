---
name: simplify
description: Review changed code for reuse, quality, and efficiency, then fix any issues found.
user-invocable: true
---

# Simplify

Review all changed code for opportunities to reduce complexity, eliminate duplication, and improve quality.

Scope: $ARGUMENTS

## Instructions

### Step 1: Get the Diff Scope

```bash
git diff main..HEAD --stat
git diff --cached --stat
git diff --stat
```

### Step 2: Review Each Changed File

For each changed file, read the **entire file** and check:

- **Code reuse**: does new code duplicate existing utilities or helpers? Search the codebase for similar patterns.
- **Over-engineering**: unnecessary abstractions, premature generalization, excessive configurability for one-time operations.
- **Dead code**: unused imports, unreachable branches, commented-out code.
- **Redundancy**: repeated logic that could be a shared helper (only if 3+ occurrences).
- **Complexity**: can any function be simplified without losing clarity?
- **Efficiency**: redundant computations, repeated file reads, duplicate API calls.
- **Naming**: unclear variable or function names that could be more descriptive.

### Step 3: Apply Simplifications

Apply each simplification directly. After changes, verify tests still pass:

```bash
make check
```

If a simplification is risky, skip it -- only apply safe, clear improvements.

### Step 4: Summary

If no simplifications are needed, state that explicitly.

If changes were made, summarize:

```
## Simplification Summary

**Changes applied**: N
- [brief description of each change]

**Tests**: passing
```

## Rules

- Do NOT reformat code that was not changed in this branch.
- Do NOT add docstrings, comments, or type annotations to unchanged code.
- Do NOT create abstractions for fewer than 3 occurrences.
- Do NOT add error handling for scenarios that cannot happen.
- Three similar lines of code is better than a premature abstraction.
- NEVER use emojis in any output.
