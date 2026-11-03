"""The ::sova-phase:: markers in command files match sova.llm.phase_usage.COMMAND_PHASES (issue #1336).

Phase names are compared across runs, so a command edit that drops, renames or
reorders a marker would silently change what every later report measures.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sova.llm.phase_usage import COMMAND_PHASES, find_phase_marker

_REPO = Path(__file__).resolve().parent.parent
_CANONICAL = _REPO / "sova" / "commands" / "data" / "commands"
_FENCE = re.compile(r"^(\s*)```(\w*)\s*$")


def _markers_with_fence(text: str) -> list[tuple[str, str | None]]:
    """Every marker line in *text*, paired with the language of the fence it sits in (None outside one)."""
    found = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match:
            fence = None if fence is not None else match.group(2)
            continue
        phase = find_phase_marker(line)
        if phase is not None:
            found.append((phase, fence))
    return found


@pytest.mark.parametrize("command", sorted(COMMAND_PHASES))
def test_canonical_command_prints_exactly_its_phases_in_order(command: str) -> None:
    text = (_CANONICAL / f"{command}.md").read_text(encoding="utf-8")

    assert [phase for phase, _ in _markers_with_fence(text)] == list(COMMAND_PHASES[command])


@pytest.mark.parametrize("command", sorted(COMMAND_PHASES))
def test_every_marker_sits_in_a_bash_block(command: str) -> None:
    """A marker outside a bash fence is prose the agent never runs, so the phase would never switch."""
    text = (_CANONICAL / f"{command}.md").read_text(encoding="utf-8")

    assert all(fence == "bash" for _, fence in _markers_with_fence(text))


@pytest.mark.parametrize(
    ("command", "rendered"),
    [
        (command, path)
        for command in sorted(COMMAND_PHASES)
        for path in (f".claude/commands/{command}.md", f".agents/skills/sova-{command}/SKILL.md")
    ],
)
def test_rendered_copies_carry_the_same_markers(command: str, rendered: str) -> None:
    text = (_REPO / rendered).read_text(encoding="utf-8")

    assert [phase for phase, _ in _markers_with_fence(text)] == list(COMMAND_PHASES[command])


def test_no_other_command_prints_a_marker() -> None:
    """A marker in a command missing from COMMAND_PHASES would record a phase no report knows about."""
    stray = {
        path.name: phases
        for path in sorted(_CANONICAL.glob("*.md"))
        if path.stem not in COMMAND_PHASES
        and (phases := [phase for phase, _ in _markers_with_fence(path.read_text(encoding="utf-8"))])
    }

    assert stray == {}


def test_integrate_pr_phase_one_marker_precedes_every_phase_one_command() -> None:
    """When $ARGUMENTS supplies the PR number, step 1 runs nothing; Phase 1's first block must still be the marker."""
    text = (_CANONICAL / "integrate-pr.md").read_text(encoding="utf-8")
    phase_one = text[text.index("### Phase 1:") : text.index("### Phase 2:")]
    first_block = phase_one[phase_one.index("```bash") :].split("```", 2)[1]

    assert [line.strip() for line in first_block.splitlines()[1:] if line.strip()] == ['echo "::sova-phase::identify"']
