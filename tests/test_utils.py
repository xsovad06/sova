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


class TestUpsertSection:
    def test_appends_when_heading_missing(self) -> None:
        from sova.utils.markdown import upsert_section

        text = "## Intro\nHello."
        result = upsert_section(text, "Confidence Score", "**80/100**")
        assert "## Intro\nHello." in result
        assert result.endswith("## Confidence Score\n\n**80/100**\n")

    def test_replaces_existing_section_in_place(self) -> None:
        from sova.utils.markdown import upsert_section

        text = "## Intro\nHello.\n\n## Confidence Score\n\nOld: 40/100\n\n## Footer\nBye."
        result = upsert_section(text, "Confidence Score", "New: 90/100")
        assert "Old: 40/100" not in result
        assert "New: 90/100" in result
        assert "## Intro" in result
        assert "## Footer\nBye." in result

    def test_idempotent_on_repeated_calls(self) -> None:
        from sova.utils.markdown import upsert_section

        text = "## Summary\nStuff."
        once = upsert_section(text, "Confidence Score", "**50/100**")
        twice = upsert_section(once, "Confidence Score", "**50/100**")
        assert once == twice
        assert twice.count("## Confidence Score") == 1

    def test_ignores_heading_inside_code_fence(self) -> None:
        from sova.utils.markdown import upsert_section

        text = "## Confidence Score\n\n```markdown\n## Confidence Score\nfake\n```\n\nreal content\n"
        result = upsert_section(text, "Confidence Score", "updated")
        assert "fake" not in result
        assert "updated" in result

    def test_ignores_heading_inside_longer_backtick_fence(self) -> None:
        from sova.utils.markdown import upsert_section

        # A real heading precedes a fence opened with four backticks that nests
        # a triple-backtick block; the inner "## Confidence Score" line must not
        # be treated as closing the fence early (which would unmask it as a
        # false section boundary and leave the fenced "fake" content behind).
        text = (
            "## Confidence Score\n\nOld: 40/100\n\n"
            "````markdown\n"
            "```\n"
            "## Confidence Score\nfake\n"
            "```\n"
            "````\n\n"
            "real content\n"
        )
        result = upsert_section(text, "Confidence Score", "updated")
        assert "fake" not in result
        assert "Old: 40/100" not in result
        assert "updated" in result
        assert result.count("## Confidence Score") == 1

    def test_ignores_heading_inside_tilde_fence(self) -> None:
        from sova.utils.markdown import upsert_section

        text = "## Confidence Score\n\n~~~markdown\n## Confidence Score\nfake\n~~~\n\nreal content\n"
        result = upsert_section(text, "Confidence Score", "updated")
        assert "fake" not in result
        assert "updated" in result


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

    def test_masks_tilde_fences(self) -> None:
        from sova.utils.markdown import _strip_fenced_blocks

        text = "before\n~~~\n## Heading\ncode\n~~~\nafter"
        result = _strip_fenced_blocks(text)
        assert "## Heading" not in result
        assert "before" in result
        assert "after" in result

    def test_longer_backtick_fence_containing_triple_backticks(self) -> None:
        from sova.utils.markdown import _strip_fenced_blocks

        text = "before\n````\n## Heading\n```\nstill inside\n````\nafter"
        result = _strip_fenced_blocks(text)
        assert "## Heading" not in result
        assert "still inside" not in result
        assert "before" in result
        assert "after" in result

    def test_closing_fence_must_match_opening_type(self) -> None:
        from sova.utils.markdown import _strip_fenced_blocks

        # A tilde line cannot close a backtick fence: content stays masked
        # through EOF since no valid closer appears.
        text = "before\n```\n## Heading\n~~~\nafter"
        result = _strip_fenced_blocks(text)
        assert "## Heading" not in result
        assert "after" not in result
        assert "before" in result


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
# check_push_permission
# ---------------------------------------------------------------------------


