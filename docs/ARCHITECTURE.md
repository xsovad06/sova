# Architecture

SOVA has four main components: CLI, Agent Core, Dashboard, and Scheduler.

```
CLI (sova/cli/)          -- Typer CLI with 33 subcommands
Agent Core (sova/core/)  -- WorkflowEngine, step pipelines, state machine
Dashboard (sova/dashboard/) -- FastAPI web UI with 32 routers and 48 services
Scheduler (sova/scheduler/) -- Watch loop, server daemon
```

Supporting modules: adapters (GitHub/Jira), LLM providers, git operations, IPC/handoff, knowledge system, config, and database.

The always-loaded summary (component overview, rule lines and pointers) is [`.claude/rules/architecture.md`](../.claude/rules/architecture.md); the full component notes, design decisions and per-module narrative are in [architecture-deep-dive.md](architecture-deep-dive.md).

For domain-specific implementation patterns, code examples, and gotchas, see the guideline files in this directory:

| File | Domain |
|------|--------|
| [security-guidelines.md](security-guidelines.md) | Credentials, sanitization, subprocess safety |
| [performance-guidelines.md](performance-guidelines.md) | Timeouts, caching, concurrency |
| [error-handling-guidelines.md](error-handling-guidelines.md) | Exceptions, logging, retries, fallbacks |
| [api-contracts-guidelines.md](api-contracts-guidelines.md) | Dashboard API, adapters, LLM formats |
| [database-guidelines.md](database-guidelines.md) | ORM, sessions, migrations |
| [testing-guidelines.md](testing-guidelines.md) | pytest patterns, fixtures, mocking |
| [integration-guidelines.md](integration-guidelines.md) | External services, handoff protocol |
| [architecture-deep-dive.md](architecture-deep-dive.md) | Full component, per-module and per-decision narrative entries, with the Key Design Decisions index; `.claude/rules/architecture.md` keeps a short rule line or index entry per topic |

## Project Structure

