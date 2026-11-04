"""CLI commands: sova audit report-path, disclosure.

Deterministic verdicts for the health-audit command's two mechanical gates, so
the command carries one instruction per verdict instead of the shell snippets
and the decision tree around them. Both fail closed: anything unexpected maps
to the conservative verdict (unsafe path, public repo).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run

log = get_logger(component="cli.audit")

app = typer.Typer(name="audit", help="Verdicts for the health-audit command's gates.", no_args_is_help=True)

DEFAULT_REPORT_PATH = "docs/HEALTH-AUDIT.md"
_POLICY_LOCATIONS = ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md")
_NON_PUBLIC_VISIBILITIES = frozenset({"PRIVATE", "INTERNAL"})
_TIMEOUT_SECONDS = 30

_PATH_ARGUMENT = typer.Argument(
    help="Report path to check, relative to the project root (the nearest SOVA project directory, else the git root)."
)


async def _exec(program: str, *args: str, cwd: Path) -> ShellResult:
    try:
        return await run(program, *args, cwd=cwd, timeout=_TIMEOUT_SECONDS)
    except OSError as exc:
        return ShellResult(returncode=127, stdout="", stderr=f"could not run {program}: {exc}")


_PROJECT_MARKERS = (".claude/sova.db", "sova.toml")


async def project_root(cwd: Path) -> Path:
    """The SOVA project directory containing `cwd`, else the git work tree root, else `cwd` itself.

    A SOVA project installed in a subdirectory of a larger repo keeps its database and config there, not at the
    git root, so the nearest ancestor holding a project marker wins over git's answer.
    """
    for candidate in (cwd, *cwd.parents):
        if any((candidate / marker).exists() for marker in _PROJECT_MARKERS):
            return candidate
    result = await _exec("git", "rev-parse", "--show-toplevel", cwd=cwd)
    top = result.stdout.strip() if result.success else ""
    return Path(top) if top else cwd


async def report_path_verdict(path: str, cwd: Path) -> str:
    """One of safe, unsafe-tracked, unsafe-untracked, unsafe-not-a-git-repo.

    Only an ignored, untracked path is safe. A git error other than "not ignored"
    is reported as unsafe-untracked, never as safe.
    """
    if (await _exec("git", "ls-files", "--error-unmatch", path, cwd=cwd)).success:
        return "unsafe-tracked"
    if (await _exec("git", "check-ignore", "-q", path, cwd=cwd)).success:
        return "safe"
    if (await _exec("git", "rev-parse", "--is-inside-work-tree", cwd=cwd)).success:
        return "unsafe-untracked"
    return "unsafe-not-a-git-repo"


def find_security_policy(cwd: Path) -> str | None:
    """The first disclosure policy file at a location GitHub honours, or None."""
    return next((rel for rel in _POLICY_LOCATIONS if (cwd / rel).is_file()), None)


async def disclosure_verdict(cwd: Path) -> str:
    """One of private, public-policy <path>, public-no-policy.

    Only an explicit PRIVATE or INTERNAL answer is private. A failed check or an
    unrecognised answer is public: the gate must fail toward asking. A failed
    lookup is logged, never printed, so stdout carries the verdict alone.
    """
    result = await _exec("gh", "repo", "view", "--json", "visibility", "--jq", ".visibility", cwd=cwd)
    visibility = result.stdout.strip().upper() if result.success else ""
    if visibility in _NON_PUBLIC_VISIBILITIES:
        return "private"
    if not result.success:
        log.warning("audit.visibility_check_failed", stderr=result.stderr[:200])
    policy = find_security_policy(cwd)
    return f"public-policy {policy}" if policy else "public-no-policy"


# The commands judge from the project root, so a subdirectory cwd cannot make a verdict describe a different file.
async def _report_path_from_root(path: str) -> str:
    return await report_path_verdict(path, await project_root(Path.cwd()))


async def _disclosure_from_root() -> str:
    return await disclosure_verdict(await project_root(Path.cwd()))


@app.command("report-path")
def report_path_cmd(path: Annotated[str, _PATH_ARGUMENT] = DEFAULT_REPORT_PATH) -> None:
    """Print whether the report path is safe to hold withheld exploit detail."""
    typer.echo(asyncio.run(_report_path_from_root(path)))


@app.command("disclosure")
def disclosure_cmd() -> None:
    """Print the repo visibility verdict that selects the disclosure pre-flight branch."""
    typer.echo(asyncio.run(_disclosure_from_root()))
