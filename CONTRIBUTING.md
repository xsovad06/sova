# Contributing to SOVA

Thank you for your interest in contributing to SOVA. This guide covers the development setup, coding standards, and contribution process.

## Development Setup

### Prerequisites

- Python 3.12+
- [GitHub CLI](https://cli.github.com/) (`gh`) -- authenticated
- `git`

**Optional** (for AI-assisted workflows):
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) -- powers SOVA's slash commands and agent orchestration

### Getting Started

```bash
git clone https://github.com/xsovad06/project-automation-kit.git
cd project-automation-kit
pip install -e ".[dev]"
git config core.hooksPath .githooks
```

### Running Checks

```bash
make check    # Lint + test (CI-equivalent)
make test     # Run all tests
make lint     # ShellCheck + Ruff
make format   # Auto-format Python code
```

All checks must pass before submitting a PR. The CI pipeline runs the same checks.

## Code Style

### Python

- **Type hints** required on all function signatures
- **Line length**: 120 characters max
- **Formatter/linter**: Ruff (`make lint`, `make format`)
- **String formatting**: f-strings preferred
- **No emojis** in code, docs, or commit messages

### Bash (invariants, hooks)

- `set -euo pipefail` at the top of every script
- Always double-quote variables (`"$var"`, `"${arr[@]}"`)
- Must pass `shellcheck` with no warnings

### Markdown (commands, docs)

- ATX-style headings (`#`, `##`, `###`)
- Code blocks always specify a language
- No emojis

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
type(scope): short description
```

**Types**: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

**Scopes**: `dashboard`, `commands`, `cli`, `core`, `adapters`, `roles`, `scheduler`, `config`, `db`, `ipc`, `knowledge`, `docs`

Examples:
- `feat(adapters): add Linear task source adapter`
- `fix(dashboard): correct cost tracking calculation`
- `docs(readme): update installation instructions`

## Pull Request Process

1. Create a feature branch from `main`: `feat/<name>`, `fix/<name>`, or `refactor/<name>`
2. Make your changes with tests
3. Run `make check` and ensure all checks pass
4. Submit a PR linking the related issue (`Closes #<number>` in the body)
5. Include a "Test Plan" section describing how to verify the changes
6. Address review feedback

## Reporting Issues

Use the [issue templates](https://github.com/xsovad06/project-automation-kit/issues/new/choose) for:
- **Bug reports**: include steps to reproduce, expected vs actual behavior
- **Feature requests**: describe the objective and acceptance criteria
- **Tasks**: for implementation work with clear scope

## Testing

- Tests live in `tests/` and use pytest with async support
- Run with `make test` or `pytest tests/`
- New features should include tests
- Bash invariant scripts should handle `--help` gracefully

## Questions?

File an [issue](https://github.com/xsovad06/project-automation-kit/issues).
