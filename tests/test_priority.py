"""Tests for sova.adapters.priority: the Priority registry."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sova.adapters.priority import Priority
from sova.utils.files import read_text_or_none

# The four-level 'critical/high/medium/low' issue-priority list (no blocker,
# no undefined) that predated this registry. A reintroduction of this exact
# straggler shape, outside the registry itself, means a new call site is
# re-deriving its own priority table instead of reading sova/adapters/priority.py.
_STRAGGLER_PRIORITY_PATTERNS = (
    re.compile(r'Literal\[\s*"critical",\s*"high",\s*"medium",\s*"low"\s*\]'),
    re.compile(r'"critical"\s*\|\s*"high"\s*\|\s*"medium"\s*\|\s*"low"'),
)

# Same straggler shape in a JS/Jinja object literal: exactly the four keys
# 'critical', 'high', 'medium', 'low' in that order with nothing else in the
# literal (a six-key object that also has 'blocker'/'undefined' does not
# match, since those keys sit outside the matched span).
_STRAGGLER_JS_PATTERN = re.compile(
    r"\{\s*critical\s*:\s*[^,{}]+,\s*high\s*:\s*[^,{}]+,\s*medium\s*:\s*[^,{}]+,\s*low\s*:\s*[^,{}]+\s*\}",
    re.IGNORECASE,
)


class TestRank:
    def test_rank_order_most_to_least_urgent(self) -> None:
        assert Priority.BLOCKER.rank < Priority.CRITICAL.rank
        assert Priority.CRITICAL.rank < Priority.HIGH.rank
        assert Priority.HIGH.rank < Priority.MEDIUM.rank
        assert Priority.MEDIUM.rank < Priority.LOW.rank
        assert Priority.LOW.rank < Priority.UNDEFINED.rank

    def test_undefined_is_last_of_six(self) -> None:
        assert Priority.UNDEFINED.rank == 5
        assert len(Priority) == 6


class TestDisplayNameIconLabel:
    @pytest.mark.parametrize(
        "level,expected",
        [
            (Priority.BLOCKER, "Blocker"),
            (Priority.CRITICAL, "Critical"),
            (Priority.HIGH, "High"),
            (Priority.MEDIUM, "Medium"),
            (Priority.LOW, "Low"),
            (Priority.UNDEFINED, "Undefined"),
        ],
    )
    def test_display_name(self, level: Priority, expected: str) -> None:
        assert level.display_name == expected

    @pytest.mark.parametrize("level", list(Priority))
    def test_icon_matches_value(self, level: Priority) -> None:
        assert level.icon == level.value

    @pytest.mark.parametrize("level", list(Priority))
    def test_github_label_is_spaced_canonical_form(self, level: Priority) -> None:
        assert level.github_label == f"priority: {level.value}"


class TestFromLabels:
    def test_no_labels_resolves_undefined(self) -> None:
        assert Priority.from_labels([]) == Priority.UNDEFINED

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("priority: blocker", Priority.BLOCKER),
            ("priority: critical", Priority.CRITICAL),
            ("priority: high", Priority.HIGH),
            ("priority: medium", Priority.MEDIUM),
            ("priority: low", Priority.LOW),
            ("priority: undefined", Priority.UNDEFINED),
        ],
    )
    def test_each_level_parses(self, label: str, expected: Priority) -> None:
        assert Priority.from_labels([label]) == expected

    def test_compact_alias_form(self) -> None:
        assert Priority.from_labels(["priority:high"]) == Priority.HIGH

    def test_unrelated_labels_ignored(self) -> None:
        assert Priority.from_labels(["area: dashboard", "type: feature"]) == Priority.UNDEFINED

    def test_priority_among_other_labels(self) -> None:
        assert Priority.from_labels(["area: dashboard", "priority: high", "type: feature"]) == Priority.HIGH

    @pytest.mark.parametrize(
        "label",
        ["Priority: Blocker", "priority:  blocker", "PRIORITY: BLOCKER", "  priority: blocker  "],
    )
    def test_case_and_whitespace_insensitive(self, label: str) -> None:
        assert Priority.from_labels([label]) == Priority.BLOCKER

    def test_unknown_priority_value_resolves_undefined(self) -> None:
        assert Priority.from_labels(["priority: urgent"]) == Priority.UNDEFINED

    def test_multiple_priority_labels_picks_most_urgent(self) -> None:
        """A mislabeled issue with two priority labels resolves deterministically
        to the single most urgent match rather than raising or returning both."""
        assert Priority.from_labels(["priority: low", "priority: critical"]) == Priority.CRITICAL
        assert Priority.from_labels(["priority: critical", "priority: low"]) == Priority.CRITICAL

    def test_multiple_priority_labels_one_unknown_one_valid(self) -> None:
        assert Priority.from_labels(["priority: urgent", "priority: medium"]) == Priority.MEDIUM


class TestFromJira:
    def test_none_resolves_undefined(self) -> None:
        assert Priority.from_jira(None) == Priority.UNDEFINED

    def test_empty_string_resolves_undefined(self) -> None:
        assert Priority.from_jira("") == Priority.UNDEFINED

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Blocker", Priority.BLOCKER),
            ("Critical", Priority.CRITICAL),
            ("Highest", Priority.CRITICAL),
            ("Major", Priority.HIGH),
            ("High", Priority.HIGH),
            ("Normal", Priority.MEDIUM),
            ("Medium", Priority.MEDIUM),
            ("Minor", Priority.LOW),
            ("Low", Priority.LOW),
            ("Trivial", Priority.LOW),
            ("Lowest", Priority.LOW),
        ],
    )
    def test_known_jira_names(self, name: str, expected: Priority) -> None:
        assert Priority.from_jira(name) == expected

    def test_case_and_whitespace_insensitive(self) -> None:
        assert Priority.from_jira("  CRITICAL  ") == Priority.CRITICAL

    def test_unrecognized_name_resolves_undefined(self) -> None:
        assert Priority.from_jira("Showstopper") == Priority.UNDEFINED


class TestHasLabel:
    def test_no_labels(self) -> None:
        assert Priority.has_label([]) is False

    def test_unrelated_labels(self) -> None:
        assert Priority.has_label(["type: feature", "area: dashboard"]) is False

    def test_recognized_priority_label(self) -> None:
        assert Priority.has_label(["priority: high"]) is True

    def test_unrecognized_priority_label_still_counts(self) -> None:
        """An unrecognized value still counts as 'has a priority label' so a
        default is never stacked alongside an existing, misspelled one."""
        assert Priority.has_label(["priority: urgent"]) is True

    def test_compact_form_counts(self) -> None:
        assert Priority.has_label(["priority:high"]) is True

    def test_bare_prefix_with_no_value_does_not_count(self) -> None:
        """An issue carrying a meaningless bare 'priority:' prefix (no value)
        has nothing to treat as an existing priority, so a default should
        still be added."""
        assert Priority.has_label(["priority:"]) is False
        assert Priority.has_label(["priority: "]) is False


class TestNoStragglerPriorityTables:
    """Guard against a new call site re-deriving its own 4-level priority
    table instead of reading Priority. Excludes this module's own fixtures
    (the patterns above) and the registry itself, which legitimately defines
    the level names; review-finding severity ratings (reviewer.py,
    _review_format.py, address_review.py) are a distinct 'severity' domain,
    not issue priority, and use their own int-scored shapes rather than
    this pattern.
    """

    def test_no_hardcoded_four_level_priority_list(self) -> None:
        repo_root = Path(__file__).parent.parent
        offenders = []
        for path in (repo_root / "sova").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = read_text_or_none(path)
            if text is None:
                continue
            for pattern in _STRAGGLER_PRIORITY_PATTERNS:
                if pattern.search(text):
                    offenders.append(str(path.relative_to(repo_root)))

        # The duplicate tables this registry replaced mostly lived in the
        # frontend (app.js's icon map, supervisor.html's sort order), not
        # Python, so a template or JS straggler needs its own scan.
        dashboard_root = repo_root / "sova" / "dashboard"
        frontend_paths = list(dashboard_root.rglob("*.html")) + list(dashboard_root.rglob("*.js"))
        for path in frontend_paths:
            if "__pycache__" in path.parts:
                continue
            text = read_text_or_none(path)
            if text is None:
                continue
            if _STRAGGLER_JS_PATTERN.search(text):
                offenders.append(str(path.relative_to(repo_root)))

        assert not offenders, f"Straggler priority table(s) found outside the registry: {offenders}"

    def test_supervisor_priority_config_keys_subset_of_registry(self) -> None:
        """supervisor.html's hand-kept PRIORITY_CONFIG color table must never
        name a level the shared registry doesn't have, so a level rename or
        removal in Priority is caught here instead of silently leaving a
        dead or colorless key.
        """
        repo_root = Path(__file__).parent.parent
        path = repo_root / "sova" / "dashboard" / "templates" / "supervisor.html"
        text = read_text_or_none(path)
        assert text is not None
        match = re.search(r"const PRIORITY_CONFIG\s*=\s*\{(.*?)\n\};", text, re.DOTALL)
        assert match, "PRIORITY_CONFIG not found in supervisor.html"
        keys = set(re.findall(r"(\w+)\s*:\s*\{", match.group(1)))
        assert keys, "no keys parsed out of PRIORITY_CONFIG"
        assert keys <= {p.value for p in Priority}


class TestLeafModule:
    def test_priority_module_has_no_intra_sova_imports(self) -> None:
        """priority.py is deliberately a leaf module (see its own docstring)
        so every tracker adapter and downstream consumer can import it
        without creating a cycle. A future `from sova...` import here would
        silently reintroduce exactly the cycle it exists to prevent.
        """
        path = Path(__file__).parent.parent / "sova" / "adapters" / "priority.py"
        text = read_text_or_none(path)
        assert text is not None
        offenders = [line for line in text.splitlines() if re.match(r"^\s*(from sova[. ]|import sova)", line)]
        assert not offenders, f"priority.py must have zero intra-sova imports, found: {offenders}"
