# Security Guidelines

Security conventions and guardrails specific to the SOVA codebase -- credential handling, input sanitization, subprocess safety, and prompt injection mitigation.

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

### Worktree Credential Copying

`WorktreeConfig.copy_files` defaults to `[".env", ".env.local"]`. These are copied into new worktrees so agents can authenticate. Ensure `.gitignore` covers these patterns.

| Do | Do Not |
|----|--------|
| Use `resolve_gh_env()` for subprocess auth | Set `GH_TOKEN` globally in your shell |
| Use `repr=False` on secret Pydantic fields | Log config objects containing tokens |
| Keep secrets in `.env` (gitignored) | Add secrets to `sova.toml` or the DB |

## Subprocess Execution Safety

All subprocess calls go through `sova/utils/shell.py:run()`, which uses `asyncio.create_subprocess_exec(*args)` -- no shell expansion, no `shell=True`.

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
})
```

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
- Dashboard binds to `127.0.0.1` only (no remote access)

## Prompt Injection Mitigation

When adding LLM-facing code:
1. Place user content in clearly delimited data sections, separate from instructions
2. Constrain output format (JSON, not freeform)
3. Validate parsed output against known schemas or enums
4. Wrap LLM calls in try/except so injection-caused errors are non-fatal

## Dashboard Security Model

- Binds to `127.0.0.1:8111` by default (localhost only)
- No authentication layer (access implies machine access)
- No CORS middleware (not needed for localhost)
- If ever exposed to a network, authentication and CORS must be added first

## Contributor Checklist

1. **New subprocess calls**: use `sova/utils/shell.py:run()`, never `os.system()` or `shell=True`
2. **New credential fields**: add `repr=False` to the Pydantic Field, never log the value
3. **New git operations**: pass arguments as separate strings, quote user-provided values
4. **New LLM prompts**: separate data from instructions, validate output, handle errors non-fatally
5. **New file paths from external input**: validate with `Path.resolve()` + root check
6. **New API endpoints**: validate/sanitize all path parameters
7. **New log output**: apply `_redact_logs()` if the source may contain tokens or keys
