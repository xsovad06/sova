"""Tripwire for patch targets that name a module no longer owning the binding (issue #1213).

``load_config``, ``get_session`` and ``invalidate_config_cache`` are bound at module scope in the modules that use
them, and the Codex runtime lives in ``sova.ipc.codex_runtime``. Patching one of those names on its source module,
or a Codex name on ``sova.ipc.runtime``, then patches a name the code under test never reads, and the test
silently runs against the real config, database or CLI. Patch the importing module (``sova.<module>.<name>``) or
``sova.ipc.codex_runtime``.

This file is the static half: it scans test source for string-literal targets. ``tests/patch_target_guard.py`` is
the runtime half and catches a test that reaches a hoisted binding indirectly (a ``CliRunner``, the ASGI app, a
scheduler fixture), which no scan of a file's imports can see.

``SOURCE_PATCHES`` records the sites that patch a source module on purpose, per file and per name, because the code
under test reads the name through the source module. Each entry names that reader, and
``test_every_pinned_reader_still_reads_the_name_through_the_source`` fails when the reader binds the name at module
scope (the pin is then stale and the patch should be retargeted). Each count is pinned exactly, so a fixed patch
fails the test until its entry is lowered or deleted, and a new one fails until it is retargeted. A Codex name
patched on ``sova.ipc.runtime`` has no allowance anywhere.

The shared spawn helpers stay in ``sova.ipc.runtime`` and are legitimate targets for the Claude Code and Aider
runtimes, but ``CodexRuntime`` binds its own copies: a test whose class, function or file name mentions Codex must
patch them on ``sova.ipc.codex_runtime``. ``_SHARED_HELPER_RE`` is the source of truth for which names that covers.
That check reads string literals only; it does not see ``patch.object(module, "name")`` or ``monkeypatch.setattr``
forms.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS_DIR = Path(__file__).parent
REPO_DIR = TESTS_DIR.parent

# Source-module targets, built from parts so this file does not match its own scan.
_SOURCE_TARGETS = {
    "load_config": "sova.config.loader." + "load_config",
    "get_session": "sova.db.session." + "get_session",
    "invalidate_config_cache": "sova.config.loader." + "invalidate_config_cache",
}
SOURCE_RES = {name: re.compile(r"""["']%s["']""" % re.escape(target)) for name, target in _SOURCE_TARGETS.items()}

# Names that moved to sova.ipc.codex_runtime. ``run`` is one: sova.ipc.runtime does not import it, so a patch of
# ``sova.ipc.runtime.run`` can only have been meant for the Codex probes.
_CODEX_NAMES = "|".join(
    (
        "run",
        "CodexRuntime",
        "CodexStdinProbeAmbiguousError",
        r"_codex_\w+",
        r"_CODEX_\w+",
        r"_probe_codex_\w+",
        r"_resolve_codex_\w+",
        r"_interpret_codex_\w+",
        "_prompt_argument_blocks",
        "_HEADLESS_PREAMBLE_CODEX",
    )
)
CODEX_RE = re.compile(r"""["']sova\.ipc\.runtime\.(?:%s)["']""" % _CODEX_NAMES)

# Names CodexRuntime imports from sova.ipc.runtime and so binds itself.
_SHARED_HELPER_RE = re.compile(
    r"^sova\.ipc\.runtime\.(?:_check_cli_available|_spawn_agent_process|_inject_agent_marker|_VERSION_CHECK_TIMEOUT)$"
)

# Files that patch a source module on purpose: {path relative to tests/: {name: (exact count, readers)}}, where
# readers are the modules that read the name through the source and so need the source patched. sova.utils is the
# bottom layer: its upward imports of config.loader stay function-local (baselined, #1274), so the source patch is the
# effective one for sova.utils.env and sova.utils.diagnostics. The dependency-health service reads
# ``loader.load_config`` off the module, and the setup router and fleet manager import ``invalidate_config_cache``
# inside the function that calls it.
SOURCE_PATCHES: dict[str, dict[str, tuple[int, tuple[str, ...]]]] = {
    "test_setup_api.py": {
        "invalidate_config_cache": (2, ("sova.dashboard.routers.setup",)),
    },
    "test_dependency_health_api.py": {
        "load_config": (1, ("sova.dashboard.services.dependency_health_service",)),
    },
    "test_diagnostics.py": {"load_config": (46, ("sova.utils.diagnostics",))},
    "test_diagnostics_models.py": {"load_config": (6, ("sova.utils.diagnostics",))},
    "test_fleet_manager_service.py": {
        "invalidate_config_cache": (3, ("sova.dashboard.services.fleet_manager_service",)),
    },
    "test_ipc.py": {"load_config": (1, ("sova.utils.env",))},  # configured_passthrough()
    "test_utils_env.py": {"load_config": (5, ("sova.utils.env",))},
}


def _test_files() -> list[Path]:
    # The guard and its own tests patch a source module on purpose, to prove the guard flags it.
    skipped = {Path(__file__).name, "patch_target_guard.py", "test_patch_target_guard.py"}
    return [p for p in sorted(TESTS_DIR.rglob("*.py")) if p.name not in skipped]


def _rel(path: Path) -> str:
    return path.relative_to(TESTS_DIR).as_posix()


def test_no_new_patch_names_a_module_that_no_longer_owns_the_binding() -> None:
    drift: dict[str, dict[str, tuple[int, int]]] = {}
    for path in _test_files():
        rel = _rel(path)
        text = path.read_text(encoding="utf-8")
        for name, pattern in SOURCE_RES.items():
            found = len(pattern.findall(text))
            pinned = SOURCE_PATCHES.get(rel, {}).get(name, (0, ()))[0]
            if found != pinned:
                drift.setdefault(rel, {})[name] = (found, pinned)
    assert not drift, (
        "patch targets naming the source module instead of the importing one, as {file: {name: (found, pinned)}}; "
        f"retarget the patch, or lower or delete the SOURCE_PATCHES entry when one was fixed: {drift}"
    )


def test_no_test_patches_a_codex_name_on_the_module_that_no_longer_has_it() -> None:
    """There is no allowance: a Codex name patched on ``sova.ipc.runtime`` patches nothing."""
    offenders = {
        _rel(path): CODEX_RE.findall(path.read_text(encoding="utf-8"))
        for path in _test_files()
        if CODEX_RE.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, f"patch these on sova.ipc.codex_runtime instead: {offenders}"


def test_every_pinned_file_exists() -> None:
    missing = [rel for rel in SOURCE_PATCHES if not (TESTS_DIR / rel).is_file()]
    assert not missing, f"SOURCE_PATCHES names files that do not exist: {missing}"


def _binds_at_module_scope(module: str, name: str) -> bool:
    """True when ``module`` imports or defines ``name`` at module scope (``TYPE_CHECKING`` blocks excluded)."""
    path = REPO_DIR / (module.replace(".", "/") + ".py")
    if not path.is_file():
        path = REPO_DIR / module.replace(".", "/") / "__init__.py"
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and any((a.asname or a.name) == name for a in node.names):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
    return False


def test_every_pinned_reader_still_reads_the_name_through_the_source() -> None:
    """A pin is legitimate only while the reader imports the name lazily or reads it off the source module."""
    stale = []
    for rel, names in SOURCE_PATCHES.items():
        for name, (_count, readers) in names.items():
            for reader in readers:
                source = (REPO_DIR / (reader.replace(".", "/") + ".py")).read_text(encoding="utf-8")
                if name not in source:
                    stale.append(f"{rel}: {reader} no longer mentions {name}")
                elif _binds_at_module_scope(reader, name):
                    stale.append(f"{rel}: {reader} binds {name} at module scope, so patch {reader}.{name}")
    assert not stale, f"stale SOURCE_PATCHES entries: {stale}"


def test_the_reader_check_sees_a_module_scope_binding() -> None:
    # sova.config.db_loader hoisted both names; sova.utils.env is the bottom layer and keeps them lazy.
    assert _binds_at_module_scope("sova.config.db_loader", "load_config")
    assert _binds_at_module_scope("sova.config.db_loader", "invalidate_config_cache")
    assert not _binds_at_module_scope("sova.utils.env", "load_config")


def _codex_scoped_shared_patches(tree: ast.AST, stem: str = "") -> list[tuple[int, str]]:
    """Patches of a shared helper on ``sova.ipc.runtime`` made inside a Codex-named file, class or function."""
    hits: list[tuple[int, str]] = []

    def walk(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, (*stack, child.name))
                continue
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and _SHARED_HELPER_RE.match(child.value)
                and any("codex" in name.lower() for name in stack)
            ):
                hits.append((child.lineno, child.value))
            walk(child, stack)

    walk(tree, (stem,))
    return hits


def test_codex_tests_patch_the_helpers_codex_runtime_binds() -> None:
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _test_files():
        hits = _codex_scoped_shared_patches(ast.parse(path.read_text(encoding="utf-8")), path.stem)
        if hits:
            offenders[_rel(path)] = hits
    assert not offenders, f"Codex tests patching a name CodexRuntime binds in sova.ipc.codex_runtime: {offenders}"


def test_the_codex_scope_check_flags_a_stale_helper_patch() -> None:
    stale = "class TestCodexRuntime:\n    def test_x(self):\n        patch('sova.ipc.runtime._spawn_agent_process')\n"
    ok = "class TestClaudeRuntime:\n    def test_x(self):\n        patch('sova.ipc.runtime._spawn_agent_process')\n"
    module_level = "patch('sova.ipc.runtime._spawn_agent_process')\n"
    assert _codex_scoped_shared_patches(ast.parse(stale)) == [(3, "sova.ipc.runtime._spawn_agent_process")]
    assert _codex_scoped_shared_patches(ast.parse(ok)) == []
    # A Codex-only file counts as Codex-scoped even when no class or function name says so.
    assert _codex_scoped_shared_patches(ast.parse(module_level), "test_codex_smoke") == [
        (1, "sova.ipc.runtime._spawn_agent_process")
    ]
    assert _codex_scoped_shared_patches(ast.parse(module_level), "test_ipc") == []
