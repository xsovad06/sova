"""Tests for log rotation in sova.utils.logging."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from tests.conftest import configure_test_logging


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    """Create a temporary log directory."""
    log_path = tmp_path / "logs"
    log_path.mkdir()
    return log_path


@pytest.fixture(autouse=True)
def cleanup_logging():
    """Clean up logging handlers and restore the test structlog config between tests."""
    # Clear all handlers before test
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        if hasattr(handler, "close"):
            handler.close()

    yield

    # Clear all handlers after test
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        if hasattr(handler, "close"):
            handler.close()

    # Every test here calls production setup_logging() unpatched, which
    # reconfigures structlog process-wide: filtering at INFO, plus a
    # _FileLogProcessor bound to the handler just closed above. The session
    # fixture in conftest.py cannot reinstate its own configuration, so
    # without this every test ordered after this module (on this xdist
    # worker) would silently drop debug events. The restore only reaches
    # loggers that re-read the config on each call: production setup_logging()
    # sets cache_logger_on_first_use, so a logger whose very first use lands
    # inside one of these tests stays frozen at INFO for the rest of the
    # process. No test here logs through sova, so none is frozen today.
    configure_test_logging()


class TestTestLoggingConfiguration:
    """Guard the conftest structlog config these tests depend on for their own restore."""

    def test_capture_logs_sees_an_already_used_logger_after_a_reconfigure(self) -> None:
        """A re-applied config must reach loggers that were already used once.

        ``configure_test_logging()`` is re-applied by this module's teardown and
        installs a fresh processor list each time. With
        ``cache_logger_on_first_use`` on, a logger that has been used once is
        frozen against the list it saw then, so neither the restore nor
        ``capture_logs()`` (which swaps the *configured* list in place) reaches
        it again, and the loss is silent: no error, just no events.
        """
        configure_test_logging()
        log = structlog.get_logger(component="reconfigure-probe")
        log.debug("first use")

        configure_test_logging()

        with capture_logs() as entries:
            log.debug("probe")

        assert [entry["event"] for entry in entries] == ["probe"]


class TestLogRotation:
    """Test log file rotation with RotatingFileHandler."""

    def test_setup_logging_with_rotating_handler(self, log_dir: Path) -> None:
        """setup_logging() uses RotatingFileHandler when log_file is provided."""
        from sova.utils.logging import setup_logging

        log_file = log_dir / "sova.log"
        setup_logging(log_file=log_file, max_bytes=1024, backup_count=3)

        # Verify the handler is a RotatingFileHandler
        root = logging.getLogger()
        rotating_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(rotating_handlers) >= 1

        handler = rotating_handlers[0]
        assert handler.maxBytes == 1024
        assert handler.backupCount == 3
        assert str(log_file) in str(handler.baseFilename)

    def test_setup_logging_without_log_file(self) -> None:
        """setup_logging() without log_file does not create a RotatingFileHandler."""
        from sova.utils.logging import setup_logging

        setup_logging()

        root = logging.getLogger()
        rotating_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(rotating_handlers) == 0

    def test_log_file_created_in_parent_dir(self, log_dir: Path) -> None:
        """setup_logging() creates parent directories for log_file."""
        from sova.utils.logging import setup_logging

        nested_log = log_dir / "nested" / "dir" / "sova.log"
        setup_logging(log_file=nested_log)

        assert nested_log.parent.exists()
        assert nested_log.exists()

    def test_rotating_handler_properties(self, log_dir: Path) -> None:
        """RotatingFileHandler is configured with correct properties."""
        from sova.utils.logging import setup_logging

        log_file = log_dir / "sova.log"
        setup_logging(log_file=log_file, max_bytes=2048, backup_count=7)

        root = logging.getLogger()
        rotating_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(rotating_handlers) >= 1

        handler = rotating_handlers[0]
        assert handler.maxBytes == 2048
        assert handler.backupCount == 7

    def test_default_max_bytes_and_backup_count(self, log_dir: Path) -> None:
        """setup_logging() uses default values when not provided."""
        from sova.utils.logging import setup_logging

        log_file = log_dir / "sova.log"
        setup_logging(log_file=log_file)

        root = logging.getLogger()
        rotating_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(rotating_handlers) >= 1

        handler = rotating_handlers[0]
        # Default values from ServerConfig
        assert handler.maxBytes == 10_485_760  # 10 MB
        assert handler.backupCount == 5


class TestRotationKwargs:
    """rotation_kwargs() is the one place a configured rotation limit is translated."""

    def test_forwards_configured_values(self) -> None:
        from sova.utils.logging import rotation_kwargs

        assert rotation_kwargs(1024, 2) == {"max_bytes": 1024, "backup_count": 2}

    def test_omits_zero_so_setup_logging_defaults_apply(self) -> None:
        """maxBytes=0 never rolls over, so 0 must mean "use the default", not "never rotate"."""
        from sova.utils.logging import rotation_kwargs

        assert rotation_kwargs(0, 0) == {}
        assert rotation_kwargs(1024, 0) == {"max_bytes": 1024}
        assert rotation_kwargs(0, 2) == {"backup_count": 2}

    def test_omits_negative_values(self) -> None:
        """maxBytes=-1 would roll over on every record, so a negative is dropped too."""
        from sova.utils.logging import rotation_kwargs

        assert rotation_kwargs(-1, -1) == {}
        assert rotation_kwargs(-1, 2) == {"backup_count": 2}
        assert rotation_kwargs(1024, -1) == {"max_bytes": 1024}

    def test_server_config_defaults_forward_documented_values(self) -> None:
        """The values every writer gets when a project never configures rotation."""
        from sova.config.models import ProjectConfig
        from sova.utils.logging import rotation_kwargs

        cfg = ProjectConfig()
        assert rotation_kwargs(cfg.server.log_max_bytes, cfg.server.log_backup_count) == {
            "max_bytes": 10_485_760,
            "backup_count": 5,
        }


class TestConfiguredRotationReachesEveryWriter:
    """`sova run` shares .claude/sova.log with the server, so it honours the same config.

    Both call ``rotation_kwargs()`` with the same two ``server.*`` values
    (``TestRotationKwargs`` above covers the translation itself); this asserts
    the `sova run` side actually reaches ``setup_logging()`` with them.
    """

    async def test_run_workflow_passes_configured_rotation(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.cli.commands.run import _run_workflow
        from sova.config.models import ProjectConfig
        from sova.roles.base import RoleResult

        cfg = ProjectConfig(server={"log_max_bytes": 4096, "log_backup_count": 9})
        role = MagicMock()
        role.name = "researcher"

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.utils.logging.setup_logging") as mock_setup,
            patch(
                "sova.roles.dispatcher.dispatch",
                new=AsyncMock(return_value=(role, RoleResult(success=True, summary="done"))),
            ),
            patch("sova.cli.commands.run._install_sigterm_handler"),
        ):
            await _run_workflow("1", project_dir=tmp_path, role_name="researcher", force=False)

        assert mock_setup.call_args.kwargs["max_bytes"] == 4096
        assert mock_setup.call_args.kwargs["backup_count"] == 9
