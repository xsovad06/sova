# Ideas from Morning Agent Evolution Plan

> **Source**: Morning Agent Evolution Plan document (April 2026)
> **Date captured**: 2026-04-17
> **Purpose**: Reference document for ideas extracted from the morning-agent predecessor

## High-Value Ideas

### 1. Intelligent Model Routing

Dynamically select models based on task complexity instead of using a single static
`AGENT_MODEL` config value:

- **Opus**: architectural decisions, complex debugging, investigation spikes
- **Sonnet**: routine PR maintenance, CI retests, straightforward bug fixes
- **Haiku**: log parsing, status checks, notification formatting, CI failure classification

Starting point: CI failure classifier uses Haiku to classify, Sonnet to fix, Opus only for
genuinely hard failures. The cost tracking infrastructure already captures per-phase spend --
adding model-level routing would significantly reduce costs while maintaining quality where it
matters.

**Status**: Model routing is not yet implemented. `sova.toml` uses a static model setting.

### 2. Agent Self-Improvement / Self-Assessment

Periodic self-assessment phase that synthesizes performance patterns and updates the agent's
own configuration:

- Track first-pass approval rate (PRs that pass review without rework)
- Measure time-to-merge by task complexity
- Identify which persona rules lead to better outcomes
- Flag recurring CI failure patterns that should become known-flaky entries
- Correlate cost per phase with task characteristics

The SQLite memory system and cost JSONL provide the raw data -- the missing piece is a periodic
analysis that turns data into actionable config changes.

**PAK status**: Cost JSONL and memory infrastructure exist. No self-assessment logic.

### 3. Team Knowledge Sharing

Shared knowledge layer across PAK installations so that learnings propagate between developers:

- Centralized memory service or sync protocol between installations
- Merge/sync mechanism that respects per-developer preferences
- Share generalizable knowledge: review feedback patterns, codebase conventions, common mistakes
- Existing SQLite memory schema supports categories and tags

The difference between one developer's agent getting smarter vs. the entire team's agents
getting smarter together.

**PAK status**: VISION.md mentions `~/.claude/shared-knowledge/` in setup wizard options. Nothing implemented.

### 4. VM Deployment / Always-On Operation

Move from developer laptop to a VM for continuous operation:

- Replace macOS `terminal-notifier` with email/webhook/Slack notifications
- systemd service management for the watch mode loop
- Secure credential storage
- Morning briefing becomes a report the developer reads on arrival
- Dashboard becomes the primary interface (already web-based)
- Hybrid model: laptop for interactive sessions, VM for monitoring and routine maintenance

**PAK status**: Watch mode exists in orchestrator. No deployment wrapper or notification abstraction.

### 5. Task Complexity Dispatcher

When the priority scanner identifies simple, well-scoped tickets (dependency bumps, config
changes, straightforward bugs with clear repro steps), route them to a different agent
configuration or a headless bot:

- Task complexity classifier (simple vs. complex)
- Routing rules: simple tasks to lightweight agent config, complex to full human-agent pair
- Could integrate with external bots (e.g., Dependabot, Renovate, or team-specific headless agents)
- The agent becomes a smart dispatcher -- triage, route, and monitor

**PAK status**: No complexity classification or routing logic exists.

## Medium-Value Ideas

### 6. PagerDuty / Alertmanager Integration

Extend CI failure classification to production incidents:

- Triage production alerts automatically
- Correlate alerts with recent deploys
- Suggest hotfixes based on the correlation
- Natural extension of the existing CI failure classification system

**PAK status**: Not applicable until PAK is deployed for services with production monitoring.

### 7. Integration Expansion (Confluence, Google Sheets)

MCP-based integrations for team knowledge and reporting:

- **Confluence MCP**: Read team documentation, ADRs, runbooks. Agent references team knowledge
  when investigating or choosing approaches.
- **Google Sheets MCP**: Track metrics, generate sprint reports, maintain project dashboards.
  Natural extension of cost tracking into team-visible reporting.

**PAK status**: MCP architecture supports this. No specific integrations planned yet.
