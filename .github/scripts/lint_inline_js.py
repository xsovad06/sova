#!/usr/bin/env python3
"""Syntax-check the inline <script> blocks of the dashboard's Jinja templates with `node --check`.

Each Jinja expression is replaced with a JavaScript identifier stand-in and each statement or
comment tag with whitespace, keeping the newlines a tag spans, so the block still parses and node's
error line is the template's own line. Blocks with a `src=` attribute or a non-JavaScript `type`
(`application/json`, `text/template`) are skipped; `type="module"` blocks are checked as modules.

An interim gate: once #1278 moves the inline scripts into static/js/pages/, the static/js pass of
`make lint-js` covers that code and this check can go. Standard library only, since CI runs it with
the runner's own python3. Exit codes: 0 clean, 1 syntax error or missing node, 2 usage error.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_TEMPLATE_DIR = Path("sova/dashboard/templates")
NODE_TIMEOUT_SECONDS = 30
MAX_PARALLEL_CHECKS = 8

_SCRIPT_BLOCK = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.DOTALL | re.IGNORECASE)
_TYPE_ATTR = re.compile(r"""\btype\s*=\s*["']?(?P<type>[^"'\s>]+)""", re.IGNORECASE)
_SRC_ATTR = re.compile(r"\bsrc\s*=", re.IGNORECASE)
_JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_TAG = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_CLASSIC_TYPES = {"", "text/javascript", "application/javascript"}
_EXPRESSION_STAND_IN = "_"


def strip_jinja(source: str) -> str:
    """Neutralize Jinja syntax in a JavaScript block, preserving its line count."""

    def blank(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n") or " "

    def stand_in(match: re.Match[str]) -> str:
        return _EXPRESSION_STAND_IN + "\n" * match.group(0).count("\n")

    return _JINJA_EXPRESSION.sub(stand_in, _JINJA_TAG.sub(blank, source))


def script_kind(attrs: str) -> str | None:
    """Return "script" or "module" for a JavaScript block, or None for a block to skip."""
    if _SRC_ATTR.search(attrs):
        return None
    match = _TYPE_ATTR.search(attrs)
    script_type = match.group("type").lower() if match else ""
    if script_type == "module":
        return "module"
    return "script" if script_type in _CLASSIC_TYPES else None


def inline_blocks(template: str) -> list[tuple[int, str, str]]:
    """Return (first body line, kind, Jinja-stripped body) for every inline JavaScript block."""
    blocks = []
    for match in _SCRIPT_BLOCK.finditer(template):
        kind = script_kind(match.group("attrs"))
        if kind is None:
            continue
        first_line = template.count("\n", 0, match.start("body")) + 1
        blocks.append((first_line, kind, strip_jinja(match.group("body"))))
    return blocks


def check_block(node: str, label: str, first_line: int, kind: str, body: str) -> str | None:
    """Run `node --check` on one block; return node's error output, or None when it parses."""
    # Leading newlines put the body on its template line, so node's "[stdin]:N" is template line N.
    padded = "\n" * (first_line - 1) + body
    input_type = "module" if kind == "module" else "commonjs"
    result = subprocess.run(
        [node, f"--input-type={input_type}", "--check"],
        input=padded,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=NODE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode == 0:
        return None
    # Keep the location, source excerpt and message; drop node's own internal stack frames.
    lines = result.stderr.replace("[stdin]", label).splitlines()
    excerpt = [line for line in lines if not line.startswith("    at ") and not line.startswith("Node.js v")]
    return "\n".join(excerpt).strip() or f"{label}: node --check failed"


def check_templates(paths: list[Path], node: str) -> int:
    """Check every inline block of the given templates; return the number of failing blocks.

    The node processes run concurrently (each spends most of its time starting up), which keeps the
    whole pass near one second for the pre-push hook.
    """
    failures = 0
    jobs = []
    for path in paths:
        try:
            template = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"lint-js: cannot read {path}: {exc}", file=sys.stderr)
            failures += 1
            continue
        jobs.extend((path, *block) for block in inline_blocks(template))
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHECKS) as pool:
        results = pool.map(lambda job: (job[0], check_block(node, str(job[0]), *job[1:])), jobs)
        for path, error in results:
            if error is not None:
                print(f"lint-js: syntax error in an inline <script> of {path}\n{error}", file=sys.stderr)
                failures += 1
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Syntax-check inline <script> blocks of Jinja templates with node --check.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=f"template files or directories (default: {DEFAULT_TEMPLATE_DIR})",
    )
    args = parser.parse_args(argv)

    node = shutil.which("node")
    if node is None:
        print("lint-js: node is required", file=sys.stderr)
        return 1

    paths: list[Path] = []
    for target in args.paths or [DEFAULT_TEMPLATE_DIR]:
        if target.is_dir():
            paths.extend(sorted(target.rglob("*.html")))
        elif target.is_file():
            paths.append(target)
        else:
            parser.error(f"no such template file or directory: {target}")
    return 1 if check_templates(paths, node) else 0


if __name__ == "__main__":
    sys.exit(main())
