# Testing Guidelines

Testing conventions and patterns for {{ project_name }}.

## Running Tests

| Command | Purpose |
|---------|---------|
| `{{ test_cmd }}` | Run the full test suite |
| `{{ lint_cmd }}` | Run linters |

## Async Test Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

With `asyncio_mode = "auto"`, all `async def test_*` functions run as async tests automatically -- no `@pytest.mark.asyncio` decorator needed.

## Mock Patterns

### Patch at the import site, not the definition site

```python
# Correct: patch where it's imported
with patch("myapp.services.run", new_callable=AsyncMock) as mock_run:

# Wrong: patching the definition module has no effect on the importer
with patch("myapp.utils.shell.run", new_callable=AsyncMock):
```

### ShellResult helper factory

Define a per-file factory for subprocess result objects:

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

### Context factory for test data

```python
def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = {"project_dir": Path("/tmp/test"), "config": Config()}
    defaults.update(kwargs)
    return ExecutionContext(**defaults)
```

### `patch.object` for split modules

When a module was split with a re-export facade, patch the actual submodule:

```python
# Correct -- patches the real implementation
patch.object(actual_module, "function_name", return_value=result)

# Wrong -- only patches the facade's attribute
patch.object(facade_module, "function_name", return_value=result)
```

## In-Memory Database Fixture

```python
@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("DATABASE_URL", None)
```

## ASGI / FastAPI Testing

```python
@pytest.fixture
async def client(tmp_path):
    app = create_app(project_dir=tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

### File-backed service isolation

Services with mtime caches need monkeypatching in tests:

```python
monkeypatch.setattr(service_module, "_resolve_project_dir", lambda: tmp_path)
service_module._cache.clear()
```

## Common Pitfalls

1. **Missing cache clears**: file-backed services cache by mtime. Clear caches between tests.
2. **Async context managers**: always `async with await get_session() as session:`.
3. **autouse DB fixture scope**: per-test (default). Never use `scope="module"` for DB fixtures.
