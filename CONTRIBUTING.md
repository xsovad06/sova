# Contributing to SOVA

Thank you for your interest in contributing to SOVA. This guide covers the development setup, coding standards, and contribution process.

## Development Setup

### Prerequisites

- Python 3.12+
- [GitHub CLI](https://cli.github.com/) (`gh`), authenticated
- `git`
- `shellcheck` (for bash linting: `brew install shellcheck` on macOS)

**Optional** (for AI-assisted workflows):
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`): powers SOVA's slash commands and agent orchestration

### Quick Setup (collaborators)

```bash
git clone https://github.com/xsovad06/sova.git
cd sova
make setup    # Installs deps + configures git hooks
make check    # Verify everything works
```

### Fork Setup (external contributors)

If you don't have push access to the main repo, work from a fork:

```bash
# 1. Fork on GitHub: https://github.com/xsovad06/sova/fork

# 2. Clone your fork
git clone https://github.com/YOUR_USERNAME/sova.git
cd sova

# 3. Add upstream remote
git remote add upstream https://github.com/xsovad06/sova.git
git fetch upstream

# 4. Set up dev environment
make setup

# 5. Create a feature branch
git checkout -b feat/my-feature upstream/main
```

To sync your fork with upstream before starting new work:

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

### Running Checks

```bash
make check    # Lint + test (CI-equivalent, run before submitting PR)
make test     # Run all tests
make lint     # ShellCheck + Ruff
make format   # Auto-format Python code
```

All checks must pass before submitting a PR. The CI pipeline runs the same checks.

### Environment Health Check

```bash
sova doctor   # Checks Python, git, gh CLI, hooks, config
```

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

**Enforced by git hooks**: the `commit-msg` hook rejects commits with `Co-Authored-By:` lines or AI branding. The `pre-push` hook runs lint and format checks before pushing.

## Commit Signing (optional)

For verified commits on GitHub, set up GPG or SSH signing:

```bash
# GPG (if you already have a key)
git config --global commit.gpgsign true
git config --global user.signingkey YOUR_KEY_ID
# Upload public key: https://github.com/settings/gpg/new

# SSH (simpler alternative)
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
# Upload signing key: https://github.com/settings/ssh/new (type: Signing Key)
```

## Pull Request Process

1. Create a feature branch from `main`: `feat/<name>`, `fix/<name>`, or `refactor/<name>`
2. Make your changes with tests
3. Run `make check` and ensure all checks pass
4. Submit a PR linking the related issue (`Closes #<number>` in the body)
5. Include a "Test Plan" section describing how to verify the changes
6. Address review feedback

**From a fork**: push to your fork, then open a PR against `xsovad06/sova:main`. GitHub will run CI automatically on fork PRs.

```bash
git push origin feat/my-feature
gh pr create --repo xsovad06/sova
```

## Reporting Issues

Use the [issue templates](https://github.com/xsovad06/sova/issues/new/choose) for:
- **Bug reports**: include steps to reproduce, expected vs actual behavior
- **Feature requests**: describe the objective and acceptance criteria
- **Tasks**: for implementation work with clear scope

## Testing

- Tests live in `tests/` and use pytest with async support
- Run with `make test` or `pytest tests/`
- New features should include tests
- Bash invariant scripts should handle `--help` gracefully

## Troubleshooting

**"shellcheck: command not found"**: Install with `brew install shellcheck` (macOS) or `apt install shellcheck` (Linux).

**Git hooks not running**: Run `make setup` or `git config core.hooksPath .githooks`. Verify with `sova doctor`.

**Ruff not found**: Ensure dev dependencies are installed: `pip install -e ".[dev]"` or `make install-deps`.

**Tests fail with import errors**: The package must be installed in editable mode: `pip install -e ".[dev]"`.

## AI-Assisted Development

If you use Claude Code, see [CLAUDE.md](CLAUDE.md) for SOVA-specific slash commands (`/develop`, `/test`, `/review`, `/pr`, etc.) and agent orchestration workflows. The commands work on any project SOVA is installed into.

## Questions?

File an [issue](https://github.com/xsovad06/sova/issues).
