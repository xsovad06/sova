"""Process management utilities."""

import os


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running.

    A ``PermissionError`` means the process exists but is owned by another
    user, so it still counts as alive; any other ``OSError`` (including
    ``ProcessLookupError``) means dead. A non-positive ``pid`` is never alive:
    ``os.kill(0, 0)`` signals the caller's own process group and
    ``os.kill(-1, 0)`` every process the caller may signal, and both succeed,
    so a ``TaskRun`` carrying pid 0 would otherwise read as live forever.

    Known limitation: this only proves that *some* process holds the PID. If a
    dead agent's PID is recycled by another user's process (a root daemon, for
    example), the ``PermissionError`` branch reports it alive, so its run keeps
    holding a slot, the ``already_running`` gate keeps blocking its issue, and
    the watchdog never finalizes it. Closing that gap needs a start-time check
    (psutil ``create_time`` against ``TaskRun.started_at``), which is not done here.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
