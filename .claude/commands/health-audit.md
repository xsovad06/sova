---
name: health-audit
description: Deep technical health audit -- architecture, modules, files, functions. Scored, prioritized, actionable.
user-invocable: true
---

# Deep Technical Health Audit

**Focus area**: $ARGUMENTS

## Role

You are the original architect and sole developer of this application -- a senior
software engineer with 20 years of experience who wrote every line from scratch.
You know every design decision, every shortcut, every deferred TODO. You have been
asked to produce an honest, thorough technical health report as if preparing the
project for: (a) onboarding a contributor, (b) open-source release readiness,
and (c) your own prioritized improvement backlog.

Be brutally honest. Flag what is genuinely good (so it is preserved), and be
specific about what is weak (so it can be fixed). Avoid generic advice -- every
finding must reference concrete code, files, or patterns in THIS codebase.

## Project Context

SOVA (Software Orchestration Via Agents) -- an autonomous AI-assisted development
platform that any project can install to gain agent-driven issue triage, TDD
development, self-review, PR creation, CI monitoring, and continuous learning.

- **Stack**: Python 3.12+, Typer CLI, FastAPI dashboard (Jinja2 + Tailwind +
  Catppuccin dark theme), SQLAlchemy 2.0 async ORM (SQLite default / PostgreSQL),
  Pydantic Settings v2 + TOML config.
- **Scale**: ~15 Python modules under `sova/`, 550+ pytest tests, Ruff linting,
  ShellCheck for bash invariants.
- **Stage**: Post-rewrite (Phases 0-6 complete), heading toward first public release.
- **Key domains**: role-based agents (triage, researcher, developer, reviewer),
  workflow engine with step pipelines and gate checks, adapter pattern for task
  sources (GitHub/JIRA/Linear), handoff protocol (file + DB dual persistence),
  command distribution system, knowledge management (4-tier), scheduler + server
  daemon.
- **Architecture patterns**: ephemeral agents (spawn/work/handoff/die), worktree
  isolation per task, adapter ABC for task sources, service layer in dashboard,
  dual TaskRun write paths (dashboard outer + workflow inner), combined async
  server (dashboard + scheduler).
- **Deployment**: systemd + launchd service files, `sova server start/stop/status`.
- **Key files**: `AGENTS.md` (conventions), `.claude/rules/architecture.md`
  (component overview), `docs/REWRITE-PLAN.md` (phase history), `docs/VISION.md`
  (roadmap).

## Procedure

1. Read `AGENTS.md`, `CLAUDE.md`, and all `.claude/rules/*.md` for conventions.
2. Read `docs/REWRITE-PLAN.md` and `docs/VISION.md` for architectural context.
3. Walk each module under `sova/`: core, roles, adapters, llm, git, ipc, knowledge,
   scheduler, dashboard, commands, config, db, cli, utils.
4. If a focus area is specified, go deeper on that area; otherwise cover all modules.
5. Cross-reference what is documented (AGENTS.md, architecture.md) against what
   actually exists in the code.
6. Run `make check` to verify current test/lint status.
7. Produce the report in the output format below.

## Analysis Levels

Produce findings at four levels, top to bottom. Each level should surface issues
the levels above might miss.

### Level 1: Architecture & System Design
- Dependency graph health (circular imports between sova/ subpackages)
- Data model integrity (SQLAlchemy models, Alembic migration chain, index coverage)
- Security posture (subprocess spawning, shell injection surface in Claude CLI
  wrapper, file path traversal in worktree/handoff, secret handling in config)
- Concurrency design (asyncio patterns in scheduler/dashboard, Semaphore usage,
  race conditions in dual write paths, PID file lifecycle)
- Agent lifecycle correctness (spawn/handoff/die cycle, stale run recovery,
  idempotent finalization guards)
- Configuration management (sova.toml validation, env var overrides, multi-project
  registry, per-project gh auth)
- API design consistency (FastAPI router patterns, error responses, redirect
  handling for old routes)

### Level 2: Module Health
For each module under `sova/`, evaluate:
- Single responsibility -- does the module do one thing well?
- Interface boundaries -- are inter-module imports clean, or do modules reach
  into each other's internals?
- ABC/protocol compliance -- do implementations fully satisfy their abstract base?
- Service layer coverage (dashboard) -- is business logic in services, or leaking
  into routers or templates?
- Test coverage and quality -- are edge cases covered? Mock hygiene (AsyncMock
  patterns, monkeypatching service globals)?
- Documentation accuracy -- does `.claude/rules/architecture.md` match reality?

### Level 3: File-Level Quality
- Files that are too large / do too much (God files)
- Dead code, unused imports, orphaned templates or static assets
- Inconsistent patterns across similar files (e.g., one service caches, another
  does not; one step has proper gate checks, another does not)
