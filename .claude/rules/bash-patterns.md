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

- **Commit format**: only `feat, fix, refactor, test, docs, chore, ci` are valid types; only the scopes in `invariants/commit-format.sh:VALID_SCOPES` (kept in sync with AGENTS.md's Scopes list) are valid. `ci` is the only type allowed without a scope. There is no dedicated `llm` scope: use `core` for `sova/llm/` changes; use `core`/`dashboard` for `sova/git/` changes depending on which directory the diff actually touches, not a semantically related area (mismatches pass the invariant but mislead `git log --grep` and code archaeology). Applies to programmatically generated commits too (`_ensure_committed`, LLM fix prompts). Run `bash invariants/commit-format.sh . main` locally before pushing. Adding a new top-level `sova/` directory (e.g. `supervisor`, `mcp`) requires adding its scope to `VALID_SCOPES`, AGENTS.md, `sova/config/models.py:DEFAULT_COMMIT_SCOPES` and `silent-revert-guard.sh:scope_paths()`, or pushes for that scope fail even with an obviously correct name.
- **`no-double-dash`** (`invariants/no-double-dash.sh`, tests in `tests/test_no_double_dash.py`): flags a space-dash-dash-space used as punctuation in `.md`, `.py`, `.html`, `.sh`, `.js`, `.yml`, `.toml` and `.css` files and in `.githooks/` hooks (docstrings and comments included, not just prose). Two modes share one set of exemption rules in one script, so they cannot disagree. The default mode (pre-push and CI: `bash invariants/no-double-dash.sh . main`) checks only lines a diff *adds* against `origin/<base>`; `--all <dir>` checks every tracked file, and `TestRepositoryHasNoProseDoubleDash` runs it over this repository on every test run, which is what keeps an old occurrence from surviving just because nothing touched its line (issue #1179 removed about 970 that had accumulated that way). Exempt: fenced code blocks and inline code in markdown, inline code in other files, and in non-markdown files a line where one of a fixed list of command names (`git`, `grep`, `find`, `make`, `diff` and so on, plus `git_*` wrapper functions) appears before the marker, treated as a POSIX end-of-options `--`. Non-obvious: (1) in the default mode, touching one word on a pre-existing line containing the pattern re-flags that whole line, and every line of a brand-new file counts as added; (2) it diffs against `origin/<base>`, not local `main`, so the set of "added" lines can shift after a rebase, so re-run it (and every other invariant) after any rebase; (3) the command-name exemption is a heuristic over the whole line, so prose that merely contains a word such as `find` or `make` before the marker is not flagged; it is still wrong, so write literal syntax in backticks and use a colon, comma, period or parentheses for punctuation; (4) a table cell holding a bare placeholder takes `n/a`, not a dash; (5) the script builds the marker from two quoted parts, and tests build it the same way (`" -" + "- "`), so neither contains the literal; (6) the marker also matches at end of line (trailing whitespace allowed), since markdown soft-wrap drops the trailing space the interior form needs, confirmed missing nine real instances across the tree before the fix (PR #1180); (7) never rewrite punctuation inside a tool directive without checking the tool accepts the result: SonarCloud rule S7632 rejects a suppression comment whose reason contains a colon (`# NOSONAR: reason`, or a parenthesized `# noqa: CODE (reason: detail)`), which silently un-suppresses the finding and fails the quality gate; a colon-free parenthesized reason (`# noqa: CODE (reason)`) is accepted and is in fact required for `# noqa: BLE001` by `tests/test_exception_handlers.py::TestNoqaJustification`, so rephrase the reason to drop the colon (use a semicolon or comma instead) rather than stripping it to a bare directive, which passes Sonar's stale cached finding but then fails that test (confirmed on PR #1258, where `fix(core): address SonarCloud findings in the shared diagnostics module` stripped two reasons entirely and broke CI). Fix: use a colon, comma, or parentheses instead of the marker. In YAML frontmatter use parentheses, since a colon followed by a space is invalid in an unquoted scalar.
- **`silent-revert-guard`** (`invariants/silent-revert-guard.sh`, tests in `tests/test_silent_revert_guard.py`): catches a commit that silently drops code another PR already landed. Motivated by PR #1153 (see the test module's docstring). Two checks per commit in `origin/<base>..HEAD`. (1) A commit with a narrow scope (`db`, `ipc`, `adapters`, ...) may not delete `SILENT_REVERT_THRESHOLD` (50) or more lines outside it; `core` and `dashboard` are exempt from that line count because they legitimately touch most of the tree (measured: 329 of the last 600 commits), and `tests/` rides along with every scope. Whole-file deletion of `docs/*.md`, `invariants/*`, `.githooks/*`, `.github/workflows/*`, `.github/scripts/*` or `.claude/rules/*.md` is flagged at any size and for every scope, `core` included, except the one that owns the path (for example `invariants` for `invariants/` and `.githooks/`). (2) Scope independent: a commit that removes at least 15 lines and 50% of what a base-branch commit from the preceding 72 hours added to a file is reverting it, and the report names that commit and its PR; lines the commit re-adds anywhere (a module split) do not count. Fix a real finding by restoring the code. `silent-revert-ok: <reason>` on its own line in the commit body exempts a commit only when the reason is 12 or more characters, because a bare acknowledgment is not a justification. The default range is `origin/<base>..HEAD`, never `@{u}..HEAD`: after a rebase and force-push the old remote tip is no longer an ancestor, so `@{u}..HEAD` would blame the branch for the base's own commits. The pre-push hook refreshes `origin/main` first for the same reason. `SILENT_REVERT_RANGE=<a>..<b>` audits an explicit range, and an unresolvable one is a hard failure rather than a pass.
- **`cdn-integrity`** (`invariants/cdn-integrity.sh`, tests in `tests/test_cdn_integrity.py`): every remote `<script src>` or stylesheet/modulepreload `<link href>` in `sova/dashboard/templates/` needs `integrity="sha256|384|512-..."`, `crossorigin`, and, on jsDelivr, unpkg and cdnjs, an exact version. A range such as `marked@15` serves new bytes on the next upstream release and the pinned hash then blocks the script, so pinning is part of the rule, not a style preference. It checks the whole tree rather than the diff, because the usual regression is a stale branch bringing an old tag back. Hash the pinned file with `curl -fsS <url> | openssl dgst -sha384 -binary | openssl base64 -A`, and prefer a file that ships in the npm package: jsDelivr generates some `.min.js` files itself (today `cytoscape-dagre.min.js` in `role_editor.html`), so their bytes are jsDelivr's, not the package's.
- **`structured-logging`** (`invariants/structured-logging.sh`, tests in `tests/test_structured_logging.py`): a line added under `sova/` may not create a logger with `logging.getLogger(...)` or `from logging import getLogger`; use `get_logger(component=...)` from `sova.utils.logging`. A stdlib logger accepts `log.warning("event")` but raises `TypeError` on structlog-style keyword arguments, only when that line runs. Diff based, so any module that already uses a stdlib logger does not fail it until a line in it is touched. End a line with `# stdlib-logging: <reason>` for a genuine need, such as quieting a third-party library's logger; a marker with no reason does not exempt it.
- **`file-encoding`** (`invariants/file-encoding.sh`, tests in `tests/test_file_encoding.py`): every `read_text()`/`write_text()` call under `sova/*.py` must pass `encoding="utf-8"` (the literal, by keyword or in pathlib's positional slot: `p.read_text("utf-8")`, `p.write_text(data, "utf-8")`; `encoding="latin-1"` or a variable fails). The platform default encoding is not UTF-8 on every OS, so an unencoded call silently mangles non-ASCII content on whichever platform it first runs on. No unencoded call remains in the tree. Two modes share one script, like `no-double-dash.sh`: the default mode (pre-push and CI: `bash invariants/file-encoding.sh . main`) checks only calls touching a line the branch adds since its fork point from `origin/<base>` (the `git merge-base`, not the base's tip, so a line the base has since rewritten and the branch still holds in its old form is not blamed on the branch), and `--all <dir>` checks every tracked `sova/*.py` file. `TestRepositoryHasNoUnencodedTextIo` runs `--all` over this repository on every test run, which is what keeps the zero-remaining state true rather than true today: the diff mode alone would let a call that no diff touches survive. Unlike `structured-logging.sh`, the script parses each file with Python's `ast` (stdlib only, via `python3 -I`) instead of grepping the diff: a string literal that merely contains `p.read_text()` is not a call, and a call split over several lines is judged as a whole. `# file-encoding: <reason>` on any line of the call exempts it (a deliberate non-UTF-8 read, e.g.). It fails closed: in diff mode an unresolvable `origin/<base>` or a failed `git diff`, and in either mode a file that does not parse, is a failure, never a pass.
- **New markdown subsections must not reuse a heading text already used elsewhere in the same file**: GitHub de-duplicates anchors (`#installation` vs `#installation-1`), making links ambiguous and fragile to reordering. Disambiguate with the feature name (e.g. `### Headroom Installation`).
