# Security Guidelines

Security conventions and guardrails specific to the SOVA codebase: credential handling, input sanitization, subprocess safety, and prompt injection mitigation.

For vulnerability reporting, see [SECURITY.md](/SECURITY.md).

## Credential Management

### GitHub Authentication

Per-project auth via `sova/utils/gh.py:resolve_gh_env()`:

- Tokens fetched fresh from `gh auth token --user` on each call, never cached
- `GH_TOKEN` injected via subprocess `env` parameter, not globally
- Returns `None` if no user configured (inherits parent environment)

Every `gh` CLI call in `sova/adapters/github.py` and `sova/git/pr.py` passes `env=await resolve_gh_env(github_user)`.

### Jira API Token

`jira_api_token` in `sova/config/models.py` uses `Field("", repr=False)` to suppress from Pydantic repr, preventing accidental logging. Store as `SOVA_TASK_JIRA_API_TOKEN` env var. See [JIRA Configuration Guide](jira-configuration-guide.md) for full setup.

### LLM API Key (Anthropic provider)

`llm.api_key` is resolved through the optional OS keyring (`sova/llm/keyring_store.py`, wraps the `keyring` package behind an import guard) before falling back to the plaintext `project_settings` database row, then `ANTHROPIC_API_KEY`. When a keyring backend is available, the settings layer (`sova/dashboard/services/settings_service.py`) moves a newly-saved key into the keyring and replaces the database row with a non-secret sentinel (`keyring_store.SENTINEL`); resolution happens once, at `create_provider()`'s anthropic branch (`sova/llm/provider.py`), not in `load_config()`, so the key never re-enters `cfg.model_dump()`. `get_config()` masks every `value_type="secret"` setting with a fixed placeholder before it can reach the settings API response. Only `key in keyring_store.RESOLVED_SECRET_KEYS` (today: `llm.api_key` alone) is actually routed through the keyring: every other `value_type="secret"` setting (Jira token, SMTP credentials, webhook secrets, MCP token secret, telemetry hub token) still reads its plaintext database value directly at its one consumption site, and moving its storage without updating that site would silently replace a working credential with the sentinel string. An existing plaintext key is never migrated automatically; the dashboard's "Move to keychain" action (`POST /settings/config/migrate-secret`) is the only way to relocate one, and it verifies the keyring write by reading it back before clearing the database row.

### Codex/OpenAI Authentication

For end-to-end setup instructions (browser login with keyring storage, the device-auth and stdin API-key fallbacks for browser-less environments, rollback to Claude Code, and remediation for a stray global `CODEX_API_KEY`/`OPENAI_API_KEY`), see [Codex Runtime Setup Guide](codex-runtime-setup.md). The rest of this section covers the credential-handling mechanics inside SOVA's own code.

Codex CLI owns its own credential storage; SOVA never reads, copies, masks, or persists it. Two distinct auth paths:

- **Saved keyring auth** (preferred for a local, long-running server): one-time `codex login`, or `cli_auth_credentials_store = "keyring"` in Codex's own config. The credential lives entirely inside Codex's credential store; SOVA's only interaction is a read-only readiness probe (`codex login status`, never a model request).
- **Per-process automation auth** (`CODEX_API_KEY`): an opt-in escape hatch for trusted automation that explicitly wants environment-based auth instead of keyring. Never written to argv, config, the database, shell profiles, or file-backed output. `CODEX_API_KEY` is in `sova/utils/env.py`'s `SCRUBBED_VARS`, so it is stripped from every spawned agent by default; only `CodexRuntime.spawn()` re-injects it scoped to the immediate `codex exec` child (via `extra_env`, applied after scrubbing). The reviewer role's `spawn_direct()` call (`agent_lifecycle.py`, via the shared `_codex_extra_env()` helper) is a wider exception: it re-injects the key into the reviewer's whole `sova run` process (and everything that process spawns), not just one child, because its optional read-only Codex repo-context sub-call (`ReviewerRole._gather_repo_context()`) runs as a plain subprocess from inside that already-scrubbed reviewer process rather than through `CodexRuntime`. To keep that wider exposure opt-in rather than ambient, `agent_lifecycle.py` only computes `_codex_extra_env()` for the reviewer when `review.repo_context_agent` (default off) is enabled for the project; with the feature off, the key is never re-admitted into the reviewer's environment at all, even if set in the server's own process environment. It still never reaches a `ClaudeCodeRuntime` or `AiderRuntime` child, or any other direct-spawn role.

