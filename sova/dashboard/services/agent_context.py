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


async def _resolve_issue_worktree(
    issue: str,
    project_dir: Path,
    *,
    branch_name: str = "",
    pr_number: int | None = None,
) -> Path:
    """Return the worktree path for an issue, creating one if needed.

    Tries issue-based directory first, then branch-based lookup. When a branch
    is known but no worktree exists yet (common for issue-less PRs), creates a
    worktree so the agent never runs in the main project directory. Falls back
    to project_dir only when no branch is known.
    """
    issue_id = issue.lstrip("#").strip()
    if issue_id and issue_id.isdigit():
        candidate = project_dir / _CLAUDE_DIR / "worktrees" / issue_id
        if candidate.is_dir():
            log.info("command.using_worktree", issue=issue_id, path=str(candidate))
            return candidate

    if branch_name:
        branch_on_main = False
        try:
            wt_path = await find_worktree_by_branch(branch_name, cwd=project_dir)
            if wt_path is not None and wt_path.resolve() != project_dir.resolve():
                log.info("command.using_branch_worktree", branch=branch_name, path=str(wt_path))
                return wt_path
            if wt_path is not None:
                branch_on_main = True
        except (RuntimeError, FileNotFoundError, subprocess.CalledProcessError):
            log.debug("command.branch_worktree_lookup_failed", branch=branch_name, exc_info=True)

        # No existing worktree found -- create one for this branch so the agent
        # doesn't run in the main project directory and pollute its working tree.
        stashed = False
        try:
            from sova.git.worktree import create_worktree

            if issue_id and issue_id.isdigit():
                wt_id = issue_id
            elif pr_number is not None:
                wt_id = f"pr-{pr_number}"
            else:
                wt_id = branch_name.replace("/", "-").replace(" ", "-")[:50]

            # Guard: wt_id must be non-empty after sanitization; an empty wt_id
            # produces a path that resolves to the worktrees directory itself and
            # causes create_worktree to fail, falling back to project_dir.
            if not wt_id.strip("-"):
                log.warning("command.worktree_skipped_no_id", branch=branch_name, pr=pr_number)
                return project_dir

            # If the branch is checked out in the main repo, switch the main
            # repo to the default branch so git allows creating a worktree for
            # that branch. Stash uncommitted changes first (mirrors sync_branch
            # pattern in sova/git/branch.py).
            if branch_on_main:
                dirty_check = await run_shell(
                    "git",
                    "status",
                    "--porcelain",
                    cwd=project_dir,
                    timeout=10,
                )
                is_dirty = dirty_check.success and dirty_check.stdout.strip()
                if is_dirty:
                    stash_result = await run_shell(
                        "git",
                        "stash",
                        "--include-untracked",
                        cwd=project_dir,
                        timeout=10,
                    )
                    stashed = stash_result.success

                default_branch = await _get_default_branch(project_dir)
                switch = await run_shell("git", "checkout", default_branch, cwd=project_dir, timeout=10)
                if switch.success:
                    log.info("command.freed_branch_for_worktree", branch=branch_name, target=default_branch)
                else:
                    log.warning("command.branch_switch_failed", branch=branch_name, stderr=switch.stderr[:200])
                    await _pop_stash(stashed, project_dir)
                    return project_dir

            wt_info = await create_worktree(
                issue_id=wt_id,
                branch=branch_name,
                base_branch="HEAD",
                project_dir=project_dir,
            )
            log.info("command.created_worktree", branch=branch_name, wt_id=wt_id, path=str(wt_info.path))
            await _pop_stash(stashed, project_dir)
            return wt_info.path
        except Exception:
            log.warning("command.create_worktree_failed", branch=branch_name, exc_info=True)
            await _pop_stash(stashed, project_dir)

    return project_dir


async def _pop_stash(stashed: bool, project_dir: Path) -> None:
    """Pop the stash if we stashed earlier; log a warning on failure."""
    if not stashed:
        return
    pop = await run_shell("git", "stash", "pop", cwd=project_dir, timeout=10)
    if not pop.success:
        log.warning("command.stash_pop_failed", stderr=(pop.stderr or "")[:200])


async def _get_default_branch(project_dir: Path) -> str:
    """Return the default branch name (main/master) for the repo."""
    result = await run_shell(
        "git",
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "--short",
        cwd=project_dir,
        timeout=5,
    )
    ref = result.stdout.strip() if result.success else ""
    if ref:
        return ref.removeprefix("origin/")
    for branch in ("main", "master"):
        exists = await run_shell(
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            cwd=project_dir,
            timeout=5,
        )
        if exists.success:
            return branch
    return "main"


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
    """Extract a linked issue number from a PR body via gh CLI (best-effort).

    Validates that the referenced number is actually an issue (not another PR)
    by checking whether the GitHub Issues API returns a pull_request field.
    """
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
            for match in re.finditer(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", result.stdout, re.IGNORECASE):
                candidate = match.group(1)
                if await _is_issue(candidate, project_dir):
                    return candidate
                log.debug("resolve_issue_from_pr.not_issue", pr=pr_number, ref=candidate)
    except Exception:
        log.debug("resolve_issue_from_pr.failed", pr=pr_number, exc_info=True)
    return ""


async def _is_issue(number: str, project_dir: Path) -> bool:
    """Return True if the GitHub number is an issue (not a PR)."""
    try:
        result = await run_shell(
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{number}",
            "--jq",
            ".pull_request // empty",
            cwd=project_dir,
            timeout=10,
        )
        return result.success and not result.stdout.strip()
    except Exception:
        log.debug("is_issue.failed", number=number, exc_info=True)
        return True


def _resolve_mcp_env(run_id: int, project_dir: Path) -> dict[str, str]:
    """Generate MCP token and URL for the agent subprocess."""
    from sova.config.loader import load_config
    from sova.dashboard.services.mcp_service import generate_mcp_token, get_or_generate_secret

    try:
        cfg = load_config(project_dir)
        if not cfg.mcp.enabled:
            return {}

        secret = get_or_generate_secret(project_dir)
        token = generate_mcp_token(run_id, secret, cfg.mcp.token_expiry_hours)
        url = f"http://127.0.0.1:{cfg.server.port}/mcp"

        return {
            "SOVA_MCP_TOKEN": token,
            "SOVA_MCP_URL": url,
        }
    except Exception:
        log.debug("mcp_env.resolve_failed", exc_info=True)
        return {}


def merge_mcp_env(gh_env: dict[str, str] | None, run_id: int, project_dir: Path) -> dict[str, str] | None:
    """Merge MCP env vars into GH env."""
    mcp_env = _resolve_mcp_env(run_id, project_dir)
    if not mcp_env:
        return gh_env

    if gh_env:
        gh_env.update(mcp_env)
        return gh_env
    return mcp_env


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
