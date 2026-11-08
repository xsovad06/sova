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


SHA_A = "a" * 40
SHA_B = "b" * 40


def _make_repo(root: Path, *, head: str, refs: dict[str, str] | None = None, packed: str | None = None) -> Path:
    """Build a minimal `.git` directory under ``root`` and return ``root``."""
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head, encoding="utf-8")
    for ref, sha in (refs or {}).items():
        ref_path = git / ref
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(sha + "\n", encoding="utf-8")
    if packed is not None:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return root


class TestResolveHeadSha:
    def test_no_git_entry_is_none(self, tmp_path: Path) -> None:
        assert version_module._resolve_head_sha(tmp_path) is None

    def test_loose_branch_ref(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, head="ref: refs/heads/main\n", refs={"refs/heads/main": SHA_A})
        assert version_module._resolve_head_sha(tmp_path) == SHA_A

    def test_detached_head_holds_a_raw_sha(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, head=SHA_B + "\n")
        assert version_module._resolve_head_sha(tmp_path) == SHA_B

    def test_branch_only_in_packed_refs(self, tmp_path: Path) -> None:
        packed = f"# pack-refs with: peeled\n{SHA_B} refs/tags/v1\n^{SHA_A}\n{SHA_A} refs/heads/main\n"
        _make_repo(tmp_path, head="ref: refs/heads/main\n", packed=packed)
        assert version_module._resolve_head_sha(tmp_path) == SHA_A

    def test_loose_ref_wins_over_packed_refs(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            head="ref: refs/heads/main\n",
            refs={"refs/heads/main": SHA_B},
            packed=f"{SHA_A} refs/heads/main\n",
        )
        assert version_module._resolve_head_sha(tmp_path) == SHA_B

    def test_unknown_ref_is_none(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, head="ref: refs/heads/gone\n", packed=f"{SHA_A} refs/heads/main\n")
        assert version_module._resolve_head_sha(tmp_path) is None

    def test_truncated_head_is_none(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, head="")
        assert version_module._resolve_head_sha(tmp_path) is None

    def test_truncated_ref_file_is_none(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, head="ref: refs/heads/main\n", refs={"refs/heads/main": SHA_A[:10]})
        assert version_module._resolve_head_sha(tmp_path) is None

    def test_non_utf8_head_is_none(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, head="")
        (tmp_path / ".git" / "HEAD").write_bytes(b"\xff\xfe\xfa")
        assert version_module._resolve_head_sha(tmp_path) is None

    def test_linked_worktree_reads_refs_from_the_common_dir(self, tmp_path: Path) -> None:
        main = _make_repo(
            tmp_path / "main",
            head="ref: refs/heads/main\n",
            refs={"refs/heads/feat": SHA_B},
            packed=f"{SHA_A} refs/heads/packed-only\n",
        )
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        (gitdir / "HEAD").write_text("ref: refs/heads/feat\n", encoding="utf-8")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

        assert version_module._resolve_head_sha(worktree) == SHA_B

        (gitdir / "HEAD").write_text("ref: refs/heads/packed-only\n", encoding="utf-8")
        assert version_module._resolve_head_sha(worktree) == SHA_A

    def test_worktree_pointer_file_without_gitdir_is_none(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("garbage\n", encoding="utf-8")
        assert version_module._resolve_head_sha(tmp_path) is None


class TestCodeStaleness:
    @pytest.fixture(autouse=True)
    def _fresh_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(version_module, "_SOURCE_DIR", tmp_path)
        monkeypatch.setattr(version_module, "_startup_code_sha", None)
        monkeypatch.setattr(version_module, "_startup_code_sha_recorded", False)
        monkeypatch.setattr(version_module, "_current_code_sha_cache", None)
        self.root = tmp_path
        self.now = [1000.0]
        monkeypatch.setattr(version_module.time, "monotonic", lambda: self.now[0])

    def _move_head(self, sha: str) -> None:
        (self.root / ".git" / "HEAD").write_text(sha + "\n", encoding="utf-8")
        self.now[0] += version_module._CODE_SHA_TTL_SECONDS + 1

    def test_unchanged_checkout_is_not_stale(self) -> None:
        _make_repo(self.root, head=SHA_A + "\n")
        assert version_module.record_startup_code_sha() == SHA_A
        staleness = version_module.get_code_staleness()
        assert staleness == version_module.CodeStaleness(SHA_A, SHA_A, False)

    def test_moved_checkout_is_stale(self) -> None:
        _make_repo(self.root, head=SHA_A + "\n")
        version_module.record_startup_code_sha()
        self._move_head(SHA_B)
        staleness = version_module.get_code_staleness()
        assert staleness == version_module.CodeStaleness(SHA_A, SHA_B, True)

    def test_returning_to_the_startup_sha_clears_the_flag(self) -> None:
        _make_repo(self.root, head=SHA_A + "\n")
        version_module.record_startup_code_sha()
        self._move_head(SHA_B)
        assert version_module.get_code_staleness().restart_required is True
        self._move_head(SHA_A)
        assert version_module.get_code_staleness().restart_required is False

    def test_install_without_git_is_never_stale(self) -> None:
        assert version_module.record_startup_code_sha() is None
        assert version_module.get_code_staleness() == version_module.CodeStaleness(None, None, False)

    def test_unknown_startup_sha_is_never_stale(self) -> None:
        version_module.record_startup_code_sha()
        _make_repo(self.root, head=SHA_B + "\n")
        self.now[0] += version_module._CODE_SHA_TTL_SECONDS + 1
        assert version_module.get_code_staleness() == version_module.CodeStaleness(None, SHA_B, False)

    def test_unreadable_head_is_never_stale(self) -> None:
        _make_repo(self.root, head=SHA_A + "\n")
        version_module.record_startup_code_sha()
        self._move_head("")
        assert version_module.get_code_staleness() == version_module.CodeStaleness(SHA_A, None, False)

    def test_first_recorded_startup_sha_wins(self) -> None:
        _make_repo(self.root, head=SHA_A + "\n")
        version_module.record_startup_code_sha()
        self._move_head(SHA_B)
        assert version_module.record_startup_code_sha() == SHA_A
        assert version_module.get_code_staleness().startup_sha == SHA_A

    def test_current_sha_is_cached_within_the_ttl_and_refreshed_after(self) -> None:
        _make_repo(self.root, head=SHA_A + "\n")
        version_module.record_startup_code_sha()
        (self.root / ".git" / "HEAD").write_text(SHA_B + "\n", encoding="utf-8")
        assert version_module.get_code_staleness().current_sha == SHA_A
        self.now[0] += version_module._CODE_SHA_TTL_SECONDS + 1
        assert version_module.get_code_staleness().current_sha == SHA_B

    def test_a_failed_read_does_not_outlive_the_ttl(self) -> None:
        _make_repo(self.root, head=SHA_A + "\n")
        version_module.record_startup_code_sha()
        self._move_head("")
        assert version_module.get_code_staleness().current_sha is None
        self._move_head(SHA_A)
        assert version_module.get_code_staleness().current_sha == SHA_A
