"""Tests for sova.utils.version (package version and git provenance).

``get_version()`` single-sources the version from installed distribution
metadata, and ``get_git_sha()`` reports build provenance on a best-effort
basis. Both must degrade to a harmless value rather than raise: one backs
``sova.__version__`` (resolved lazily by ``sova/__init__.py``'s PEP 562
``__getattr__``) and the other feeds ``sova --version`` and ``/api/health``.
"""

from __future__ import annotations

import subprocess
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest

import sova.utils.version as version_module
from sova.utils.version import get_git_sha, get_version

# Mirrors the directory `get_git_sha()` itself probes, so the one test that
# asserts a real SHA is skipped (not failed) when the tests run from a source
# export, a release tarball, or a checkout made without a `.git` entry.
_CHECKOUT_HAS_GIT = (Path(version_module.__file__).resolve().parents[2] / ".git").exists()


class TestGetVersion:
    def setup_method(self) -> None:
        get_version.cache_clear()

    def teardown_method(self) -> None:
        get_version.cache_clear()

    def test_returns_installed_distribution_version(self) -> None:
        with patch("importlib.metadata.version", return_value="1.2.3"):
            assert get_version() == "1.2.3"

    def test_falls_back_when_distribution_is_not_installed(self) -> None:
        """A source tree on sys.path with no install must not raise."""
        with patch("importlib.metadata.version", side_effect=PackageNotFoundError("sova")):
            assert get_version() == "0.0.0+unknown"

    def test_package_dunder_version_matches_helper(self) -> None:
        import sova

        assert sova.__version__ == get_version()

    def test_package_rejects_an_unknown_attribute(self) -> None:
        """The PEP 562 __getattr__ must not turn every typo into a silent None."""
        import sova

        with pytest.raises(AttributeError):
            sova.not_a_real_attribute  # noqa: B018


class TestGetGitSha:
    def setup_method(self) -> None:
        get_git_sha.cache_clear()

    def teardown_method(self) -> None:
        get_git_sha.cache_clear()

    @pytest.mark.skipif(not _CHECKOUT_HAS_GIT, reason="no .git entry in this checkout: nothing to report a SHA from")
    def test_returns_short_sha_from_this_checkout(self) -> None:
        sha = get_git_sha()
        assert sha is not None
        assert sha.isalnum()

    def test_returns_none_without_a_git_directory(self) -> None:
        """A wheel install in a venv inside another repo must not report that repo's HEAD."""
        with (
            patch("pathlib.Path.exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            assert get_git_sha() is None
        mock_run.assert_not_called()

    def test_returns_none_when_git_is_missing(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("git")):
            assert get_git_sha() is None

    def test_returns_none_when_git_times_out(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2)):
            assert get_git_sha() is None

    def test_returns_none_on_nonzero_exit(self) -> None:
        completed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal")
        with patch("subprocess.run", return_value=completed):
            assert get_git_sha() is None

    def test_returns_none_on_empty_stdout(self) -> None:
        """A zero exit with no output is still "unknown", never an empty string."""
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="  \n", stderr="")
        with patch("subprocess.run", return_value=completed):
            assert get_git_sha() is None
