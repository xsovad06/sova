"""Shrink-only size caps for the distributable commands and skills (issue #1363).

A command or skill is loaded whole into an agent's context when it runs, so a
file that grows without a limit costs every run. Each file over the default
limit holds a cap in `_COMMAND_CAPS` (or `_SKILL_CAPS`). A file over its cap
fails, and so does a capped file more than `_SLACK_CHARS` under its cap: every
cut must lower its cap in the same diff, so the cap ratchets down and a later
PR cannot quietly re-grow the file into the room the cut freed.

A PR that adds text to a capped file has to offset the text within the cap or
raise the cap with a stated reason in the same diff; the test enforces the first
half and review enforces the reason. This module does not re-measure rules,
guidelines or `CLAUDE.md`: `test_instruction_surface_budget.py` owns those.

Size is characters (`len()` of the UTF-8 decoded text), not bytes, so a
non-ASCII character is not overcounted.
"""

from __future__ import annotations

from pathlib import Path

from sova.utils.files import read_text_or_none

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMMANDS_DIR = _REPO_ROOT / "sova" / "commands" / "data" / "commands"
_SKILLS_DIR = _REPO_ROOT / "sova" / "commands" / "data" / "skills"

_DEFAULT_COMMAND_CAP = 12_000
_DEFAULT_SKILL_CAP = 8_000
# How far under its cap a capped file may sit before the cap must come down.
_SLACK_CHARS = 1_000

# Command file name -> cap. Only files over _DEFAULT_COMMAND_CAP belong here. Lower an entry in
# the same diff as any cut, and delete it once the file is at or under the default.
_COMMAND_CAPS: dict[str, int] = {
    "address-pr.md": 40_000,
    "find-task.md": 15_500,
    # Raised from the #1363 target of 24,000 by 2,500 for folding open issues before filing: the fold search,
    # the fold comment posted only after confirmation, and the separate `folded=` and `deferred=` tracker
    # fields that keep fold targets and cap deferrals out of the created-issue and disclosure paths.
    "health-audit.md": 26_500,
    # Issue #1321 raised this by 6,000 for the required-check name lookup in Phase 4 (fast path and poll):
    # the rulesets (every page), the matching against the registered checks, the required-only verdict, and
    # the union with the names `gh pr checks --required` reports (classic protection).
    # Phase 1b (SOVA verdict gate) added 2,400: a COMMENT review is never a GitHub block, so the merge path
    # asks the shared resolver for a standing REVISE or BLOCK on the head (issue #1436).
    # Issue #1428 raised this by 700 for the Phase 5 `sova refresh-pr-body --only-if-unparseable --head` re-judge
    # of a body whose verdicts could not be parsed: the shell time budget (two LLM calls) and the exit codes.
    "integrate-pr.md": 67_700,
    # Issue #1321 raised this by 3,700 for the required-check name lookup in step 15: the rulesets (every page),
    # the matching against the registered checks, and the required-only verdict (every `--required` row decides).
    "pr.md": 36_000,
    # Issue #1383 took this over the default with the Test Coverage check that flags a diff adding far more
    # test lines than production lines, the main source of test-suite growth.
    "review.md": 12_400,
    "review-pr.md": 22_000,
}
# Skill directory name -> cap for its SKILL.md. Only skills over _DEFAULT_SKILL_CAP belong here.
_SKILL_CAPS: dict[str, int] = {
    # Raised by 1,000 for "Before Filing a New Issue": search the open issues for the files a new
    # issue would touch and fold into a match instead of filing, the main source of issue inflow.
    "issue-template": 9_000,
}


def _size(path: Path) -> int:
    text = read_text_or_none(path)
    assert text is not None, f"{path} is missing or not valid UTF-8"
    return len(text)


