# CLI Reference

| Category | Commands |
|----------|----------|
| **Core** | `sova run <issue>`, `sova triage <issue>`, `sova harden <issue>`, `sova watch`, `sova parallel` |
| **Server** | `sova server start\|stop\|restart\|status\|digest\|install-service` |
| **Setup** | `sova install <path>`, `sova setup <path>`, `sova uninstall <path>`, `sova init-db`, `sova doctor` |
| **PR Ops** | `sova address-pr <pr>`, `sova maintain-pr <pr>`, `sova review-pr <pr>`, `sova linked-issues <pr> [--apply \| --mark-in-review]`, `sova pr-verdict <pr>`, `sova handoff-summary --source command:<name> --pr <pr>` (closing run summary, from stdin), `sova merge-subject <pr>` |
| **Monitor** | `sova status`, `sova costs`, `sova briefing` ([setup](awareness-setup.md)), `sova config`, `sova dashboard` |
| **Knowledge** | `sova assess set\|get\|clear <issue>` |
| **Commands** | `sova commands list\|diff\|update\|sync` |
| **Review retro** | `sova retro record <pr> [--force]`, `sova retro backfill [--dry-run]`, `sova retro report [--since YYYY-MM]`, `sova retro top-rules [--limit N] [--since YYYY-MM]` (the most repeated rules as JSON, read by `/review` and `/review-pr`): which recorded rules (bold-labelled bullets in AGENTS.md, CLAUDE.md, `.claude/rules/`, common-mistakes.md) each merged PR's review findings repeat, per month |
| **MCP** | `sova mcp serve` (blocking command tools, plus operator tools that drive a running `sova server`: status, pending handoffs and plan, start, stop, resume, handoff dismiss and action, plan approve and skip) |
| **Maintenance** | `sova cleanup` |

Run `sova --help` for the full list.

## Registering the MCP Server in Claude Code

Bind the server to one project so every tool acts on it, then allow the two read-only operator tools and leave the rest on ask, so each start, stop or approval shows Claude Code's permission prompt:

```bash
claude mcp add sova -- sova mcp serve --project /path/to/project
```

Put this in `.claude/settings.json` (project) or `~/.claude/settings.json` (user), merged into any existing `permissions.allow` list:

```json
{
  "permissions": {
    "allow": ["mcp__sova__sova_status", "mcp__sova__sova_pending"]
  }
}
```

The operator tools call the running `sova server` (or `sova dashboard`) over loopback at the configured `server.host` and `server.port`, then `dashboard.port`, so they go through the same duplicate, budget and suitability checks as the dashboard. A server started with `--port` on a port the config does not name is not found. Each call first asks the endpoint which project it serves, so a `sova server` started before this version existed (no identity route) must be restarted on the same SOVA version before the operator tools will act on it; an endpoint that is not a SOVA dashboard, or serves another project, is skipped in favor of the next configured port. That permission prompt is the human confirmation: tracker text the session reads (issue titles, handoff summaries) can carry instructions, and the tools never accept a force or budget override.
