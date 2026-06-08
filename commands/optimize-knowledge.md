---
name: optimize-knowledge
description: Analyze all knowledge, memories, and skills for quality gaps, staleness, and optimization opportunities.
user-invocable: true
category: learning
inputs:
  - focus
outputs:
  - optimization_report
---

# Optimize Knowledge System

Audit the entire Claude Code knowledge system for this project. Find gaps between what's known and what's enforced, prune stale entries, and embed lessons into skills.

Focus: $ARGUMENTS

## Instructions

### Phase 1: Discover and Read

Auto-discover all knowledge layers. Read every file found.

**Layer 1 -- Shared knowledge** (cross-project):
```bash
ls ~/.claude/shared-knowledge/*.md 2>/dev/null
```

**Layer 2 -- Project rules** (stable patterns):
```bash
ls .claude/rules/*.md 2>/dev/null
```

**Layer 3 -- Agent memory** (accumulated learnings):
```bash
ls .claude/agent-memory/*.md 2>/dev/null
```

**Layer 4 -- Auto-memory** (user preferences, feedback, project state):
```bash
# Find the project memory directory
find ~/.claude/projects/ -maxdepth 2 -name "MEMORY.md" -path "*/memory/*" 2>/dev/null
# Then list all files in that directory
```

**Layer 5 -- Skills/commands**:
```bash
ls .claude/commands/*.md 2>/dev/null
ls ~/.claude/commands/*.md 2>/dev/null
```

**Layer 6 -- Project instructions**:
```bash
cat CLAUDE.md 2>/dev/null | head -5
```

Read ALL discovered files. Skip binary files and files over 500 lines (summarize those instead).

### Phase 2: Cross-Reference Analysis

#### 2a. Knowledge vs Skills (gap detection)

For each feedback memory and cookbook/learnings entry:
- **Is it enforced in any skill?** A "never do X" that no skill checks for is a gap waiting to bite.
- **Could it prevent a class of bugs?** If yes, it belongs as a pre-flight check in the relevant skill.

For each skill:
- **Does it miss patterns from the knowledge system?** Cookbook says "check X before Y" but the skill doesn't.
- **Does it reference outdated things?** Old branch names, deprecated tools, removed workflows.
- **Does it have error recovery gaps?** What happens when a step fails?

#### 2b. Staleness check

For each knowledge entry:
- **Is it still true?** Verify claims about file paths, function names, branch names against current code.
- **Is it too specific?** One-time fixes that will never recur don't belong in long-term memory.
- **Is it duplicated?** Same lesson in multiple layers -- consolidate to the most appropriate layer.

#### 2c. Layer health

Check each layer for:
- **Size**: agent-memory files should be under 200 lines each. Project memories should be concise.
- **Empty stubs**: files with just a header and no content -- delete them.
- **Orphaned references**: MEMORY.md links to files that don't exist.

### Phase 3: Report

Organize findings into three sections:

#### A. Skill Improvements

For each finding:
- Which skill to modify
- What to add/change (be specific -- include the text)
- Which knowledge source backs this up
- Expected impact (prevents bug class / improves consistency / removes friction)

Prioritize by impact: bug prevention > consistency > ergonomics.

#### B. Knowledge Cleanup

- **Merge**: files covering the same topic across layers
- **Promote**: cookbook entries confirmed 2+ times that should move to rules
- **Prune**: stale, too-specific, or duplicated entries
- **Delete**: empty stubs and orphaned files

#### C. Missing Knowledge

- Patterns implicitly followed but not documented
- Error classes that recur but aren't captured
- Skill interactions worth documenting (e.g., "always run X before Y")

### Phase 4: Implement (with approval)

Present the full report. Wait for user to approve specific items. Then:

1. Update skill files with approved improvements
2. Clean up knowledge files (merge, deduplicate, prune)
3. Promote confirmed patterns to the right layer
4. Remove stale entries
5. Delete empty stubs
6. Verify all files are within size guidelines

## Scope Control

If `$ARGUMENTS` specifies a focus area (e.g., "skills only", "cleanup only", "specific domain"), limit the audit to that area. Otherwise, do the full audit.

## Rules

- Do NOT delete knowledge without verifying it's obsolete -- check current code
- Do NOT bloat skills with boilerplate -- keep additions concise and actionable
- Prioritize by real impact, not completeness
- The goal is better Claude output, not more documentation
- Present findings before making changes -- never auto-implement
- NEVER use emojis in any output
