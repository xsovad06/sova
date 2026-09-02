# Testing Guidelines

Conventions and patterns for writing and running tests in the SOVA codebase.

## Running Tests

| Command | Purpose |
|---------|---------|
| `make check` | Lint + test (CI-equivalent, run before every push) |
| `make test` | Bash + Python tests |
| `make test-py` | pytest suite only |
| `make test-bash` | ShellCheck + invariant `--help` validation |
| `make lint` | ShellCheck + Ruff (lint + format check) |

CI runs `pytest tests/ --cov=sova --cov-report=xml -q`. The pre-push hook in `.githooks/pre-push` mirrors CI checks locally.

## Framework Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

`asyncio_mode = "auto"` means all `async def test_*` functions are async tests automatically -- no `@pytest.mark.asyncio` needed. Dependencies: `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx` (ASGI testing), `aiosqlite`.

## Test File Layout

All tests live in `tests/` as flat modules (no subdirectories, no `conftest.py`):

| Test file | Source package | Approx tests |
|-----------|---------------|--------------|
| `test_dashboard.py` | `sova/dashboard/` | ~325 |
| `test_core.py` | `sova/core/` | ~205 |
| `test_roles.py` | `sova/roles/` | ~128 |
| `test_git.py` | `sova/git/` | ~89 |
| `test_ipc.py` | `sova/ipc/` | ~82 |
| `test_cli.py` | `sova/cli/` | ~57 |
| `test_adapters.py` | `sova/adapters/` | ~54 |
| `test_config.py` | `sova/config/` | ~40 |

Plus: `test_batch_service.py`, `test_commands.py`, `test_dag.py`, `test_db.py`, `test_external_reviews.py`, `test_extraction.py`, `test_harden.py`, `test_jira_adapter.py`, `test_knowledge.py`, `test_knowledge_sharing.py`, `test_lifecycle.py`, `test_llm.py`, `test_mcp.py`, `test_scheduler.py`, `test_settings_meta.py`, `test_spec.py`, `test_utils.py`.

Tests use classes to group related assertions. Fixtures are defined per-file, not in conftest.

## In-Memory Database Fixture

```python
@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)
```

- `run_migrations=False` uses `create_all` (faster than Alembic for tests)
- `close_db()` disposes the engine so each test gets a fresh DB
- Env var scoping prevents cross-test pollution

## Seeding Project Config

Production SOVA stores configuration in `.claude/sova.db` (the `project_settings` table), not in `sova.toml`. Tests that need configured values should use the shared `seed_config` fixture (`tests/conftest.py`) rather than writing a `sova.toml` file. Writing `sova.toml` only works because `load_config()` still has an auto-migration fallback that epic #550 intends to remove (issue #557).

```python
def test_something(tmp_path, seed_config):
    seed_config(tmp_path, github_repo="user/repo", validation={"fix_timeout": 240})
    cfg = load_config(tmp_path)
    assert cfg.github_repo == "user/repo"
```

- Nested dicts flatten to dot-notation (`{"pipeline": {"auto_handoff": False}}` -> `pipeline.auto_handoff`); flat dotted keys pass through unchanged
- Values are passed as native Python types (str, int, float, bool, list) and JSON-encoded to match production storage
- The fixture is synchronous, so it works in both sync and async tests
- Priority still holds: `SOVA_*` env vars > DB > TOML > defaults
- Only tests exercising the TOML parse-error/fallback path should still write `sova.toml` (annotate with a comment)

## Mock Patterns

### Patch at the import site, not the definition site

```python
# Correct: patch where it's imported
with patch("sova.adapters.github.run", new_callable=AsyncMock) as mock_run:

# Wrong: patching the definition module has no effect on the importer
with patch("sova.utils.shell.run", new_callable=AsyncMock):
```

### ShellResult helpers

Each test file defines its own factory:

```python
def _shell_result(stdout="", stderr="", returncode=0):
    return ShellResult(returncode=returncode, stdout=stdout, stderr=stderr)
```

### Multi-call methods with `side_effect`

```python
mock_run.side_effect = [
    _shell_result(stdout='{"labels": []}'),  # first call
    _shell_result(),                          # second call
]
```

### Context factory: `_make_ctx`

```python
def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = {
        "project_dir": Path("/tmp/test"),
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)
```

### `patch.object` for split modules

When a module was split with a re-export facade, patch the actual submodule:

```python
# Correct
patch.object(agent_lifecycle, "_get_project_agents", return_value=pa)

# Wrong -- only patches the facade's attribute
patch.object(control_service, "_get_project_agents", return_value=pa)
```

## Dashboard / FastAPI Testing

```python
@pytest.fixture
async def client(tmp_path):
    app = create_app(project_dir=tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

### File-backed service isolation

Services with mtime caches (`handoff_service`, `log_service`) need monkeypatching:

```python
monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
handoff_service._handoff_caches.clear()
```

## CLI Testing

```python
from typer.testing import CliRunner
runner = CliRunner()

result = runner.invoke(app, ["status"])
assert result.exit_code == 0
```

## Bash / Invariant Testing

Each invariant script must:
1. Pass `shellcheck` (enforced by `make lint-bash`)
2. Handle `--help` gracefully (tested by `make test-bash`)

## Parametrized Tests

```python
@pytest.mark.parametrize(
    "model_cls, field, invalid_value",
    [
        (AgentConfig, "max_budget", Decimal("-1")),
        (AgentConfig, "step_timeout", 0),
    ],
)
def test_rejects_invalid_values(model_cls, field, invalid_value):
    with pytest.raises(ValueError):
        model_cls(**{field: invalid_value})
```

## Common Pitfalls

1. **Missing cache clears**: file-backed services cache by mtime. Clear `_handoff_caches` in tests.
2. **Async context managers**: always `async with await get_session() as session:`.
3. **autouse DB fixture scope**: per-test (default). Never use `scope="module"` for DB fixtures.
4. **No conftest.py**: helpers (`_make_ctx`, `_shell_result`) are defined per-file. Copy the pattern.
