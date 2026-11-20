"""Runtime guard for stale patch targets (issue #1213), the dynamic half of ``tests/test_patch_targets.py``.

``load_config``, ``get_session`` and ``invalidate_config_cache`` are bound at module scope in the modules that use
them. Patching the name on its source module (``sova.config.loader``, ``sova.db.session``) then replaces a name
those modules no longer read, and the code under test silently runs the real function. A static scan of a test
file's imports cannot see this when the test reaches the module indirectly (a ``CliRunner`` on the CLI app, the ASGI
app, a scheduler fixture), so this plugin checks it while the tests run.

Every ``sova`` module is imported once per session, before the first test, and every module-scope binding of a
guarded name is wrapped, so the coverage does not depend on which test happens to import a module first (under
xdist that order depends on the sharding). A call through a wrapper is an offence when:

- the *source* attribute is replaced (by a mock or any other object): the test patched the source, but the code under
  test read its own binding. A test that does this on purpose, because the code under test imports the name lazily,
  opts out with ``@pytest.mark.source_patch("<name>", "<reason>")``;
- for ``load_config``, another module's binding is replaced at that moment: the test stubbed the config for one
  module on the path and the next one read the real config. Before the hoist one source patch covered every caller;
  a test that migrated it must patch each binding the path reaches. A test that stubs one module and wants the real
  config for the rest opts out with ``@pytest.mark.partial_patch("load_config", "<reason>")``.

The offence is recorded and the test fails in teardown, so a broad ``except`` in the code under test cannot swallow
it. After every fixture has torn down, a binding still holding a mock (a module first imported, or re-imported, while
the source was patched binds the mock for good) fails the test and is restored, so it cannot leak into later tests.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import pkgutil
import sys
from collections.abc import Callable, Generator, Iterator
from typing import Any
from unittest.mock import NonCallableMock

import pytest

# (source module, guarded name)
GUARDED: tuple[tuple[str, str], ...] = (
    ("sova.config.loader", "load_config"),
    ("sova.db.session", "get_session"),
    ("sova.config.loader", "invalidate_config_cache"),
)

# Names whose bindings are checked against each other: a test stubbing one module's copy must stub every copy its
# path reaches. ``get_session`` is not: a test commonly fails one module's session while the rest use the test DB.
SIBLING_CHECKED = frozenset({"load_config"})

# Modules never imported eagerly: migration scripts run Alembic operations at import, ``__main__`` runs the CLI.
_SKIP_PREFIXES = ("sova.db.migrations",)
_SKIP_LEAF = "__main__"
_OPT_OUT_MARKERS = frozenset({"source_patch", "partial_patch"})

_offences: list[tuple[str, str]] = []  # (marker that excuses it, message)
_originals: dict[tuple[str, str], Callable[..., Any]] = {}
_wrappers: dict[tuple[str, str], Callable[..., Any]] = {}  # (module name, guarded name) -> installed wrapper
_seen_modules = 0
_imported_all = False


def _replaced_source(source_module: str, name: str) -> bool:
    return getattr(sys.modules[source_module], name, None) is not _originals[(source_module, name)]


def _patched_sibling(module_name: str, name: str) -> str | None:
    """Another module whose binding of *name* is replaced right now, if any."""
    for (other, other_name), wrapper in _wrappers.items():
        if other_name != name or other == module_name:
            continue
        module = sys.modules.get(other)
        if module is not None and getattr(module, name, wrapper) is not wrapper:
            return other
    return None


def _wrap(source_module: str, name: str, module_name: str, real: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(real)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _replaced_source(source_module, name):
            _offences.append(
                ("source_patch", f"{module_name}.{name} ran for real while {source_module}.{name} was patched")
            )
        elif name in SIBLING_CHECKED and (sibling := _patched_sibling(module_name, name)) is not None:
            _offences.append(("partial_patch", f"{module_name}.{name} ran for real while {sibling}.{name} was patched"))
        return real(*args, **kwargs)

    if inspect.iscoroutinefunction(real):
        inspect.markcoroutinefunction(wrapper)
    return wrapper


def _import_all_sova_modules() -> None:
    """Import every ``sova`` module once, so each binding is wrapped before any test can patch its source."""
    global _imported_all
    if _imported_all:
        return
    _imported_all = True
    import sova

    for info in pkgutil.walk_packages(sova.__path__, "sova."):
        if info.name.startswith(_SKIP_PREFIXES) or info.name.rsplit(".", 1)[-1] == _SKIP_LEAF:
            continue
        try:
            importlib.import_module(info.name)
        except Exception:  # noqa: BLE001 (an optional dependency missing here only leaves that module unguarded)
            continue


def _wrap_new_bindings() -> None:
    """Wrap each not-yet-wrapped module-scope binding of a guarded name; cheap when no module was imported."""
    global _seen_modules
    if len(sys.modules) == _seen_modules:
        return
    _seen_modules = len(sys.modules)
    for source_module, name in GUARDED:
        real = _originals[(source_module, name)]
        for module_name, module in list(sys.modules.items()):
            if (
                module is None
                or not module_name.startswith("sova.")
                or module_name == source_module
                or getattr(module, name, None) is not real
            ):
                continue
            if _wrappers.get((module_name, name)) is getattr(module, name):
                continue
            wrapper = _wrap(source_module, name, module_name, real)
            setattr(module, name, wrapper)
            _wrappers[(module_name, name)] = wrapper


def _leaked_bindings() -> list[str]:
    """Restore and name every module-scope binding of a guarded name still holding a mock after a test."""
    leaked: list[str] = []
    for source_module, name in GUARDED:
        real = _originals[(source_module, name)]
        for module_name, module in list(sys.modules.items()):
            if module is None or not module_name.startswith("sova."):
                continue
            value = module.__dict__.get(name)
            if not isinstance(value, NonCallableMock):
                continue
            if module_name == source_module:
                setattr(module, name, real)
            else:
                wrapper = _wrap(source_module, name, module_name, real)
                setattr(module, name, wrapper)
                _wrappers[(module_name, name)] = wrapper
            leaked.append(f"{module_name}.{name}")
    return leaked


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "source_patch(name, reason): this test patches the source module of a guarded name on purpose "
        "(the code under test imports it lazily); see tests/patch_target_guard.py",
    )
    config.addinivalue_line(
        "markers",
        "partial_patch(name, reason): this test stubs one module's binding of a guarded name and lets the others "
        "read the real one on purpose; see tests/patch_target_guard.py",
    )
    # Captured before any test runs, so no patch can be mistaken for the real function.
    for source_module, name in GUARDED:
        _originals.setdefault((source_module, name), getattr(importlib.import_module(source_module), name))


@pytest.fixture(autouse=True)
def _guard_stale_source_patches(request: pytest.FixtureRequest) -> Iterator[None]:
    _import_all_sova_modules()
    _wrap_new_bindings()
    _offences.clear()
    yield
    _wrap_new_bindings()
    allowed = {(m.name, m.args[0]) for m in request.node.iter_markers() if m.name in _OPT_OUT_MARKERS and m.args}
    stale = sorted(
        {
            message
            for marker, message in _offences
            if (marker, message.split(" ", 1)[0].rsplit(".", 1)[1]) not in allowed
        }
    )
    _offences.clear()
    if stale:
        pytest.fail(
            "a patch did not reach the code under test, which reads its own module-scope binding. Patch "
            "sova.<module>.<name> on every module the path reaches instead of the source (mark the test "
            "source_patch(<name>, <reason>) when the code under test imports the name lazily), and when one "
            "module's load_config is stubbed, stub every other one the path calls (or mark the test "
            f"partial_patch(load_config, <reason>)): {stale}",
            pytrace=False,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, None, None]:
    """After every fixture has torn down (``monkeypatch`` included), no module may keep a mock bound."""
    result = yield
    leaked = _leaked_bindings()
    if leaked:
        raise AssertionError(
            "a module-scope binding still holds a mock after the test (it was imported while its source was "
            f"patched, so it bound the mock for good); restored now: {sorted(leaked)}"
        )
    return result
