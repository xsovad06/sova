---
name: testing-patterns
description: Test conventions for {{ project_name }} -- pytest patterns, mock strategies, fixture patterns. Auto-activates when writing or modifying test files.
allowed_tools: Read, Grep, Glob, Bash, Edit, Write
---

# Testing Patterns

When writing or modifying test files, follow these conventions.

## Running Tests

```bash
{{ test_cmd }}
```

## Key Principles

- Write tests that verify behavior, not implementation details
- Use descriptive test names that explain the scenario and expected outcome
- Each test should be independent and not rely on execution order
- Prefer real implementations over mocks when practical
- Mock at system boundaries (external APIs, file I/O, databases), not internal interfaces

## Fixture Patterns

- Define fixtures per-file, not shared across test files
- Use `autouse=True` fixtures sparingly -- only for setup/teardown that every test needs
- Name fixtures descriptively: `mock_api_client` not `client`

## Mock Rules

- Patch at the import site, not the definition site
- Always use `patch.object()` for methods on instances
- Verify mock calls when the call itself is the behavior under test
- Do not assert mock call counts when only the return value matters

## Test Organization

- Group related tests with classes
- Use parametrize for testing multiple inputs with the same logic
- Keep test files focused on one module or feature