Codex spawns also scrub `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (via `extra_scrub`, which wins over `agent.env_passthrough`) so a credential meant for Claude Code cannot leak into an unrelated Codex child. `check_available()` reports credential presence as a boolean only (`", CODEX_API_KEY set"`), never the value.

`OPENAI_API_KEY` is also in `SCRUBBED_VARS`, so it is stripped from every spawned child by default, same as `CODEX_API_KEY`. Its one re-injection path is narrower than `CODEX_API_KEY`'s: `spawn_direct()` (`sova/ipc/runtime.py`), the launcher for the developer/researcher/planner pipeline roles, re-admits it via `_openai_extra_env()` (applied after scrubbing, mirroring `_codex_extra_env()`), because that subprocess's own `WorkflowEngine` may need it for an in-process litellm call (`sova/llm/litellm_provider.py`) when the project configures an OpenAI-backed model. No `AgentRuntime` spawn (`ClaudeCodeRuntime`, `AiderRuntime`, `CodexRuntime`) ever receives it. The parent server's own in-process litellm/OpenAI SDK read is a separate, unaffected path: it reads `os.environ` directly, and `scrub_agent_env()` only transforms the dict handed to a *spawned* child. Neither `OPENAI_API_KEY` nor `CODEX_API_KEY` can be re-admitted by naming it in `agent.env_passthrough`: `scrub_agent_env()` strips both out of the passthrough keep-set unconditionally, since that escape hatch exists for provider-routing vars (Vertex/Bedrock), not for credentials, and is global across every runtime rather than scoped to one trusted call site.

`CodexRuntime.spawn()` delivers the workflow prompt via stdin instead of argv when `_probe_codex_stdin_prompt_support()` confirms the installed Codex CLI documents reading an omitted `PROMPT` from stdin (read via `codex exec --help`, never a model request). This closes the `ps`/`pkill -f` argv-visibility gap for a CLI that supports it; a CLI the probe cannot confirm, or one that errors/times out, falls back to the pre-existing argv delivery.

### Worktree Credential Copying

`WorktreeConfig.copy_files` defaults to `[".env", ".env.local"]`. These are copied into new worktrees so agents can authenticate. Ensure `.gitignore` covers these patterns.

### SQLite DB File Mode

`.claude/sova.db` stores `project_settings`, which is where every secret without a `RESOLVED_SECRET_KEYS` keyring entry (Jira token, SMTP credentials, webhook secrets, MCP token secret, telemetry hub token) lives in plaintext. `sova/db/session.py:_secure_db_file()` chmods the file to `0600` once per creation (`init_db()`, `init_db_for_project()`) and once per `.db.bak` backup copy (`_backup_db()`), rather than on every `get_session()` call, so group/world access is denied regardless of the process umask without adding a syscall to every DB access.

| Do | Do Not |
|----|--------|
| Use `resolve_gh_env()` for subprocess auth | Set `GH_TOKEN` globally in your shell |
| Use `repr=False` on secret Pydantic fields | Log config objects containing tokens |
| Keep secrets in `.env` (gitignored) | Add secrets to `sova.toml` or the DB |

## Subprocess Execution Safety

All subprocess calls go through `sova/utils/shell.py:run()`, which uses `asyncio.create_subprocess_exec(*args)`, no shell expansion, no `shell=True`.

- Arguments are never concatenated into a shell string
- Default timeout of 300s prevents hung processes
- `subprocess_error()` factory truncates stderr to 500 chars
- `ClaudeCodeRuntime.spawn()` in `sova/ipc/runtime.py` follows the same pattern

### shlex.quote for Dashboard Commands

Dashboard constructs CLI commands for headless agents with `shlex.quote()` (`sova/dashboard/services/agent_lifecycle.py`):

```python
cmd_parts.append(shlex.quote(issue))
cmd_parts.extend(["--role", shlex.quote(role)])
```

## Suspicious File Guard

`sova/git/branch.py:commit()` checks staged files against `_SUSPICIOUS_PATHS` after `git add` but before `git commit`:

```python
_SUSPICIOUS_PATHS = frozenset({
    ".venv", ".env", ".env.local", "credentials.json",
    ".secrets", "node_modules", ".DS_Store", "__pycache__",
    ".mcp.json",
})
```

`.mcp.json` (project root, outside `.claude/`) can hold a plaintext MCP sidecar token (see `sova/cli/commands/project.py`'s Atlassian sidecar config), so it needs the same guard as `.env`. It is also gitignored in this repo's own `.gitignore` for the same reason; `sova/utils/mcp_config.py` already writes it with mode 0600.

Uses `Path(f).parts` for component matching so nested paths like `src/.env` are caught. Bad files are unstaged with `git reset HEAD` and a `RuntimeError` is raised.

## Secret Redaction in Logs

`sova/core/steps/monitor_ci.py` redacts sensitive patterns from CI log output before LLM processing:

```python
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)"
    r"(?:token|secret|password|api[_-]?key|authorization)[=:\s]+\S+"
    r"|ghp_[A-Za-z0-9]{36}"           # GitHub classic PAT
    r"|ghs_[A-Za-z0-9]{36}"           # GitHub OAuth token
    r"|github_pat_[A-Za-z0-9_]{82}"   # GitHub fine-grained PAT
    r"|sk-[A-Za-z0-9]{48}"            # OpenAI key
)
```

`_redact_logs()` replaces matches with `[REDACTED]`. Apply whenever passing external log output to the LLM or dashboard.

## Input Sanitization

### Branch Names

`invariants/branch-naming.sh` enforces: `^(feat|fix|refactor|docs|chore|test)/[a-z0-9][a-z0-9-]+$`

Prevents shell metacharacters, path traversal sequences, and spaces in branch names.

### JQL Values

`sova/adapters/jira.py:_sanitize_jql_value()` strips `"`, `\`, and control chars via `re.sub(r'["\\\x00-\x1f]', "", value)`. Applied to `project_key`, `component`, and `labels`. The `jql_filter` field is trusted config input, not sanitized.

