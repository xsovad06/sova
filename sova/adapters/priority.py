"""Priority registry: single source of truth for issue priority levels.

Leaf module (zero intra-sova imports) so both GitHub and Jira adapters, and
every downstream consumer (dependency graph, queue, dashboard templates/JS),
can import it without creating a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum

_LABEL_PREFIX = "priority:"

# Name variants real Jira instances wrap around a plain level name: a 'P2 - ' rank prefix and a
# ' (migrated)' suffix left by a priority-scheme migration. Both are stripped before the alias lookup.
_JIRA_RANK_PREFIX_RE = re.compile(r"^p\d+\s*[-:]\s*")
_JIRA_MIGRATED_SUFFIX = "(migrated)"


class Priority(StrEnum):
    """Issue priority level, declared most to least urgent.

    Declaration order doubles as rank order (see ``rank``): ``UNDEFINED``
    sorts after ``LOW`` rather than being treated as medium or as having no
    rank penalty, since an unlabelled issue should never silently outrank
    every labelled one.
    """

    BLOCKER = "blocker"
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNDEFINED = "undefined"

    @property
    def rank(self) -> int:
        """Sort key for priority ordering: lower sorts first (more urgent)."""
        return _RANK[self]

    @property
    def display_name(self) -> str:
        return self.value.capitalize()

    @property
    def icon(self) -> str:
        """Icon filename stem under ``sova/dashboard/static/priority/``."""
        return self.value

    @property
    def github_label(self) -> str:
        """Canonical spaced label form: every label-writing path emits this, never the compact alias."""
        return f"priority: {self.value}"

    @classmethod
    def from_labels(cls, labels: list[str]) -> Priority:
        """Parse the priority level out of a list of tracker labels.

        Recognises 'priority: <level>' and the compact 'priority:<level>'
        alias, case- and whitespace-insensitive. Multiple priority labels
        (e.g. a human mislabeling) resolve to the single most urgent match
        rather than raising or returning more than one. An absent or
        unrecognized priority label resolves to UNDEFINED.
        """
        matches = []
        for label in labels:
            normalized = label.strip().lower()
            if not normalized.startswith(_LABEL_PREFIX):
                continue
            try:
                matches.append(cls(normalized.removeprefix(_LABEL_PREFIX).strip()))
            except ValueError:
                continue
        if not matches:
            return cls.UNDEFINED
        return min(matches, key=lambda p: p.rank)

    @classmethod
    def from_jira(cls, jira_priority_name: str | None, overrides: Mapping[str, str] | None = None) -> Priority:
        """Map a Jira native priority field name to a Priority level.

        A None, empty, or unrecognized name resolves to UNDEFINED rather than
        raising; ``lookup_jira`` tells an unrecognized name apart.
        """
        found = cls.lookup_jira(jira_priority_name, overrides)
        return cls.UNDEFINED if found is None else found

    @classmethod
    def lookup_jira(cls, jira_priority_name: str | None, overrides: Mapping[str, str] | None = None) -> Priority | None:
        """Map a Jira priority name to a level, or None when the name is unrecognized.

        Jira's priority scheme is configurable per instance, so the built-in
        table covers the common defaults plus customized schemes
        ('P1 - Critical', 'Critical (migrated)', MoSCoW). ``overrides`` (the
        operator's ``task_source.jira_priority_mapping``, Jira name to level
        value) is checked first and wins over the built-in table. Matching is
        case-insensitive and trimmed, and a 'P<n> - ' prefix and ' (migrated)'
        suffix are stripped. A None or empty name is "no priority set", not
        unrecognized, so it resolves to UNDEFINED.
        """
        raw = (jira_priority_name or "").strip().lower()
        if not raw:
            return cls.UNDEFINED
        normalized = _JIRA_RANK_PREFIX_RE.sub("", raw.removesuffix(_JIRA_MIGRATED_SUFFIX).rstrip()).strip()
        for name, level in (overrides or {}).items():
            if name.strip().lower() in (raw, normalized):
                try:
                    return cls(level.strip().lower())
                except ValueError:
                    continue
        return _JIRA_ALIASES.get(raw, _JIRA_ALIASES.get(normalized))

    @classmethod
    def has_label(cls, labels: list[str]) -> bool:
        """Return True if any label in the list looks like a priority label.

        Used to decide whether a default 'priority: undefined' label needs
        to be added on issue creation; checks the 'priority:' prefix alone
        (not whether the value is recognized) so an issue is never given a
        second priority label alongside an existing, unrecognized one. A
        bare prefix with no value after it does not count: there is nothing
        there to treat as an existing priority.
        """
        normalized = [label.strip().lower() for label in labels]
        return any(n.startswith(_LABEL_PREFIX) and n.removeprefix(_LABEL_PREFIX).strip() for n in normalized)


_RANK: dict[Priority, int] = {p: i for i, p in enumerate(Priority)}

_JIRA_ALIASES: dict[str, Priority] = {
    "blocker": Priority.BLOCKER,
    "critical": Priority.CRITICAL,
    "highest": Priority.CRITICAL,
    "major": Priority.HIGH,
    "high": Priority.HIGH,
    "normal": Priority.MEDIUM,
    "medium": Priority.MEDIUM,
    "minor": Priority.LOW,
    "low": Priority.LOW,
    "trivial": Priority.LOW,
    "lowest": Priority.LOW,
    "urgent": Priority.CRITICAL,
    "must have": Priority.HIGH,
    "should have": Priority.MEDIUM,
    "could have": Priority.LOW,
    "won't have": Priority.LOW,
    "won\u2019t have": Priority.LOW,
    "optional": Priority.LOW,
    "unprioritized": Priority.UNDEFINED,
}
