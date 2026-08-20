# Architecture

SOVA has four main components: CLI, Agent Core, Dashboard, and Scheduler.

```
CLI (sova/cli/)         : Typer CLI with 24 top-level commands (43 total with subcommands)
Agent Core (sova/core/) : WorkflowEngine, step pipelines, state machine
Dashboard (sova/dashboard/): FastAPI web UI with 26 routers and 39 services
Scheduler (sova/scheduler/): Watch loop, parallel executor, server daemon
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
