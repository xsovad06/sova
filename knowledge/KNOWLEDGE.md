# Knowledge Management System

Standardized knowledge architecture for AI-assisted development. This system ensures agents learn from every session, retain knowledge across conversations, and share learnings across projects.

---

## Architecture Overview

Knowledge is organized in **four tiers**, from most permanent and broadly shared to most ephemeral and session-specific:

```
Tier 0: Shared Knowledge (~/.claude/shared-knowledge/)
        Cross-project patterns, reusable across all repos
        |
Tier 1: Project Rules (AGENTS.md, CLAUDE.md, .claude/rules/*.md)
        Canonical conventions, loaded every session, never truncated
        |
Tier 2: Agent Memory (.claude/agent-memory/)
        Lessons learned from development and reviews, gitignored
        |
Tier 3: Session Memory (~/.claude/projects/.../memory/)
        User preferences and project state, auto-managed by Claude Code
```

### How Tiers Interact

- Knowledge starts at **Tier 2** (agent learns a pattern from a review or debugging session)
- Once confirmed across multiple sessions, it gets **promoted to Tier 1** (added to `.claude/rules/` or `AGENTS.md`)
- If a pattern applies to multiple projects, it gets **promoted to Tier 0** (shared knowledge)
- **Tier 3** is auto-managed by Claude Code -- agents don't write here directly

---

## Tier 0: Shared Knowledge

**Location**: `~/.claude/shared-knowledge/`
**Scope**: Cross-project -- patterns that apply regardless of tech stack or project
**Tracked in git**: No (lives outside any repo)
**Loaded by**: SOVA agent startup

### Structure

```
~/.claude/shared-knowledge/
  patterns.md          # Universal development patterns
  review-lessons.md    # Code review patterns that apply everywhere
  git-workflows.md     # Git/GitHub workflow knowledge
  debugging.md         # Cross-project debugging techniques
```

### What Belongs Here

- Git workflow patterns (rebase strategies, commit organization)
- Code review heuristics (what to look for, scoring patterns)
- Universal debugging techniques
- Cross-project architectural principles
- Patterns confirmed in 2+ projects

### What Does NOT Belong Here

