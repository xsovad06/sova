"""Tests for the issue-template skill.

The skill's only job is to emit an issue body that clears the triage quality
gate without enrichment, so these tests score the templates the skill ships
with the real ``compute_quality_score()`` and parse them with the real
``parse_dependencies()``. If either function's contract changes, the templates
fail here rather than silently degrading every issue written with the skill.

Convention: every fenced ``markdown`` block in a SKILL.md is treated as a body
the skill tells the agent to produce, so all of them must score 8/8. Blocks
that deliberately show a wrong body must use a different fence tag.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sova.adapters.jira import JiraAdapter, _build_adf_doc
from sova.commands.distribution import _collect_skills
from sova.commands.templates import build_variables, render_command
from sova.config.models import ProjectConfig
from sova.dashboard.routers.settings import _REQUIRED_LABELS
from sova.roles.triage import _LLM_LEAK_PATTERNS, compute_quality_score
from sova.supervisor.dependency_graph import parse_dependencies

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_DIR = _REPO_ROOT / "skills"
_DISTRIBUTABLE = _SKILLS_DIR / "issue-template" / "SKILL.md"
_SOVA_LOCAL = _REPO_ROOT / ".claude" / "skills" / "issue-template" / "SKILL.md"

_MARKDOWN_BLOCK = re.compile(r"^```markdown\n(.*?)^```", re.MULTILINE | re.DOTALL)
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)
# Exactly two backticks: an inline code span left empty by a blank variable.
# Fences are three backticks, so they never match.
_EMPTY_CODE_SPAN = re.compile(r"(?<!`)``(?!`)")
# A backticked, fully-qualified label name, e.g. `type: epic` or `agent:human-only`.
# The `type:`/`area:` families carry a space after the colon and the `agent:`
# family does not, so the spacing here is load-bearing, not cosmetic.
_LABEL_MENTION = re.compile(r"`((?:type|priority|area|agent|sova):\s?[a-z][a-z-]*)`")

# The headings compute_quality_score() recognizes, in the order the skill uses.
_CANONICAL_HEADINGS = [
    "Objective",
    "Detailed Description",
    "Acceptance Criteria",
    "Files / Modules to Change",
    "Out of Scope / Constraints",
    "Dependencies",
    "References",
]

_BOTH_COPIES = [_DISTRIBUTABLE, _SOVA_LOCAL]


def _markdown_blocks(path: Path) -> list[str]:
    """Return every fenced ``markdown`` block in a SKILL.md."""
    return _MARKDOWN_BLOCK.findall(path.read_text())


def _canonical_body(path: Path) -> str:
    """Return the first fenced ``markdown`` block: the canonical issue body."""
    blocks = _markdown_blocks(path)
    assert blocks, f"{path} has no fenced markdown template block"
    return blocks[0]


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the simple ``key: value`` YAML frontmatter of a SKILL.md."""
    match = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
    assert match is not None, f"{path} has no YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


class TestSkillFiles:
    """Both copies exist, are discoverable, and are self-consistent."""

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_skill_file_exists(self, path: Path) -> None:
        assert path.is_file(), f"missing skill file: {path}"

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_frontmatter_name_matches_directory(self, path: Path) -> None:
        """Claude Code discovers skills by directory, so name must match it."""
        fields = _frontmatter(path)
        assert fields.get("name") == path.parent.name == "issue-template"
        assert fields.get("description"), f"{path} has no description to trigger activation"

    def test_distributable_is_collected_for_install(self) -> None:
        """_collect_skills() globs skills/<dir>/SKILL.md, so no wiring is needed."""
        keys = [key for key, _ in _collect_skills(_SKILLS_DIR)]
        assert "issue-template/SKILL.md" in keys