### MCP Path Traversal

`sova/mcp/tools.py:_validate_project_dir()` resolves symlinks via `Path.resolve()` and validates against an allowed root when the MCP server is bound to a startup project.

### MCP JSON-RPC Argument Whitelisting

`sova/dashboard/routers/mcp.py:_filter_tool_arguments()` strips any client-supplied JSON-RPC argument the target tool function's own signature does not declare, and always strips `project_dir` even when declared: every `mcp_service` tool scopes its DB/session calls by `project_dir`, so forwarding a client-supplied value verbatim would let a token valid for one run redirect a query at an arbitrary other project. The allowlist is built per tool from `inspect.signature()` rather than one global "safe keys" list, so a tool with a legitimately different optional parameter is never broken by a list sized for another tool.

### Dashboard Command Name Validation

`POST /api/agents/command`'s `command` field names a file resolved relative to the project's own and SOVA's own `.claude/commands/`/`commands/` directories (`sova/dashboard/services/agent_context.py:_resolve_command_prompt()`); an unvalidated value is both a path-traversal vector (building a filesystem path from it) and a prompt-injection vector (its fallback sends a bare `/{command}` to the headless, tool-using agent for anything it cannot resolve to a real file). `known_command_names()` mirrors `_resolve_command_prompt()`'s own lookup order via `sova.commands.catalog.discover()`, and the router rejects (`400`) any `command` not in that set.

### Multi-Project Slug Validation

`sova/dashboard/middleware.py` validates project slugs against `^[a-z0-9-]+$` before path resolution, preventing directory traversal via URL segments.

### Template Variables in Inline JavaScript

Jinja2's `{{ var }}` uses HTML escaping, not JavaScript string escaping. A value containing `'` or `</script>` can break the handler or become XSS when interpolated into inline JS.

```html
<!-- Wrong -- HTML-escaped, not JS-safe -->
<button onclick="doThing('{{ slug }}')">

