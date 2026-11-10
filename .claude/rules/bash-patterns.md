# Bash Patterns and Conventions

## Script Structure
Every bash script in this project follows this structure:
```bash
#!/usr/bin/env bash
set -euo pipefail

# Description comment

# Constants / config
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Functions (snake_case, local variables)
my_function() {
  local arg="$1"
  # ...
}

# Main logic
main() {
  # ...
}

main "$@"
```

## Variable Handling
- Always double-quote: `"$var"`, `"${array[@]}"`
- Use `${var:-default}` for optional variables
- Use `${var:?error message}` for required variables
- Declare `local` inside functions

## Error Handling
- `set -euo pipefail` at the top of every script
- Use `|| true` only when failure is explicitly acceptable
- Trap cleanup: `trap cleanup EXIT` for temp files
- Exit codes: 0=success, 1=error, 2=usage

## Logging
- Use logging helper functions (log_info, log_error, log_warn) when available
- stderr for diagnostics (`>&2`), stdout for data
- Never use bare `echo` for status messages in library code

## Common Gotchas
- **Boolean short-circuit**: `some_command && echo "ok"` returns exit 1 if command fails and `set -e` is active. Use `if some_command; then ...` instead.
- **Word splitting**: unquoted `$var` splits on spaces. Always quote.
- **Subshell scope**: `var=x | while ...`. The while runs in a subshell, var changes are lost. Use process substitution: `while ... done < <(command)`.
- **readlink -f**: not available on macOS by default. Use `cd "$(dirname "$0")" && pwd` pattern instead.
- **Arrays**: `"${arr[@]+"${arr[@]}"}"` for safe expansion of potentially empty arrays.
- **Broken pipe with `pipefail`**: `sed ... | head -N` causes SIGPIPE when `head` closes early, which `set -o pipefail` treats as failure. Use `sed`'s quit command instead: `sed -n '...; Nq'` to limit output without a pipe.
- **A failing command piped straight into a counter (`wc -l`) or `|| true` silently reports zero/partial instead of erroring**: `cmd | wc -l` takes the exit status of `wc -l` (always 0), not `cmd`, so a failed `gh api`/`curl`/etc. upstream of the pipe is invisible and counts as "no matches" instead of "unknown". The same masking happens with `output=$(cmd) || true`: a failure partway through a paginated call can still have printed a complete early page to stdout, and `|| true` preserves that partial output as if it were the full answer. Capture the command's own exit status explicitly (`if ! out=$(cmd); then handle-failure; fi`) before ever counting or trusting its output, especially in any decision that gates a retry cap or a cycle limit. Confirmed twice: `sova/commands/data/commands/address-pr.md`'s round-counter (PR #1082) and the docs-only CI detector (PR #1015, `.github/scripts/detect-code-changes.sh`).

## ShellCheck
All bash scripts must pass `shellcheck` with no warnings. Common suppressions:
- `# shellcheck source=./path` for dynamic sources
- `# shellcheck disable=SC2034` for variables used by sourced scripts

## Pre-Push Invariants (`invariants/`)

One line per check; each pointer leads to the full rule, its exemptions and its history. Re-run every invariant after a rebase, since each diffs against `origin/<base>`.

- **Commit format**: types `feat, fix, refactor, test, docs, chore, ci`; scopes only from `invariants/commit-format.sh:VALID_SCOPES` (in sync with AGENTS.md); `ci` alone is scopeless; `core` covers `sova/llm/`, and `sova/git/` takes `core` or `dashboard` by the directory the diff touches. A new top-level `sova/` directory needs its scope in `VALID_SCOPES`, AGENTS.md, `sova/config/models.py:DEFAULT_COMMIT_SCOPES` and `silent-revert-guard.sh:scope_paths()`. Run `bash invariants/commit-format.sh . main` before pushing (full entry: docs/invariants.md).
- **`no-double-dash`**: no space-dash-dash-space used as punctuation, checked on added lines (pre-push, CI) and on the whole tree (`--all`, in `make lint` and CI's invariants job); use a colon, comma, period or parentheses, and never put a colon in a NOSONAR or noqa reason, but keep the reason: `# noqa: BLE001 (reason)` is required by `tests/test_exception_handlers.py::TestNoqaJustification`, so rephrase it instead of stripping it (full entry: docs/invariants.md).
- **`silent-revert-guard`**: fails a commit that deletes 50+ lines outside its scope, deletes a protected file (`docs/*.md`, `invariants/*`, `.claude/rules/*.md`, ...), or removes half of what a recent base commit added. The scope count is raw deleted lines, so a verbatim move out of an out-of-scope file still trips it. Restore the code, or add `silent-revert-ok: <reason>` (12+ characters) for a deliberate removal (full entry: docs/invariants.md).
- **`cdn-integrity`**: every remote script or stylesheet in `sova/dashboard/templates/` needs `integrity`, `crossorigin` and an exact pinned version (full entry: docs/invariants.md).
- **`structured-logging`**: added lines under `sova/` use `get_logger(component=...)`, never `logging.getLogger`; exempt with `# stdlib-logging: <reason>` (full entry: docs/invariants.md).
- **`file-encoding`**: every `read_text()`/`write_text()` under `sova/` passes the literal `encoding="utf-8"`; exempt with `# file-encoding: <reason>` (full entry: docs/invariants.md).
- **New markdown subsections must not reuse a heading text already used elsewhere in the same file** (full entry: docs/invariants.md).
