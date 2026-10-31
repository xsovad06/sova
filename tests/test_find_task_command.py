"""Content checks for the canonical /find-task command's BLOCKED write-back (issue #1228).

The command is prose an agent follows, so the only mechanical guard on its
recording rules is pinning the text: a dependency block records nothing for
blocking (the graph reports it live), a hold records `agent:on-hold`, and
`agent:human-only` is reserved for work only a human can do. The rendered
copies under `.claude/commands/` and `.agents/skills/` are guarded separately
by `tests/test_skill_render_drift.py`.
"""

from __future__ import annotations

import re

from sova.commands.catalog import get_canonical_dir

_TEXT = (get_canonical_dir() / "find-task.md").read_text(encoding="utf-8")


def _section(heading: str) -> str:
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^### |^## )", _TEXT, re.MULTILINE | re.DOTALL)
    assert match is not None, f"no '### {heading}' section in find-task.md"
    return match.group(1)


def _bullet(section: str, prefix: str) -> str:
    match = re.search(rf"^- {re.escape(prefix)}.*$", section, re.MULTILINE)
    assert match is not None, f"no '- {prefix}' bullet"
    return match.group(0)


class TestClassification:
    _SECTION = _section("5. Classify Into One Mode")

    def test_blocked_names_its_three_causes(self) -> None:
        for cause in ("**Dependency**", "**Hold**", "**Human work**"):
            assert cause in self._SECTION

    def test_skip_list_match_is_human_work_and_wins_over_a_hold(self) -> None:
        human_work = re.search(r"\*\*Human work\*\*.*", self._SECTION)
        assert human_work is not None
        assert "triage.skip_title_prefixes" in human_work.group(0)
        assert "wins over a hold" in human_work.group(0)

    def test_underlying_mode_is_defined_for_blocked_tickets(self) -> None:
        underlying = _bullet(self._SECTION, "**Underlying mode**")
        assert "AUTO or INTERACTIVE" in underlying
        assert "INTERACTIVE here too" in underlying

    def test_failed_blocker_lookup_stays_interactive_for_a_held_ticket(self) -> None:
        step3 = _section("3. Read Each Ticket in Full")
        assert "A held ticket stays BLOCKED, and this applies to its underlying mode" in step3

    def test_existing_on_hold_label_counts_as_a_hold(self) -> None:
        hold = re.search(r"\*\*Hold\*\*.*", self._SECTION)
        assert hold is not None
        assert "an `agent:on-hold` label already on the ticket" in hold.group(0)
        assert "no later comment lifts" in hold.group(0)


class TestWriteBack:
    _SECTION = _section("7. Write Back Suitability")

    def test_dependency_block_records_nothing_for_blocking(self) -> None:
        bullet = _bullet(self._SECTION, "BLOCKED, dependency only")
        assert "nothing for blocking" in bullet
        assert "`agent:on-hold`" not in bullet
        assert "`agent:human-only`" not in bullet

    def test_hold_records_the_underlying_mode_plus_the_on_hold_label(self) -> None:
        bullet = _bullet(self._SECTION, "BLOCKED, hold")
        assert "underlying mode" in bullet
        assert "`agent:on-hold`" in bullet
        assert "`agent:human-only`" not in bullet

    def test_unassigned_hold_records_interactive_so_the_supervisor_cannot_start_it(self) -> None:
        bullet = _bullet(self._SECTION, "BLOCKED, hold")
        assert "which gets no label, records `interactive` instead of its underlying mode" in bullet
        assert "`interactive` for an unassigned held ticket" in self._SECTION

    def test_on_hold_is_added_in_the_same_call_as_the_mode_label(self) -> None:
        assert "add it for a hold in the same edit call as the mode label" in self._SECTION

    def test_human_only_is_written_only_for_human_work(self) -> None:
        bullets = [line for line in self._SECTION.splitlines() if line.startswith("- ")]
        mapping = [line for line in bullets if "`agent:human-only`" in line]
        assert len(mapping) == 1
        assert mapping[0].startswith("- BLOCKED, human work")

    def test_on_hold_is_removed_only_when_present(self) -> None:
        assert "remove it only if the ticket currently has it and is no longer held" in self._SECTION

    def test_on_hold_is_not_a_fourth_suitability_shorthand(self) -> None:
        assert "There is no local shorthand for a hold" in self._SECTION

    def test_label_creation_covers_on_hold_on_github_and_not_jira(self) -> None:
        assert "`agent:on-hold` needs no creation step there" in self._SECTION
        assert 'gh label create "agent:on-hold"' in self._SECTION