class TestCanonicalBodyPassesQualityGate:
    """The shipped bodies score 8/8 against the real triage scorer."""

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_every_markdown_block_scores_full_marks(self, path: Path) -> None:
        for index, block in enumerate(_markdown_blocks(path)):
            score = compute_quality_score(block)
            assert score.total == 8, (
                f"{path} markdown block {index} scored {score.total}/8: {score}. "
                "Counter-examples must not use a ```markdown fence."
            )
            assert score.meets_threshold()

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_acceptance_criteria_are_unchecked(self, path: Path) -> None:
        """meets_threshold() hard-requires a literal unchecked box at any total."""
        body = _canonical_body(path)
        assert "- [ ]" in body
        assert "- [x]" not in body.lower()

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_checking_every_box_caps_the_body_at_six(self, path: Path) -> None:
        """Pins the cost the skill documents for pre-checked acceptance criteria."""
        checked = _canonical_body(path).replace("- [ ]", "- [x]")
        score = compute_quality_score(checked)
        assert not score.has_acceptance_criteria
        assert score.total == 6
        # Hard-required independently of the total: no threshold lets it through.
        assert not score.meets_threshold(0)

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_body_starts_at_objective_with_no_llm_preamble(self, path: Path) -> None:
        body = _canonical_body(path)
        assert body.lstrip().startswith("## Objective")
        assert compute_quality_score(body).no_llm_leaks

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_headings_are_level_two_and_canonical(self, path: Path) -> None:
        """The scorer's regex is ``^##\\s+``: a ### or bolded section scores zero."""
        body = _canonical_body(path)
        assert [h.strip() for h in _HEADING.findall(body)] == _CANONICAL_HEADINGS
        assert "###" not in body


class TestDependenciesSection:
    """parse_dependencies() must find no phantom edges in the shipped bodies."""

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_canonical_body_declares_no_dependencies(self, path: Path) -> None:
        """The blank template says None, so a fresh issue is never self-blocked."""
        assert parse_dependencies(_canonical_body(path)) == set()

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_exactly_one_dependencies_heading_per_block(self, path: Path) -> None:
        """Only the first ## Dependencies section is parsed; a second is ignored."""
        for index, block in enumerate(_markdown_blocks(path)):
            count = len(re.findall(r"^## dependencies\s*$", block, re.MULTILINE | re.IGNORECASE))
            assert count == 1, f"{path} markdown block {index} has {count} '## Dependencies' headings"

    @pytest.mark.parametrize("path", _BOTH_COPIES)
    def test_dependencies_heading_is_exactly_matchable(self, path: Path) -> None:
        """extract_section() anchors on ``^## Dependencies$`` with no trailing text."""
        assert re.search(r"^## Dependencies$", _canonical_body(path), re.MULTILINE)


class TestTemplateVariables:
    """The distributable is rendered; unknown placeholders would ship literally."""

    def test_distributable_uses_only_known_variables(self) -> None:
        known = set(build_variables(ProjectConfig()))
        used = set(_PLACEHOLDER.findall(_DISTRIBUTABLE.read_text()))
        assert used <= known, f"unknown template variables: {sorted(used - known)}"

    def test_distributable_renders_without_leftover_placeholders(self) -> None:
        rendered = render_command(_DISTRIBUTABLE.read_text(), build_variables(ProjectConfig()))
        assert "{{" not in rendered

    def test_rendered_distributable_still_scores_full_marks(self) -> None:
        """Rendering must not disturb the headings the scorer keys on."""
        rendered = render_command(_DISTRIBUTABLE.read_text(), build_variables(ProjectConfig()))
        for block in _MARKDOWN_BLOCK.findall(rendered):
            assert compute_quality_score(block).total == 8

    def test_sova_copy_has_no_template_placeholders(self) -> None:
        """The SOVA-specific copy is never rendered, so it must be literal."""
        assert "{{" not in _SOVA_LOCAL.read_text()


