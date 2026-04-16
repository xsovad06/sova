# FastAPI Persona

> Auto-detected when: `fastapi` found in requirements/pyproject.toml

## Architecture

- Organize by feature/domain, not by type (routers/, models/, schemas/)
- Each feature: `router.py`, `schemas.py`, `service.py`, `models.py`
- Use dependency injection for shared logic (DB sessions, auth, settings)

## Routers

- One router per feature/resource
- Use `APIRouter(prefix="/feature", tags=["feature"])`
- Path operations should be thin — delegate to services
- Use proper HTTP methods and status codes
- Return Pydantic models, not dicts

## Pydantic Models

- Separate `Create`, `Update`, `Response` schemas
- Use `Field()` for validation constraints and descriptions
- Use `model_config = ConfigDict(from_attributes=True)` for ORM integration
- Keep schemas close to their router, not in a global schemas file

## Async

- Use `async def` for I/O-bound endpoints (DB, HTTP calls)
- Use `def` for CPU-bound endpoints (FastAPI runs them in threadpool)
- Never mix sync DB calls in async endpoints without `run_in_executor`
- Use `httpx.AsyncClient` for outbound HTTP calls

## Dependencies

- Use `Depends()` for DB sessions, auth, pagination
- Create reusable dependencies for common patterns
- Use `Security()` for auth-related dependencies

## Database (SQLAlchemy)

- Use async sessions with `asyncpg`
- Always use context managers for sessions
- Use Alembic for migrations
- Scope queries by tenant/user

## Error Handling

- Use `HTTPException` for expected errors
- Use custom exception handlers for domain errors
- Return consistent error response format
- Don't catch generic `Exception` in endpoints

## Testing

- Use `httpx.AsyncClient` with `ASGITransport` for async tests
- Use `pytest-asyncio` for async test functions
- Override dependencies in tests (DB, auth, external services)
- Test both success and error paths
- Use factory functions for test data

## Common Pitfalls

- Don't create Pydantic models inside endpoint functions
- Don't use `response_model` with `Union` types carelessly (field filtering)
- Don't forget to add CORS middleware for frontend clients
- Don't use synchronous `requests` in async endpoints
