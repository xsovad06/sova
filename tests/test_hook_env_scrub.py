"""Every git commit or push that fires the project's hooks must scrub the orchestrator-only variables.

A project's pre-commit and pre-push hooks run its test suite. When that suite inherits the orchestrator's
``SOVA_DATABASE_URL`` it opens the orchestrator's database (sixteen writer tests failed that way), so each
call under ``sova/`` that runs ``git commit`` or ``git push`` passes an ``env`` built by ``scrub_check_env``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOVA = Path(__file__).resolve().parent.parent / "sova"
_RUNNERS = {"run", "run_checked"}
_HOOK_FIRING = {"commit", "push"}


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(_SOVA.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            literals = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name not in _RUNNERS or literals[:1] != ["git"] or not _HOOK_FIRING & set(literals[1:3]):
                continue
            if "--no-verify" in literals:
                continue  # hooks are skipped, so there is no suite to inherit the variable
            if not any(kw.arg == "env" for kw in node.keywords):
                found.append(f"{path.relative_to(_SOVA.parent)}:{node.lineno}: git {literals[1]} without env=")
    return found


def test_hook_firing_git_calls_pass_an_env() -> None:
    assert _violations() == []


@pytest.mark.parametrize("module", ["sova/supervisor/rebase.py", "sova/git/worktree.py", "sova/git/rebase.py"])
def test_the_env_is_scrubbed_not_just_present(module: str) -> None:
    source = (_SOVA.parent / module).read_text(encoding="utf-8")
    assert "scrub_check_env" in source
