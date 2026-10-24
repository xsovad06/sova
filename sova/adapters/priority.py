"""Priority registry: single source of truth for issue priority levels.

Leaf module (zero intra-sova imports) so both GitHub and Jira adapters, and
every downstream consumer (dependency graph, queue, dashboard templates/JS),
can import it without creating a cycle.
"""

from __future__ import annotations

from enum import StrEnum

_LABEL_PREFIX = "priority:"


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
    def from_jira(cls, jira_priority_name: str | None) -> Priority:
        """Map a Jira native priority field name to a Priority level.

        Jira's priority scheme is configurable per instance, so this covers
        the common defaults (Highest/Blocker through Lowest/Trivial). A
        None, empty, or unrecognized name resolves to UNDEFINED rather than
        raising.
        """
        if not jira_priority_name:
            return cls.UNDEFINED
        return _JIRA_ALIASES.get(jira_priority_name.strip().lower(), cls.UNDEFINED)

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
}
