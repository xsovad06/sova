"""The logging configuration a process has before ``setup_logging()`` runs.

Only ``sova run`` and the dashboard call ``setup_logging()``, so ``sova install``,
``sova uninstall`` and ``sova config get`` log through the fallback that importing
``sova.utils.logging`` installs. Each test runs in a real subprocess: this suite's
``configure_test_logging()`` deliberately replaces structlog's configuration, so an
in-process check could not see the fallback.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_python(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "SOVA_DATABASE_URL"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


class TestFallbackLevel:
    def test_debug_and_info_events_write_nothing(self) -> None:
        """lastResort showed WARNING and above; an un-setup process must not print debug or info."""
        script = (
            "from sova.utils.logging import get_logger\n"
            "log = get_logger(component='t')\n"
            "log.debug('debug.event')\n"
            "log.info('info.event')\n"
            "log.warning('warning.event')\n"
        )

        result = _run_python(script)

        assert result.returncode == 0, result.stderr
        assert "debug.event" not in result.stderr
        assert "info.event" not in result.stderr
        assert "warning.event" in result.stderr

    def test_events_go_to_stderr_not_stdout(self) -> None:
        script = "from sova.utils.logging import get_logger\nget_logger(component='t').error('error.event')\n"

        result = _run_python(script)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert "error.event" in result.stderr

    def test_setup_logging_still_replaces_the_fallback(self) -> None:
        """A logger created before setup_logging() picks up the level it installs."""
        script = (
            "from sova.utils.logging import get_logger, setup_logging\n"
            "log = get_logger(component='t')\n"
            "setup_logging(level='DEBUG')\n"
            "log.debug('debug.event')\n"
        )

        result = _run_python(script)

        assert result.returncode == 0, result.stderr
        assert "debug.event" in result.stderr

