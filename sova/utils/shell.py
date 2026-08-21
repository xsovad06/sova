"""Safe subprocess execution helpers for SOVA.

Approved exception: ``spawn_direct()`` in ``sova/ipc/runtime.py`` creates
long-lived subprocesses that return a live process handle for streaming.
It bypasses this module intentionally; see its docstring for rationale.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="shell")

DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
_STDERR_LOG_LIMIT = 200
_STDERR_ERROR_LIMIT = 500


@dataclass
class ShellResult:
    """Result of a shell command execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def is_rate_limited(self) -> bool:
        """Check if the command failed due to a GitHub API rate limit."""
        if self.success:
            return False
        lower = self.stderr.lower()
        return "rate limit" in lower or "abuse detection" in lower


async def run(
    *args: str,
    cwd: Path | str | None = None,
    timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    capture: bool = True,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> ShellResult:
    """Run a command asynchronously and return the result.

    Args:
        *args: Command and arguments (no shell expansion).
        cwd: Working directory.
        timeout: Timeout in seconds (default 5 minutes).
        capture: Whether to capture stdout/stderr.
        env: Environment variables. None inherits parent env.
        stdin: Optional string to pass as stdin to the process.
    """
    log.debug("shell.run", cmd=args, cwd=str(cwd) if cwd else None)

    stdout_pipe = asyncio.subprocess.PIPE if capture else None
    stderr_pipe = asyncio.subprocess.PIPE if capture else None
    stdin_pipe = asyncio.subprocess.PIPE if stdin is not None else None

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=stdin_pipe,
        stdout=stdout_pipe,
        stderr=stderr_pipe,
        cwd=cwd,
        env=env,
    )

    stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
    try:
        async with asyncio.timeout(timeout):
            stdout_bytes, stderr_bytes = await proc.communicate(input=stdin_bytes)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ShellResult(returncode=-1, stdout="", stderr=f"Command timed out after {timeout}s")

    stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        log.debug("shell.failed", cmd=args[0], returncode=proc.returncode, stderr=stderr[:_STDERR_LOG_LIMIT])

    return ShellResult(returncode=proc.returncode or 0, stdout=stdout, stderr=stderr)


async def run_checked(
    *args: str,
    cwd: Path | str | None = None,
    timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> ShellResult:
    """Run a command and raise on failure."""
    result = await run(*args, cwd=cwd, timeout=timeout, env=env)
    if not result.success:
        raise subprocess_error(args, result)
    return result


def subprocess_error(cmd: tuple[str, ...], result: ShellResult) -> RuntimeError:
    """Create a descriptive error for a failed subprocess."""
    stderr_excerpt = result.stderr[:_STDERR_ERROR_LIMIT]
    return RuntimeError(f"Command failed: {' '.join(cmd)}\nExit code: {result.returncode}\nstderr: {stderr_excerpt}")


@dataclass
class GitIdentityResult:
    """Result of a git identity validation check."""

    name: str
    email: str

    @property
    def valid(self) -> bool:
        return bool(self.name) and bool(self.email)

    @property
    def missing_fields(self) -> list[str]:
        fields = []
        if not self.name:
            fields.append("user.name")
        if not self.email:
            fields.append("user.email")
        return fields


async def check_git_identity(cwd: Path | str | None = None) -> GitIdentityResult:
    """Check whether git user.name and user.email are configured.

    Uses git's standard resolution order (local overrides global).
    Treats empty strings as missing.
    """
    try:
        name_result, email_result = await asyncio.gather(
            run("git", "config", "user.name", cwd=cwd),
            run("git", "config", "user.email", cwd=cwd),
        )
    except OSError as exc:
        log.warning("check_git_identity.failed", error=str(exc))
        return GitIdentityResult(name="", email="")

    name = name_result.stdout.strip() if name_result.success else ""
    email = email_result.stdout.strip() if email_result.success else ""

    return GitIdentityResult(name=name, email=email)
