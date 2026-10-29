"""Tests for sova.config.context: project context resolution."""

from __future__ import annotations

from pathlib import Path

from sova.config.context import (
    clear_project_context,
    resolve_project_dir,
    resolve_project_dir_or_none,
    set_project_context,
)


class TestResolveProjectDir:
    def teardown_method(self) -> None:
        clear_project_context()

    def test_explicit_argument_wins(self) -> None:
        explicit = Path("/explicit")
        set_project_context(Path("/ambient"), "ambient")
        assert resolve_project_dir(explicit) == explicit

    def test_contextvar_before_registry_and_default(self) -> None:
        set_project_context(Path("/ambient"), "ambient")
        result = resolve_project_dir(
            daemon_registry={"/registry": object()},
            default_project_dir=Path("/default"),
        )
        assert result == Path("/ambient")

    def test_single_entry_registry_used_when_no_contextvar(self) -> None:
        result = resolve_project_dir(daemon_registry={"/registry": object()})
        assert result == Path("/registry")

    def test_default_project_dir_used_when_registry_ambiguous(self) -> None:
        result = resolve_project_dir(
            daemon_registry={"/a": object(), "/b": object()},
            default_project_dir=Path("/default"),
        )
        assert result == Path("/default")

    def test_falls_back_to_cwd(self) -> None:
        assert resolve_project_dir() == Path.cwd().resolve()

    def test_or_none_variant_returns_none_instead_of_the_cwd(self) -> None:
        assert resolve_project_dir_or_none() is None

    def test_or_none_variant_runs_the_same_chain(self) -> None:
        explicit = Path("/explicit")
        set_project_context(Path("/ambient"), "ambient")
        assert resolve_project_dir_or_none(explicit) == explicit
        assert resolve_project_dir_or_none(default_project_dir=Path("/default")) == Path("/ambient")
        clear_project_context()
        assert resolve_project_dir_or_none(daemon_registry={"/registry": object()}) == Path("/registry")
        assert resolve_project_dir_or_none(daemon_registry={"/a": object(), "/b": object()}) is None
        assert resolve_project_dir_or_none(default_project_dir=Path("/default")) == Path("/default")


class TestNoHandRolledFallbackChain:
    """Every project-dir fallback goes through resolve_project_dir(), so no two callers can disagree."""

    def test_no_module_chains_get_project_dir_with_or(self) -> None:
        import re

        pattern = re.compile(r"get_project_dir\(\)\s+or\b")
        sova_root = Path(__file__).resolve().parent.parent / "sova"
        offenders = [
            f"{path.relative_to(sova_root.parent)}:{lineno}"
            for path in sorted(sova_root.rglob("*.py"))
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if pattern.search(line)
        ]

        assert offenders == [], (
            "use resolve_project_dir() or resolve_project_dir_or_none() from sova.config.context "
            f"instead of chaining get_project_dir() with `or`: {offenders}"
        )
