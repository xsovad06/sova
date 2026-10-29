#!/usr/bin/env bash
# Invariant: new sova/ code reads/writes text files with an explicit UTF-8 encoding.
#
# `Path.read_text()`/`Path.write_text()` with no `encoding=` argument uses the
# platform's default encoding, not UTF-8. On a non-UTF-8 locale this silently
# mangles non-ASCII content, and the bug only surfaces on that platform. Two modes
# share one set of rules, so they cannot disagree. The default mode (pre-push and
# CI) checks only calls touching a line a branch adds. `--all` checks every tracked
# sova/*.py file, which is what keeps a call that predates the rule, or one added by
# a path the diff does not see, from surviving just because no change touched its
# line (see TestRepositoryHasNoUnencodedTextIo in tests/test_file_encoding.py).
#
# Each changed file is parsed with Python's `ast` rather than grepped, so a string
# literal that merely contains `p.read_text()`, a call split over several lines,
# `encoding="latin-1"`, and the positional `p.read_text("utf-8")` are all judged as the
# calls they are. A base ref that
# cannot be resolved, or a file that cannot be parsed, fails the check: neither
# may read as "nothing to flag".
#
# Escape hatch: put `# file-encoding: <reason>` on a line of the call.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: $0 <worktree-dir> [base-branch]"
  echo "       $0 --all <worktree-dir>"
  echo "Checks that read_text()/write_text() calls under sova/*.py pass encoding=\"utf-8\"."
  echo "Default mode checks calls on lines added since the branch forked from origin/<base-branch>; --all checks every tracked file."
  echo "Exempt a call with '# file-encoding: <reason>' on one of its lines."
  exit 0
fi

MODE=added
if [[ "${1:-}" == "--all" ]]; then
  MODE=all
  shift
fi

WORKTREE_DIR="${1:-.}"
BASE_BRANCH="${2:-main}"

exec python3 -I - "$MODE" "$WORKTREE_DIR" "$BASE_BRANCH" << 'PY'
import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

mode, worktree, base = sys.argv[1], sys.argv[2], f"origin/{sys.argv[3]}"
check_all = mode == "all"
# What "added" is measured against: the point this branch forked from the base. Diffing against the base's
# tip instead would report a line the base has since rewritten, and this branch still holds in its old form,
# as added by this branch. Set in the default mode, once the base is known to resolve.
fork = base
# Where pathlib takes `encoding` positionally: read_text(encoding, errors) and write_text(data, encoding, ...).
METHODS = {"read_text": 0, "write_text": 1}
UTF8_NAMES = {"utf-8", "utf8"}
EXEMPTION = re.compile(r"#\s*file-encoding:\s*\S")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True, check=True).stdout


def added_lines(path: str) -> set[int]:
    lines: set[int] = set()
    for row in git("diff", "-U0", fork, "--", path).splitlines():
        hunk = HUNK.match(row)
        if hunk:
            start = int(hunk.group(1))
            lines.update(range(start, start + int(hunk.group(2) or 1)))
    return lines


def is_utf8_name(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.lower() in UTF8_NAMES


def has_utf8_encoding(call: ast.Call, method: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "encoding":
            return is_utf8_name(keyword.value)
    position = METHODS[method]
    return len(call.args) > position and is_utf8_name(call.args[position])


def exempt_lines(source: str) -> set[int]:
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return {tok.start[0] for tok in tokens if tok.type == tokenize.COMMENT and EXEMPTION.search(tok.string)}


def violations_in(path: str) -> list[str]:
    source = Path(worktree, path).read_text(encoding="utf-8")
    added = set() if check_all else added_lines(path)
    exempt = exempt_lines(source)
    lines = source.splitlines()
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in METHODS):
            continue
        span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if (check_all or span & added) and not span & exempt and not has_utf8_encoding(node, node.func.attr):
            found.append((node.lineno, f"  {path}:{node.lineno}: {lines[node.lineno - 1].strip()}"))
    return [text for _, text in sorted(found)]


try:
    if check_all:
        changed = [name for name in git("ls-files", "--", "sova/*.py").splitlines() if name]
    else:
        resolved = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], capture_output=True
        )
        if resolved.returncode != 0:
            print(f"FAIL: cannot resolve {base}, so the added lines cannot be checked (git fetch origin).")
            sys.exit(1)
        fork = git("merge-base", base, "HEAD").strip()
        changed = [name for name in git("diff", "--name-only", fork, "--", "sova/*.py").splitlines() if name]
    problems: list[str] = []
    for name in changed:
        if not Path(worktree, name).is_file():
            continue
        try:
            problems.extend(violations_in(name))
        except (SyntaxError, UnicodeDecodeError, tokenize.TokenError) as exc:
            problems.append(f"  {name}: cannot be parsed, so its text I/O calls cannot be checked ({exc})")
except subprocess.CalledProcessError as exc:
    print(f"FAIL: git failed ({' '.join(exc.cmd[2:])}), so the files cannot be checked.")
    sys.exit(1)

if problems:
    scope = "read_text()/write_text() calls under sova/" if check_all else "new read_text()/write_text() calls under sova/"
    print(f'FAIL: {scope} without encoding="utf-8":')
    print("\n".join(problems))
    print()
    print('  path.read_text(encoding="utf-8")')
    print('  path.write_text(text, encoding="utf-8")')
    print("If a non-UTF-8 encoding is genuinely required, put '# file-encoding: <reason>' on a line of the call.")
    sys.exit(1)
PY
