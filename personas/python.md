# Python Persona

> Auto-detected when: `pyproject.toml` or `setup.py` exists, no specific framework detected

## Project Structure

- Use `pyproject.toml` for project metadata and tool config (PEP 621)
- Prefer flat layout (`mypackage/`) over src layout unless publishing to PyPI
- Keep `__init__.py` files minimal -- avoid circular imports from re-exports
- Entry points: use `[project.scripts]` in pyproject.toml, not manual `setup(entry_points=...)`

## Type Hints

- Add type hints on all function signatures
- Use `from __future__ import annotations` for forward references
- Prefer `X | None` over `Optional[X]` (Python 3.10+)
- Use `typing.Protocol` for structural subtyping instead of ABC when possible
- Use `@dataclass` or Pydantic `BaseModel` for structured data, not plain dicts

## Functions and Classes

- Prefer functions over classes for stateless operations
- Use `@staticmethod` sparingly -- a module-level function is usually clearer
- Avoid deep inheritance hierarchies -- composition over inheritance
- Use `__slots__` on data-heavy classes for memory efficiency
- Use `enum.StrEnum` for string constants that need validation

## Error Handling

- Raise specific exceptions, not bare `Exception`
- Use custom exception classes for domain errors
- Catch the narrowest exception type possible
- Use `contextlib.suppress()` instead of empty `except` blocks
- Always pass `exc_info=True` to loggers when re-raising or swallowing exceptions

## Testing

- Use `pytest` with fixtures, not `unittest.TestCase`
- Use `tmp_path` fixture for file operations, not manual temp dir creation
- Use `monkeypatch` for patching, not `unittest.mock.patch` (unless needed for async)
- Use `pytest.mark.parametrize` for data-driven tests
- Test public API, not implementation details
- Use `AsyncMock` for async function mocking

## Async

- Use `asyncio` for I/O-bound concurrency
- Use `async with` for context managers, `async for` for iterators
- Never call blocking I/O in async functions without `run_in_executor`
- Use `asyncio.gather()` for parallel coroutines, not sequential `await`
- Use `asyncio.TaskGroup` (Python 3.11+) over bare `create_task()`

## Dependencies

- Pin direct dependencies with compatible ranges (`>=1.2,<2`)
- Use `pip install -e .` for development installs
- Use `ruff` for linting and formatting

## Common Pitfalls

- Don't use mutable default arguments (`def f(items=[])`) -- use `None` with internal init
- Don't catch `Exception` when you mean `ValueError` or `KeyError`
- Don't use `os.path` -- use `pathlib.Path`
- Don't use `%` or `.format()` for strings -- use f-strings
- Don't use bare `assert` for validation -- it's stripped with `-O`
- Don't import at function level unless needed to break circular imports
- Don't use `type()` for type checking -- use `isinstance()`
