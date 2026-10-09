"""Tests for ``invariants/structured-logging.sh``.

SOVA logs with structlog (``get_logger(component=...)``) and passes context as
keyword arguments. A stdlib ``logging.getLogger`` logger accepts the same call
shape at a glance but raises ``TypeError`` on those keywords at runtime, so new
code must not create one. Only lines a branch adds are checked: the modules that
already use a stdlib logger keep passing until someone migrates them.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.conftest import INVARIANTS_DIR, InvariantRepo

SCRIPT = "structured-logging.sh"
MODULE = "sova/core/example.py"


def add_line(repo: InvariantRepo, line: str, path: str = MODULE) -> subprocess.CompletedProcess[str]:
    """On a new branch, add ``line`` to ``path`` and run the invariant."""
    repo.start_branch()
    repo.write(path, f'"""Example module."""\n\n{line}\n')
    repo.commit("feat(core): example")
    return repo.run(SCRIPT)


def test_script_is_executable_and_handles_help() -> None:
    assert os.access(INVARIANTS_DIR / SCRIPT, os.X_OK)
    result = subprocess.run(["bash", str(INVARIANTS_DIR / SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0


def test_no_changes_passes(invariant_repo: InvariantRepo) -> None:
    invariant_repo.start_branch()
    assert invariant_repo.run(SCRIPT).returncode == 0


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('log = get_logger(component="core.example")', id="the project helper"),
        pytest.param("import logging\nLEVEL = logging.WARNING", id="stdlib constants"),
        pytest.param("from logging import getLoggerClass", id="a different name sharing the prefix"),
        pytest.param("# never call logging.getLogger( in this module", id="a comment"),
        pytest.param(
            'logging.getLogger("sqlalchemy").setLevel(logging.WARNING)  # stdlib-logging: quiet a third-party library',
            id="exempted with a reason",
        ),
    ],
)
def test_allowed(invariant_repo: InvariantRepo, line: str) -> None:
    result = add_line(invariant_repo, line)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("logger = logging.getLogger(__name__)", id="module logger"),
        pytest.param('    log = logging.getLogger ("sova.db")', id="indented, space before the call"),
        pytest.param("from logging import getLogger", id="imported name"),
        pytest.param("from logging import Handler, getLogger", id="imported among others"),
        pytest.param('log = logging.getLogger("x")  # stdlib-logging:', id="exemption without a reason"),
    ],
)
def test_rejected(invariant_repo: InvariantRepo, line: str) -> None:
    result = add_line(invariant_repo, line)
    assert result.returncode == 1
    assert f"{MODULE}: {line}" in result.stdout
    assert "get_logger" in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("sova/utils/logging.py", id="the logging helper itself"),
        pytest.param("tests/test_example.py", id="tests"),
        pytest.param("scripts/tool.py", id="outside the package"),
    ],
)
def test_paths_outside_the_rule(invariant_repo: InvariantRepo, path: str) -> None:
    assert add_line(invariant_repo, "logger = logging.getLogger(__name__)", path=path).returncode == 0


def test_an_existing_stdlib_logger_does_not_fail_an_unrelated_edit(invariant_repo: InvariantRepo) -> None:
    invariant_repo.write(MODULE, "import logging\n\nlogger = logging.getLogger(__name__)\n")
    invariant_repo.land_on_main("feat(core): legacy module")
    invariant_repo.start_branch()
    invariant_repo.write(MODULE, "import logging\n\nlogger = logging.getLogger(__name__)\nVALUE = 1\n")
    invariant_repo.commit("feat(core): add a constant")
    assert invariant_repo.run(SCRIPT).returncode == 0


def test_uncommitted_changes_are_checked_too(invariant_repo: InvariantRepo) -> None:
    """Like the other diff-based invariants, it compares the working tree with the base."""
    invariant_repo.start_branch()
    invariant_repo.write(MODULE, "logger = logging.getLogger(__name__)\n")
    invariant_repo.git("add", "-N", MODULE)
    assert invariant_repo.run(SCRIPT).returncode == 1