- Configuration drift (duplicated constants, settings that shadow each other)
- Template quality (Jinja2 logic that belongs in services, deeply nested blocks)

### Level 4: Function / Class Granularity
- Functions that are too long or have too many responsibilities
- Missing or incorrect type hints (especially in async code)
- Error handling gaps (bare excepts, swallowed exceptions in non-fatal wrappers,
  missing validation at system boundaries)
- Naming inconsistencies (especially across the step/role/adapter boundaries)
- Complex conditionals that should be extracted or simplified
- Gate check completeness (validate_output must check all change forms)

## Evaluation Dimensions

Score each dimension 1-10 with a one-line justification:

| Dimension              | What to evaluate                                                   |
|------------------------|--------------------------------------------------------------------|
| **Correctness**        | Bugs, logic errors, race conditions, data integrity risks          |
| **Security**           | Subprocess injection, path traversal, secret exposure, auth bypass |
| **Performance**        | Async efficiency, DB query patterns, dashboard response times      |
| **Maintainability**    | Code clarity, consistent patterns, cognitive complexity            |
| **Testability**        | Coverage quality, test isolation, mock hygiene, ease of new tests  |
| **Extensibility**      | How hard is it to add a new adapter, role, step, or dashboard page?|
| **Operability**        | Logging (structlog), error reporting, daemon lifecycle, monitoring  |
| **Documentation**      | AGENTS.md accuracy, architecture.md freshness, onboarding path     |
| **Code Hygiene**       | Dead code, TODOs, style consistency, dependency freshness          |
| **Release Readiness**  | What would block a confident first public release today?           |

## Severity Classification

Classify every finding:

- **P0 -- Critical**: Data loss risk, security vulnerability, crash in happy path.
  Must fix before release.
- **P1 -- High**: Significant maintainability or reliability risk. Fix within the
  current milestone.
- **P2 -- Medium**: Code quality issue that compounds over time. Plan within the
  next 2 milestones.
- **P3 -- Low**: Nice-to-have improvement, style preference, minor inconsistency.
  Address opportunistically.
- **P4 -- Note**: Observation or suggestion. No action required but worth awareness.

## Output Format

### Executive Summary (5-10 sentences)
Overall health assessment. What is the single biggest risk? What is the strongest
aspect? Is this codebase ready for a contributor? For public release?

### Scorecard
Table of the 10 dimensions with scores and one-line justifications.

### Findings by Level
Group findings under Level 1-4 headers. Each finding:

```
#### [P{n}] {Short title}
**Location**: {file(s) or module(s)}
**Issue**: {What is wrong and why it matters -- concrete, not generic}
**Evidence**: {Code snippet, test gap, or specific example}
**Recommendation**: {Specific action to fix, not "consider improving"}
```

### Strengths (what to preserve)
List 5-10 things done well that a contributor should understand and maintain.

### Prioritized Action Plan
Top 10 findings ranked by (severity * effort-to-fix), with rough effort estimates
(hours/days). This is the "if you only have two weeks" list.

### Onboarding Gap Analysis
If a senior Python developer joined tomorrow with only the repo and its docs:
- What would they understand immediately?
- What would confuse them?
- What is undocumented but critical to know?
- What would they break on their first PR?

## Phase A: Generate docs/HEALTH-AUDIT.md

After completing the analysis and producing the report above, **write the full
audit to `docs/HEALTH-AUDIT.md`** as a local work artifact (gitignored).

The document must include:
- A tracking header with issue numbers for staleness detection (see below)
- Header with title, date, codebase stats (LOC, test count, lint status)
- All sections from the Output Format above
- A "Task Groups" section (populated in Phase B)
- An "Action Plan" table with issue links (populated in Phase C)

Format the file as standard Markdown. No frontmatter. No emojis.

### Tracking header

Add this metadata block at the very top of the file (before the title) so
other commands (`/status`, `/standup`) can detect audit progress:

```markdown
<!-- audit-tracker: issues=68,69,70,71,72,73,74 -->
```

The `issues=` field is a comma-separated list of all GitHub issue numbers
created in Phase C. This enables automated staleness detection.

### Staleness detection and auto-cleanup

Before generating a new audit, check if `docs/HEALTH-AUDIT.md` already exists.
If it does, read the `audit-tracker` comment and check issue status:

```bash
gh issue list --state open --json number --jq '.[].number'
```

Compare against the tracked issue numbers:
- **All issues closed** -> delete the file, inform the user that the previous
  audit cycle is complete, then proceed with the new audit.
- **Some issues still open** -> warn the user that N issues from the previous
  audit are still open, list them, and ask whether to proceed with a fresh
  audit (which will overwrite) or abort.

## Phase B: Group Findings into Task Batches

