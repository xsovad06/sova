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


def _validate_project_dir(project_dir: str, allowed_root: Path | None) -> Path:
    """Resolve and validate a project directory path.

    Args:
        project_dir: Raw path string from the caller.
        allowed_root: If set, the resolved path must be under this root
            (path traversal guard). None disables the check.

    Returns:
        The resolved Path.

    Raises:
        FileNotFoundError: If the path does not exist.
        NotADirectoryError: If the path is not a directory.
        ValueError: If the path escapes the allowed root.
    """
    resolved = Path(project_dir).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Project directory not found: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {resolved}")
    if allowed_root is not None:
        allowed = allowed_root.resolve()
        if not (resolved == allowed or allowed in resolved.parents):
            raise ValueError(f"Project directory {resolved} is outside the allowed root {allowed}")
    return resolved


def register_tools(server: FastMCP, *, project_dir: Path | None = None) -> None:
    """Register all SOVA tools on the given MCP server.

    Args:
        server: The FastMCP server to register tools on.
        project_dir: If provided, tools are bound to this project directory
            and the ``project_dir`` parameter is removed from tool signatures.
            Paths are also constrained to stay within this root.
    """
    # When bound to a startup project, use it as the allowed root for
    # path-traversal protection and as the default directory.
    bound_dir = project_dir
    allowed_root = project_dir

    @server.tool(
        name="sova_develop",
        description=(
            "Develop a solution for a GitHub issue using TDD. "
            "Reads the issue, writes tests first, implements the solution, "
            "and verifies with linter and test suite."
        ),
    )
    async def develop(
        issue_number: Annotated[int, "GitHub issue number to develop (must be positive)"],
        project_dir: Annotated[str, "Path to the project directory"] = "",
    ) -> str:
        if issue_number <= 0:
            raise ValueError(f"Invalid issue number: {issue_number}. Must be positive.")
        effective = project_dir or (str(bound_dir) if bound_dir else ".")
        return await _run_command("/develop", str(issue_number), effective, allowed_root=allowed_root)

    @server.tool(
        name="sova_review",
        description=(
            "Review changed code as a senior engineer before pushing. "
            "Scores findings by priority, checks for bugs, security issues, "
            "and style violations, then fixes issues scored 3/10 or higher."
        ),
    )
    async def review(
        project_dir: Annotated[str, "Path to the project directory"] = "",
    ) -> str:
        effective = project_dir or (str(bound_dir) if bound_dir else ".")
        return await _run_command("/review", "", effective, allowed_root=allowed_root)

    @server.tool(
        name="sova_test",
        description=(
            "Run the project's linter and test suite iteratively. "
            "If tests fail, attempts to fix and re-run up to 3 times."
        ),
    )
    async def test(
        project_dir: Annotated[str, "Path to the project directory"] = "",
    ) -> str:
        effective = project_dir or (str(bound_dir) if bound_dir else ".")
        return await _run_command("/test", "", effective, allowed_root=allowed_root)

    @server.tool(
        name="sova_simplify",
        description=(
            "Review changed code for reuse, quality, and efficiency. "
            "Simplifies overly complex implementations and removes dead code."
        ),
    )
    async def simplify(
        project_dir: Annotated[str, "Path to the project directory"] = "",
    ) -> str:
        effective = project_dir or (str(bound_dir) if bound_dir else ".")
        return await _run_command("/simplify", "", effective, allowed_root=allowed_root)

    @server.tool(
        name="sova_address_review",
        description=(
            "Address PR review comments. Reads review findings, fixes or "
            "acknowledges each comment, and pushes the changes."
        ),
    )
    async def address_review(
        pr_number: Annotated[int, "Pull request number to address (must be positive)"],
        project_dir: Annotated[str, "Path to the project directory"] = "",
    ) -> str:
        if pr_number <= 0:
            raise ValueError(f"Invalid PR number: {pr_number}. Must be positive.")
        effective = project_dir or (str(bound_dir) if bound_dir else ".")
        return await _run_command("/address-pr", str(pr_number), effective, allowed_root=allowed_root)

    @server.tool(
        name="sova_create_pr",
        description=(
            "Create a pull request with a structured description. "
            "Analyzes all commits and changes on the current branch."
        ),
    )
    async def create_pr(
        project_dir: Annotated[str, "Path to the project directory"] = "",
    ) -> str:
        effective = project_dir or (str(bound_dir) if bound_dir else ".")
        return await _run_command("/pr", "", effective, allowed_root=allowed_root)

    @server.tool(
        name="sova_read_project",
        description=(
            "Read project context: AGENTS.md conventions, CLAUDE.md instructions, "
            "and architecture rules. Use this to understand a SOVA-managed "
            "project before starting work."
        ),
    )
    async def read_project(
        project_dir: Annotated[str, "Path to the project directory"] = "",
    ) -> str:
        effective = project_dir or (str(bound_dir) if bound_dir else ".")
        return _read_project_context(effective, allowed_root=allowed_root)


async def _run_command(command: str, args: str, project_dir: str, *, allowed_root: Path | None = None) -> str:
    """Run a SOVA command via the LLM client and return the result text."""
    resolved = _validate_project_dir(project_dir, allowed_root)

    try:
        config = load_config(resolved)
    except Exception as e:
        raise ValueError(f"Failed to load SOVA config from {resolved}: {e}") from e

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


def _read_project_context(project_dir: str, *, allowed_root: Path | None = None) -> str:
    """Read project context files and return their contents."""
    resolved = _validate_project_dir(project_dir, allowed_root)
    sections: list[str] = []

    context_files = [
        ("AGENTS.md", "Agent Conventions"),
        ("CLAUDE.md", "Claude Code Instructions"),
        (".claude/rules/architecture.md", "Architecture"),
    ]

    for filename, label in context_files:
        path = resolved / filename
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            continue
        sections.append(f"## {label}\n\n```\n{content}\n```")

    if not sections:
        return f"No SOVA project context found in {resolved}"

    return "\n\n".join(sections)
