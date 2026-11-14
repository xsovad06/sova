---
name: testing-patterns
description: "Test conventions for {{ project_name }}: pytest patterns, mock strategies, fixture patterns. Auto-activates when writing or modifying test files."
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
- Mock at system boundaries (external APIs, network, clock), not internal interfaces
- Prefer the framework's test database (a real test DB, an in-memory engine) over mocking the database

## Before Adding a Test

- One test per behavior: add a test only for something no existing test already proves
- A case that differs from an existing test only in inputs and expected result becomes a case of that test (parametrize it if it is not yet), not a new function
- No test whose only assertion is that a mock was called, unless that call is the contract (the side effect is what the code exists to produce)
- No test asserting that a phrase exists in documentation or other prose, unless another component parses that phrase; no test asserting only that an attribute or import exists

## Fixture Patterns

- Put fixtures used by several test files in the nearest shared fixture module (for example `conftest.py`); keep single-file fixtures local
- Use `autouse=True` fixtures sparingly: only for setup/teardown that every test needs
- Name fixtures descriptively: `mock_api_client` not `client`

## Mock Rules

- Patch at the import site, not the definition site
- Always use `patch.object()` for methods on instances
- A mock standing in for a side effect (a write, a send, a spawn) asserts it was called and with what (`assert_called_once_with`), so the test fails when the code skips the side effect

## Test Organization

- Group related tests with classes
- Use parametrize for testing multiple inputs with the same logic
- Keep test files focused on one module or feature

## Negative Control

For a test written after the code it covers: break the covered line with a change that keeps the module importable, run the test, and confirm it fails on an assertion for the expected behavior, not with a syntax, import, or collection error. Restore the line, and confirm with `git diff` that the mutation is gone. A test that stays green with the line broken does not cover it.
