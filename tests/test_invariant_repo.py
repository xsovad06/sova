"""Tests for the copied InvariantRepo fixtures in tests/conftest.py (issue #1191)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.conftest import InvariantRepo

_builds: list[str] = []


def _land_feature(repo: InvariantRepo, name: str) -> None:
    _builds.append(name)
    repo.write(f"{name}.py", "VALUE = 1\n")
    repo.land_on_main(f"feat(core): add {name}")


def test_a_copy_pushes_to_its_own_origin(invariant_repo: InvariantRepo, tmp_path: Path) -> None:
    other = invariant_repo.copy_to(tmp_path / "other")
    other.write("extra.txt", "x\n")
    other.land_on_main("chore(docs): extra")
    pushed = other.git("rev-parse", "HEAD")

    assert other.git("rev-parse", "origin/main") == pushed
    assert invariant_repo.git("ls-remote", str(invariant_repo.origin), "main").split()[0] != pushed


def test_a_copy_is_clean_to_git_plumbing(invariant_repo: InvariantRepo, tmp_path: Path) -> None:
    """`git diff-index --quiet HEAD` is the usual clean-tree idiom; a stale copied index would fail it."""
    other = invariant_repo.copy_to(tmp_path / "other")

    other.git("diff-index", "--quiet", "HEAD")
    other.git("diff-files", "--quiet")


def test_a_history_is_built_once_per_module_and_copied(
    tmp_path: Path, invariant_history: Callable[..., InvariantRepo]
) -> None:
    first = invariant_history(tmp_path / "first", _land_feature, "alpha")
    second = invariant_history(tmp_path / "second", _land_feature, "alpha")
    second.write("alpha.py", "VALUE = 2\n")
    second.commit("fix(core): change alpha")

    assert _builds == ["alpha"]
    assert (first.dir / "alpha.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert first.git("rev-parse", "HEAD") == first.git("rev-parse", "origin/main")
