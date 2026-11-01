"""Tests for the `agent:on-hold` label: a catalog entry that is deliberately not a task state."""

from __future__ import annotations

from sova.adapters import github, jira
from sova.adapters.label_catalog import LABEL_CATALOG, REQUIRED_LABELS

_ON_HOLD = "agent:on-hold"


class TestOnHoldLabelCatalog:
    def test_on_hold_is_a_required_label(self) -> None:
        """Settings reads the shared catalog, so registering it here makes the Settings page create it."""
        assert _ON_HOLD in {label["name"] for label in REQUIRED_LABELS}

    def test_on_hold_has_a_catalog_entry_with_color_and_description(self) -> None:
        entry = LABEL_CATALOG[_ON_HOLD]
        assert entry["color"]
        assert entry["description"]

    def test_required_label_names_are_unique(self) -> None:
        names = [label["name"] for label in REQUIRED_LABELS]
        assert len(names) == len(set(names))

    def test_github_may_auto_create_it_on_a_not_found_retry(self) -> None:
        assert _ON_HOLD in github._AUTO_CREATABLE_LABELS


class TestOnHoldIsNotAStateLabel:
    """A state label would change the adapter-derived `TaskState` of a held ticket."""

    def test_not_a_github_state_label(self) -> None:
        assert _ON_HOLD not in github._STATE_LABELS.values()
        assert _ON_HOLD not in github._LABEL_TO_STATE

    def test_not_a_jira_state_label(self) -> None:
        assert _ON_HOLD not in jira._STATE_LABELS.values()
        assert _ON_HOLD not in jira._LABEL_TO_STATE
