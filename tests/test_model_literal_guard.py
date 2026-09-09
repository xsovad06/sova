"""AST-based guard against hardcoded model= literals at LLM call sites.

A regex cannot distinguish ``model="sonnet"`` in a real call from the same
text in a docstring, a log line (``log.info("llm.invoke", model=model)``),
or a test fixture. AST gives exact call-site resolution: only a bare string
constant passed as the ``model`` keyword to ``invoke()``, ``invoke_command()``,
or ``invoke_streaming()`` is flagged. A defaulting expression such as
``ctx.resolved_model or "haiku"`` is a legitimate fallback and is not a bare
``ast.Constant``, so it is never flagged.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOVA_ROOT = _REPO_ROOT / "sova"
_TARGET_FUNCS = frozenset({"invoke", "invoke_command", "invoke_streaming"})


def _collect_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local alias -> real name for ``from ... import <target> as <alias>``.

    ``from sova.llm.client import invoke as llm_invoke`` followed by
    ``llm_invoke(..., model="sonnet")`` would otherwise escape inspection:
    the call's bare name is ``llm_invoke``, not one of ``_TARGET_FUNCS``.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _TARGET_FUNCS:
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _callee_name(func: ast.expr, aliases: dict[str, str]) -> str | None:
    """Return the called function's name for a bare-name or attribute-form call.

    Resolving attribute-form callees (``client.invoke(...)``,
    ``provider.invoke(...)``) is required, or call sites like
    ``sova/git/rebase.py`` escape inspection entirely. Bare names are
    resolved through *aliases* first, so an imported-as-renamed target
    function is still recognized.
    """
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_literal_model_calls(path: Path) -> list[tuple[str, int]]:
    """Return (relative_path, lineno) for every bare string `model=` literal in *path*.

    Parse failures raise (not skip): a malformed file must fail the guard
    loudly rather than silently passing uninspected.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _collect_import_aliases(tree)
    rel_path = str(path.relative_to(_REPO_ROOT))
    violations: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node.func, aliases) not in _TARGET_FUNCS:
            continue
        for kw in node.keywords:
            if kw.arg == "model" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                violations.append((rel_path, node.lineno))
    return violations


def test_no_hardcoded_model_literals_at_invoke_sites() -> None:
    """No invoke()/invoke_command()/invoke_streaming() call may hardcode model=.

    Model selection must flow through task_type routing, role config, or a
    defaulting expression over ctx.resolved_model / config (never a bare
    string baked into the call site). tests/ is excluded: test fixtures
    legitimately pass literal model names.
    """
    unexpected: list[tuple[str, int]] = []
    for path in sorted(_SOVA_ROOT.rglob("*.py")):
        unexpected.extend(_find_literal_model_calls(path))
    assert not unexpected, (
        "Hardcoded model= string literal(s) found at invoke()/invoke_command()/"
        f"invoke_streaming() call sites: {unexpected}. Route via task_type= or "
        "a defaulting expression instead of a bare literal."
    )


def test_aliased_invoke_import_is_still_flagged() -> None:
    """A ``from ... import invoke as llm_invoke`` alias must not bypass the guard."""
    source = textwrap.dedent(
        """
        from sova.llm.client import invoke as llm_invoke


        async def _call():
            return await llm_invoke("prompt", model="sonnet")
        """
    )
    tree = ast.parse(source)
    aliases = _collect_import_aliases(tree)
    assert aliases == {"llm_invoke": "invoke"}

    violations = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_name(node.func, aliases) in _TARGET_FUNCS
        for kw in node.keywords
        if kw.arg == "model" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    ]
    assert violations, "aliased invoke() call with a literal model= was not flagged"
