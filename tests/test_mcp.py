"""Tests for SOVA MCP server and tools."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.llm.models import LLMResult

# ---------------------------------------------------------------------------
# Server creation
# ---------------------------------------------------------------------------


class TestCreateServer:
    def test_creates_server_with_tools(self) -> None:
        from sova.mcp.server import create_server

        server = create_server()
        assert server.name == "sova"

    def test_creates_server_with_project_dir(self, tmp_path: Path) -> None:
        from sova.mcp.server import create_server

        server = create_server(project_dir=tmp_path)
        assert server.name == "sova"

    async def test_server_lists_tools(self) -> None:
        from sova.mcp.server import create_server

        server = create_server()
        tools = await server.list_tools()
        tool_names = {t.name for t in tools}

        expected = {
            "sova_develop",
            "sova_review",
            "sova_test",
            "sova_simplify",
            "sova_address_review",
            "sova_create_pr",
            "sova_read_project",
        }
        assert expected == tool_names

    async def test_each_tool_has_description(self) -> None:
        from sova.mcp.server import create_server

        server = create_server()
        tools = await server.list_tools()

        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"


# ---------------------------------------------------------------------------
# _validate_project_dir
# ---------------------------------------------------------------------------


class TestValidateProjectDir:
    def test_valid_directory(self, tmp_path: Path) -> None:
        from sova.mcp.tools import _validate_project_dir

        result = _validate_project_dir(str(tmp_path), allowed_root=None)
        assert result == tmp_path.resolve()

    def test_nonexistent_path(self) -> None:
        from sova.mcp.tools import _validate_project_dir

        with pytest.raises(FileNotFoundError, match="Project directory not found"):
            _validate_project_dir("/nonexistent/path/abc123", allowed_root=None)

    def test_file_not_directory(self, tmp_path: Path) -> None:
        from sova.mcp.tools import _validate_project_dir

        f = tmp_path / "afile.txt"
        f.write_text("hello")

        with pytest.raises(NotADirectoryError, match="Not a directory"):
            _validate_project_dir(str(f), allowed_root=None)

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        from sova.mcp.tools import _validate_project_dir

        allowed = tmp_path / "projects"
        allowed.mkdir()
        outside = tmp_path / "secrets"
        outside.mkdir()

        with pytest.raises(ValueError, match="outside the allowed root"):
            _validate_project_dir(str(outside), allowed_root=allowed)

    def test_path_within_allowed_root(self, tmp_path: Path) -> None:
        from sova.mcp.tools import _validate_project_dir

        sub = tmp_path / "child"
        sub.mkdir()

        result = _validate_project_dir(str(sub), allowed_root=tmp_path)
        assert result == sub.resolve()

    def test_path_equals_allowed_root(self, tmp_path: Path) -> None:
        from sova.mcp.tools import _validate_project_dir

        result = _validate_project_dir(str(tmp_path), allowed_root=tmp_path)
        assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Tool: sova_read_project
# ---------------------------------------------------------------------------


class TestReadProjectTool:
    def test_reads_existing_files(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agent Rules\nBe thorough.")
        (tmp_path / "sova.toml").write_text('[project]\nname = "test"')

        from sova.mcp.tools import _read_project_context

        result = _read_project_context(str(tmp_path))
        assert "Agent Rules" in result
        assert "Be thorough" in result
        assert "test" in result

    def test_returns_message_when_no_context(self, tmp_path: Path) -> None:
        from sova.mcp.tools import _read_project_context

        result = _read_project_context(str(tmp_path))
        assert "No SOVA project context found" in result

    def test_reads_nested_architecture(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "architecture.md").write_text("# Architecture\nPython + FastAPI")

        from sova.mcp.tools import _read_project_context

        result = _read_project_context(str(tmp_path))
        assert "Architecture" in result

    def test_rejects_nonexistent_dir(self) -> None:
        from sova.mcp.tools import _read_project_context

        with pytest.raises(FileNotFoundError):
            _read_project_context("/nonexistent/dir/xyz")

    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        from sova.mcp.tools import _read_project_context

        allowed = tmp_path / "project"
        allowed.mkdir()
        outside = tmp_path / "other"
        outside.mkdir()

        with pytest.raises(ValueError, match="outside the allowed root"):
            _read_project_context(str(outside), allowed_root=allowed)


# ---------------------------------------------------------------------------
# Tool: sova_develop (via _run_command)
# ---------------------------------------------------------------------------


class TestRunCommand:
    async def test_invokes_command_with_config(self, tmp_path: Path) -> None:
        mock_result = LLMResult(
            text="Implementation complete",
            model="sonnet",
            cost_usd=Decimal("0.50"),
            input_tokens=1000,
            output_tokens=500,
            duration_ms=10000,
        )

        with (
            patch("sova.mcp.tools.invoke_command", new_callable=AsyncMock, return_value=mock_result) as mock_invoke,
            patch("sova.mcp.tools.load_config") as mock_config,
        ):
            cfg = mock_config.return_value
            cfg.agent.model = "sonnet"
            cfg.agent.max_budget = 10
            cfg.agent.step_timeout = 1800

            from sova.mcp.tools import _run_command

            result = await _run_command("/develop", "42", str(tmp_path))

        assert result == "Implementation complete"
        mock_invoke.assert_called_once()
        call_args = mock_invoke.call_args
        assert call_args.args[0] == "/develop"
        assert call_args.kwargs["args"] == "42"
        assert call_args.kwargs["model"] == "sonnet"

    async def test_propagates_runtime_error(self, tmp_path: Path) -> None:
        with (
            patch(
                "sova.mcp.tools.invoke_command",
                new_callable=AsyncMock,
                side_effect=RuntimeError("CLI failed"),
            ),
            patch("sova.mcp.tools.load_config") as mock_config,
        ):
            cfg = mock_config.return_value
            cfg.agent.model = "sonnet"
            cfg.agent.max_budget = 10
            cfg.agent.step_timeout = 1800

            from sova.mcp.tools import _run_command

            with pytest.raises(RuntimeError, match="CLI failed"):
                await _run_command("/develop", "42", str(tmp_path))

    async def test_invalid_project_dir(self) -> None:
        from sova.mcp.tools import _run_command

        with pytest.raises(FileNotFoundError, match="Project directory not found"):
            await _run_command("/develop", "42", "/nonexistent/path/abc")

    async def test_load_config_failure(self, tmp_path: Path) -> None:
        with patch("sova.mcp.tools.load_config", side_effect=RuntimeError("bad config")):
            from sova.mcp.tools import _run_command

            with pytest.raises(ValueError, match="Failed to load SOVA config"):
                await _run_command("/develop", "42", str(tmp_path))


# ---------------------------------------------------------------------------
# Issue number validation
# ---------------------------------------------------------------------------


class TestDevelopValidation:
    async def test_zero_issue_number(self, tmp_path: Path) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        from sova.mcp.server import create_server

        server = create_server()
        with pytest.raises(ToolError, match="Must be positive"):
            await server.call_tool("sova_develop", {"issue_number": 0, "project_dir": str(tmp_path)})

    async def test_negative_issue_number(self, tmp_path: Path) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        from sova.mcp.server import create_server

        server = create_server()
        with pytest.raises(ToolError, match="Must be positive"):
            await server.call_tool("sova_develop", {"issue_number": -5, "project_dir": str(tmp_path)})


class TestAddressReviewValidation:
    async def test_zero_pr_number(self, tmp_path: Path) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        from sova.mcp.server import create_server

        server = create_server()
        with pytest.raises(ToolError, match="Must be positive"):
            await server.call_tool("sova_address_review", {"pr_number": 0, "project_dir": str(tmp_path)})

    async def test_negative_pr_number(self, tmp_path: Path) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        from sova.mcp.server import create_server

        server = create_server()
        with pytest.raises(ToolError, match="Must be positive"):
            await server.call_tool("sova_address_review", {"pr_number": -3, "project_dir": str(tmp_path)})


# ---------------------------------------------------------------------------
# Tool registration (via call_tool)
# ---------------------------------------------------------------------------


class TestToolCallIntegration:
    async def test_call_read_project(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Conventions")

        from sova.mcp.server import create_server

        server = create_server()
        content, _structured = await server.call_tool("sova_read_project", {"project_dir": str(tmp_path)})

        assert len(content) > 0
        assert "Conventions" in content[0].text

    async def test_call_develop_tool(self, tmp_path: Path) -> None:
        mock_result = LLMResult(text="Done", model="sonnet", cost_usd=Decimal("0.10"))

        with (
            patch("sova.mcp.tools.invoke_command", new_callable=AsyncMock, return_value=mock_result),
            patch("sova.mcp.tools.load_config") as mock_config,
        ):
            cfg = mock_config.return_value
            cfg.agent.model = "sonnet"
            cfg.agent.max_budget = 10
            cfg.agent.step_timeout = 1800

            from sova.mcp.server import create_server

            server = create_server()
            content, _structured = await server.call_tool(
                "sova_develop",
                {"issue_number": 42, "project_dir": str(tmp_path)},
            )

        assert len(content) > 0
        assert "Done" in content[0].text

    async def test_bound_project_dir(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Bound project")

        from sova.mcp.server import create_server

        server = create_server(project_dir=tmp_path)
        content, _structured = await server.call_tool("sova_read_project", {})

        assert len(content) > 0
        assert "Bound project" in content[0].text


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


class TestMCPCLI:
    def test_mcp_app_has_serve(self) -> None:
        from typer.testing import CliRunner

        from sova.cli.commands.mcp import app

        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert "serve" in result.output

    def test_app_registered_in_main(self) -> None:
        from sova.cli.app import app

        group_names = [g.typer_instance.info.name for g in app.registered_groups if g.typer_instance]
        assert "mcp" in group_names

    def test_invalid_transport_rejected(self) -> None:
        from typer.testing import CliRunner

        from sova.cli.commands.mcp import app

        runner = CliRunner()
        result = runner.invoke(app, ["serve", "--transport", "websocket"])
        assert result.exit_code != 0

    def test_nonexistent_project_dir(self) -> None:
        from typer.testing import CliRunner

        from sova.cli.commands.mcp import app

        runner = CliRunner()
        result = runner.invoke(app, ["serve", "--project", "/nonexistent/path/xyz"])
        assert result.exit_code != 0
