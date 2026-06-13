---
name: update-docs
description: Update all project documentation to match current code state. Portable across projects.
user-invocable: true
category: core
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

Run the project's actual count verification commands and compare against documented values. This catches drift that prose-only review misses.

### Standard count checks (adapt commands to the project's structure)

```bash
# Tests
TEST_COUNT=$(find tests -name 'test_*.py' -exec grep -c 'def test_\|async def test_' {} + 2>/dev/null | awk -F: '{s+=$2}END{print s}')

# Python modules (top-level packages under the main package)
MODULE_COUNT=$(find sova -maxdepth 1 -type d | grep -v __pycache__ | wc -l)

# Dashboard metrics (if applicable)
ROUTER_COUNT=$(ls sova/dashboard/routers/*.py 2>/dev/null | grep -v __init__ | wc -l)
SERVICE_COUNT=$(ls sova/dashboard/services/*.py 2>/dev/null | grep -v __init__ | wc -l)
TEMPLATE_COUNT=$(find sova/dashboard/templates -name '*.html' 2>/dev/null | wc -l)

# Pipeline steps (if applicable)
STEP_COUNT=$(find sova/core/steps -name '*.py' -not -name '__init__*' -not -name 'base*' -not -name '_*' 2>/dev/null | wc -l)

# CLI subcommands
CLI_COUNT=$(grep -c 'app.command\|app.add_typer' sova/cli/app.py 2>/dev/null)

# Distributable commands
CMD_COUNT=$(ls commands/*.md 2>/dev/null | wc -l)

echo "Tests: $TEST_COUNT | Modules: $MODULE_COUNT | Routers: $ROUTER_COUNT | Services: $SERVICE_COUNT"
echo "Templates: $TEMPLATE_COUNT | Steps: $STEP_COUNT | CLI: $CLI_COUNT | Commands: $CMD_COUNT"
```

### Cross-check against documentation

Search for these counts in the documentation files and flag mismatches:

```bash
# Files that commonly contain counts
for f in README.md AGENTS.md docs/VISION.md .claude/rules/architecture.md; do
  [ -f "$f" ] && echo "=== $f ===" && grep -niE '(tests|services|routers|templates|steps|commands|pages|modules|models|subcommands)[[:space:]]*:?[[:space:]]*[0-9][0-9,]*\+?|[0-9][0-9,]*\+?[[:space:]]*(tests|services|routers|templates|steps|commands|pages|modules|models|subcommands)' "$f"
done
```

Compare each documented count against the actual count. Any mismatch is a stale doc.

## Phase 3: Check Scope-Specific Docs

Based on which files changed (`$CHANGED`), check related documentation:

| Changed path pattern | Docs to verify |
|---------------------|----------------|
| `sova/core/steps/` | Step count in AGENTS.md, VISION.md. Pipeline step lists. |
| `sova/dashboard/routers/` | Router count in AGENTS.md, architecture.md, VISION.md |
| `sova/dashboard/services/` | Service count in AGENTS.md, architecture.md, VISION.md |
| `sova/dashboard/templates/` | Page count in README.md, VISION.md |
| `sova/cli/` | CLI subcommand list in README.md, architecture.md |
| `sova/db/models.py` | Model count in VISION.md, architecture.md |
| `sova/adapters/` | Adapter method count in VISION.md |
| `commands/*.md` | Command count in README.md, AGENTS.md, VISION.md |
| `tests/` | Test count in AGENTS.md, VISION.md |
| `sova/config/models.py` | Config sections in README.md |
| `sova/*/` (new package) | Module count in VISION.md, architecture tree in AGENTS.md |

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

```text
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
