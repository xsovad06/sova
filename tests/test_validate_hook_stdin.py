"""Tests for pre-push hook stdin handling in the validate step.

Regression cover for the hang where ValidateStep ran a pre-push hook without
feeding it the ref lines git supplies on stdin. Hooks that loop over those
lines blocked on read until the step timeout killed them, producing no output
and an empty auto-fix prompt.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.core.steps.validate import ValidateStep, build_pre_push_stdin
from sova.utils.shell import ShellResult, run

# A hook shaped like a real pre-push hook: it consumes stdin before working.
_READS_STDIN_HOOK = """#!/usr/bin/env bash
set -euo pipefail
all_deletions=true
while IFS=' ' read -r _lref lsha _rref _rsha; do
  if [ "$lsha" != "0000000000000000000000000000000000000000" ]; then
    all_deletions=false
  fi
done
if [ "$all_deletions" = "true" ]; then
  echo "SKIPPED"
  exit 0
fi
echo "CHECKS_RAN"
"""


async def _init_repo(path: Path) -> None:
    await run("git", "init", "-q", "-b", "main", cwd=path)
    await run("git", "config", "user.email", "test@example.com", cwd=path)
    await run("git", "config", "user.name", "Test User", cwd=path)
    await run("git", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)


@pytest.fixture
def stdin_hook(tmp_path: Path) -> str:
    hook = tmp_path / "pre-push"
    hook.write_text(_READS_STDIN_HOOK)
    hook.chmod(0o755)
    return str(hook)


class TestShellStdinIsolation:
    async def test_run_does_not_inherit_parent_stdin(self, stdin_hook: str) -> None:
        """Inheriting our stdin lets a stdin-reading child block until the timeout."""
        with patch("asyncio.create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as spawn:
            await run(stdin_hook, timeout=10)

        assert spawn.call_args.kwargs["stdin"] is asyncio.subprocess.DEVNULL

    async def test_stdin_payload_reaches_the_child(self, stdin_hook: str) -> None:
        result = await run(stdin_hook, timeout=10, stdin=f"refs/heads/x {'a' * 40} refs/heads/x {'0' * 40}\n")

        assert "CHECKS_RAN" in result.stdout

    async def test_empty_stdin_makes_the_hook_skip_its_checks(self, stdin_hook: str) -> None:
        """Why the payload must carry a real SHA: EOF alone is a silent pass."""
        result = await run(stdin_hook, timeout=10)

        assert "SKIPPED" in result.stdout
        assert "CHECKS_RAN" not in result.stdout


class TestBuildPrePushStdin:
    async def test_emits_a_well_formed_ref_line(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)

        payload = await build_pre_push_stdin(tmp_path, "feat/thing")
        local_ref, local_sha, remote_ref, remote_sha = payload.split()

        assert payload.endswith("\n")
        assert local_ref == remote_ref == "refs/heads/feat/thing"
        assert len(local_sha) == 40
        assert local_sha != "0" * 40
        assert remote_sha == "0" * 40  # no upstream yet

    async def test_falls_back_to_current_branch_when_unset(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)

        payload = await build_pre_push_stdin(tmp_path, "")

        assert payload.startswith("refs/heads/main ")


class TestValidateStepTimeout:
    async def test_hook_timeout_does_not_trigger_an_llm_fix(self) -> None:
        """A killed hook has no diagnosable output, so do not spend an LLM call on it."""
        from tests.test_core import _make_ctx

        ctx = _make_ctx()
        timed_out = ShellResult(returncode=-1, stdout="", stderr="Command timed out after 120s", timed_out=True)

        with (
            patch("sova.core.steps.validate.find_pre_push_hook", AsyncMock(return_value=".githooks/pre-push")),
            patch("sova.core.steps.validate.build_pre_push_stdin", AsyncMock(return_value="ref sha ref sha\n")),
            patch("sova.core.steps.validate.run", AsyncMock(return_value=timed_out)),
            patch("sova.core.steps.validate.invoke", AsyncMock()) as mock_invoke,
        ):
            result = await ValidateStep().execute(ctx)

        assert not result.success
        assert result.error == "hook_timeout"
        mock_invoke.assert_not_called()

    async def test_retry_timeout_stops_instead_of_spending_more_attempts(self) -> None:
        from sova.llm.models import LLMResult
        from tests.test_core import _make_ctx

        ctx = _make_ctx()
        failed = ShellResult(returncode=1, stdout="FAIL: lint\n", stderr="")
        timed_out = ShellResult(returncode=-1, stdout="", stderr="Command timed out after 120s", timed_out=True)

        with (
            patch("sova.core.steps.validate.find_pre_push_hook", AsyncMock(return_value=".githooks/pre-push")),
            patch("sova.core.steps.validate.build_pre_push_stdin", AsyncMock(return_value="ref sha ref sha\n")),
            patch("sova.core.steps.validate.run", AsyncMock(side_effect=[failed, timed_out])),
            patch("sova.core.steps.validate.invoke", AsyncMock()) as mock_invoke,
        ):
            mock_invoke.return_value = LLMResult(text="fixed", model="sonnet", cost_usd=Decimal("0.02"))
            result = await ValidateStep().execute(ctx)

        assert not result.success
        assert result.error == "hook_timeout"
        assert mock_invoke.await_count == 1  # stopped rather than burning attempt 2


class TestValidateStepEndToEnd:
    async def test_step_delivers_stdin_to_a_real_hook(self, tmp_path: Path) -> None:
        """End to end: the hook only passes if it actually received ref lines."""
        from tests.test_core import _make_ctx

        await _init_repo(tmp_path)
        await run("git", "config", "core.hooksPath", ".githooks", cwd=tmp_path)
        hooks = tmp_path / ".githooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nread -r _lref lsha _rref _rsha\n"
            'test -n "$lsha" && test "$lsha" != "' + "0" * 40 + '"\n'
        )
        hook.chmod(0o755)

        ctx = _make_ctx(worktree_dir=tmp_path)
        with patch("sova.core.steps.validate.invoke", AsyncMock()) as mock_invoke:
            result = await ValidateStep().execute(ctx)

        assert result.success, result.summary
        mock_invoke.assert_not_called()

    async def test_retry_sends_the_fix_commit_sha_not_the_stale_one(self, tmp_path: Path) -> None:
        """Regression: a retry that reuses the pre-fix stdin validates the wrong commit."""
        from sova.llm.models import LLMResult
        from tests.test_core import _make_ctx

        await _init_repo(tmp_path)
        await run("git", "config", "core.hooksPath", ".githooks", cwd=tmp_path)
        hooks = tmp_path / ".githooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        received_shas = tmp_path / "received_shas.txt"
        fixed_marker = tmp_path / ".fixed"
        hook.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nread -r _lref lsha _rref _rsha\n"
            f'echo "$lsha" >> "{received_shas}"\n'
            f'test -f "{fixed_marker}" && exit 0\nexit 1\n'
        )
        hook.chmod(0o755)

        ctx = _make_ctx(worktree_dir=tmp_path)

        async def _fake_fix(*_args: object, **_kwargs: object) -> LLMResult:
            # Simulate the LLM committing a fix: HEAD moves before the retry.
            await run(
                "git", "commit", "-q", "--allow-empty", "-m", "fix: address pre-push hook violations", cwd=tmp_path
            )
            fixed_marker.write_text("1")
            return LLMResult(text="fixed", model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.core.steps.validate.invoke", AsyncMock(side_effect=_fake_fix)):
            result = await ValidateStep().execute(ctx)

        assert result.success, result.summary
        first_sha, retry_sha = received_shas.read_text().splitlines()
        assert first_sha != retry_sha
        head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        assert retry_sha == head.stdout.strip()
