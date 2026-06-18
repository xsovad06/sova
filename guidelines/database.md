# Database Guidelines

ORM conventions, session management, and migration patterns for {{ project_name }}.

## Session Management

Always acquire sessions with the async context manager pattern:

```python
# Correct -- auto-closes on exit
async with await get_session() as session:
    result = await session.execute(select(Model))

# Wrong -- leaks sessions on exception
session = await get_session()
result = await session.execute(select(Model))
await session.close()
```

Use `expire_on_commit=False` so attributes remain accessible after commit without re-querying.

## ORM Model Conventions

| Convention | Example |
|---|---|
| Type-annotated columns | `Mapped[str]` with `mapped_column(String(50))` |
| Decimal for money | `Numeric(10, 6)` with `Decimal("0")` default |
| UTC timestamps | `DateTime(timezone=True)` with `lambda: datetime.now(timezone.utc)` |
| `onupdate` for modified-at | `onupdate=lambda: datetime.now(timezone.utc)` |
| Indexes via `__table_args__` | `Index("ix_table_column", "column")` |

## JSON Column NULL Gotcha

SQLAlchemy's `JSON` type defaults to `none_as_null=False`. Python `None` is stored as JSON text `"null"`, not SQL `NULL`.

```python
# Only excludes true SQL NULLs (pre-migration or raw SQL rows):
Model.json_col.isnot(None)

# Use `if not value:` to catch both None and {}
# Use `if value is None:` only for NULL/json-null specifically
```

## create_all vs Alembic

| Context | Method | Why |
|---|---|---|
| **Tests** | `create_all` (skip migrations) | In-memory DBs; fast setup |
| **Production** | Alembic migrations | Adds columns to existing tables |

`create_all` only creates missing tables -- it never adds columns to existing ones. Adding a column without a migration causes `OperationalError: no such column`.

## Migration Conventions

1. **Use `batch_alter_table`** for all SQLite DDL (set `render_as_batch=True` in `env.py`)
2. **Idempotent checks**: verify column/table/index existence before adding
3. **Engine disposal after migrations**: call `await engine.dispose()` after Alembic runs on file-backed SQLite to clear stale schema cache. Skip for in-memory DBs.
4. **Self-healing fallback**: if Alembic fails, drop corrupted `alembic_version`, run `create_all` + stamp at head

## Test Fixtures

```python
@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("DATABASE_URL", None)
```

In-memory SQLite gives each test a fresh DB. Always `close_db()` in teardown.

## Index Naming

All indexes follow `ix_{table}_{column}` pattern. Single-column indexes cover current query patterns.
