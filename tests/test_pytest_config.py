"""Guards on the pytest configuration in pyproject.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pytest_options() -> dict[str, Any]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]


class TestAsyncioLoopScope:
    def test_default_fixture_loop_scope_is_set(self) -> None:
        """pytest-asyncio warns at configure time while this option is unset.

        Since pytest 9.2 the ``filterwarnings = ["error"]`` entry also applies to
        warnings raised during ``pytest_configure``, so that warning aborts the
        whole session with INTERNALERROR before any test runs (Dependabot PR #1398).
        """
        assert _pytest_options().get("asyncio_default_fixture_loop_scope") == "function"
