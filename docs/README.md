# SOVA Documentation

Every document in this folder, grouped by what you are trying to do. New here? Read the [project README](../README.md) first, then [Configuration](configuration.md) and the [Dashboard](dashboard.md).

## Use and operate SOVA

| Doc | What it covers |
|-----|----------------|
| [configuration.md](configuration.md) | Where settings live, the minimal config and the key optional sections |
| [dashboard.md](dashboard.md) | Every dashboard page, the Work board's stages, and phone access over Tailscale |
| [cli-reference.md](cli-reference.md) | The `sova` commands by category, and registering the MCP server in Claude Code |
| [notifications.md](notifications.md) | Desktop, Slack, email, webhook and ntfy notification backends |
| [server-operations.md](server-operations.md) | Running the always-on server: service install, restarts that keep agents alive, health and logs |
| [always-on-mac-setup.md](always-on-mac-setup.md) | Leaving a Mac on to run SOVA unattended: sleep, launchd, restarts, phone access |
| [ai-provider-setup-guide.md](ai-provider-setup-guide.md) | LLM provider setup: logins, API keys, Vertex AI, credentials and troubleshooting |
| [codex-runtime-setup.md](codex-runtime-setup.md) | Running pipeline steps through OpenAI's Codex CLI: auth, sandbox, rollback |
| [jira-configuration-guide.md](jira-configuration-guide.md) | Jira as a task source: setup, JQL filters, status mapping |
| [awareness-setup.md](awareness-setup.md) | Setting up the daily briefing providers (Gmail, Calendar and others) |
| [commit-convention-setup.md](commit-convention-setup.md) | Per-project commit types and scopes for generated commit subjects and PR titles |
| [github-token-setup.md](github-token-setup.md) | Making the `gh` token reachable from agent subprocesses |
| [token-compression.md](token-compression.md) | Optional RTK and Headroom token compression |
| [issue-organization.md](issue-organization.md) | Organizing issues with phase milestones for the Priority Queue |
| [handoff-protocol.md](handoff-protocol.md) | The agent handoff file format and lifecycle |
| [troubleshooting-config.md](troubleshooting-config.md) | Diagnosing config load and agent spawn failures |
| [agent-spawn-guide.md](agent-spawn-guide.md) | Troubleshooting an agent that fails before its first step |

## Understand the design

| Doc | What it covers |
|-----|----------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture overview and the project structure |
| [architecture-deep-dive.md](architecture-deep-dive.md) | Full component, module and design-decision entries |
| [VISION.md](VISION.md) | Product vision and roadmap |
| [model-selection-architecture.md](model-selection-architecture.md) | LLM model routing, provider abstraction and model discovery |
| [model-selection-migration-guide.md](model-selection-migration-guide.md) | Config shape for the openai, ollama and vertex provider types |
| [model-selection-risk-assessment.md](model-selection-risk-assessment.md) | Model selection risks, migration path and rollback |
| [openai-only-runbook.md](openai-only-runbook.md) | Running one pipeline OpenAI-only: model fields, isolation, rollback |
| [design-system.md](design-system.md) | Dashboard design system reference |
| [authentication-ui-design.md](authentication-ui-design.md) | Design record for the dashboard Connections page |
| [pipeline-determinism.html](pipeline-determinism.html) | Pipeline determinism analysis |
| [deferred-consolidation.md](deferred-consolidation.md) | Consolidation work an audit deferred, and what landed |

## Contribute

| Doc | What it covers |
|-----|----------------|
| [testing-guidelines.md](testing-guidelines.md) | pytest patterns, fixtures, mocking, test isolation |
| [security-guidelines.md](security-guidelines.md) | Credentials, input sanitization, subprocess safety |
| [performance-guidelines.md](performance-guidelines.md) | Timeouts, caching, concurrency, async patterns |
| [error-handling-guidelines.md](error-handling-guidelines.md) | Exceptions, logging, retries, fallbacks, circuit breakers |
| [api-contracts-guidelines.md](api-contracts-guidelines.md) | Dashboard API, adapters, LLM output formats |
| [database-guidelines.md](database-guidelines.md) | ORM models, session management, migrations |
| [integration-guidelines.md](integration-guidelines.md) | GitHub, Jira and SonarCloud integration, the handoff protocol |
| [invariants.md](invariants.md) | The pre-push and CI checks in `invariants/`, rule by rule |
| [workflow-history.md](workflow-history.md) | The full text and incident history behind the workflow rules |
| [commands/health-audit.md](commands/health-audit.md) | Maintainer rationale for the health-audit command |

## Specs, results and archive

| Doc | What it covers |
|-----|----------------|
| [specs/awareness-use-cases.md](specs/awareness-use-cases.md) | Awareness subsystem use cases |
| [specs/awareness-implementation-spec.md](specs/awareness-implementation-spec.md) | Awareness subsystem implementation spec |
| [specs/model-selection-research-context.md](specs/model-selection-research-context.md) | The original model selection briefing, superseded by the architecture doc |
| [specs/model-selection-task-plan.md](specs/model-selection-task-plan.md) | Model selection task plan |
| [benchmark-results.md](benchmark-results.md) | SOVA versus interactive development velocity benchmark, 2026-08-10 |
| [archive/blog-post-draft.md](archive/blog-post-draft.md) | Unpublished blog post draft |
| [archive/naming-journey.md](archive/naming-journey.md) | How SOVA got its name |