- Project-specific conventions (those go in Tier 1)
- Framework-specific patterns (those go in Tier 1 of the relevant project)
- Session-specific state (that's Tier 3)

---

## Tier 1: Project Rules (Always-Loaded Context)

**Location**: Repo root + `.claude/rules/`
**Scope**: Project-specific -- conventions for this repository
**Tracked in git**: Yes
**Loaded by**: Claude Code automatically (no truncation)

### Structure

```
project-root/
  AGENTS.md                       # Cross-cutting conventions (all AI tools read this)
  CLAUDE.md                       # Claude Code-specific (imports @AGENTS.md)
  .claude/
    rules/                        # Modular knowledge files
      architecture.md             # App structure, key paths, decisions
      patterns.md                 # Framework/language gotchas and lessons
      testing.md                  # Test framework, fixtures, conventions
      [domain].md                 # Additional domain-specific knowledge
    commands/                     # Workflow commands (on-demand skills)
      develop.md
      review.md
      [topic].md                  # Deep domain knowledge loaded via /topic
```

### File Roles

| File | Role | Size Guidance |
|------|------|---------------|
| `AGENTS.md` | Agent-agnostic conventions, project overview, tracker config | < 200 lines |
| `CLAUDE.md` | Claude-specific: run commands, knowledge system, agent config | < 150 lines |
| `.claude/rules/*.md` | Modular knowledge -- one file per domain | < 150 lines each |
| `.claude/commands/*.md` | Workflow commands and deep domain skills | No limit (on-demand) |

### Rules Files Guidelines

Each `.claude/rules/` file should:
- Focus on a single domain (architecture, patterns, testing, UI, models, etc.)
- Contain **confirmed, stable patterns** -- not experiments
- Use a consistent format: bold label + concise description
- Include the "why" -- not just the rule, but the reason behind it
- Stay under 150 lines -- if it grows beyond that, split into sub-domains

### When to Add to Tier 1

A pattern belongs in Tier 1 when:
- It has been confirmed across multiple sessions (not a one-off finding)
- It applies to all contributors (human and agent)
- It's stable enough that changing it would be a deliberate decision
- It would cause bugs or inconsistency if not followed

### Commands as Knowledge

`.claude/commands/*.md` files serve two purposes:
1. **Workflow commands** (e.g., `/develop`, `/pr`) -- procedural knowledge for development tasks
2. **Domain deep-dives** (e.g., `/architecture-overview`, `/import-patterns`) -- detailed reference material loaded on demand

The distinction: rules files are always loaded; commands are loaded only when invoked. Put frequently-needed patterns in rules files. Put detailed reference material in commands.

---

## Tier 2: Agent Memory

**Location**: `.claude/agent-memory/`
**Scope**: Agent-specific learnings, accumulated over time
**Tracked in git**: No (gitignored)
**Loaded by**: Agent startup (reads at the beginning of each task)

### Structure

```
.claude/agent-memory/
  MEMORY.md              # Quick reference -- project patterns, testing shortcuts
  learnings.md           # Self-review findings (framework gotchas, patterns)
  review-feedback.md     # Lessons from PR reviews (automated + human)
  common-mistakes.md     # Recurring errors to check before submitting
  task-history.md        # Completed tasks log (ticket, date, summary, outcome)
  memory.db              # (Optional) SQLite FTS5 for fast memory search
  costs.jsonl            # (Optional) Per-task cost tracking
```

### File Roles

| File | Content | Size Limit |
|------|---------|------------|
| `MEMORY.md` | Quick reference: key patterns, shortcuts, current project state | 80 lines |
| `learnings.md` | Framework/ORM gotchas, testing tricks, debugging insights | 150 lines |
| `review-feedback.md` | What reviewers flagged and why, grouped by category | 150 lines |
| `common-mistakes.md` | Errors that appeared 2+ times, with prevention steps | 100 lines |
| `task-history.md` | Log of completed tasks for context | 200 lines |

### Entry Format

One line per pattern, bold label, then the lesson:

```markdown
- **Coalesce NULL annotations**: `Sum()` returns NULL when no rows match. Always wrap with `Coalesce(Sum(...), Decimal("0"))`.
- **Template .count in loops = N+1**: Use `.annotate(foo_count=Count("related"))` instead of `{{ obj.related_set.count }}` in loops.
```

### Knowledge Lifecycle in Tier 2

1. **Capture**: Agent learns a pattern during `/develop`, `/review`, or `/ingest-review`
2. **Record**: Pattern is written to the appropriate agent-memory file
3. **Deduplicate**: Before writing, check if the pattern already exists
4. **Promote**: Once confirmed stable (appears in 2+ tasks), promote to Tier 1 (`.claude/rules/` or `AGENTS.md`)
5. **Prune**: Remove promoted entries from Tier 2 to avoid duplication; prune oldest entries if file exceeds size limit

### Commands That Write to Tier 2

| Command | What It Writes | Target File |
|---------|---------------|-------------|
| `/extract-knowledge` | Session findings, patterns, gotchas | `learnings.md`, `MEMORY.md` |
| `/ingest-review` | PR review lessons | `review-feedback.md`, `common-mistakes.md` |
| `/after-merge` | Task completion + calls `/ingest-review` | `task-history.md` |
| `/develop-full` | Calls `/review` which calls `/extract-knowledge` | Via chain |

---

## Tier 3: Session Memory (Auto-Memory)

**Location**: `~/.claude/projects/<project-hash>/memory/`
**Scope**: User preferences, project state, workflow conventions
**Tracked in git**: No (outside repo, managed by Claude Code)
**Loaded by**: Claude Code automatically (`MEMORY.md` loaded each session, 200-line limit with truncation)

### What Belongs Here

- Current project phase and next steps
- User preferences (workflow, tools, communication style)
- Links to `.claude/rules/` files for quick reference
- High-level project state that changes between sessions

### What Does NOT Belong Here

- Detailed patterns (those go in Tier 1 or Tier 2)
- Task-specific context (ephemeral, lost after session)
- Anything that contradicts `AGENTS.md` or `CLAUDE.md`

---

## Knowledge Flow

### During Development (`/develop`, `/develop-full`)

```
Session work --> /review --> findings scored 3+
                               |
                               v
                         /extract-knowledge
                               |
               +---------------+---------------+
               |                               |
         Tier 1 (.claude/rules/)         Tier 2 (agent-memory/)
         if stable, cross-cutting        if new, needs confirmation
```

### After PR Merge (`/after-merge`)

```
PR merged --> /ingest-review --> analyze review comments
                                       |
                       +---------------+---------------+
                       |               |               |
               review-feedback.md  common-mistakes.md  task-history.md
                       |
              (if recurring pattern)
                       |
                       v
              Promote to Tier 1
```

### Promotion Flow

```
Tier 2 (agent-memory)
  -- pattern confirmed in 2+ tasks -->
Tier 1 (.claude/rules/ or AGENTS.md)
  -- pattern applies to 2+ projects -->
Tier 0 (~/.claude/shared-knowledge/)
```

---

## Setup for a New Project

### 1. Initialize Tier 1

```bash
# AGENTS.md and CLAUDE.md (use templates or /agent-readiness)
cp templates/AGENTS.md /path/to/project/AGENTS.md
cp templates/CLAUDE.md /path/to/project/CLAUDE.md

# Rules directory (start empty, grows organically)
mkdir -p /path/to/project/.claude/rules
```

### 2. Initialize Tier 2

```bash
mkdir -p /path/to/project/.claude/agent-memory
cat > /path/to/project/.claude/agent-memory/MEMORY.md << 'EOF'
# Agent Memory

Quick reference for patterns learned during development.
EOF

touch /path/to/project/.claude/agent-memory/{learnings,review-feedback,common-mistakes,task-history}.md

# Add headers
for f in learnings review-feedback common-mistakes task-history; do
  echo "# ${f//-/ }" > /path/to/project/.claude/agent-memory/$f.md
done
```

### 3. Gitignore Tier 2

Add to `.gitignore`:
```gitignore
.claude/agent-memory/
```

### 4. Initialize Tier 0 (once, globally)

```bash
mkdir -p ~/.claude/shared-knowledge
touch ~/.claude/shared-knowledge/{patterns,review-lessons,git-workflows,debugging}.md
```

---

## Maintenance

### Regular Pruning

Agent memory files have size limits. When they grow too large:
1. Identify patterns that have been promoted to Tier 1 -- remove from Tier 2
2. Remove entries that turned out to be wrong or no longer relevant
3. Merge similar entries
4. Archive very old `task-history.md` entries

### Promotion Checklist

Before promoting a pattern from Tier 2 to Tier 1:
- [ ] Pattern has appeared in 2+ separate tasks or sessions
- [ ] Pattern is stable (not likely to change)
- [ ] Pattern applies to all contributors, not just the agent
- [ ] No existing entry in `.claude/rules/` covers the same thing
- [ ] The rule file stays under 150 lines after adding

### Cross-Project Promotion

Before promoting a pattern from Tier 1 to Tier 0:
- [ ] Pattern applies in 2+ different projects
- [ ] Pattern is tech-stack-agnostic (or clearly labeled when it isn't)
- [ ] No contradiction with other projects' conventions
