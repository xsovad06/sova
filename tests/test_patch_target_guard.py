"""The runtime patch-target guard (``tests/patch_target_guard.py``, issue #1213) must catch what it claims to."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sova.cli.commands import config as config_command
from sova.cli.commands import project
from sova.config import db_loader, loader
from tests import patch_target_guard as guard

pytest_plugins = ["pytester"]

_INI = "[pytest]\nasyncio_default_fixture_loop_scope = function\n"
_STALE_TEST = """
from unittest.mock import MagicMock, patch

def test_stale(tmp_path):
    from sova.cli.commands import project

    with patch("sova.config.loader.load_config", return_value=MagicMock()):
        project.load_config(tmp_path)
"""


def _offences(marker: str) -> list[str]:
    return [message for kind, message in guard._offences if kind == marker]


def test_the_guard_wraps_the_module_scope_bindings() -> None:
    assert project.load_config is not loader.load_config, "the hoisted binding should be the guard's wrapper"
    assert guard._wrappers[("sova.cli.commands.project", "load_config")] is project.load_config
    assert db_loader.invalidate_config_cache is not loader.invalidate_config_cache


def test_every_sova_module_is_imported_and_wrapped_before_the_first_test() -> None:
    """Coverage must not depend on which test imports a module first: under xdist that order follows the sharding."""
    assert "sova.dashboard.services.agent_handoff" in sys.modules
    module = sys.modules["sova.dashboard.services.queue_service"]
    assert guard._wrappers[("sova.dashboard.services.queue_service", "load_config")] is module.load_config
    assert not any(name.endswith(".__main__") for name in sys.modules if name.startswith("sova."))


@pytest.mark.source_patch("load_config", "this test provokes the offence on purpose")
def test_a_hoisted_binding_called_while_only_the_source_is_patched_is_recorded(tmp_path: Path) -> None:
    with patch("sova.config.loader.load_config", return_value=MagicMock()):
        project.load_config(tmp_path)

    assert any(o.startswith("sova.cli.commands.project.load_config ran for real") for o in _offences("source_patch"))


@pytest.mark.source_patch("load_config", "this test provokes the offence on purpose")
def test_a_source_replaced_by_a_plain_function_is_recorded_too(tmp_path: Path) -> None:
    """``monkeypatch.setattr(source, lambda ...)`` bypasses a hoisted binding just as a mock does."""
    with patch("sova.config.loader.load_config", new=lambda *_a, **_kw: MagicMock()):
        project.load_config(tmp_path)

    assert _offences("source_patch")


@pytest.mark.partial_patch("load_config", "this test provokes the offence on purpose")
def test_a_real_load_config_while_a_sibling_binding_is_stubbed_is_recorded(tmp_path: Path) -> None:
    """One module's stub no longer covers the next module on the path, as the source patch it replaced did."""
    with patch.object(config_command, "load_config", return_value=MagicMock()):
        project.load_config(tmp_path)

    assert any(
        o == "sova.cli.commands.project.load_config ran for real while sova.cli.commands.config.load_config was patched"
        for o in _offences("partial_patch")
    )


def test_patching_the_importing_module_is_not_an_offence(tmp_path: Path) -> None:
    with patch("sova.config.loader.load_config", return_value=MagicMock()):
        with patch("sova.cli.commands.project.load_config", return_value=MagicMock()):
            project.load_config(tmp_path)

    assert not guard._offences


def test_an_unpatched_call_is_not_an_offence(tmp_path: Path) -> None:
    project.load_config(tmp_path)

    assert not guard._offences


def test_a_stale_source_patch_fails_the_test_in_teardown(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(_STALE_TEST)
    result = pytester.runpytest_inprocess("-p", "tests.patch_target_guard", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*did not reach the code under test*sova.cli.commands.project.load_config*"])


def test_the_source_patch_marker_opts_a_test_out(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(
        _STALE_TEST.replace(
            "def test_stale", '@pytest.mark.source_patch("load_config", "lazy reader")\ndef test_stale'
        ).replace("from unittest.mock", "import pytest\nfrom unittest.mock", 1)
    )
    result = pytester.runpytest_inprocess("-p", "tests.patch_target_guard", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_the_partial_patch_marker_opts_a_test_out(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
import pytest
from unittest.mock import MagicMock, patch

@pytest.mark.partial_patch("load_config", "only the config command is stubbed")
def test_partial(tmp_path):
    from sova.cli.commands import config, project

    with patch.object(config, "load_config", return_value=MagicMock()):
        project.load_config(tmp_path)
"""
    )
    result = pytester.runpytest_inprocess("-p", "tests.patch_target_guard", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_a_mock_left_bound_after_the_test_fails_it_and_is_restored(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
from unittest.mock import MagicMock

def test_leaks():
    from sova.cli.commands import project

    project.load_config = MagicMock()

def test_after_the_leak():
    from unittest.mock import NonCallableMock

    from sova.cli.commands import project

    assert not isinstance(project.load_config, NonCallableMock)
"""
    )
    result = pytester.runpytest_inprocess("-p", "tests.patch_target_guard", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*still holds a mock*sova.cli.commands.project.load_config*"])
