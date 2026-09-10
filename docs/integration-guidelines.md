# Integration Guidelines

Patterns for integrating with external services in the SOVA codebase.

## Subprocess Execution

All external process calls go through `sova/utils/shell.py`. Never use `subprocess` directly.

| Function | Behavior | Use when |
|----------|----------|----------|
| `run(*args, env=, timeout=)` | Returns `ShellResult`, never raises | Caller checks `.success` |
| `run_checked(*args)` | Raises `RuntimeError` on non-zero exit | Failure is always fatal |

- Uses `asyncio.create_subprocess_exec` (no shell expansion, no injection risk)
- Default timeout: 300s. Kills the process and returns `ShellResult(returncode=-1)`
- Output decoded with `errors="replace"` for non-UTF-8 safety
- Stderr truncated to 200 chars in log messages
- Pass credentials via `env=` dict, never as CLI arguments

## GitHub (gh CLI)

### Credential Injection

`sova/utils/gh.py:resolve_gh_env()` is the single auth entry point:

```python
env = await resolve_gh_env(github_user)  # {**os.environ, "GH_TOKEN": token}
await run("gh", "pr", "view", ..., env=env)
```

The GitHub adapter centralizes this in a private `_gh()` method so every call inherits correct auth.

### Gotchas

- **`GH_TOKEN` env var overrides `gh auth switch`**: unset it before switching accounts
- **`gh auth switch` does not persist across subprocesses**: use `resolve_gh_env()` per-call
- **`gh pr create` returns plain text (URL), not JSON**: parse PR number from the URL path
- **`gh pr edit --title/--body` requires `read:org` scope**: fails with "missing required scopes [read:org]" when the token only has `repo` access. Use the REST API instead: `gh api -X PATCH repos/<owner>/<repo>/pulls/<number> -f title="..." -f body="..."` (needs only `repo` scope). Pass `-f` values as separate flags rather than combining inline text with a body read via process substitution in one call: do title and body as two calls if one is inline and the other multi-line. For a multi-line markdown body alone, `gh api -X PATCH .../pulls/<number> --input file.json` (`file.json` = `{"body": "..."}`) avoids the quoting problem entirely.

### PR Reviews vs Comments

| Method | API | Result |
|--------|-----|--------|
| `post_pr_comment()` | `gh pr comment` | Conversation-level comment in timeline |
| `post_pr_review()` | `gh api repos/{repo}/pulls/{pr}/reviews` | Formal review with inline code comments |

`post_pr_review()` sends JSON via stdin (`--input -`). If inline comments fail (422), retries body-only. GitHub returns 422 for `APPROVE`/`REQUEST_CHANGES` on own PRs; fall back to `event=COMMENT`.

### GraphQL

Used for Projects V2 board operations via `gh api graphql -f query=...`. Board metadata is cached in `_board_meta` to avoid repeated fetches.

### JSON Parsing

Always wrap `json.loads(result.stdout)` in `try/except (json.JSONDecodeError, TypeError)`. The `TypeError` handles `None` stdout from edge cases.

## Jira Cloud

`sova/adapters/jira.py` uses `httpx.AsyncClient` with Basic auth (base64 `email:api_token`). Lazily initialized.

