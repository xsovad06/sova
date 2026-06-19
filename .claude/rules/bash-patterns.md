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
