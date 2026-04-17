"""Safe subprocess execution helpers for SOVA."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="shell")


@dataclass
class ShellResult:
    """Result of a shell command execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


async def run(
    *args: str,
    cwd: Path | str | None = None,
    timeout: float | None = 300,
    capture: bool = True,
) -> ShellResult:
    """Run a command asynchronously and return the result.

    Args:
        *args: Command and arguments (no shell expansion).
        cwd: Working directory.
        timeout: Timeout in seconds (default 5 minutes).
        capture: Whether to capture stdout/stderr.
    """
    log.debug("shell.run", cmd=args, cwd=str(cwd) if cwd else None)

    stdout_pipe = asyncio.subprocess.PIPE if capture else None
    stderr_pipe = asyncio.subprocess.PIPE if capture else None

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=stdout_pipe,
        stderr=stderr_pipe,
        cwd=cwd,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ShellResult(returncode=-1, stdout="", stderr=f"Command timed out after {timeout}s")

    stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        log.debug("shell.failed", cmd=args[0], returncode=proc.returncode, stderr=stderr[:200])

    return ShellResult(returncode=proc.returncode or 0, stdout=stdout, stderr=stderr)


async def run_checked(*args: str, cwd: Path | str | None = None, timeout: float | None = 300) -> ShellResult:
    """Run a command and raise on failure."""
    result = await run(*args, cwd=cwd, timeout=timeout)
    if not result.success:
        raise subprocess_error(args, result)
    return result


def subprocess_error(cmd: tuple[str, ...], result: ShellResult) -> RuntimeError:
    """Create a descriptive error for a failed subprocess."""
    return RuntimeError(
        f"Command failed: {' '.join(cmd)}\nExit code: {result.returncode}\nstderr: {result.stderr[:500]}"
    )
