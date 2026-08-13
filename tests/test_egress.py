"""Tests for the egress filter (sova/llm/egress.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sova.llm.egress import filter_egress, scan_and_redact

# ---------------------------------------------------------------------------
# scan_and_redact -- pattern detection
# ---------------------------------------------------------------------------


class TestScanAndRedactPatterns:
    def test_clean_text(self) -> None:
        result = scan_and_redact("This is a normal PR description with no secrets.")
        assert result.clean
        assert result.flags == []
        assert result.redacted_text == "This is a normal PR description with no secrets."

    def test_empty_text(self) -> None:
        result = scan_and_redact("")
        assert result.clean
        assert result.redacted_text == ""

    def test_aws_access_key(self) -> None:
        text = "Key: AKIAIOSFODNN7EXAMPLE found in config"
        result = scan_and_redact(text)
        assert not result.clean
        assert "aws_access_key" in result.flags
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text
        assert result.redacted_text == "Key: [REDACTED:aws_key] found in config"

    def test_aws_secret_key(self) -> None:
        text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1"
        result = scan_and_redact(text)
        assert not result.clean
        assert "aws_secret_key" in result.flags
        assert "wJalrXUtnFEMI" not in result.redacted_text

    def test_github_token(self) -> None:
        text = "export GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm"
        result = scan_and_redact(text)
        assert not result.clean
        assert "github_token" in result.flags
        assert "ghp_" not in result.redacted_text
        assert result.redacted_text == "export GITHUB_TOKEN=[REDACTED:github_token]"

    def test_slack_token(self) -> None:
        text = "SLACK_TOKEN=xoxb-123456789012-abcdefghij"
        result = scan_and_redact(text)
        assert not result.clean
        assert "slack_token" in result.flags
        assert result.redacted_text == "SLACK_TOKEN=[REDACTED:slack_token]"

    def test_generic_api_key(self) -> None:
        text = 'api_key = "sk-proj-abc123def456ghi789"'
        result = scan_and_redact(text)
        assert not result.clean
        assert "api_key" in result.flags
        assert "sk-proj-abc123def456ghi789" not in result.redacted_text

    def test_generic_token_with_separator(self) -> None:
        text = "token=abc123def456ghi789jkl"
        result = scan_and_redact(text)
        assert not result.clean
        assert "generic_token" in result.flags

    def test_token_in_word_no_false_positive(self) -> None:
        """Words like 'tokenizer' should not trigger the generic token pattern."""
        result = scan_and_redact("The tokenizer splits text into tokens for processing.")
        assert result.clean

    def test_token_count_no_false_positive(self) -> None:
        result = scan_and_redact("Token count: 1500. JWT token validation passed.")
        assert result.clean

    def test_password_in_config(self) -> None:
        text = "password = my_secret_password_123"
        result = scan_and_redact(text)
        assert not result.clean
        assert "password" in result.flags
        assert "my_secret_password_123" not in result.redacted_text
        assert result.redacted_text == "[REDACTED:password]"

    def test_connection_string_postgresql(self) -> None:
        text = "DATABASE_URL=postgresql://admin:s3cret@db.example.com:5432/mydb"
        result = scan_and_redact(text)
        assert not result.clean
        assert "connection_string" in result.flags
        # Scheme and host should be preserved
        assert "postgresql://" in result.redacted_text
        assert "@" in result.redacted_text
        # Credentials should be redacted
        assert "admin:s3cret" not in result.redacted_text

    def test_connection_string_mysql(self) -> None:
        text = "mysql://root:password@localhost/db"
        result = scan_and_redact(text)
        assert not result.clean
        assert "connection_string" in result.flags
        assert "root:password" not in result.redacted_text

    def test_private_key_block(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        result = scan_and_redact(text)
        assert not result.clean
        assert "private_key" in result.flags

    def test_jwt_token(self) -> None:
        text = (
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        result = scan_and_redact(text)
        assert not result.clean
        assert "jwt" in result.flags

    def test_jwt_in_bearer_header(self) -> None:
        text = (
            "Authorization: Bearer "
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        result = scan_and_redact(text)
        assert not result.clean
        assert result.flags[0] in ("jwt", "bearer_token")

    def test_github_fine_grained_pat(self) -> None:
        token = "github_pat_" + "A" * 82
        text = f"Use {token} for authentication"
        result = scan_and_redact(text)
        assert not result.clean
        assert "github_token" in result.flags
        assert "github_pat_" not in result.redacted_text
        assert "[REDACTED:github_token]" in result.redacted_text

    def test_bearer_token_header(self) -> None:
        text = "Authorization: Bearer abc123def456ghi789jkl"
        result = scan_and_redact(text)
        assert not result.clean
        assert "bearer_token" in result.flags
        assert "abc123def456ghi789jkl" not in result.redacted_text

    def test_bearer_token_lowercase(self) -> None:
        text = "authorization: bearer mytoken12345678abcdef"
        result = scan_and_redact(text)
        assert not result.clean
        assert "bearer_token" in result.flags

    def test_bearer_no_false_positive_in_prose(self) -> None:
        """The word 'bearer' in normal text should not trigger detection."""
        result = scan_and_redact("The bearer of this certificate is authorized.")
        assert result.clean

    def test_multiple_patterns(self) -> None:
        text = "api_key=sk-abcdef123456\npassword=hunter2!"
        result = scan_and_redact(text)
        assert not result.clean
        assert len(result.flags) >= 2

    def test_redaction_inside_code_block(self) -> None:
        """[REDACTED] inside fenced code blocks is acceptable."""
        text = "```python\napi_key = 'sk-abcdef123456789012'\n```"
        result = scan_and_redact(text)
        assert not result.clean
        assert "[REDACTED:" in result.redacted_text
        assert "```python" in result.redacted_text


# ---------------------------------------------------------------------------
# filter_egress -- mode behavior
# ---------------------------------------------------------------------------


class TestFilterEgress:
    def test_off_mode_passthrough(self) -> None:
        text = "api_key=sk-secret12345678"
        result = filter_egress(text, mode="off")
        assert result == text

    def test_warn_mode_redacts(self) -> None:
        text = "Found api_key=sk-secret12345678 in output"
        result = filter_egress(text, mode="warn")
        assert result is not None
        assert "sk-secret12345678" not in result
        assert "[REDACTED:" in result

    def test_block_mode_returns_none(self) -> None:
        text = "Found api_key=sk-secret12345678 in output"
        result = filter_egress(text, mode="block")
        assert result is None

    def test_clean_text_passes_all_modes(self) -> None:
        text = "This is clean text with no secrets."
        assert filter_egress(text, mode="off") == text
        assert filter_egress(text, mode="warn") == text
        assert filter_egress(text, mode="block") == text

    def test_empty_text_returns_empty(self) -> None:
        assert filter_egress("", mode="warn") == ""
        assert filter_egress("", mode="block") == ""
        assert filter_egress("", mode="off") == ""


# ---------------------------------------------------------------------------
# Template Method integration (adapter ABC)
# ---------------------------------------------------------------------------


class TestAdapterEgressIntegration:
    """Test that the adapter ABC's Template Method pattern filters egress."""

    @pytest.fixture()
    def mock_adapter(self) -> AsyncMock:
        """Create a mock adapter with _do_* methods."""
        from sova.adapters.base import TaskAdapter

        adapter = AsyncMock(spec=TaskAdapter)
        adapter.repo = "owner/repo"
        adapter.github_user = "testuser"
        # Use real post_comment/post_pr_comment from ABC
        adapter.post_comment = TaskAdapter.post_comment.__get__(adapter)
        adapter.post_pr_comment = TaskAdapter.post_pr_comment.__get__(adapter)
        adapter.post_pr_review = TaskAdapter.post_pr_review.__get__(adapter)
        adapter.edit_body = TaskAdapter.edit_body.__get__(adapter)
        adapter.create_issue = TaskAdapter.create_issue.__get__(adapter)
        return adapter

    @pytest.mark.asyncio()
    async def test_post_comment_redacts_in_warn_mode(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="warn"):
            await mock_adapter.post_comment("42", "secret: api_key=sk-abcdef1234567890")
            mock_adapter._do_post_comment.assert_called_once()
            call_body = mock_adapter._do_post_comment.call_args[0][1]
            assert "sk-abcdef1234567890" not in call_body
            assert "[REDACTED:" in call_body

    @pytest.mark.asyncio()
    async def test_post_comment_blocks_in_block_mode(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="block"):
            await mock_adapter.post_comment("42", "api_key=sk-abcdef1234567890")
            mock_adapter._do_post_comment.assert_not_called()

    @pytest.mark.asyncio()
    async def test_post_comment_passes_in_off_mode(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="off"):
            await mock_adapter.post_comment("42", "api_key=sk-abcdef1234567890")
            mock_adapter._do_post_comment.assert_called_once_with("42", "api_key=sk-abcdef1234567890")

    @pytest.mark.asyncio()
    async def test_post_pr_review_filters_inline_comments(self, mock_adapter: AsyncMock) -> None:
        comments = [
            {"path": "main.py", "line": 10, "body": "Fix: password=hunter2abc"},
            {"path": "config.py", "line": 5, "body": "This looks fine"},
        ]
        with patch("sova.adapters.base._get_egress_mode", return_value="warn"):
            await mock_adapter.post_pr_review(1, "Review body", "COMMENT", comments)
            mock_adapter._do_post_pr_review.assert_called_once()
            call_comments = mock_adapter._do_post_pr_review.call_args[0][3]
            assert "hunter2abc" not in call_comments[0]["body"]
            assert call_comments[1]["body"] == "This looks fine"

    @pytest.mark.asyncio()
    async def test_post_pr_review_blocks_if_comment_has_secret(self, mock_adapter: AsyncMock) -> None:
        comments = [{"path": "main.py", "line": 10, "body": "password=hunter2abc"}]
        with patch("sova.adapters.base._get_egress_mode", return_value="block"):
            await mock_adapter.post_pr_review(1, "Clean body", "COMMENT", comments)
            mock_adapter._do_post_pr_review.assert_not_called()

    @pytest.mark.asyncio()
    async def test_edit_body_redacts(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="warn"):
            await mock_adapter.edit_body("42", "password=mysecretvalue")
            call_body = mock_adapter._do_edit_body.call_args[0][1]
            assert "mysecretvalue" not in call_body

    @pytest.mark.asyncio()
    async def test_post_pr_comment_redacts_in_warn_mode(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="warn"):
            await mock_adapter.post_pr_comment(1, "api_key=sk-abcdef1234567890")
            mock_adapter._do_post_pr_comment.assert_called_once()
            call_body = mock_adapter._do_post_pr_comment.call_args[0][1]
            assert "sk-abcdef1234567890" not in call_body
            assert "[REDACTED:" in call_body

    @pytest.mark.asyncio()
    async def test_post_pr_comment_blocks_in_block_mode(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="block"):
            await mock_adapter.post_pr_comment(1, "api_key=sk-abcdef1234567890")
            mock_adapter._do_post_pr_comment.assert_not_called()

    @pytest.mark.asyncio()
    async def test_post_pr_review_blocks_if_body_has_secret(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="block"):
            await mock_adapter.post_pr_review(1, "password=hunter2abc", "COMMENT", [])
            mock_adapter._do_post_pr_review.assert_not_called()

    @pytest.mark.asyncio()
    async def test_edit_body_blocks_in_block_mode(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="block"):
            await mock_adapter.edit_body("42", "password=mysecretvalue")
            mock_adapter._do_edit_body.assert_not_called()

    @pytest.mark.asyncio()
    async def test_create_issue_filters_body(self, mock_adapter: AsyncMock) -> None:
        from sova.adapters.base import Task

        mock_adapter._do_create_issue.return_value = Task(id="1", title="Test")
        with patch("sova.adapters.base._get_egress_mode", return_value="warn"):
            await mock_adapter.create_issue("New task", body="password=secret123")
            call_body = mock_adapter._do_create_issue.call_args[0][1]
            assert "secret123" not in call_body

    @pytest.mark.asyncio()
    async def test_create_issue_blocks_title_raises(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="block"):
            with pytest.raises(RuntimeError, match="Egress filter blocked issue title"):
                await mock_adapter.create_issue("api_key=sk-abcdef1234567890", body="clean body")

    @pytest.mark.asyncio()
    async def test_create_issue_blocked_body_raises(self, mock_adapter: AsyncMock) -> None:
        with patch("sova.adapters.base._get_egress_mode", return_value="block"):
            with pytest.raises(RuntimeError, match="Egress filter blocked issue body"):
                await mock_adapter.create_issue("Clean title", body="password=hunter2abc")


