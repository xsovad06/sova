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
- **Subshell scope**: `var=x | while ...` -- the while runs in a subshell, var changes are lost. Use process substitution: `while ... done < <(command)`.
- **readlink -f**: not available on macOS by default. Use `cd "$(dirname "$0")" && pwd` pattern instead.
- **Arrays**: `"${arr[@]+"${arr[@]}"}"` for safe expansion of potentially empty arrays.
- **Broken pipe with `pipefail`**: `sed ... | head -N` causes SIGPIPE when `head` closes early, which `set -o pipefail` treats as failure. Use `sed`'s quit command instead: `sed -n '...; Nq'` to limit output without a pipe.

## ShellCheck
All bash scripts must pass `shellcheck` with no warnings. Common suppressions:
- `# shellcheck source=./path` for dynamic sources
- `# shellcheck disable=SC2034` for variables used by sourced scripts

## Pre-Push Invariants (`invariants/`)

- **Commit format**: only `feat, fix, refactor, test, docs, chore, ci` are valid types; only the scopes in `invariants/commit-format.sh:VALID_SCOPES` (kept in sync with AGENTS.md's Scopes list) are valid. `ci` is the only type allowed without a scope. There is no dedicated `llm` scope: use `core` for `sova/llm/` changes; use `core`/`dashboard` for `sova/git/` changes depending on which directory the diff actually touches, not a semantically related area (mismatches pass the invariant but mislead `git log --grep` and code archaeology). Applies to programmatically generated commits too (`_ensure_committed`, LLM fix prompts). Run `bash invariants/commit-format.sh . main` locally before pushing. Adding a new top-level `sova/` directory (e.g. `supervisor`, `mcp`) requires adding its scope to both `VALID_SCOPES` and AGENTS.md, or pushes for that scope fail even with an obviously correct name.
- **`no-double-dash`** (`invariants/no-double-dash.sh`): flags a space-dash-dash-space in any line a diff *adds* to `.py`/`.md`/`.html` (docstrings and comments included, not just prose), excluding fenced/inline code and POSIX end-of-options (`git diff origin/main -- path`). Two non-obvious triggers: (1) it only scans added lines, so touching one word on a pre-existing line containing the pattern re-flags that whole line, including bullet-list separators like `- **\`path\`** -- description` in older docs, and every line of a brand-new file counts as added; (2) it diffs against `origin/<base>`, not local `main`, so the set of "added" lines can shift after a rebase; re-run it (and every other invariant) after any rebase, not just once before the first push. Fix: use a colon, comma, or parentheses instead of ` -- `.
- **New markdown subsections must not reuse a heading text already used elsewhere in the same file**: GitHub de-duplicates anchors (`#installation` vs `#installation-1`), making links ambiguous and fragile to reordering. Disambiguate with the feature name (e.g. `### Headroom Installation`).
