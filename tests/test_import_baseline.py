"""The import-linter baseline (``ignore_imports``) may only shrink.

import-linter alerts on an ignore entry that no longer matches, so a removed import forces its entry out of
``pyproject.toml``. Nothing stops a pull request from appending an entry for a new upward import, or swapping one
entry for another, so this test pins the exact set: a new entry fails until the upward import is inverted, and a
removed one fails until it is also deleted from ``EXPECTED_BASELINE`` here. The pin only ever loses lines (issue
#1274).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# Delete a line when its import is removed; never add one, invert the new upward import instead.
EXPECTED_BASELINE = frozenset(
    {
        "sova.adapters.github -> sova.supervisor.github_quota",
        "sova.config.db_loader -> sova.db.models",
        "sova.config.db_loader -> sova.db.session",
        "sova.config.loader -> sova.llm.keyring_store",
        "sova.config.root -> sova.llm.provider",
        "sova.core.dag -> sova.dashboard.services.agent_lifecycle",
        "sova.core.steps.address_review -> sova.roles.reviewer",
        "sova.core.steps.create_pr -> sova.supervisor.coderabbit_quota",
        "sova.core.steps.create_pr -> sova.supervisor.pr_throttle",
        "sova.dashboard.app -> sova.cli.commands.project",
        "sova.dashboard.routers.setup -> sova.cli.commands.project",
        "sova.dashboard.services.agent_finalize -> sova.scheduler.service",
        "sova.dashboard.services.batch_service -> sova.cli.commands.harden",
        "sova.git.pr -> sova.supervisor.github_quota",
        "sova.git.rebase -> sova.core.agent_dispatch",
        "sova.git.rebase -> sova.core.steps.rebase",
        "sova.git.worktree -> sova.core.steps.commit_convention",
        "sova.roles.developer -> sova.supervisor.labels",
        "sova.roles.triage -> sova.supervisor.dependency_graph",
        "sova.supervisor.daemon -> sova.dashboard.services.supervisor_service",
        "sova.supervisor.dependency_graph -> sova.dashboard.services.spec_service",
        "sova.supervisor.gates.budget -> sova.dashboard.services.agent_validation",
        "sova.supervisor.planner -> sova.dashboard.services.supervisor_service",
        "sova.supervisor.progression -> sova.dashboard.services.agent_lifecycle",
        "sova.supervisor.progression -> sova.dashboard.services.agent_recovery",
        "sova.supervisor.watchdog -> sova.dashboard.services.agent_lifecycle",
        "sova.utils.diagnostics -> sova.awareness",
        "sova.utils.diagnostics -> sova.config.db_loader",
        "sova.utils.diagnostics -> sova.config.loader",
        "sova.utils.diagnostics -> sova.config.models",
        "sova.utils.diagnostics -> sova.dashboard.services.models_service",
        "sova.utils.diagnostics -> sova.dashboard.services.settings_service",
        "sova.utils.diagnostics -> sova.db.session",
        "sova.utils.diagnostics -> sova.ipc.runtime",
        "sova.utils.diagnostics -> sova.llm.backends",
        "sova.utils.diagnostics -> sova.llm.client",
        "sova.utils.diagnostics -> sova.llm.keyring_store",
        "sova.utils.diagnostics -> sova.llm.models",
        "sova.utils.diagnostics -> sova.llm.provider",
        "sova.utils.env -> sova.config.loader",
        "sova.utils.env -> sova.config.models",
        "sova.utils.gh -> sova.supervisor.github_quota",
        "sova.utils.mcp_config -> sova.config.models",
    }
)


# The target order (docs/architecture-deep-dive.md, "Target layout"), highest first. ``:`` joins siblings that may
# import each other. Pinned with the baseline: reordering, merging or dropping a layer would legalise upward imports
# without a baseline entry, so it needs an explicit, reviewable edit here too.
EXPECTED_LAYERS = [
    "sova.cli : sova.scheduler",
    "sova.dashboard",
    "sova.supervisor",
    "sova.roles",
    "sova.core : sova.work",
    "sova.llm : sova.git : sova.ipc : sova.adapters",
    "sova.db",
    "sova.config",
    "sova.utils",
]


def _layers_contract() -> dict:
    contracts = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "importlinter"
    ]["contracts"]
    layers = [c for c in contracts if c.get("type") == "layers"]
    assert len(layers) == 1, f"expected exactly one layers contract, found {len(layers)}"
    return layers[0]


_CONTRACT = _layers_contract()


def test_baseline_is_exactly_the_pinned_set() -> None:
    current = set(_CONTRACT["ignore_imports"])
    added = sorted(current - EXPECTED_BASELINE)
    removed = sorted(EXPECTED_BASELINE - current)
    assert not added, f"new upward imports baselined (invert them instead of adding entries): {added}"
    assert not removed, f"baseline entries removed from pyproject.toml but still pinned here: {removed}"


def test_the_layer_order_is_the_pinned_one() -> None:
    assert _CONTRACT["layers"] == EXPECTED_LAYERS
    assert "containers" not in _CONTRACT, "the contract layers whole packages; a container would scope it down"


def test_baseline_has_no_duplicates() -> None:
    entries = _CONTRACT["ignore_imports"]
    assert len(entries) == len(set(entries))


def test_unmatched_ignore_entries_are_an_error() -> None:
    """The shrink-only guarantee must not rest on an import-linter default."""
    assert _CONTRACT["unmatched_ignore_imports_alerting"] == "error"


def _locked_version(name: str) -> str:
    lock = tomllib.loads((Path(__file__).parent.parent / "uv.lock").read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == name]
    assert len(versions) == 1, f"expected exactly one {name} entry in uv.lock, found {versions}"
    return versions[0]


def test_ci_pins_import_linter_and_grimp_to_the_locked_versions() -> None:
    """CI installs these two outside uv; a `uv lock` bump that skips ci.yml would change the graph CI builds."""
    ci = (Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for package in ("import-linter", "grimp"):
        assert f"'{package}=={_locked_version(package)}'" in ci, (
            f"ci.yml must pin {package}=={_locked_version(package)} (the version locked in uv.lock)"
        )