def check_caps(sizes: dict[str, int], caps: dict[str, int], default_cap: int, *, kind: str) -> list[str]:
    """Every way `sizes` (name -> measured chars) breaks the shrink-only rule against `caps`."""
    problems = []
    for name, size in sorted(sizes.items()):
        cap = caps.get(name)
        if cap is None:
            if size > default_cap:
                problems.append(
                    f"{kind} {name} is {size} chars, over the default cap of {default_cap}: cut it, or add a "
                    "cap entry for it in the same diff with a stated reason"
                )
        elif size <= default_cap:
            problems.append(
                f"{kind} {name} is {size} chars, at or under the default cap of {default_cap}: delete its cap entry"
            )
        elif size > cap:
            problems.append(
                f"{kind} {name} is {size} chars, over its cap of {cap}: cut it, or raise the cap with a stated reason"
            )
        elif cap - size > _SLACK_CHARS:
            problems.append(
                f"{kind} {name} is {size} chars, {cap - size} under its cap of {cap}: "
                f"lower the cap to at most {size + _SLACK_CHARS}"
            )
    problems.extend(
        f"{kind} {name} has a cap entry but no such file: remove the stale entry"
        for name in sorted(set(caps) - set(sizes))
    )
    return problems


def _command_sizes() -> dict[str, int]:
    return {path.name: _size(path) for path in sorted(_COMMANDS_DIR.glob("*.md"))}


def _skill_sizes(skills_dir: Path = _SKILLS_DIR) -> dict[str, int]:
    """SKILL.md size per skill directory. A directory with no SKILL.md is not a skill yet, so it is skipped."""
    return {path.parent.name: _size(path) for path in sorted(skills_dir.glob("*/SKILL.md"))}


def test_every_command_is_within_its_cap() -> None:
    sizes = _command_sizes()
    assert sizes, f"no commands found in {_COMMANDS_DIR}"
    problems = check_caps(sizes, _COMMAND_CAPS, _DEFAULT_COMMAND_CAP, kind="command")
    assert not problems, "\n".join(problems)


def test_every_skill_is_within_its_cap() -> None:
    sizes = _skill_sizes()
    assert sizes, f"no SKILL.md files found in {_SKILLS_DIR}"
    problems = check_caps(sizes, _SKILL_CAPS, _DEFAULT_SKILL_CAP, kind="skill")
    assert not problems, "\n".join(problems)


def test_skill_directory_without_skill_md_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "half-made-skill").mkdir()
    (tmp_path / "real-skill").mkdir()
    (tmp_path / "real-skill" / "SKILL.md").write_text("# skill\n", encoding="utf-8")

    assert _skill_sizes(tmp_path) == {"real-skill": len("# skill\n")}


class TestCheckCaps:
    """The rule itself, against synthetic sizes, so a regression in it cannot hide behind today's files."""

    def test_file_at_default_without_entry_passes(self) -> None:
        assert check_caps({"a.md": 12_000}, {}, 12_000, kind="command") == []

    def test_file_over_default_without_entry_fails_naming_size_and_default(self) -> None:
        (problem,) = check_caps({"a.md": 12_001}, {}, 12_000, kind="command")
        assert "a.md" in problem
        assert "12001" in problem
        assert "12000" in problem

    def test_entry_for_file_at_or_under_default_is_stale(self) -> None:
        assert check_caps({"a.md": 12_000}, {"a.md": 12_500}, 12_000, kind="command")
        assert check_caps({"a.md": 100}, {"a.md": 12_500}, 12_000, kind="command")

    def test_entry_for_missing_file_is_stale(self) -> None:
        (problem,) = check_caps({}, {"gone.md": 20_000}, 12_000, kind="command")
        assert "gone.md" in problem
        assert "no such file" in problem

    def test_file_over_its_cap_fails(self) -> None:
        (problem,) = check_caps({"a.md": 20_001}, {"a.md": 20_000}, 12_000, kind="command")
        assert "a.md" in problem
        assert "20001" in problem
        assert "20000" in problem

    def test_file_exactly_at_its_cap_passes(self) -> None:
        assert check_caps({"a.md": 20_000}, {"a.md": 20_000}, 12_000, kind="command") == []

    def test_cap_exactly_slack_above_size_passes(self) -> None:
        assert check_caps({"a.md": 20_000}, {"a.md": 21_000}, 12_000, kind="command") == []

    def test_cap_one_char_more_than_slack_above_size_fails(self) -> None:
        (problem,) = check_caps({"a.md": 20_000}, {"a.md": 21_001}, 12_000, kind="command")
        assert "lower the cap to at most 21000" in problem

    def test_skill_default_is_applied_per_kind(self) -> None:
        assert check_caps({"s": 8_000}, {}, 8_000, kind="skill") == []
        assert check_caps({"s": 8_001}, {}, 8_000, kind="skill")
