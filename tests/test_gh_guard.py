"""The autouse guard in tests/conftest.py that refuses to start a real gh process (#1425)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import RealGhCallError, _real_gh_command


class TestRealGhCommand:
    @pytest.mark.parametrize(
        ("args", "shell"),
        [
            (["gh", "issue", "edit", "1", "--add-label", "agent:in-review"], False),
            (("gh", "pr", "view", "5"), False),
            (["sh", "-c", "gh issue view 1"], False),
            (["bash", "-c", "cd /tmp && gh pr list"], False),
            (["/bin/sh", "-c", "GH_TOKEN=x gh api user"], False),
            ("gh issue list", True),
            ("echo start; gh issue list | head", True),
            ("out=$(gh pr view 1)", True),
        ],
    )
    def test_gh_is_recognised(self, args: object, shell: bool) -> None:
        assert _real_gh_command(args, shell=shell, env=None) is not None

    @pytest.mark.parametrize(
        ("args", "shell"),
        [
            (["git", "commit", "-m", "use gh pr view"], False),
            (["sh", "-c", "echo gh"], False),
            (["ghost", "run"], False),
            ("git log --grep gh", True),
            ([], False),
        ],
    )
    def test_other_commands_pass(self, args: object, shell: bool) -> None:
        assert _real_gh_command(args, shell=shell, env=None) is None

    def test_a_stub_gh_first_on_path_is_not_the_real_one(self, tmp_path: Path) -> None:
        stub = tmp_path / "gh"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

        assert _real_gh_command(["gh", "pr", "view"], shell=False, env={"PATH": str(tmp_path)}) is None
        assert _real_gh_command([str(stub), "pr", "view"], shell=False, env=None) is None

    def test_on_a_machine_without_gh_only_an_unresolvable_gh_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = tmp_path / "gh"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        monkeypatch.setattr("tests.conftest._REAL_GH", None)

        assert _real_gh_command(["gh", "pr", "view"], shell=False, env={"PATH": str(tmp_path)}) is None
        assert _real_gh_command(["gh", "pr", "view"], shell=False, env={"PATH": str(tmp_path / "none")}) is not None


@pytest.mark.parametrize(
    "start",
    [
        lambda: subprocess.run(["gh", "issue", "list"], check=False),
        lambda: subprocess.run("true && gh issue list", shell=True, check=False),
    ],
    ids=["argv", "shell"],
)
def test_the_guard_refuses_a_real_gh_process(start: Callable[[], object], _no_real_gh: list[str]) -> None:
    with pytest.raises(RealGhCallError, match="gh issue list"):
        start()
    assert len(_no_real_gh) == 1
    _no_real_gh.clear()  # the deliberate call must not fail this test at teardown


async def test_the_guard_covers_asyncio_subprocesses(_no_real_gh: list[str]) -> None:
    with pytest.raises(RealGhCallError):
        await asyncio.create_subprocess_exec("gh", "pr", "view", "1")
    _no_real_gh.clear()


def test_the_guard_lets_other_commands_run() -> None:
    assert subprocess.run(["git", "--version"], capture_output=True, check=True).returncode == 0
