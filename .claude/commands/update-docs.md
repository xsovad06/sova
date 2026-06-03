---
name: update-docs
description: Update all project documentation to match current code state. Portable across projects.
user-invocable: true
category: core
---

# Update Documentation

Ensure all documentation matches the current code state. Updates both git-tracked docs (committed) and local-only docs (e.g., CLAUDE.md files if gitignored).

Context: $ARGUMENTS

## Phase 1: Discover Project Structure

```bash
# Find the base branch
BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")

# Changed files on this branch
git diff --name-only $(git merge-base HEAD origin/$BASE)..HEAD

# All tracked markdown docs
git ls-files '*.md'

# Local-only markdown docs (exist on disk but not tracked)
find . -name '*.md' -not -path './.git/*' | while read f; do
  git ls-files --error-unmatch "$f" 2>/dev/null || echo "$f"
done
```

Read the project's CLAUDE.md or README.md to understand the documentation conventions.

## Phase 2: Identify What Needs Updating

For each doc file (tracked or local), check if any of its factual claims are stale:

### Test counts
```bash
# Find all test files and count tests
find . -name 'test_*.py' -o -name '*_test.py' -o -name '*.test.ts' -o -name '*.spec.ts' | while read f; do
  dir=$(dirname "$f")
  count=$(grep -cE '(def test_|it\(|test\()' "$f" 2>/dev/null || echo 0)
  echo "$dir: $count"
done
```

Compare against documented test counts in README.md, CLAUDE.md, or similar files.

### API references
If the project has API documentation, check that:
- Endpoint URLs match the current code
- Request/response shapes match
- Auth patterns match

### Architecture docs
If the project has architecture documentation, check that:
- Module/component lists are complete
- Dependency descriptions are accurate
- Pattern descriptions match the code

### Configuration docs
Check that documented config values, environment variables, and settings match the code.

## Phase 3: Apply Updates

For each stale doc:
1. Read the full current file
2. Identify the specific section(s) that need updating
3. Update only the factual content -- never rewrite prose
4. Preserve the existing format and structure

## Phase 4: Stage and Report

```bash
# Stage tracked docs only
git diff --name-only | grep '\.md$' | while read f; do
  git ls-files --error-unmatch "$f" 2>/dev/null && git add "$f"
done

# Show what changed
git diff --cached --stat
git status
```

Report what was updated:

```
## Docs Updated

### Tracked (will be committed)
- <file>: <what changed>

### Local (stays on disk)
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
- **Skip unchanged areas** -- only check docs related to changed code
- NEVER use emojis in any output
