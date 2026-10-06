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
from sova.utils.network import looks_like_network_outage

log = get_logger(component="shell")

# Max seconds to wait for a killed process to exit. SIGKILL is unblockable;
# delays beyond ~1s indicate uninterruptible kernel I/O (e.g., deleted mount).
_KILL_TIMEOUT_SECONDS = 5

# Bounds the stdin write for long-lived spawns (write_stdin_and_close). A
# stall here would mean the child is not reading stdin at all, since it
# needs the prompt before it can produce any output; the timeout exists so
# such a CLI build degrades to a logged warning instead of hanging the
# spawn path forever.
_STDIN_WRITE_TIMEOUT_SECONDS = 30


@dataclass
class ShellResult:
    """Result of a shell command execution."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

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

    @property
    def is_network_unreachable(self) -> bool:
        """Check if the command failed because the network was unreachable.

        Distinct from ``is_rate_limited``: a throttled call reached the API,
        an unreachable one never left the machine. Both stderr and stdout are
        scanned because ``gh`` writes its connection error to stderr while
        some git porcelain reports transport failures on stdout.
        """
        if self.success:
            return False
        return looks_like_network_outage(self.stderr) or looks_like_network_outage(self.stdout)


async def run(
    *args: str,
    cwd: Path | str | None = None,
    timeout: float | None = 300,
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
    # DEVNULL, not None: inheriting our stdin lets a child that reads stdin
    # (e.g. a git pre-push hook) block until the step timeout kills it.
    stdin_pipe = asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL

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
        try:
            async with asyncio.timeout(_KILL_TIMEOUT_SECONDS):
                await proc.wait()
        except TimeoutError:
            log.warning("shell.kill_timeout", cmd=args[0], pid=proc.pid)
        return ShellResult(returncode=-1, stdout="", stderr=f"Command timed out after {timeout}s", timed_out=True)
    except asyncio.CancelledError:
        # Outer scope cancelled us (e.g., workflow verification timeout).
        # Kill the child process before propagating cancellation.
        proc.kill()
        try:
            async with asyncio.timeout(_KILL_TIMEOUT_SECONDS):
                await proc.wait()
        except TimeoutError:
            log.warning("shell.kill_timeout_on_cancel", cmd=args[0], pid=proc.pid)
        raise

    stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        log.debug("shell.failed", cmd=args[0], returncode=proc.returncode, stderr=stderr[:200])

    return ShellResult(returncode=proc.returncode or 0, stdout=stdout, stderr=stderr)


async def write_stdin_and_close(proc: asyncio.subprocess.Process, data: str) -> None:
    """Write ``data`` to a long-lived subprocess's stdin, then close it (EOF).

    For spawn paths that keep the process handle alive for streaming (unlike
    ``run()``, which pipes stdin through ``communicate()`` in one shot).
    Encodes as UTF-8 and awaits ``drain()`` (never a bare ``write()``, which
    would truncate or hang on a prompt larger than the pipe buffer) under a
    bounded timeout.

    A missing ``proc.stdin`` (inherited stdin, or a test double with none
    configured) is a no-op. Delivery cannot be confirmed complete on a
    broken pipe or a drain timeout: the child is killed and reaped (mirroring
    ``run()``'s own timeout handling above) rather than left running on a
    truncated prompt, and the failure is only logged, not raised, so the
    caller's existing exit-code/stderr handling (the killed child now exits
    non-zero) still produces the real diagnostic. Cancellation of the
    awaiting task is different: the child is killed and reaped the same way,
    but ``CancelledError`` is always re-raised (never swallowed), since the
    caller has not yet received a process wrapper to track or clean it up
    otherwise.
    """
    if proc.stdin is None:
        return
    try:
        async with asyncio.timeout(_STDIN_WRITE_TIMEOUT_SECONDS):
            proc.stdin.write(data.encode("utf-8"))
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        log.warning("shell.stdin_write_broken_pipe", pid=getattr(proc, "pid", None), exc_info=True)
        await _kill_and_reap(proc)
    except TimeoutError:
        log.warning("shell.stdin_write_timeout", pid=getattr(proc, "pid", None), exc_info=True)
        await _kill_and_reap(proc)
    except asyncio.CancelledError:
        await _kill_and_reap(proc)
        raise
    finally:
        proc.stdin.close()


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill+wait for a child whose stdin delivery could not be confirmed."""
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        async with asyncio.timeout(_KILL_TIMEOUT_SECONDS):
            await proc.wait()
    except TimeoutError:
        log.warning("shell.stdin_kill_timeout", pid=getattr(proc, "pid", None))


async def run_checked(
    *args: str,
    cwd: Path | str | None = None,
    timeout: float | None = 300,
    env: dict[str, str] | None = None,
) -> ShellResult:
    """Run a command and raise on failure."""
    result = await run(*args, cwd=cwd, timeout=timeout, env=env)
    if not result.success:
        raise subprocess_error(args, result)
    return result


def subprocess_error(cmd: tuple[str, ...], result: ShellResult) -> RuntimeError:
    """Create a descriptive error for a failed subprocess."""
    return RuntimeError(
        f"Command failed: {' '.join(cmd)}\nExit code: {result.returncode}\nstderr: {result.stderr[:500]}"
    )


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
