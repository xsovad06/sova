# Security Guidelines

Security conventions for {{ project_name }} -- subprocess safety, credential handling, input sanitization, and commit guards.

## Subprocess Execution

All subprocess calls should use `asyncio.create_subprocess_exec(*args)` or equivalent -- no shell expansion, no `shell=True`.

- Pass arguments as separate strings, never concatenated into a shell string
- Set a default timeout to prevent hung processes
- Pass credentials via the `env=` parameter, never as CLI arguments

## Credential Management

| Do | Do Not |
|----|--------|
| Inject secrets via subprocess `env` parameter | Set secrets globally in your shell |
| Use `repr=False` on secret Pydantic fields | Log config objects containing tokens |
| Keep secrets in `.env` (gitignored) | Add secrets to config files tracked by git |

## Suspicious File Guard

Before committing, check staged files against a blocklist of paths that should never be committed:

```python
_SUSPICIOUS_PATHS = frozenset({
    ".venv", ".env", ".env.local", "credentials.json",
    ".secrets", "node_modules", ".DS_Store", "__pycache__",
})
```

Use `Path(f).parts` for component matching so nested paths (e.g., `src/.env`) are caught. Unstage bad files with `git reset HEAD` before the commit proceeds.

## Input Sanitization

- **Branch names**: enforce a strict regex (e.g., `^(feat|fix|refactor)/[a-z0-9-]+$`) to prevent shell metacharacters and path traversal
- **File paths from external input**: resolve symlinks via `Path.resolve()` and validate against an allowed root directory
- **URL path parameters**: validate/sanitize all path segments before filesystem operations

## Force Push Safety

Always use `--force-with-lease` instead of `--force` to protect against data loss from concurrent work.

## Secret Redaction in Logs

Before passing external log output to an LLM or dashboard, redact sensitive patterns:

```python
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)"
    r"(?:token|secret|password|api[_-]?key|authorization)[=:\s]+\S+"
    r"|ghp_[A-Za-z0-9]{36}"
    r"|sk-[A-Za-z0-9]{48}"
)
```

## Prompt Injection Mitigation

When building LLM prompts from external data:
1. Place user content in clearly delimited data sections, separate from instructions
2. Constrain output format (JSON, not freeform)
3. Validate parsed output against known schemas or enums
4. Wrap LLM calls in try/except so injection-caused errors are non-fatal

## Contributor Checklist

1. **New subprocess calls**: use exec-based invocation, never `os.system()` or `shell=True`
2. **New credential fields**: add `repr=False` to the Pydantic Field, never log the value
3. **New git operations**: pass arguments as separate strings, quote user-provided values
4. **New LLM prompts**: separate data from instructions, validate output, handle errors non-fatally
5. **New file paths from external input**: validate with `Path.resolve()` + root check
6. **New API endpoints**: validate/sanitize all path parameters
7. **New log output**: redact if the source may contain tokens or keys
