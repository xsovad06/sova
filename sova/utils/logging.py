"""Structured logging setup for SOVA using structlog."""

from __future__ import annotations

import sys

import structlog


def setup_logging(*, json_output: bool = False, level: str = "INFO") -> None:
    """Configure structlog for SOVA.

    Args:
        json_output: If True, output JSON lines (for dashboard streaming).
                     If False, output human-readable colored logs.
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
    """
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

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(structlog.get_level_from_name(level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(**initial_values: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger with optional initial context values."""
    return structlog.get_logger(**initial_values)
