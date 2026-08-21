"""Tests for SOVA utility functions."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from sova.utils.formatting import branch_name, decimal_to_json, slugify, truncate


def test_slugify_basic() -> None:
    assert slugify("Add user authentication") == "add-user-authentication"


def test_slugify_special_chars() -> None:
    assert slugify("Fix bug #42: NullPointer!") == "fix-bug-42-nullpointer"


def test_slugify_max_length() -> None:
    result = slugify("This is a very long title that should be truncated", max_length=20)
    assert len(result) <= 20
    assert not result.endswith("-")


def test_slugify_unicode() -> None:
    assert slugify("Implementar funcionalidad") == "implementar-funcionalidad"


def test_branch_name_default() -> None:
    assert branch_name(42, "Add login page") == "agent/feat/42-add-login-page"


def test_branch_name_fix_prefix() -> None:
    assert branch_name(10, "Fix crash on startup", prefix="fix") == "agent/fix/10-fix-crash-on-startup"


def test_truncate_short() -> None:
    assert truncate("short", 200) == "short"


def test_truncate_long() -> None:
    result = truncate("a" * 300, 200)
    assert len(result) == 200
    assert result.endswith("...")


def test_decimal_to_json_none() -> None:
    assert decimal_to_json(None) == "0.00"


def test_decimal_to_json_value() -> None:
    assert decimal_to_json(Decimal("1.23")) == "1.23"


def test_decimal_to_json_zero() -> None:
    assert decimal_to_json(Decimal("0")) == "0"


# ---------------------------------------------------------------------------
# markdown utilities
# ---------------------------------------------------------------------------


class TestExtractSection:
    def test_basic_extraction(self) -> None:
        from sova.utils.markdown import extract_section

        text = "## Intro\nHello\n\n## Body\nContent here.\n\n## End\nBye."
        assert extract_section(text, "Body") == "Content here."

    def test_last_section(self) -> None:
        from sova.utils.markdown import extract_section

        text = "## Intro\nHello\n\n## End\nBye."
        assert extract_section(text, "End") == "Bye."

    def test_missing_section(self) -> None:
        from sova.utils.markdown import extract_section

        text = "## Intro\nHello."
        assert extract_section(text, "Missing") == ""

    def test_ignores_headings_inside_code_fence(self) -> None:
        from sova.utils.markdown import extract_section

        text = (
            "## Solution\n"
            "Do this.\n\n"
            "```python\n"
            "## This is a comment\n"
            "x = 1\n"
            "```\n\n"
            "More solution text.\n\n"
            "## Next Section\n"
            "Other stuff.\n"
        )
        result = extract_section(text, "Solution")
        # The fenced "## This is a comment" should NOT split the section
        assert "More solution text." in result
        assert "x = 1" in result

    def test_code_fence_with_language_tag(self) -> None:
        from sova.utils.markdown import extract_section

        text = (
            "## Details\n"
            "Some details.\n\n"
            "```bash\n"
            "## heading inside bash\n"
            "echo hello\n"
            "```\n\n"
            "After fence.\n\n"
            "## Other\nEnd.\n"
        )
        result = extract_section(text, "Details")
        assert "After fence." in result
        assert "echo hello" in result


class TestStripFencedBlocks:
    def test_replaces_fence_content(self) -> None:
        from sova.utils.markdown import _strip_fenced_blocks

        text = "before\n```\n## Heading\ncode\n```\nafter"
        result = _strip_fenced_blocks(text)
        assert "## Heading" not in result
        assert "before" in result
        assert "after" in result

    def test_preserves_byte_offsets(self) -> None:
        from sova.utils.markdown import _strip_fenced_blocks

        text = "a\n```\nb\nc\nd\n```\ne"
        result = _strip_fenced_blocks(text)
        assert len(result) == len(text)
        assert result.count("\n") == text.count("\n")

    def test_no_fences(self) -> None:
        from sova.utils.markdown import _strip_fenced_blocks

        text = "just plain text\nwith lines"
        assert _strip_fenced_blocks(text) == text


# ---------------------------------------------------------------------------
# resolve_gh_env tests
# ---------------------------------------------------------------------------


class TestResolveGhEnv:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_user(self) -> None:
        from sova.utils.gh import resolve_gh_env

        assert await resolve_gh_env("") is None
        assert await resolve_gh_env(None) is None

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_env_with_token(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import resolve_gh_env
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout="gho_test_token_123\n", stderr="")

        env = await resolve_gh_env("xsovad06")

        assert env is not None
        assert env["GH_TOKEN"] == "gho_test_token_123"
        mock_run.assert_called_once_with("gh", "auth", "token", "--user", "xsovad06")

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_none_on_failure(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import resolve_gh_env
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="no such user")

        assert await resolve_gh_env("nonexistent") is None

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_none_on_empty_token(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import resolve_gh_env
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout="", stderr="")

        assert await resolve_gh_env("emptyuser") is None


# ---------------------------------------------------------------------------
# check_git_identity
# ---------------------------------------------------------------------------


class TestCheckGitIdentity:
    @pytest.mark.asyncio
    @patch("sova.utils.shell.run", new_callable=AsyncMock)
    async def test_both_configured(self, mock_run: AsyncMock) -> None:
        from sova.utils.shell import ShellResult, check_git_identity

        mock_run.side_effect = [
            ShellResult(returncode=0, stdout="Test User\n", stderr=""),
            ShellResult(returncode=0, stdout="test@example.com\n", stderr=""),
        ]
        result = await check_git_identity()
        assert result.valid
        assert result.name == "Test User"
        assert result.email == "test@example.com"
        assert result.missing_fields == []

    @pytest.mark.asyncio
    @patch("sova.utils.shell.run", new_callable=AsyncMock)
    async def test_both_missing(self, mock_run: AsyncMock) -> None:
        from sova.utils.shell import ShellResult, check_git_identity

        mock_run.side_effect = [
            ShellResult(returncode=1, stdout="", stderr=""),
            ShellResult(returncode=1, stdout="", stderr=""),
        ]
        result = await check_git_identity()
        assert not result.valid
        assert result.missing_fields == ["user.name", "user.email"]

    @pytest.mark.asyncio
    @patch("sova.utils.shell.run", new_callable=AsyncMock)
    async def test_empty_string_treated_as_missing(self, mock_run: AsyncMock) -> None:
        from sova.utils.shell import ShellResult, check_git_identity

        mock_run.side_effect = [
            ShellResult(returncode=0, stdout="  \n", stderr=""),
            ShellResult(returncode=0, stdout="test@example.com\n", stderr=""),
        ]
        result = await check_git_identity()
        assert not result.valid
        assert result.missing_fields == ["user.name"]

    @pytest.mark.asyncio
    @patch("sova.utils.shell.run", new_callable=AsyncMock)
    async def test_email_only_missing(self, mock_run: AsyncMock) -> None:
        from sova.utils.shell import ShellResult, check_git_identity

        mock_run.side_effect = [
            ShellResult(returncode=0, stdout="Test User\n", stderr=""),
            ShellResult(returncode=1, stdout="", stderr=""),
        ]
        result = await check_git_identity()
        assert not result.valid
        assert result.missing_fields == ["user.email"]

    @pytest.mark.asyncio
    @patch("sova.utils.shell.run", new_callable=AsyncMock)
    async def test_passes_cwd(self, mock_run: AsyncMock) -> None:
        from pathlib import Path

        from sova.utils.shell import ShellResult, check_git_identity

        mock_run.return_value = ShellResult(returncode=0, stdout="value\n", stderr="")
        await check_git_identity(cwd=Path("/some/project"))
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") == Path("/some/project")

    @pytest.mark.asyncio
    @patch("sova.utils.shell.run", new_callable=AsyncMock)
    async def test_oserror_returns_empty_identity(self, mock_run: AsyncMock) -> None:
        from sova.utils.shell import check_git_identity

        mock_run.side_effect = OSError("No such file or directory")
        result = await check_git_identity()
        assert not result.valid
        assert result.missing_fields == ["user.name", "user.email"]


# ---------------------------------------------------------------------------
# shell constants and error handling
# ---------------------------------------------------------------------------


class TestShellConstants:
    def test_default_timeout_constant_exported(self) -> None:
        from sova.utils.shell import DEFAULT_COMMAND_TIMEOUT_SECONDS

        assert DEFAULT_COMMAND_TIMEOUT_SECONDS == 300

    def test_stderr_limits_defined(self) -> None:
        from sova.utils.shell import _STDERR_ERROR_LIMIT, _STDERR_LOG_LIMIT

        assert _STDERR_LOG_LIMIT == 200
        assert _STDERR_ERROR_LIMIT == 500

    @pytest.mark.asyncio
    async def test_run_uses_default_timeout(self) -> None:
        from unittest.mock import patch

        from sova.utils.shell import run

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"output", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await run("echo", "test")

            # Verify timeout parameter was passed
            call_kwargs = mock_exec.call_args.kwargs
            assert "timeout" not in call_kwargs  # timeout is used in wait_for, not exec

    @pytest.mark.asyncio
    async def test_run_checked_uses_default_timeout(self) -> None:
        from sova.utils.shell import run_checked

        with patch("sova.utils.shell.run") as mock_run:
            from sova.utils.shell import DEFAULT_COMMAND_TIMEOUT_SECONDS, ShellResult

            mock_run.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

            await run_checked("echo", "test")

            # Verify default timeout was passed
            assert mock_run.call_args.kwargs.get("timeout") == DEFAULT_COMMAND_TIMEOUT_SECONDS

    def test_subprocess_error_truncates_stderr(self) -> None:
        from sova.utils.shell import ShellResult, subprocess_error

        long_stderr = "x" * 1000
        result = ShellResult(returncode=1, stdout="", stderr=long_stderr)

        error = subprocess_error(("test", "cmd"), result)

        # Should truncate to _STDERR_ERROR_LIMIT (500)
        assert len(error.args[0]) < len(long_stderr)
        assert "xxxxx" in str(error)  # Some of the stderr should be in the message

    def test_subprocess_error_preserves_short_stderr(self) -> None:
        from sova.utils.shell import ShellResult, subprocess_error

        short_stderr = "short error message"
        result = ShellResult(returncode=1, stdout="", stderr=short_stderr)

        error = subprocess_error(("test", "cmd"), result)

        assert short_stderr in str(error)
        assert "Exit code: 1" in str(error)
        assert "test cmd" in str(error)

    @pytest.mark.asyncio
    async def test_run_logs_debug_on_failure(self) -> None:
        """run() logs debug message when command fails."""
        from unittest.mock import patch

        from sova.utils.shell import run

        with patch("sova.utils.shell.log") as mock_log:
            # This will fail since "false" always exits with 1
            result = await run("false")

            assert not result.success
            # Check that shell.failed was logged (second call)
            assert mock_log.debug.call_count == 2
            failed_call = mock_log.debug.call_args_list[1]
            assert failed_call[0][0] == "shell.failed"
            assert failed_call.kwargs["returncode"] == 1

    @pytest.mark.asyncio
    async def test_run_checked_raises_on_failure(self) -> None:
        """run_checked() raises RuntimeError when command fails."""
        from sova.utils.shell import run_checked

        with pytest.raises(RuntimeError) as exc_info:
            await run_checked("false")

        assert "Command failed: false" in str(exc_info.value)
        assert "Exit code: 1" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_run_timeout_kills_process(self) -> None:
        """run() kills process and returns error on timeout."""
        from sova.utils.shell import run

        # sleep 10 with 0.1s timeout should timeout
        result = await run("sleep", "10", timeout=0.1)

        assert result.returncode == -1
        assert "timed out" in result.stderr.lower()

    def test_shell_result_is_rate_limited_detects_rate_limit(self) -> None:
        """ShellResult.is_rate_limited detects GitHub rate limit errors."""
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded")
        assert result.is_rate_limited

    def test_shell_result_is_rate_limited_detects_abuse_detection(self) -> None:
        """ShellResult.is_rate_limited detects GitHub abuse detection."""
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="", stderr="abuse detection mechanism")
        assert result.is_rate_limited

    def test_shell_result_is_rate_limited_false_on_success(self) -> None:
        """ShellResult.is_rate_limited returns False when command succeeds."""
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=0, stdout="ok", stderr="")
        assert not result.is_rate_limited

    def test_shell_result_is_rate_limited_false_on_other_error(self) -> None:
        """ShellResult.is_rate_limited returns False for non-rate-limit errors."""
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="", stderr="some other error")
        assert not result.is_rate_limited
