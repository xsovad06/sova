# Persona: Go Microservice

## Project Context
Go microservices in the Kessel platform ecosystem.
Each service is an independent module with its own go.mod.

## Tech Stack
- Go 1.23+
- gRPC and REST APIs
- PostgreSQL or SpiceDB for storage
- Kessel authorization patterns

## Testing Patterns
- Table-driven tests with `t.Run` subtests
- `testify/assert` for assertions
- Integration tests use real database connections
- Mock external services only (not databases)

## Code Style
- `golangci-lint` with project `.golangci.yml`
- Effective Go conventions
- Error wrapping with `fmt.Errorf("context: %w", err)`
- Context propagation through all function chains

## Common Pitfalls
- Always pass `context.Context` as first parameter
- Close database rows/connections (use defer)
- Check error returns — never ignore
- Use structured logging (slog or zerolog)
- Avoid global state — use dependency injection

## MCP Tools
- mcp_db: PostgreSQL introspection
- mcp_openapi: OpenAPI spec discovery
