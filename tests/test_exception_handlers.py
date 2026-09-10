"""Guards on broad exception handlers across sova/ (issue #643).

Broad handlers (``except Exception``/``except BaseException``/bare ``except``) are
allowed, but they must never fail silently and must never log without a traceback.
See docs/error-handling-guidelines.md.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

SOVA_ROOT = Path(__file__).resolve().parent.parent / "sova"

_BROAD_TYPES = {"Exception", "BaseException"}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
# Only these levels must carry a traceback; info/debug calls inside a handler are
# often progress or recovery-succeeded messages, not error reports.
_TRACEBACK_LEVELS = {"warning", "warn", "error", "exception", "critical"}
_REPORT_FUNCS = {"print", "echo", "secho"}


def _iter_python_files() -> list[Path]:
    return sorted(SOVA_ROOT.rglob("*.py"))


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    return isinstance(handler.type, ast.Name) and handler.type.id in _BROAD_TYPES


def _calls_in(handler: ast.ExceptHandler) -> list[ast.Call]:
    module = ast.Module(body=handler.body, type_ignores=[])
    return [node for node in ast.walk(module) if isinstance(node, ast.Call)]


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _log_calls(handler: ast.ExceptHandler) -> list[ast.Call]:
    return [
        call for call in _calls_in(handler) if isinstance(call.func, ast.Attribute) and call.func.attr in _LOG_METHODS
    ]


def _reports(handler: ast.ExceptHandler) -> bool:
    return any(_call_name(call) in _REPORT_FUNCS for call in _calls_in(handler))


def _reraises(handler: ast.ExceptHandler) -> bool:
    module = ast.Module(body=handler.body, type_ignores=[])
    return any(isinstance(node, ast.Raise) for node in ast.walk(module))


def _has_traceback(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Attribute) and call.func.attr == "exception":
        return True
    # `exc_info=False` suppresses the traceback, so it does not satisfy the rule.
    return any(
        kw.arg == "exc_info" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
        for kw in call.keywords
    )


@lru_cache(maxsize=1)
def _broad_handlers() -> list[tuple[Path, ast.ExceptHandler, str]]:
    """Return (path, handler, source_line) for every broad handler in sova/."""
    found = []
    for path in _iter_python_files():
        source = path.read_text()
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _is_broad(node):
                line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
                found.append((path, node, line))
    return found


def _location(path: Path, handler: ast.ExceptHandler) -> str:
    return f"{path.relative_to(SOVA_ROOT.parent)}:{handler.lineno}"


class TestBroadHandlerDiscovery:
    def test_finds_broad_handlers(self) -> None:
        """Sanity check: the AST scan actually walks the package."""
        assert len(_broad_handlers()) > 100


class TestNoSilentSwallow:
    def test_broad_handlers_are_not_silent(self) -> None:
        """A broad handler must log, report, re-raise, or carry a noqa justification."""
        offenders = [
            _location(path, handler)
            for path, handler, line in _broad_handlers()
            if not _log_calls(handler) and not _reports(handler) and not _reraises(handler) and "BLE001" not in line
        ]
        assert not offenders, (
            "Broad exception handlers must not swallow silently. Add logging, or "
            "annotate the line with `# noqa: BLE001 (<reason>)`:\n  " + "\n  ".join(offenders)
        )


class TestLoggingIncludesTraceback:
    def test_broad_handler_logs_pass_exc_info(self) -> None:
        """Logging inside a broad handler must carry the traceback."""
        offenders = [
            _location(path, handler)
            for path, handler, _ in _broad_handlers()
            for call in _log_calls(handler)
            if call.func.attr in _TRACEBACK_LEVELS and not _has_traceback(call)
        ]
        assert not offenders, (
            "Log calls inside broad exception handlers must pass exc_info=True "
            "(or use log.exception):\n  " + "\n  ".join(sorted(set(offenders)))
        )


class TestNoqaJustification:
    def test_ble001_suppressions_have_a_reason(self) -> None:
        """Every `# noqa: BLE001` must explain why the broad catch is correct."""
        offenders = [
            f"{_location(path, handler)}: {line.strip()}"
            for path, handler, line in _broad_handlers()
            if "BLE001" in line and not line.split("BLE001", 1)[1].strip().startswith("(")
        ]
        assert not offenders, (
            "`# noqa: BLE001` must be followed by a parenthesised justification, "
            "e.g. `# noqa: BLE001 (health probe reports failure instead of raising)`:\n  " + "\n  ".join(offenders)
        )
