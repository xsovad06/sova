"""Structured logging setup for SOVA using structlog."""

from __future__ import annotations

import logging
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


def setup_logging(*, json_output: bool = False, level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure structlog for SOVA.

    Args:
        json_output: If True, output JSON lines (for dashboard streaming).
                     If False, output human-readable colored logs.
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Optional path to a JSON-per-line log file (e.g. .claude/sova.log).
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
        _log_file_handle = open(log_file, "a", encoding="utf-8")  # noqa: SIM115
        processors.append(_FileLogProcessor(_log_file_handle))
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
    """Structlog processor that writes a JSON copy of each event to a file."""

    def __init__(self, fh: object) -> None:
        self._fh = fh
        self._json_serializer = structlog.processors.JSONRenderer()

    def __call__(self, logger: object, method_name: str, event_dict: dict) -> dict:
        if self._fh and not getattr(self._fh, "closed", True):
            try:
                json_line = self._json_serializer(logger, method_name, dict(event_dict))
                self._fh.write(json_line + "\n")
                self._fh.flush()
            except Exception:
                pass
        return event_dict


def get_logger(**initial_values: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger with optional initial context values."""
    return structlog.get_logger(**initial_values)