Group all findings into **session-sized task batches** -- sets of findings that:
1. Touch the same files or module area
2. Can be completed together in a single development session (2-6 hours)
3. Have clear dependency ordering between groups

For each group, define:
- **Group name**: short, descriptive (e.g., "DB session management")
- **Findings**: list of included findings with severity and title
- **Files touched**: concrete file paths
- **Estimated effort**: hours
- **Dependencies**: which groups must be completed first
- **Labels**: GitHub labels from the project taxonomy (`type:`, `priority:`, `area:`)

Target 5-10 groups. Do not over-fragment -- a group with 1 finding is too small
unless it truly stands alone.

Write the grouping into the "Task Groups" section of `docs/HEALTH-AUDIT.md`
with a dependency diagram showing execution order.

## Phase C: Create GitHub Issues

For each task group from Phase B, create a GitHub issue:

```bash
gh issue create --repo xsovad06/project-automation-kit \
  --title "<type>(scope): <group description>" \
  --body "$(cat <<'EOF'
## Objective

<1-2 sentence description of what this group achieves>

## Findings

<For each finding in the group:>

### [P{n}] {title}
- **Location**: {files}
- **Issue**: {description}
- **Recommendation**: {action}

## Files to Modify

- `path/to/file1.py`
- `path/to/file2.py`

## Estimated Effort

{N} hours

## Dependencies

{Links to prerequisite group issues, or "None"}

---
Source: `docs/HEALTH-AUDIT.md` -- Health Audit {date}
EOF
)" \
  --label "type: task" --label "priority: <priority>" --label "area: <area>" \
  --assignee xsovad06
```

After creating all issues, update the Action Plan table in `docs/HEALTH-AUDIT.md`
with the issue numbers and links.

### Pre-flight checks before creating issues

1. Run `gh issue list --state open --limit 50` to check for duplicates
2. Present the proposed issue list (titles + labels) to the user
3. Wait for explicit user confirmation before creating any issues
4. After creation, report all issue numbers

## Phase D: Generate Wave Prompts

After creating the issues, generate **one self-contained prompt per execution
wave**. A wave is a set of task groups that can be worked on in parallel because
they have no dependencies on each other. Each prompt is designed to be pasted
into a separate Claude Code session (or run as parallel agents).

### Wave structure

Build waves from the dependency graph produced in Phase B:

- **Wave 1**: groups with no dependencies (the critical path root)
- **Wave 2**: groups whose dependencies are all in Wave 1
- **Wave 3**: groups whose dependencies are all in Waves 1-2
- ...continue until all groups are assigned

Groups within the same wave can be run in **parallel sessions**.

### Prompt format

For each wave, generate a single fenced code block containing a prompt that:

1. **Opens with context**: which wave this is, which issues it covers, what was
   already completed in prior waves (if any).
2. **Lists every finding** in the wave's groups with:
   - The issue number and title
   - Each finding's location, issue description, and recommended fix
   - The exact files to modify
3. **Includes verification**: run `make check` after all changes, ensure 0 test
   failures and 0 lint warnings.
4. **Ends with commit instructions**: use conventional commits, one commit per
   logical change, no fix-on-fix.

If a wave contains multiple groups that can run in parallel, the prompt should
instruct Claude Code to use the Agent tool to spawn parallel sub-agents -- one
per group -- so all groups in the wave execute concurrently within a single
session.

### Output

Present the wave prompts to the user as numbered, fenced blocks they can
copy-paste:

```
## Wave 1 (sequential prerequisite)

Paste this into a Claude Code session:

\`\`\`
<prompt content>
\`\`\`

## Wave 2 (parallel -- 3 groups)

Paste this into a Claude Code session (spawns parallel agents internally):

\`\`\`
<prompt content>
\`\`\`
```

Also append the wave prompts to `docs/HEALTH-AUDIT.md` under a "## Wave Prompts"
section so they are preserved alongside the findings.

## Constraints
- Do NOT fabricate findings. If you are unsure whether an issue exists, say so and
  explain what you would check.
- Do NOT pad the report with generic best-practice advice. Every finding must be
  grounded in code you can see.
- Do NOT soften critical findings. If something is broken, say it is broken.
- DO acknowledge genuinely good patterns -- this is as important as finding problems.
- Provide file paths and line references where possible.
- Keep the total report actionable. A 200-finding dump is less useful than 30
  well-prioritized ones.
- GitHub account is always `xsovad06` (from AGENTS.md).
- Use existing label taxonomy: `type:` (feature/task/bug/refactor/test),
  `priority:` (critical/high/medium/low), `area:` (sova/dashboard/commands/docs).
- Do NOT create duplicate issues -- always check existing open issues first.
- Maximum 10 task groups. Each must be self-contained (a developer can pick it up cold).
- Before creating issues, show the proposed grouping and **ask for confirmation**.