# ---------------------------------------------------------------------------
# _get_egress_mode caching
# ---------------------------------------------------------------------------


class TestGetEgressMode:
    def test_loads_from_config(self) -> None:
        from sova.adapters.base import _get_egress_mode

        with patch("sova.config.loader.load_config") as mock_load:
            mock_load.return_value.egress.mode = "block"
            result = _get_egress_mode()
            assert result == "block"

    def test_no_cache_reads_config_each_call(self) -> None:
        from sova.adapters.base import _get_egress_mode

        with patch("sova.config.loader.load_config") as mock_load:
            mock_load.return_value.egress.mode = "off"
            assert _get_egress_mode() == "off"
            assert _get_egress_mode() == "off"
            assert mock_load.call_count == 2

    def test_falls_back_to_warn_on_config_error(self) -> None:
        from sova.adapters.base import _get_egress_mode

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")):
            result = _get_egress_mode()
            assert result == "warn"


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestEgressConfig:
    def test_default_mode_is_warn(self) -> None:
        from sova.config.models import EgressConfig

        cfg = EgressConfig()
        assert cfg.mode == "warn"

    def test_mode_validation(self) -> None:
        from sova.config.models import EgressConfig

        for valid in ("off", "warn", "block"):
            cfg = EgressConfig(mode=valid)
            assert cfg.mode == valid

    def test_unknown_key_rejected(self) -> None:
        """EgressConfig uses extra='forbid' to catch typos that would silently weaken the filter."""
        from pydantic import ValidationError

        from sova.config.models import EgressConfig

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            EgressConfig(mdoe="block")

    def test_project_config_has_egress(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        assert hasattr(cfg, "egress")
        assert cfg.egress.mode == "warn"
