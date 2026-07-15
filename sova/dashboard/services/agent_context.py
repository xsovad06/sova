"""Agent context resolution -- worktree lookup, command prompts, GH env, PR-to-issue.

Separated from agent_lifecycle to isolate context/environment resolution logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sova.git.worktree import find_worktree_by_branch
from sova.utils.logging import get_logger
from sova.utils.shell import run as run_shell

log = get_logger(component="dashboard.control.context")

_CLAUDE_DIR = ".claude"


async def _resolve_branch_name(pr_number: int | None, project_dir: Path) -> str:
    """Look up the head branch name for a PR via the GitHub CLI.

    Returns an empty string on failure or when *pr_number* is ``None``.
    """
    if not pr_number:
        return ""
    try:
        from sova.config.loader import load_config
        from sova.git.pr import get_pr_branch

        cfg = load_config(project_dir)
        if cfg.github_repo:
            branch = await get_pr_branch(
                pr_number,
                repo=cfg.github_repo,
                github_user=cfg.github_user,
            )
            if not branch:
                log.debug("pr_branch_empty", pr=pr_number)
                return ""
            return branch
    except (RuntimeError, KeyError, subprocess.CalledProcessError, FileNotFoundError):
        log.debug("pr_branch_lookup_failed", pr=pr_number, exc_info=True)
    return ""


async def _resolve_issue_worktree(issue: str, project_dir: Path, *, branch_name: str = "") -> Path:
    """Return the worktree path for an issue if one exists, else project_dir.

    Falls back to branch-based lookup via ``find_worktree_by_branch()`` when
    the issue-based directory doesn't exist but *branch_name* is provided.
    Filters out the main worktree to avoid running in the project root.
    """
    issue_id = issue.lstrip("#").strip()
    if issue_id and issue_id.isdigit():
        candidate = project_dir / _CLAUDE_DIR / "worktrees" / issue_id
        if candidate.is_dir():
            log.info("command.using_worktree", issue=issue_id, path=str(candidate))
            return candidate

    if branch_name:
        try:
            wt_path = await find_worktree_by_branch(branch_name, cwd=project_dir)
            if wt_path is not None and wt_path.resolve() != project_dir.resolve():
                log.info("command.using_branch_worktree", branch=branch_name, path=str(wt_path))
                return wt_path
        except (RuntimeError, FileNotFoundError, subprocess.CalledProcessError):
            log.debug("command.branch_worktree_lookup_failed", branch=branch_name, exc_info=True)

    return project_dir


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from command file content."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3 :].lstrip("\n")
    return content


def _resolve_command_prompt(command: str, args: dict | None, project_dir: Path) -> str:
    """Build the prompt for a Claude Code command."""
    arg_str = ""
    if args:
        arg_str = " ".join(f"{k}={v}" for k, v in args.items())

    target_cmd = project_dir / _CLAUDE_DIR / "commands" / f"{command}.md"
    if target_cmd.is_file():
        prompt = f"/{command}"
        if arg_str:
            prompt += " " + arg_str
        return prompt

    sova_root = Path(__file__).resolve().parent.parent.parent.parent
    sova_cmd = sova_root / _CLAUDE_DIR / "commands" / f"{command}.md"
    if not sova_cmd.is_file():
        sova_cmd = sova_root / "commands" / f"{command}.md"

    if sova_cmd.is_file():
        content = sova_cmd.read_text(encoding="utf-8")
        content = _strip_frontmatter(content)
        content = content.replace("$ARGUMENTS", arg_str)
        log.info("command.resolved_from_sova", command=command, source=str(sova_cmd))
        return content

    prompt = f"/{command}"
    if arg_str:
        prompt += " " + arg_str
    return prompt


async def _resolve_command_context(safe_args: dict, command: str, project_dir: Path) -> tuple[int | None, str]:
    """Extract PR number and issue identifier from command args."""
    raw_pr = safe_args.get("pr")
    try:
        pr_number = int(raw_pr) if raw_pr is not None else None
    except (ValueError, TypeError):
        pr_number = None
        raw_pr = None

    issue = str(safe_args.get("issue", "")).strip()
    if not issue:
        if raw_pr is not None:
            issue = await _resolve_issue_from_pr(raw_pr, project_dir)
    return pr_number, issue


async def _resolve_issue_from_pr(pr_number: int | str, project_dir: Path) -> str:
    """Extract a linked issue number from a PR body via gh CLI (best-effort)."""
    import re

    try:
        pr_str = str(int(pr_number))
        result = await run_shell(
            "gh",
            "pr",
            "view",
            pr_str,
            "--json",
            "body",
            "--jq",
            ".body",
            cwd=project_dir,
            timeout=10,
        )
        if result.success and result.stdout:
            match = re.search(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", result.stdout, re.IGNORECASE)
            if match:
                return match.group(1)
    except Exception:
        log.debug("resolve_issue_from_pr.failed", pr=pr_number, exc_info=True)
    return ""


async def _resolve_project_gh_env(project_dir: Path) -> dict[str, str] | None:
    """Resolve GH_TOKEN env for the project's configured github_user."""
    try:
        from sova.config.loader import load_config
        from sova.utils.gh import resolve_gh_env

        cfg = load_config(project_dir)
        return await resolve_gh_env(cfg.github_user)
    except Exception:
        log.debug("gh_env.resolve_failed", exc_info=True)
        return None
