"""Tests for architectural import boundaries.

Ensures that lower-layer modules (sova/core/, sova/roles/) do not import
from higher-layer modules (sova/dashboard/) at module scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _collect_python_files(root: Path) -> list[Path]:
    """Collect all .py files under root, excluding __pycache__."""
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _extract_dashboard_imports(filepath: Path) -> list[tuple[int, str]]:
    """Return (line_number, import_string) for dashboard imports in a file.

    Detects both ``from sova.dashboard`` and ``import sova.dashboard`` forms
    at any nesting level (module scope, functions, methods).
    Raises on unparseable files so the caller can report them.
    """
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("sova.dashboard"):
                violations.append((node.lineno, f"from {node.module} import ..."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sova.dashboard"):
                    violations.append((node.lineno, f"import {alias.name}"))

    return violations


class TestCoreDoesNotImportDashboard:
    """sova/core/ must not import from sova/dashboard/ at module scope."""

    @pytest.fixture
    def core_files(self) -> list[Path]:
        repo = Path(__file__).parent.parent
        return _collect_python_files(repo / "sova" / "core")

    def test_no_dashboard_imports_in_core(self, core_files: list[Path]) -> None:
        all_violations: list[str] = []
        unparseable: list[str] = []
        for filepath in core_files:
            try:
                violations = _extract_dashboard_imports(filepath)
            except (SyntaxError, UnicodeDecodeError):
                unparseable.append(str(filepath))
                continue
            for line, import_str in violations:
                rel = filepath.relative_to(filepath.parent.parent.parent)
                all_violations.append(f"  {rel}:{line}: {import_str}")

        assert not unparseable, f"Could not parse files (check skipped): {unparseable}"
        assert not all_violations, "sova/core/ must not import from sova/dashboard/:\n" + "\n".join(all_violations)


class TestRolesDoesNotImportDashboardSpecService:
    """sova/roles/ must not import spec_service from sova/dashboard/ at module scope."""

    @pytest.fixture
    def roles_files(self) -> list[Path]:
        repo = Path(__file__).parent.parent
        return _collect_python_files(repo / "sova" / "roles")

    def test_no_spec_service_imports_in_roles(self, roles_files: list[Path]) -> None:
        all_violations: list[str] = []
        unparseable: list[str] = []
        for filepath in roles_files:
            try:
                violations = _extract_dashboard_imports(filepath)
            except (SyntaxError, UnicodeDecodeError):
                unparseable.append(str(filepath))
                continue
            spec_violations = [(line, imp) for line, imp in violations if "spec_service" in imp]
            for line, import_str in spec_violations:
                rel = filepath.relative_to(filepath.parent.parent.parent)
                all_violations.append(f"  {rel}:{line}: {import_str}")

        assert not unparseable, f"Could not parse files (check skipped): {unparseable}"
        assert not all_violations, "sova/roles/ must not import spec_service from sova/dashboard/:\n" + "\n".join(
            all_violations
        )


class TestCoreSpecUtils:
    """Verify sova.core.spec_utils exports work correctly."""

    def test_find_spec_file_returns_none_for_missing_dir(self, tmp_path: Path) -> None:
        from sova.core.spec_utils import find_spec_file

        result = find_spec_file("42", tmp_path)
        assert result is None

    def test_find_spec_file_matches_github_pattern(self, tmp_path: Path) -> None:
        from sova.core.spec_utils import find_spec_file

        specs = tmp_path / ".claude" / "specs"
        specs.mkdir(parents=True)
        spec_file = specs / "42-my-feature.md"
        spec_file.write_text("# Spec\n")

        result = find_spec_file("42", tmp_path)
        assert result == spec_file

    def test_find_spec_file_matches_jira_pattern(self, tmp_path: Path) -> None:
        from sova.core.spec_utils import find_spec_file

        specs = tmp_path / ".claude" / "specs"
        specs.mkdir(parents=True)
        spec_file = specs / "PROJ-42-my-feature.md"
        spec_file.write_text("# Spec\n")

        result = find_spec_file("42", tmp_path)
        assert result == spec_file

    def test_find_spec_file_disambiguates_github_and_jira(self, tmp_path: Path) -> None:
        """GitHub pattern (42-slug) takes priority over JIRA (PROJ-42-slug) when both exist."""
        from sova.core.spec_utils import find_spec_file

        specs = tmp_path / ".claude" / "specs"
        specs.mkdir(parents=True)
        github_file = specs / "42-github-feature.md"
        jira_file = specs / "PROJ-42-jira-feature.md"
        github_file.write_text("# GitHub spec\n")
        jira_file.write_text("# JIRA spec\n")

        result = find_spec_file("42", tmp_path)
        assert result == github_file

    def test_find_spec_file_no_false_positive_on_number_in_prefix(self, tmp_path: Path) -> None:
        """A file like '1-42-test.md' should not match issue 42 (42 is not the first segment)."""
        from sova.core.spec_utils import find_spec_file

        specs = tmp_path / ".claude" / "specs"
        specs.mkdir(parents=True)
        wrong_file = specs / "1-42-overlapping.md"
        wrong_file.write_text("# Spec\n")

        result = find_spec_file("42", tmp_path)
        assert result is None

    def test_read_spec_returns_parsed_dict(self, tmp_path: Path) -> None:
        from sova.core.spec_utils import read_spec

        specs = tmp_path / ".claude" / "specs"
        specs.mkdir(parents=True)
        spec_file = specs / "42-test.md"
        spec_file.write_text(
            "# Spec: Test Feature\n\n**Status**: draft\n**Complexity**: moderate\n**Created**: 2026-01-01\n"
        )

        result = read_spec("42", tmp_path)
        assert result is not None
        assert result["status"] == "draft"
        assert result["complexity"] == "moderate"
        assert result["title"] == "Test Feature"

    def test_read_spec_returns_none_for_missing(self, tmp_path: Path) -> None:
        from sova.core.spec_utils import read_spec

        result = read_spec("999", tmp_path)
        assert result is None

    def test_read_spec_returns_none_for_unreadable_file(self, tmp_path: Path) -> None:
        from sova.core.spec_utils import read_spec

        specs = tmp_path / ".claude" / "specs"
        specs.mkdir(parents=True)
        spec_file = specs / "42-bad.md"
        spec_file.write_bytes(b"\xff\xfe" + b"\x80" * 100)

        result = read_spec("42", tmp_path)
        assert result is None

    def test_extract_open_questions(self) -> None:
        from sova.core.spec_utils import _extract_open_questions

        text = "## Open Questions\n\n- Should we use X or Y?\n- What about Z?\n"
        questions = _extract_open_questions(text)
        assert len(questions) == 2
        assert questions[0]["text"] == "Should we use X or Y?"

    def test_extract_open_questions_with_answers(self) -> None:
        from sova.core.spec_utils import _extract_open_questions

        text = "## Open Questions\n\n- Q: Should we use X? A: Yes, use X.\n"
        questions = _extract_open_questions(text)
        assert len(questions) == 1
        assert questions[0]["text"] == "Should we use X?"
        assert questions[0]["answer"] == "Yes, use X."

    def test_extract_open_questions_omitted(self) -> None:
        from sova.core.spec_utils import _extract_open_questions

        text = "## Open Questions\n\n(Omit if no open questions.)\n"
        questions = _extract_open_questions(text)
        assert questions == []

    def test_reexport_from_spec_service(self) -> None:
        """spec_service re-exports core functions for backward compatibility."""
        from sova.core.spec_utils import (
            _extract_open_questions as core_eoq,
        )
        from sova.core.spec_utils import (
            _parse_spec as core_ps,
        )
        from sova.core.spec_utils import (
            _specs_dir as core_sd,
        )
        from sova.core.spec_utils import (
            find_spec_file as core_fsf,
        )
        from sova.core.spec_utils import (
            read_spec as core_rs,
        )
        from sova.dashboard.services.spec_service import (
            _extract_open_questions,
            _parse_spec,
            _specs_dir,
            find_spec_file,
            read_spec,
        )

        assert find_spec_file is core_fsf
        assert read_spec is core_rs
        assert _parse_spec is core_ps
        assert _extract_open_questions is core_eoq
        assert _specs_dir is core_sd
