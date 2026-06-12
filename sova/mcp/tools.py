"""MCP tool definitions for SOVA.

Each tool wraps a SOVA capability (develop, review, test, etc.) and exposes
it as an MCP tool that any compliant agent runtime can invoke.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP

from sova.config.loader import load_config
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger

log = get_logger(component="mcp.tools")


def register_tools(server: FastMCP) -> None:
    """Register all SOVA tools on the given MCP server."""

    @server.tool(
        name="sova_develop",
        description=(
            "Develop a solution for a GitHub issue using TDD. "
            "Reads the issue, writes tests first, implements the solution, "
            "and verifies with linter and test suite."
        ),
    )
    async def develop(
        issue_number: Annotated[int, "GitHub issue number to develop"],
        project_dir: Annotated[str, "Path to the project directory"] = ".",
    ) -> str:
        return await _run_command("/develop", str(issue_number), project_dir)

    @server.tool(
        name="sova_review",
        description=(
            "Review changed code as a senior engineer before pushing. "
            "Scores findings by priority, checks for bugs, security issues, "
            "and style violations, then fixes issues scored 3/10 or higher."
        ),
    )
    async def review(
        project_dir: Annotated[str, "Path to the project directory"] = ".",
    ) -> str:
        return await _run_command("/review", "", project_dir)

    @server.tool(
        name="sova_test",
        description=(
            "Run the project's linter and test suite iteratively. "
            "If tests fail, attempts to fix and re-run up to 3 times."
        ),
    )
    async def test(
        project_dir: Annotated[str, "Path to the project directory"] = ".",
    ) -> str:
        return await _run_command("/test", "", project_dir)

    @server.tool(
        name="sova_simplify",
        description=(
            "Review changed code for reuse, quality, and efficiency. "
            "Simplifies overly complex implementations and removes dead code."
        ),
    )
    async def simplify(
        project_dir: Annotated[str, "Path to the project directory"] = ".",
    ) -> str:
        return await _run_command("/simplify", "", project_dir)

    @server.tool(
        name="sova_address_review",
        description=(
            "Address PR review comments. Reads review findings, fixes or "
            "acknowledges each comment, and pushes the changes."
        ),
    )
    async def address_review(
        pr_number: Annotated[int, "Pull request number to address"],
        project_dir: Annotated[str, "Path to the project directory"] = ".",
    ) -> str:
        return await _run_command("/address-pr", str(pr_number), project_dir)

    @server.tool(
        name="sova_create_pr",
        description=(
            "Create a pull request with a structured description. "
            "Analyzes all commits and changes on the current branch."
        ),
    )
    async def create_pr(
        project_dir: Annotated[str, "Path to the project directory"] = ".",
    ) -> str:
        return await _run_command("/pr", "", project_dir)

    @server.tool(
        name="sova_read_project",
        description=(
            "Read project context: AGENTS.md conventions, sova.toml config, "
            "and architecture rules. Use this to understand a SOVA-managed "
            "project before starting work."
        ),
    )
    async def read_project(
        project_dir: Annotated[str, "Path to the project directory"] = ".",
    ) -> str:
        return _read_project_context(project_dir)


async def _run_command(command: str, args: str, project_dir: str) -> str:
    """Run a SOVA command via the LLM client and return the result text."""
    resolved = Path(project_dir).resolve()
    config = load_config(resolved)

    log.info("mcp.run_command", command=command, args=args, project_dir=str(resolved))

    result = await invoke_command(
        command,
        args=args,
        model=config.agent.model,
        cwd=resolved,
        max_budget_usd=config.agent.max_budget,
        timeout=config.agent.step_timeout,
    )

    log.info(
        "mcp.command_complete",
        command=command,
        cost_usd=str(result.cost_usd),
        tokens=result.total_tokens,
    )

    return result.text


def _read_project_context(project_dir: str) -> str:
    """Read project context files and return their contents."""
    resolved = Path(project_dir).resolve()
    sections: list[str] = []

    context_files = [
        ("AGENTS.md", "Agent Conventions"),
        ("CLAUDE.md", "Claude Code Instructions"),
        ("sova.toml", "SOVA Configuration"),
        (".claude/rules/architecture.md", "Architecture"),
    ]

    for filename, label in context_files:
        path = resolved / filename
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace")
            sections.append(f"## {label}\n\n```\n{content}\n```")

    if not sections:
        return f"No SOVA project context found in {resolved}"

    return "\n\n".join(sections)
