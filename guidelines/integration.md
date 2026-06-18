# Integration Guidelines

Patterns for integrating with external services in {{ project_name }}.

## Subprocess Execution

All external process calls should go through a centralized shell utility. Never use `subprocess` directly.

| Function | Behavior | Use when |
|----------|----------|----------|
| `run(*args, env=, timeout=)` | Returns result object, never raises | Caller checks `.success` |
| `run_checked(*args)` | Raises on non-zero exit | Failure is always fatal |

- Uses `asyncio.create_subprocess_exec` (no shell expansion, no injection risk)
- Default timeout prevents hung processes. Kills and returns failure on expiry.
- Output decoded with `errors="replace"` for non-UTF-8 safety
- Pass credentials via `env=` dict, never as CLI arguments

## GitHub CLI (gh) Integration

### Credential Injection

Use a centralized auth function that injects `GH_TOKEN` per-subprocess:

```python
env = await resolve_gh_env(github_user)
await run("gh", "pr", "view", ..., env=env)
```

### Common Gotchas

- **`GH_TOKEN` env var overrides `gh auth switch`**: unset it before switching accounts
- **`gh auth switch` does not persist across subprocesses**: inject token per-call
- **`gh pr create` returns plain text (URL), not JSON**: parse PR number from the URL path

### JSON Parsing

Always wrap `json.loads(result.stdout)` in try/except. `gh` can return empty stdout on success:

```python
try:
    data = json.loads(result.stdout)
except (json.JSONDecodeError, TypeError):
    return []
```

### PR Reviews vs Comments

| Method | API | Result |
|--------|-----|--------|
| `post_pr_comment()` | `gh pr comment` | Conversation-level comment |
| `post_pr_review()` | `gh api .../reviews` | Formal review with inline comments |

GitHub returns 422 for `APPROVE`/`REQUEST_CHANGES` on own PRs; fall back to `event=COMMENT`.

## Error Handling Tiers

| Tier | Behavior | Used for |
|------|----------|----------|
| Hard (raise) | Caller must handle | Primary operations: task fetch, PR create |
| Soft (log + default) | Pipeline continues | Side effects: notifications, label changes |

## Desktop Notifications

Fire-and-forget via `asyncio.create_task()`. All paths wrapped in try/except -- notifications never crash the pipeline.