class TestLLMLeakGuidance:
    """The skill enumerates the leak patterns, so the list must stay complete."""

    # One opener per pattern in _LLM_LEAK_PATTERNS, each documented by the skill.
    _OPENERS = [
        "Here's the plan for this change.",
        "Here is a summary of the work.",
        "I've drafted the issue below.",
        "I have updated the description.",
        "Let me explain the approach.",
        "Sure, the details follow.",
        "Certainly! The fix is small.",
        "Absolutely. The cache is the problem.",
        "I'll write this up now.",
        "Feel free to adjust the wording.",
        "Don't hesitate to ask for changes.",
    ]

    @pytest.mark.parametrize("opener", _OPENERS)
    def test_documented_opener_costs_the_point(self, opener: str) -> None:
        assert not compute_quality_score(f"{opener}\n\n## Objective\nDo the thing.").no_llm_leaks

    def test_let_me_know_is_exempt(self) -> None:
        """``^let me (?!know\\b)`` carves this out, and the skill says so."""
        assert compute_quality_score("Let me know if this is unclear.\n\n## Objective\nX.").no_llm_leaks

    def test_skill_documents_every_leak_pattern(self) -> None:
        """A new pattern in triage.py must be mirrored into both SKILL.md copies."""
        assert len(_LLM_LEAK_PATTERNS) == 6, (
            "_LLM_LEAK_PATTERNS changed: update the leak list in both "
            "skills/issue-template/SKILL.md and .claude/skills/issue-template/SKILL.md"
        )


class TestDistributableRendersForAnyTracker:
    """github_repo is empty on Jira-backed projects, which the skill supports."""

    def test_renders_cleanly_with_no_github_repo(self) -> None:
        variables = build_variables(ProjectConfig())
        assert variables["github_repo"] == "", "fixture must model a tracker with no GitHub repo"
        rendered = render_command(_DISTRIBUTABLE.read_text(), variables)
        assert not _EMPTY_CODE_SPAN.search(rendered), "a variable rendered empty inside an inline code span"


class TestLabelTaxonomy:
    """The SOVA copy enumerates the tracker's labels, so it must match the registry."""

    _REGISTERED = {label["name"] for label in _REQUIRED_LABELS}

    def test_every_documented_label_name_is_registered(self) -> None:
        """Catches the spacing trap: `agent:human-only` has no space, `type: epic` does."""
        mentioned = set(_LABEL_MENTION.findall(_SOVA_LOCAL.read_text()))
        assert mentioned, "no fully-qualified label names found; did the taxonomy section move?"
        assert mentioned <= self._REGISTERED, f"unregistered labels: {sorted(mentioned - self._REGISTERED)}"

    @pytest.mark.parametrize("prefix", ["type", "priority", "area"])
    def test_documented_values_cover_the_registered_family(self, prefix: str) -> None:
        """A new `type:`/`area:` label in the registry must be added to the skill."""
        registered = {name.split(": ", 1)[1] for name in self._REGISTERED if name.startswith(f"{prefix}: ")}
        section = re.search(rf"^- `{prefix}:` (.+?)(?=\n- `|\n\n)", _SOVA_LOCAL.read_text(), re.MULTILINE | re.DOTALL)
        assert section is not None, f"no `{prefix}:` bullet in the taxonomy section"
        documented = set(re.findall(r"`([a-z][a-z-]*)`", section.group(1)))
        assert documented == registered, f"`{prefix}:` drift: {documented ^ registered}"


class TestJiraRoundTrip:
    """The distributable claims the body survives Jira's ADF conversion. Pin it."""

    def test_canonical_body_still_scores_full_marks_after_adf_round_trip(self) -> None:
        rendered = render_command(_DISTRIBUTABLE.read_text(), build_variables(ProjectConfig()))
        body = _MARKDOWN_BLOCK.findall(rendered)[0]
        restored = JiraAdapter._extract_text(_build_adf_doc(body))
        score = compute_quality_score(restored)
        assert score.total == 8, f"ADF round-trip degraded the body to {score.total}/8: {score}"
        assert score.meets_threshold()

    def test_rich_editor_bullet_list_loses_the_checkboxes(self) -> None:
        """Pins the trap the skill's Jira section warns about.

        _extract_text() reads only text nodes directly under a block, so a
        bulletList's nested listItem/paragraph text never surfaces.
        """
        heading = {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "Acceptance Criteria"}],
        }
        item = {
            "type": "listItem",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "- [ ] a criterion"}]}],
        }
        adf = {
            "type": "doc",
            "version": 1,
            "content": [heading, {"type": "bulletList", "content": [item]}],
        }
        restored = JiraAdapter._extract_text(adf)
        assert "- [ ]" not in restored
        assert not compute_quality_score(restored).has_acceptance_criteria
