---
name: update-docs
description: Update all project documentation to match current code state. Portable across projects.
user-invocable: true
category: core
inputs:
  - project_dir
outputs:
  - docs_updated
---

# Update Documentation

Ensure all documentation matches the current code state. Updates both git-tracked docs (committed) and local-only docs (e.g., CLAUDE.md files if gitignored).

Context: $ARGUMENTS

## Phase 1: Discover What Changed

```bash
# Find the base branch
BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")

# Changed files on this branch (determines which docs to check)
CHANGED=$(git diff --name-only $(git merge-base HEAD origin/$BASE)..HEAD)
echo "$CHANGED"
```

Read the project's CLAUDE.md or AGENTS.md to understand documentation conventions and which files track counts.

## Phase 2: Verify Structural Counts

Run the project's actual count verification commands and compare against documented values. Adapt the commands below to the project's structure.

```bash
# Tests (Python)
TEST_COUNT=$(find tests -name 'test_*.py' -exec grep -c 'def test_\|async def test_' {} + 2>/dev/null | awk -F: '{s+=$2}END{print s}')

# Tests (JS/TS)
# TEST_COUNT=$(find src -name '*.test.ts' -o -name '*.spec.ts' -exec grep -c 'it(\|test(' {} + 2>/dev/null | awk -F: '{s+=$2}END{print s}')
```

Search documentation files for count references and compare:

```bash
for f in README.md CLAUDE.md AGENTS.md docs/*.md .claude/rules/*.md; do
  [ -f "$f" ] && grep -nE '[0-9]+ (tests|services|routers|templates|steps|commands|pages|modules|models)' "$f"
done
```

Flag any documented count that doesn't match the actual count.

## Phase 3: Check Scope-Specific Docs

Based on which files changed, check related documentation:

- **Source code structure changed** (new files, renamed modules) -- verify architecture docs, module lists, project tree
- **Tests added/removed** -- verify test counts in all docs
- **CLI commands changed** -- verify command lists and help text references
- **Configuration changed** -- verify config docs, env var references, default values
- **API endpoints changed** -- verify API references, request/response shapes

Only check docs related to the changed code paths. Skip unchanged areas.

## Phase 4: Apply Updates

For each stale doc:
1. Read the full current file
2. Identify the specific section(s) that need updating
3. Update only the factual content -- never rewrite prose
4. Preserve the existing format and structure

## Phase 5: Stage and Report

```bash
# Stage tracked docs only
git diff --name-only | grep '\.md$' | while read f; do
  git ls-files --error-unmatch "$f" 2>/dev/null && git add "$f"
done

# Show what changed
git diff --cached --stat
```

Report what was updated:

```
## Docs Updated

### Tracked (will be committed)
- <file>: <what changed>

### No changes needed
- <file>
```

## Rules

- **No approval gate** -- runs as an automated step
- **Only update factual content** -- counts, lists, names, values. Never rewrite prose
- **Idempotent** -- running twice produces no diff
- **Two-tier staging** -- stage tracked docs; update local-only docs on disk without staging
- **Minimal changes** -- don't reorganize or reformat docs that aren't stale
- **Preserve structure** -- match existing format and section headers
- **Scope-driven** -- only check docs related to changed code paths, not the entire project
- NEVER use emojis in any output
