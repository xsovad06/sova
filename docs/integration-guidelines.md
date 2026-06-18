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

## SonarCloud and CodeRabbit

`sova/adapters/external_reviews.py` fetches findings from both services.

- **SonarCloud**: `curl` via `run()` with Bearer `SONAR_TOKEN` from env. Proceeds without auth for public projects.
- **CodeRabbit**: `gh api graphql` via `run()`. Filters threads by bot author, skips resolved.
- Both return empty lists on any failure
- `_fetch_coderabbit_threads()` truncates message bodies to 500 chars

## LLM Providers

### Provider ABC (`sova/llm/provider.py`)

`LLMProvider` ABC with factory `create_provider(type)`. Two backends: `claude-code` (default) and `litellm`. Module-level singleton via `get_provider()`/`set_provider()`.

### Claude Code CLI (`sova/llm/providers/claude_code.py`)

Shells out to `claude -p <prompt> --output-format json|stream-json`. Model aliases: `fast`->sonnet, `smart`->opus, `cheap`->haiku.

### LiteLLM (`sova/llm/litellm_provider.py`)

Automatic fallback: primary model fails, `fallback_model` is tried. Optional dependency guarded by `_HAS_LITELLM` flag.

### Wiring Requirement

Adding a provider config field without calling `set_provider(create_provider(cfg))` at startup means the config has no effect. Wire in CLI (`sova/cli/app.py`) and dashboard (`sova/dashboard/app.py`).

## Desktop Notifications (`sova/ipc/notifications.py`)

Fire-and-forget via `asyncio.create_task()`. Three backends by platform:

| Backend | Platform | Mechanism |
|---------|----------|-----------|
| `terminal-notifier` | macOS (preferred) | `run()` with `-appIcon`, `-group`, `-sound` |
| JXA | macOS (fallback) | `osascript -l JavaScript`, `json.dumps()` for escaping |
| `notify-send` | Linux | Standard `run()` call |

All paths wrapped in `try/except` -- notifications never crash the pipeline.

## Agent Process Control (`sova/ipc/control.py`)

`AgentProcess.spawn()` runs `claude -p` with `--permission-mode auto`. A headless preamble instructs the model to act without confirmation. CLI commands must be framed as bash code blocks to prevent natural language interpretation.

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
