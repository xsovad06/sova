"""Entry point for the dashboard's Authorize Google control: runs the interactive consent flow in a child process.

``authenticate_google()`` blocks on a local consent server that a thread cannot cancel, so the dashboard runs
this module as a subprocess it can terminate. It is not a second authorization implementation: it loads the
project config and calls ``authenticate_google()``, which writes the token where it always does.
Exit code 0 means a usable token exists; anything else is a failure. The parent discards this process's
stdout and stderr, so the only diagnostics are the log entries written to the project's ``.claude/sova.log``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sova.utils.logging import get_logger

_log = get_logger(component="awareness.auth")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        return 2
    from sova.awareness.auth.google_oauth import authenticate_google
    from sova.config.loader import load_config
    from sova.utils.logging import rotation_kwargs, setup_logging

    project_dir = Path(argv[0])
    log_file = project_dir / ".claude" / "sova.log"
    try:
        try:
            config = load_config(project_dir)
        except Exception:
            # stderr is discarded by the parent, so even a config failure must reach the shared log file.
            setup_logging(log_file=log_file)
            raise
        setup_logging(log_file=log_file, **rotation_kwargs(config.server.log_max_bytes, config.server.log_backup_count))
        authenticate_google(config.awareness)
    except Exception:  # noqa: BLE001 (the parent only needs pass or fail; details stay in the log)
        _log.warning("authorize_failed", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