<!-- Correct -- pass via data attribute, read in handler -->
<button data-slug="{{ slug | e }}" onclick="doThing(this.dataset.slug)">
```

### Manifest Path Traversal

`_remove_managed_commands()` in `sova/cli/commands/project.py` reads filenames from `.sova-manifest.json`. A tampered entry like `../../sova.toml` could escape the managed directory. Guard with `resolve()` + `is_relative_to()`.

## Third-Party Browser Assets

The dashboard loads a few libraries from public CDNs, including DOMPurify, which sanitizes rendered markdown. Each remote `<script>` or stylesheet must pin an exact version and carry `integrity="sha384-..."` and `crossorigin="anonymous"`, so a compromised CDN or package account cannot run code in the dashboard. The version must be exact because a range such as `@15` changes the served bytes on the next release and the hash then blocks the script. `invariants/cdn-integrity.sh` enforces all three.

## Force Push Safety

`sova/git/branch.py:push()` always uses `--force-with-lease` instead of `--force`, protecting against data loss from concurrent work.

## Budget Limits (Denial-of-Wallet)

| Limit | Default | Config Key |
|-------|---------|------------|
| Per-run budget | $10.00 | `agent.max_budget` |
| Per-issue budget | $50.00 | `agent.max_issue_budget` |
| Step timeout | 1800s | `agent.step_timeout` |
| Max run wall clock | 14400s | `runaway.max_run_wall_clock_seconds` (0=disable) |
| Max run steps | 100 | `runaway.max_run_steps` (0=disable) |
| Max LLM calls | 250 | `runaway.max_llm_calls` (0=disable) |
| Max step attempts | 80 | `runaway.max_step_attempts` (0=disable) |

Per-issue budget is checked in `start_agent()` before spawning; only an explicit
`budget_override` bypasses it (`--force` does not, so a force retry spawned for an unrelated
reason still gets the hard budget stop). Per-run budget is checked at step boundaries in
`WorkflowEngine`.

The two dollar limits go blind against a provider that reports `cost_usd=0` for every call, so
`WorkflowEngine._check_runaway_guard()` backstops them with cost-independent wall-clock,
step-count, and LLM-call-count limits (`[runaway]` config section), also enforced at step
boundaries. Wall clock is scaled by task complexity (same multiplier as `agent.step_timeout`,
via `sova.llm.complexity.complexity_multiplier`), so a legitimate EPIC-complexity run is not
paused by a limit sized for the default tier. `max_run_steps` counts completed steps within a
single run, which is bounded by the pipeline length (16 for the developer pipeline), so it is a
ceiling for future longer pipelines rather than an active guard today; `max_llm_calls` is the
limit that catches retry/fix loops (CI-fix, address-review consensus) that burn LLM calls inside
a single step without advancing the step count. `max_step_attempts` caps cumulative attempts at
a single step across model-fallback switches, tracked via `sova.llm.client`'s ContextVar-based
call counter (`start_call_counter`/`get_call_count`), incremented at the four `invoke*` entry
points so a step calling the client indirectly cannot bypass it. `ExecutionContext.resource_remaining_fraction`
composes the budget, wall-clock, and call-count signals into the single fraction that graceful
degradation call sites (skip optional steps, stop retrying, skip hooks) read.

## Headless Agent Security

`sova/ipc/runtime.py` spawns Claude CLI with `--permission-mode bypassPermissions` (`auto` only auto-approves tools already in the settings.json allowlist, which is insufficient for headless agents that must edit arbitrary project files). Mitigations:
- Per-run and per-issue budget caps
- Step timeouts (default 1800s)
- Suspicious file guard catches credential commits even from autonomous agents
- Dashboard binds to `127.0.0.1` by default; a non-loopback `--host` is supported but then relies on the CSRF guard's fail-closed behavior and `TrustedHostMiddleware` rather than being unreachable outright (see "Dashboard Security Model" below)

## Prompt Injection Mitigation

When adding LLM-facing code:
1. Place user content in clearly delimited data sections, separate from instructions
2. Constrain output format (JSON, not freeform)
3. Validate parsed output against known schemas or enums
4. Wrap LLM calls in try/except so injection-caused errors are non-fatal

## Dashboard Security Model

- Single-user, loopback-only deployment model: no user accounts, sessions, or login page. Binds to `127.0.0.1:8111` by default; access to the machine implies access to the dashboard.
- No CORS middleware (not needed for localhost); instead, `sova.dashboard.security.require_same_origin_csrf` checks the request's Origin/Referer against the dashboard's own scheme+host+port and a double-submit CSRF cookie/header pair. This defends against a different threat than CORS: any web page the browser visits (not just the dashboard's own origin) can otherwise issue cross-origin `fetch()` calls at the local port, and DNS rebinding makes same-origin policy alone incomplete (a rebound page's `Origin` header still reflects where it was loaded from, which the guard checks against).
- The guard is a no-op for safe methods (GET/HEAD/OPTIONS) and is registered once, at router level, in `sova/dashboard/app.py:_register_api_routers()`: every router it installs gets the dependency, so a mutating route is covered by construction rather than by a per-route `Depends()` a new route can simply omit (issue #1204; the per-route pattern is exactly how most routes went unguarded before this). `mcp.router` (`/mcp`) is the sole exemption, since its own bearer-token scheme already authenticates every call; `/api/telemetry/ingest` is exempt structurally (registered by a separate call that never passes through `_register_api_routers()`). `issue_csrf_cookie()` is called from every page route, not a handful, so any page's own JS can call a guarded endpoint.
- `TrustedHostMiddleware` validates the `Host` header against the same loopback-aware host set the CSRF guard derives (`sova.dashboard.security.build_allowed_hosts`). A wildcard bind (`0.0.0.0`, `::`) has no single derivable host, so it allows `"*"` there and relies on the CSRF guard's fail-closed behavior for the real protection.
- A `Content-Security-Policy` plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: strict-origin-when-cross-origin` are set on every response (`sova/dashboard/app.py`'s `security_headers` middleware). The CSP allows `'unsafe-inline'` for scripts/styles: dashboard templates use inline `<script>` blocks and `onclick` handlers extensively, and tightening that to a nonce/hash scheme is a template-by-template rewrite, not done here. The CDN hosts allowed (`cdn.jsdelivr.net`, `cdnjs.cloudflare.com`) match `invariants/cdn-integrity.sh`'s pinned set.
- When bound to a non-loopback host (`--host 0.0.0.0` or similar), the guard fails closed on every dependent route unless `dashboard.csrf_secret` is configured; the server logs a warning at startup in that case.
- `dashboard.csrf_secret` is an opt-in switch, not a credential: only its presence is checked, never its value. Neither the origin check nor the double-submit pair authenticates a caller; both stop a *browser* driven cross-origin request, while a direct client (curl) can set all of them itself. A non-loopback bind stays reachable by anyone who can reach the port, so treat it as unauthenticated until the auth router adds real token validation (see `sova/dashboard/services/mcp_service.py` for the HMAC precedent).
- The A2A protocol's one mutating route (`POST /a2a/tasks/{task_id}/cancel`) is outside the CSRF guard (A2A is a non-browser, agent-to-agent surface) but is not unauthenticated: it reuses `mcp_service`'s run-scoped HMAC generate/validate pair, the same bearer-token shape an agent already presents to `/mcp`, scoped to the exact run_id the task ID encodes.
- Read-only status endpoints do not need the guard, provided they leak no secret material.
- Known limitation, not addressed by issue #1204: `dashboard.rate_limit_per_minute` (`sova/dashboard/app.py`) applies one shared `{rate}/minute` bucket across every route via `slowapi`'s `default_limits`, keyed by remote address. A caller exhausting that bucket on one endpoint throttles itself on all others too. Listed as a [P3] finding without its own acceptance criterion; left as a known gap here rather than changed.
- See `.claude/rules/architecture.md` for the guard's implementation details.

## Contributor Checklist

1. **New subprocess calls**: use `sova/utils/shell.py:run()`, never `os.system()` or `shell=True`
2. **New credential fields**: add `repr=False` to the Pydantic Field, never log the value
3. **New git operations**: pass arguments as separate strings, quote user-provided values
4. **New LLM prompts**: separate data from instructions, validate output, handle errors non-fatally
5. **New file paths from external input**: validate with `Path.resolve()` + root check
6. **New API endpoints**: validate/sanitize all path parameters
7. **New log output**: apply `_redact_logs()` if the source may contain tokens or keys
