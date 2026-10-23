# Architecture

SOVA has four main components: CLI, Agent Core, Dashboard, and Scheduler.

```
CLI (sova/cli/)          -- Typer CLI with 29 subcommands
Agent Core (sova/core/)  -- WorkflowEngine, step pipelines, state machine
Dashboard (sova/dashboard/) -- FastAPI web UI with 33 routers and 49 services
Scheduler (sova/scheduler/) -- Watch loop, server daemon
```

Supporting modules: adapters (GitHub/Jira), LLM providers, git operations, IPC/handoff, knowledge system, config, and database.

For the full architecture reference (component details, design decisions, and key patterns), see [`.claude/rules/architecture.md`](../.claude/rules/architecture.md).

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
| [architecture-deep-dive.md](architecture-deep-dive.md) | Full per-module and per-decision narrative entries indexed (not duplicated) in `.claude/rules/architecture.md` |

## Other Documents

Every other file under `docs/`, grouped by purpose. Dated reports and drafts are kept as historical records, not as current guidance.

| File | Purpose |
|------|---------|
| [design-system.md](design-system.md) | Dashboard design system reference |
| [handoff-protocol.md](handoff-protocol.md) | Agent handoff file format and lifecycle |
| [awareness-setup.md](awareness-setup.md) | Setting up the awareness briefing providers |
| [troubleshooting-config.md](troubleshooting-config.md) | Diagnosing config load and agent spawn failures |
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
