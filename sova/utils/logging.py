"""Structured logging setup for SOVA using structlog."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

_NAME_TO_LEVEL: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_log_file_handle: object | None = None


def setup_logging(
    *,
    json_output: bool = False,
    level: str = "INFO",
    log_file: Path | None = None,
    max_bytes: int = 10_485_760,  # 10 MB
    backup_count: int = 5,
) -> None:
    """Configure structlog for SOVA.

    Args:
        json_output: If True, output JSON lines (for dashboard streaming).
                     If False, output human-readable colored logs.
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Optional path to a JSON-per-line log file (e.g. .claude/sova.log).
        max_bytes: Maximum size of log file before rotation (default 10 MB).
        backup_count: Number of rotated log files to keep (default 5).
    """
    global _log_file_handle

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    processors: list[structlog.types.Processor] = [
        *shared_processors,
        structlog.processors.format_exc_info,
    ]

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Use RotatingFileHandler for automatic log rotation
        rotating_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        rotating_handler.setLevel(_NAME_TO_LEVEL.get(level.upper(), logging.INFO))

        # Add the handler to Python's root logger so structlog can use it
        root_logger = logging.getLogger()
        root_logger.addHandler(rotating_handler)
        root_logger.setLevel(_NAME_TO_LEVEL.get(level.upper(), logging.INFO))

        _log_file_handle = rotating_handler
        processors.append(_FileLogProcessor(rotating_handler))
    else:
        _log_file_handle = None

    processors.append(renderer)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(_NAME_TO_LEVEL.get(level.upper(), logging.INFO)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


class _FileLogProcessor:
    """Structlog processor that writes a JSON copy of each event to a file via handler.emit().

    Uses the handler's emit() method so RotatingFileHandler rotation logic
    (shouldRollover / doRollover) is triggered on each log line.
    """

    def __init__(self, handler: logging.Handler) -> None:
        self._handler = handler
        self._json_serializer = structlog.processors.JSONRenderer()

    def __call__(self, logger: object, method_name: str, event_dict: dict) -> dict:
        if self._handler:
            try:
                json_line = self._json_serializer(logger, method_name, dict(event_dict))
                level = _NAME_TO_LEVEL.get(event_dict.get("level", "").upper(), logging.INFO)
                record = logging.LogRecord(
                    name="sova",
                    level=level,
                    pathname="",
                    lineno=0,
                    msg=json_line,
                    args=None,
                    exc_info=None,
                )
                self._handler.emit(record)
            except Exception:
                pass
        return event_dict


def get_logger(**initial_values: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger with optional initial context values."""
    return structlog.get_logger(**initial_values)
