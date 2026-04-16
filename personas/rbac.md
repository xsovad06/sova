# Persona: RBAC Service

## Project Context
Django-based RBAC (Role-Based Access Control) service for the Kessel platform.
Multi-tenant architecture with workspace-scoped permissions.

## Tech Stack
- Python 3.11+ / Django 4.2+
- PostgreSQL with multi-tenant schema
- Kessel authorization (SpiceDB)
- REST API with Django REST Framework

## Testing Patterns
- pytest with Django test fixtures
- Factory Boy for test data
- Integration tests hit real database (no mocking DB)
- Permission tests must cover cross-tenant isolation

## Code Style
- Black formatter, isort for imports
- Type hints on public API functions
- Django conventions: fat models, thin views
- Custom permissions inherit from `BasePermission`

## Common Pitfalls
- Always filter by `org_id` / workspace — never return cross-tenant data
- Permission checks must go through Kessel, not direct DB queries
- Migrations must be backwards-compatible (zero-downtime deploys)
- Never hardcode permission names — use constants from `permissions.py`

## MCP Tools
- mcp_spicedb: SpiceDB schema exploration and relationship queries
- mcp_db: PostgreSQL introspection for RBAC database
