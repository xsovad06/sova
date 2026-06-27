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
