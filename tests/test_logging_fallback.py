"""The logging configuration a process has before ``setup_logging()`` runs.

Only ``sova run`` and the dashboard call ``setup_logging()``, so ``sova install``,
``sova uninstall`` and ``sova config get`` log through the fallback that importing
``sova.utils.logging`` installs. Each test runs in a real subprocess: this suite's
``configure_test_logging()`` deliberately replaces structlog's configuration, so an
in-process check could not see the fallback.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SECRET = "ghp_FAKESECRET123_must_not_reach_stderr"


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


class TestFallbackTraceback:
    def test_failed_settings_write_does_not_print_env_secrets(self, tmp_path: Path) -> None:
        """permissions.inject_failed logs with exc_info; the traceback must not carry frame locals.

        The failing frame holds ``data``, the parsed settings.json, which can carry an
        ``env`` block of API tokens. structlog's default renderer prints every local of
        every frame, so the token reached stderr until the fallback set a plain formatter.
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"env": {"GITHUB_TOKEN": _SECRET}}), encoding="utf-8")
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "from sova.utils.permissions import inject_agent_permissions\n"
            "with patch.object(Path, 'write_text', side_effect=OSError('read-only filesystem')):\n"
            "    inject_agent_permissions(Path(sys.argv[1]))\n"
        )

        result = _run_python(script, str(claude_dir))

        assert result.returncode == 0, result.stderr
        assert "permissions.inject_failed" in result.stderr
        assert "read-only filesystem" in result.stderr
        assert _SECRET not in result.stderr
        assert _SECRET not in result.stdout

    def test_failed_rtk_settings_write_does_not_print_env_secrets(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"env": {"GITHUB_TOKEN": _SECRET}}), encoding="utf-8")
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "from sova.utils.rtk import inject_rtk_hook\n"
            "with patch.object(Path, 'write_text', side_effect=OSError('read-only filesystem')):\n"
            "    inject_rtk_hook(Path(sys.argv[1]))\n"
        )

        result = _run_python(script, str(claude_dir))

        assert result.returncode == 0, result.stderr
        assert "rtk.inject_failed" in result.stderr
        assert _SECRET not in result.stderr
