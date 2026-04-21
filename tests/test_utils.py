"""Tests for SOVA utility functions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sova.utils.formatting import branch_name, slugify, truncate


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