- **JQL sanitization**: `_sanitize_jql_value()` strips `"`, `\`, and control chars
- **ADF format**: comments use Atlassian Document Format JSON, not plain text
- **State via labels**: same `agent:` label pattern as GitHub, with priority chain
- **PR operations are no-ops**: `post_pr_comment`, `post_pr_review`, `get_pr_reviews` log and return empty

For setup instructions, JQL filter recipes, and status mapping configuration, see [JIRA Configuration Guide](jira-configuration-guide.md).

### Atlassian MCP sidecar (Confluence + enhanced Jira)

`[mcp.atlassian]` (`AtlassianMCPConfig` in `sova/config/models.py`) configures [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) as an opt-in MCP sidecar server for the Claude Code CLI, separate from the `TaskAdapter` used for issue state transitions. It gives agents read access to Confluence pages and richer Jira queries during research/development, not a replacement for the Jira adapter's workflow-level operations.

- `mcp.atlassian.enabled` (default `False`): opt-in per project
- `auth_type`: `"pat"` (on-prem Personal Access Token, e.g. Red Hat's `issues.redhat.com`) or `"api_token"` (Cloud, paired with `email`)
- `read_only` (default `True`): mcp-atlassian's `READ_ONLY_MODE` env var; writes still go through the `TaskAdapter`
- `toolsets`: mcp-atlassian's `TOOLSETS` env var (e.g. `jira_read`, `confluence_read`, `confluence_search`)
- `sova/utils/mcp_config.py:build_atlassian_mcp_server_config()` builds the server entry (`uvx mcp-atlassian` plus auth env vars); `sova/cli/commands/project.py:_configure_atlassian_mcp()` writes it during `sova install` when enabled, mirroring the existing PatternFly MCP auto-configuration
- **The entry goes in `<project>/.mcp.json`, not `.claude/settings.json`**: the Claude Code CLI reads project-scope MCP servers only from `.mcp.json` and ignores an `mcpServers` key inside settings.json, so a server written there is silently never started. `.mcp.json` is resolved by walking up from the working directory, so agents running in `.claude/worktrees/{id}` inherit the project's servers
- Servers declared in `.mcp.json` stay pending until approved, and a headless agent never sees the approval prompt, so `set_project_mcp_approval()` also adds the server name to `enabledMcpjsonServers` in `.claude/settings.json`
- Injection is an upsert, not add-only: a changed config (rotated token, new URL, different toolsets) is rewritten on the next `sova install`, and an unchanged one is left alone
- Credentials: with `mcp.atlassian.token` set, the literal token lands in `.mcp.json` and the file is restricted to mode 600 (a warning says so, since `.mcp.json` is conventionally committed). Leave the token empty and export `SOVA_MCP_ATLASSIAN_TOKEN` instead to emit a `${SOVA_MCP_ATLASSIAN_TOKEN}` placeholder that the CLI expands at launch, keeping the secret off disk
- `atlassian_config_problems()` gates injection: an enabled sidecar with no URL, no credential, or `auth_type = "api_token"` without an `email` is skipped with the missing keys listed, instead of writing an entry that cannot start
- Enabling the sidecar in the dashboard or `sova.toml` does not write the entry on its own: re-run `sova install` (or `sova install --update`) to propagate the change
- Graceful degradation is inherent to the MCP sidecar model: if the server fails to start or auth fails, the Claude CLI simply has no Confluence/Jira tools available that turn; no SOVA-side fallback code is needed

## SonarCloud and CodeRabbit

`sova/adapters/external_reviews.py` fetches findings from both services.

- **SonarCloud**: `curl` via `run()` with Bearer `SONAR_TOKEN` from env. Proceeds without auth for public projects.
- **CodeRabbit**: `gh api graphql` via `run()`. Filters threads by bot author, skips resolved.
- Both return empty lists on any failure
- `_fetch_coderabbit_threads()` truncates message bodies to 500 chars

## LLM Providers

### Provider ABC (`sova/llm/provider.py`)

`LLMProvider` ABC with factory `create_provider(cfg: LLMConfig)`. It takes the whole `llm` config section, not individual kwargs, so a newly added field cannot silently no-op at a call site that was never updated to forward it. Four backends: `claude-code` (default), `litellm`, `hybrid`, `anthropic`. Module-level singleton via `get_provider()`/`set_provider()`.

### Claude Code CLI (`sova/llm/providers/claude_code.py`)

Shells out to `claude -p <prompt> --output-format json|stream-json`. Model aliases: `fast`->sonnet, `smart`->opus, `cheap`->haiku.

### LiteLLM (`sova/llm/litellm_provider.py`)

Automatic fallback: primary model fails, `fallback_model` is tried. Optional dependency guarded by `_HAS_LITELLM` flag.

### Wiring Requirement

Adding a provider config field without calling `set_provider(create_provider(cfg.llm))` at startup means the config has no effect. Wire in CLI (`sova/cli/app.py`) and dashboard (`sova/dashboard/app.py`); both delegate to `reload_provider(cfg)`, which is also the settings hot-reload path.

## Desktop Notifications (`sova/ipc/notifications.py`)

Fire-and-forget via `asyncio.create_task()`. Three backends by platform:

| Backend | Platform | Mechanism |
|---------|----------|-----------|
| `terminal-notifier` | macOS (preferred) | `run()` with `-appIcon`, `-group`, `-sound` |
| JXA | macOS (fallback) | `osascript -l JavaScript`, `json.dumps()` for escaping |
| `notify-send` | Linux | Standard `run()` call |

All paths wrapped in `try/except` -- notifications never crash the pipeline.

## Agent Process Control (`sova/ipc/control.py`, `sova/ipc/runtime.py`)

`ClaudeCodeRuntime.spawn()` runs `claude -p` with `--permission-mode auto`. A headless preamble instructs the model to act without confirmation. CLI commands must be framed as bash code blocks to prevent natural language interpretation. `AgentProcess` is a generic async subprocess handle; runtime-specific spawning logic lives in the corresponding `AgentRuntime` implementation.

Lifecycle: SIGTERM with configurable wait, then SIGKILL. `ProcessTracker` maps `task_run_id -> AgentProcess` for crash detection.

## Handoff File I/O (`sova/ipc/handoff.py`)

Per-issue files at `.claude/agent-control/handoff-{issue}.json` with legacy `handoff.json` fallback. Issue identifiers sanitized via regex (`^[A-Za-z0-9_-]+$`).

- `write_handoff_file()` / `read_handoff_file(issue=N)` for dashboard polling
- `write_handoff()` / `read_handoff()` for DB persistence (`TaskRun.handoff_json`)
- `read_all_handoff_files()` globs all per-issue files, sorted by `created_at` desc
- Pydantic `model_validate()` enforces schema. Parse errors return `None`, never raise

## Error Handling Tiers

| Tier | Behavior | Used for |
|------|----------|----------|
| Hard (raise `RuntimeError`) | Caller must handle | Primary operations: task fetch, PR create, state transition |
| Soft (log warning, return default) | Pipeline continues | Side effects: board moves, label changes, notifications |