class TestCheckPushPermission:
    @pytest.fixture(autouse=True)
    def _clear_push_permission_cache(self):
        from sova.utils.gh import _push_permission_cache

        _push_permission_cache.clear()
        yield
        _push_permission_cache.clear()

    @pytest.mark.asyncio
    async def test_returns_unknown_when_no_repo(self) -> None:
        from sova.utils.gh import PushPermission, check_push_permission

        result = await check_push_permission("")
        assert result.permission is PushPermission.UNKNOWN
        assert result.checked_as is None

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_allowed_when_push_true(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout="true\n", stderr="")

        result = await check_push_permission("owner/repo")

        assert result.permission is PushPermission.ALLOWED
        mock_run.assert_called_once_with(
            "gh", "api", "repos/owner/repo", "--jq", ".permissions.push", env=None, timeout=15
        )

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_denied_when_push_false(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout="false\n", stderr="")

        result = await check_push_permission("owner/repo")
        assert result.permission is PushPermission.DENIED

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_denied_on_404(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="HTTP 404: Not Found")

        result = await check_push_permission("owner/repo")
        assert result.permission is PushPermission.DENIED

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_unknown_on_other_api_failure(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="network error")

        result = await check_push_permission("owner/repo")
        assert result.permission is PushPermission.UNKNOWN

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_unknown_when_gh_not_installed(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission

        mock_run.side_effect = FileNotFoundError("gh not found")

        result = await check_push_permission("owner/repo")
        assert result.permission is PushPermission.UNKNOWN

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_returns_unknown_on_missing_permissions_key(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        # --jq on a permissions object missing "push" emits nothing to stdout but exits 0
        mock_run.return_value = ShellResult(returncode=0, stdout="\n", stderr="")

        result = await check_push_permission("owner/repo")
        assert result.permission is PushPermission.UNKNOWN

    @pytest.mark.asyncio
    @patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock)
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_uses_resolved_env_for_github_user(self, mock_run: AsyncMock, mock_env: AsyncMock) -> None:
        from sova.utils.gh import check_push_permission
        from sova.utils.shell import ShellResult

        mock_env.return_value = {"GH_TOKEN": "abc"}
        mock_run.return_value = ShellResult(returncode=0, stdout="true\n", stderr="")

        result = await check_push_permission("owner/repo", github_user="xsovad06")

        mock_env.assert_called_once_with("xsovad06")
        assert result.checked_as == "xsovad06"

    @pytest.mark.asyncio
    @patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock)
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_checked_as_none_when_token_resolution_fails(self, mock_run: AsyncMock, mock_env: AsyncMock) -> None:
        """A github_user is configured but resolve_gh_env can't get a token for it
        (never `gh auth login`'d on this machine, expired/revoked cached token). The
        API call then runs under whatever env it was given (None -> ambient-active gh
        account), so checked_as must NOT report the configured user as having been
        checked."""
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_env.return_value = None
        mock_run.return_value = ShellResult(returncode=0, stdout="false\n", stderr="")

        result = await check_push_permission("owner/repo", github_user="xsovad06")

        mock_run.assert_called_once_with(
            "gh", "api", "repos/owner/repo", "--jq", ".permissions.push", env=None, timeout=15
        )
        assert result.permission is PushPermission.DENIED
        assert result.checked_as is None

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_denied_only_on_http_404_not_broad_not_found_text(self, mock_run: AsyncMock) -> None:
        """A jq/query error mentioning 'not found' in unrelated prose must not be
        misread as a definitive access denial (the fail-open guarantee's weakest
        link): only gh's literal 'HTTP 404' failure shape is a real 404."""
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=1, stdout="", stderr='jq: error: "push" key not found in object')

        result = await check_push_permission("owner/repo")
        assert result.permission is PushPermission.UNKNOWN

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_skips_api_call_for_ssh_remote(self, mock_run: AsyncMock) -> None:
        """check_push_permission() verifies a GitHub REST API token, which has no
        bearing on SSH push auth. Skip the API call entirely for an SSH remote
        rather than reporting a verdict for an auth path the real push won't use."""
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        def fake_run(*args: str, **kwargs: object) -> ShellResult:
            if args[:3] == ("git", "remote", "get-url"):
                return ShellResult(returncode=0, stdout="git@github.com:owner/repo.git\n", stderr="")
            raise AssertionError(f"unexpected call: {args}")

        mock_run.side_effect = fake_run

        result = await check_push_permission("owner/repo", cwd="/tmp/repo")

        assert result.permission is PushPermission.UNKNOWN
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_checks_api_for_https_remote(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        def fake_run(*args: str, **kwargs: object) -> ShellResult:
            if args[:3] == ("git", "remote", "get-url"):
                return ShellResult(returncode=0, stdout="https://github.com/owner/repo.git\n", stderr="")
            return ShellResult(returncode=0, stdout="true\n", stderr="")

        mock_run.side_effect = fake_run

        result = await check_push_permission("owner/repo", cwd="/tmp/repo")

        assert result.permission is PushPermission.ALLOWED
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_caches_confirmed_result_across_calls(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout="false\n", stderr="")

        first = await check_push_permission("owner/repo")
        second = await check_push_permission("owner/repo")

        assert first.permission is PushPermission.DENIED
        assert second.permission is PushPermission.DENIED
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_does_not_cache_unknown_result(self, mock_run: AsyncMock) -> None:
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="network error")

        first = await check_push_permission("owner/repo")
        second = await check_push_permission("owner/repo")

        assert first.permission is PushPermission.UNKNOWN
        assert second.permission is PushPermission.UNKNOWN
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    @patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock)
    async def test_returns_unknown_when_gh_not_installed_for_configured_user(self, mock_env: AsyncMock) -> None:
        """resolve_gh_env(github_user) itself raises OSError (gh binary
        missing) before the API-call try/except is ever reached. This must
        still fail open like every other gh-unavailable path, not propagate
        out of check_push_permission and crash SyncStep."""
        from sova.utils.gh import PushPermission, check_push_permission

        mock_env.side_effect = FileNotFoundError("gh")

        result = await check_push_permission("owner/repo", github_user="xsovad06")

        assert result.permission is PushPermission.UNKNOWN
        assert result.checked_as is None

    @pytest.mark.asyncio
    @patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock)
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_cache_keyed_by_checked_identity_not_requested_user(
        self, mock_run: AsyncMock, mock_env: AsyncMock
    ) -> None:
        """A confirmed result for an unresolved configured user must be
        cached under the ambient identity's key, not the configured
        (unexercised) user's key: otherwise a later call for that same
        configured user, made after its credentials actually resolve, could
        incorrectly reuse a verdict that reflects a different account."""
        from sova.utils.gh import PushPermission, check_push_permission
        from sova.utils.shell import ShellResult

        mock_env.return_value = None
        mock_run.return_value = ShellResult(returncode=0, stdout="false\n", stderr="")

        first = await check_push_permission("owner/repo", github_user="xsovad06")
        assert first.permission is PushPermission.DENIED
        assert first.checked_as is None
        assert mock_run.call_count == 1

        # Same configured user, now resolving successfully: must not reuse
        # the ambient-identity cache entry from the call above.
        mock_env.return_value = {"GH_TOKEN": "abc"}
        mock_run.return_value = ShellResult(returncode=0, stdout="true\n", stderr="")

        second = await check_push_permission("owner/repo", github_user="xsovad06")
        assert second.permission is PushPermission.ALLOWED
        assert second.checked_as == "xsovad06"
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    @patch("sova.supervisor.github_quota.track_rate_limit")
    @patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock)
    @patch("sova.utils.gh.run", new_callable=AsyncMock)
    async def test_rate_limit_tracked_under_checked_identity_not_configured_user(
        self, mock_run: AsyncMock, mock_env: AsyncMock, mock_track: AsyncMock
    ) -> None:
        """When token resolution for a configured github_user fails, the API call
        actually runs under the ambient account, so the quota hit must not be
        credited to the unexercised configured identity (the same misattribution
        bug this PR fixed for the user-facing error message)."""
        from sova.utils.gh import check_push_permission
        from sova.utils.shell import ShellResult

        mock_env.return_value = None
        mock_run.return_value = ShellResult(returncode=0, stdout="true\n", stderr="")

        await check_push_permission("owner/repo", github_user="xsovad06")

        mock_track.assert_called_once()
        _, identity = mock_track.call_args.args
        assert identity == ""


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