Moved from `AGENTS.md` (issue #1338).

```
sova/
  sova/                            # Python package (SOVA)
    cli/                           # Typer CLI (sova run, sova triage, etc.)
      app.py                       # Main app, subcommand registration
      commands/                    # Command modules (run, triage, harden, project, pr, admin, briefing, memory, assess, migrate, commands, server, mcp, supervisor, checks, board, audit)
    core/                          # Workflow engine, steps, state machine, context, output writer, DAG executor
    roles/                         # Agent roles (triage, researcher, developer, reviewer, custom, planner, dispatcher)
    adapters/                      # Task source plugins (github; jira, linear, manual planned)
    llm/                           # Claude CLI wrapper, cost tracking
    git/                           # Git operations (branch, pr, rebase), worktree management
    ipc/                           # Inter-process: handoff protocol, process control, notifications
    agents/                        # RuntimeAdapter ABC: where each coding-agent runtime (claude-code, codex) reads its commands/skills from on disk
    knowledge/                     # Local suitability assessments, tech-stack detection, per-PR review retrospectives
    scheduler/                     # Watch loop, parallel executor, server daemon
    dashboard/                     # FastAPI web UI
      app.py                       # App factory
      routers/                     # 32 API routers (auth, overview, runs, costs, briefing, feed, handoff, health, lifecycle, models, tasks, queue, quota, reliability, settings, setup, agents, work, issues, roles, spec, prs, dependencies, dependency_health, resources, supervisor, oversight, fleet_manager, fleet_insights, telemetry, a2a, mcp)
      services/                    # 48 services (run, cost, models, control, feed, handoff, health, lifecycle, queue, batch, reliability, work, work_item, work_board, work_state, work_verdict, settings, setup, agent_lifecycle, agent_output, agent_recovery, agent_adopt, agent_handoff, agent_pool, agent_db, agent_status, agent_context, agent_progress, agent_validation, agent_approval, agent_finalize, agent_resource, role, spec, pr, pr_metrics, resource, llm_suggestion, output_stream, fleet, fleet_manager, supervisor, oversight, telemetry_push, merge_queue_monitor, mcp_service, awareness, dependency_health)
      templates/                   # Jinja2 HTML (Catppuccin dark + Tailwind)
      static/                      # JS + CSS + favicon + logo; js/pages/<page>/ holds a page's ES modules (agents: 8 modules + actions.js + the render-utils.js leaf)
    a2a/                           # A2A (Agent-to-Agent) protocol: agent card generation, task state mapping
    mcp/                           # MCP stdio server (provider-agnostic agent tools via Model Context Protocol)
    dashboard/routers/mcp.py       # MCP HTTP endpoint for agent self-inspection (read-only runtime queries)
    dashboard/services/mcp_service.py  # MCP token generation/validation and tool handlers
    awareness/                     # Awareness subsystem (AwarenessProvider ABC, AwarenessItem, ItemCategory, provider registry, BriefingService aggregation engine, rendering models, rendering/cli_renderer.py Rich terminal output)
    oversight/                     # Oversight agent: background daemon, operations persona (user-maintained LLM guidance)
    supervisor/                    # Supervisor-level services (TaskProgressionEngine, CodeRabbit quota, task dependency graph, suitability classifier)
      gates/                       # Gate check functions (one per module): rate_limit, connectivity, memory_pressure, dependency, human_involvement, quota, ci_budget, slots, budget, already_running, repeated_failure, ownership, file_conflict, review_completed, stale_commands
    commands/                      # Command + guideline distribution (catalog, templates, manifest, distribution)
      data/commands/               # 28 canonical commands (markdown with category frontmatter); packaged here (not repo-root) so a wheel install ships them, resolved via sova.commands.catalog.get_canonical_dir()
      data/guidelines/             # Distributable guideline templates (installed to .claude/rules/)
      data/skills/                 # Distributable skill templates (installed to .claude/skills/, and rendered into .agents/skills/ for Codex)
    config/                        # Pydantic Settings + TOML config + project registry + request context
    monitoring/                    # Resource monitoring (psutil-based CPU, memory, I/O tracking)
    db/                            # SQLAlchemy ORM models + async session
    utils/                         # Logging, shell, formatting
  .claude/commands/                # Rendered copy Claude Code loads (generated by `make commands-render`)
  .agents/skills/                  # Rendered copy Codex loads: one sova-<name> SKILL.md per command, plus one bare <name> per distributable skill (generated by `make skills-render`)
  .claude-plugin/marketplace.json  # Claude Code plugin marketplace manifest (direct-install source, points at plugins/sova)
  plugins/sova/                    # Standalone Claude Code plugin: subset of commands (develop, spec, review, pr, debug, test) adapted for any project without installing SOVA
  .githooks/                       # Git hooks (tracked, mirroring CI checks)
  .github/scripts/                 # CI gate scripts (bash, unit-tested from tests/)
  invariants/                      # Pre-push constraint check scripts (bash)
  knowledge/
    KNOWLEDGE.md                   # 4-tier knowledge management system
  templates/                       # Project scaffolding templates
  deploy/                          # systemd + launchd service files
  tests/                           # pytest suite (13000+ tests)
  docs/
    VISION.md                      # Product vision and roadmap
    ARCHITECTURE.md                # Architecture overview (points to .claude/rules/)
    *-guidelines.md                # Domain-specific guidelines (7 files)
    design-system.md               # Dashboard design system reference
    handoff-protocol.md            # Agent handoff protocol
    naming-journey.md              # How SOVA got its name
  assets/
    agent-icon.png                 # Notification icon
```

## Agentic Workflow Commands

Moved from `AGENTS.md` (issue #1338).

This repo has Claude Code commands in `.claude/commands/`:

### Planning
- `/spec`: produce a structured spec document for an issue; approving it guides `/develop`

### Core Development
- `/develop`: implement a feature or fix (TDD approach)
- `/develop-full`: full workflow: TDD, lint, test, self-review, commit organization
- `/test`: run linter and tests iteratively
- `/review`: pre-push code review with scoring and auto-fix (>=3/10)
- `/review-full`: full pre-push pipeline: simplify -> review -> rearrange-commits
- `/debug`: systematic debugging workflow

### Pull Requests
- `/pr`: create PR with standard template (supports AI feedback + incremental workflows)
- `/rearrange-commits`: reorganize branch commits into clean steps

### Shipping Pipeline
- `/integrate-pr`: full pipeline: rebase, CI, merge, cleanup, learn (one click)
- `/agent-resume`: smart router: assess PR state and decide next action

### Post-Work
- `/after-merge`: post-merge cleanup
- `/ingest-review`: extract review feedback into agent memory
- `/extract-knowledge`: promote learnings across knowledge tiers

### Project Management
- `/new-feature`: set up a new feature branch
- `/standup`: daily context summary
- `/find-task`: browse GitHub Issues backlog

## Other Documents

Every other file under `docs/`, grouped by purpose. Dated reports and drafts are kept as historical records, not as current guidance.

| File | Purpose |
|------|---------|
| [design-system.md](design-system.md) | Dashboard design system reference |
| [handoff-protocol.md](handoff-protocol.md) | Agent handoff file format and lifecycle |
| [awareness-setup.md](awareness-setup.md) | Setting up the awareness briefing providers |
| [troubleshooting-config.md](troubleshooting-config.md) | Diagnosing config load and agent spawn failures |
| [invariants.md](invariants.md) | Full rules, exemptions and history of each pre-push and CI invariant |
| [workflow-history.md](workflow-history.md) | Full text and incident history behind `.claude/rules/workflow.md`'s rules |
| [token-compression.md](token-compression.md) | Optional RTK and Headroom token compression setup |
| [agent-spawn-guide.md](agent-spawn-guide.md) | Troubleshooting an agent that fails before its first step |
| [github-token-setup.md](github-token-setup.md) | Making the `gh` token reachable from agent subprocesses |
| [issue-organization.md](issue-organization.md) | Organizing issues with phase milestones for the Priority Queue |
| [authentication-ui-design.md](authentication-ui-design.md) | Design record for the dashboard Connections page |
| [pipeline-determinism.html](pipeline-determinism.html) | Pipeline determinism analysis |
| [benchmark-results.md](benchmark-results.md) | SOVA vs interactive velocity benchmark, 2026-08-10 (written by `/benchmark-report`) |
| [failure-audit-2026-08-20.md](failure-audit-2026-08-20.md) | Failure rate audit, 2026-08-20 |
| [blog-post-draft.md](blog-post-draft.md) | Unpublished blog post draft |
| [naming-journey.md](naming-journey.md) | How SOVA got its name |

### Specs

Design specs for shipped or planned work. Specs written by `/spec` during development live in `.claude/specs/` instead.

| File | Purpose |
|------|---------|
| [specs/571-benchmark-report-command.md](specs/571-benchmark-report-command.md) | `/benchmark-report` command (#571) |
| [specs/issue-602-llm-planning.md](specs/issue-602-llm-planning.md) | LLM planning step for the supervisor (#602) |
| [specs/awareness-use-cases.md](specs/awareness-use-cases.md) | Awareness subsystem use cases |
| [specs/awareness-implementation-spec.md](specs/awareness-implementation-spec.md) | Awareness subsystem implementation spec |
| [specs/model-selection-research-context.md](specs/model-selection-research-context.md) | Original model selection briefing, superseded by [model-selection-architecture.md](model-selection-architecture.md) |
| [specs/model-selection-task-plan.md](specs/model-selection-task-plan.md) | Model selection task plan (epic #1031) |
