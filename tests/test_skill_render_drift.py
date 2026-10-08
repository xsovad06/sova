"""Guards against .agents/skills/ drifting from commands/*.md and skills/*.

Mirrors tests/test_command_render_drift.py for .claude/commands/: the
rendered tree under .agents/skills/ is a checked-in build artifact
(mechanically derived from commands/*.md and skills/*, per issue #1123), and
a later `sova commands sync` would overwrite drift silently and leave the
checkout dirty. These tests fail with an instruction to run
`make skills-render` rather than regenerating anything themselves.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sova.agents.codex import CodexAdapter
from sova.commands.manifest import MANIFEST_FILENAME, file_hash
from sova.commands.self_render import PLACEHOLDER_RE, repo_root, self_config, used_placeholders
from sova.commands.skill_render import SKILL_NAME_PREFIX, render_codex_skills, render_self_skills
from sova.commands.templates import build_variables

_ROOT = repo_root()
_STANDALONE_SKILLS_DIR = _ROOT / "skills"
_COMMANDS_DIR = _ROOT / "commands"
# Resolved via the adapter, not a hand-pinned literal, so this guard and
# render_self_skills() can't silently diverge the next time CodexAdapter's
# skills_dir() moves (it already has once: .codex/skills -> .agents/skills).
_RENDERED_DIR = CodexAdapter().skills_dir(_ROOT)
assert _RENDERED_DIR is not None

_RERUN = "Run `make skills-render` and commit the result."

# Pre-existing, independently-maintained content under plain names (e.g.
# testing-patterns, database-patterns) is deliberately not SOVA-managed and
# must not be touched by this guard. A command-derived entry is always
# prefixed; a standalone one (e.g. design-taste, issue-template) is managed
# too but installs bare, so "managed" can no longer be read off the name
# alone (issue #1136) and must come from the manifest instead.
_MANAGED_PREFIX = SKILL_NAME_PREFIX

# Hand-authored content that predates this renderer and is never produced by
# it. dashboard-design/database-patterns/visual-audit have no skills/
# counterpart at all; testing-patterns does, but materialize_combined_skill_
# sources() deliberately skips the generic standalone render in favor of
# this repo's own more specific hand-authored copy already occupying that
# bare name (see that function's docstring and _SKIPPED_STANDALONE below).
# Pinned explicitly, rather than inferred from the manifest or the on-disk
# tree, so a directory that is neither rendered nor explicitly listed here
# fails this guard instead of being silently accepted either way.
_HAND_AUTHORED = frozenset({"dashboard-design", "database-patterns", "testing-patterns", "visual-audit"})
# Standalone skills (from skills/) whose generic render is skipped at this
# repo's own target because a _HAND_AUTHORED entry already occupies that bare
# name there.
_SKIPPED_STANDALONE = frozenset({"testing-patterns"})


def _checked_in_managed_names() -> set[str]:
    """Directory names the checked-in manifest marks as SOVA-managed, bare or prefixed alike.

    Used only to scope *which* files a content-level check (placeholders,
    duplicate names, ...) should look at, never to define what "managed"
    itself means: that must come from an independent source (see
    ``_expected_managed_names()``), or a manifest entry a rendering bug
    silently drops would change both the render output and the expectation
    together, and this guard would never notice (issue #1136 finding).
    """
    manifest = json.loads((_RENDERED_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    return {name.removesuffix("/SKILL.md") for name in manifest["commands"]}


def _expected_managed_names() -> set[str]:
    """The managed name set, derived independently of the rendered manifest under test.

    Prefixed names come from re-rendering the canonical commands directly
    (the same call ``render_self_skills()`` makes internally); bare names
    come from listing ``skills/`` and excluding the entries known to be
    skipped in favor of this repo's own hand-authored content. Neither
    source reads the checked-in manifest, so a manifest entry dropped by a
    rendering bug fails this guard instead of silently redefining it.
    """
    prefixed = {
        f"{SKILL_NAME_PREFIX}{name}"
        for name in render_codex_skills(_COMMANDS_DIR, supports=CodexAdapter().supports_command)
    }
    standalone = {
        p.name
        for p in _STANDALONE_SKILLS_DIR.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file() and p.name not in _SKIPPED_STANDALONE
    }
    return prefixed | standalone


@pytest.fixture(scope="module")
def rendered_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Render canonical commands + skills once into an isolated directory."""
    target = tmp_path_factory.mktemp("rendered-skills")
    render_self_skills(root=_ROOT, target_dir=target)
    return target


class TestNoDrift:
    """The checked-in managed skills must equal a fresh render of canonical."""

    def test_every_rendered_skill_is_checked_in(self, rendered_dir: Path) -> None:
        produced = {p.name for p in rendered_dir.iterdir() if p.is_dir()}
        checked_in = _checked_in_managed_names()
        assert produced == checked_in, f"{produced.symmetric_difference(checked_in)}. {_RERUN}"

    def test_checked_in_manifest_matches_the_independently_derived_expected_set(self) -> None:
        """The checked-in manifest keys must equal the independently-derived expected set,
        so a manifest entry dropped by a rendering bug (or a hand-edit to the manifest
        itself) fails here instead of silently redefining what "managed" means for every
        other assertion in this module (issue #1136 finding)."""
        checked_in = _checked_in_managed_names()
        expected = _expected_managed_names()
        assert checked_in == expected, f"{checked_in.symmetric_difference(expected)}. {_RERUN}"

    def test_rendered_skill_matches_canonical(self, rendered_dir: Path) -> None:
        for skill_dir in sorted(rendered_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            expected = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            checked_in_path = _RENDERED_DIR / skill_dir.name / "SKILL.md"
            assert checked_in_path.is_file(), f".agents/skills/{skill_dir.name}/SKILL.md is missing. {_RERUN}"
            assert checked_in_path.read_text(encoding="utf-8") == expected, (
                f".agents/skills/{skill_dir.name}/SKILL.md is out of sync with canonical. {_RERUN}"
            )

    def test_manifest_matches_a_fresh_render(self, rendered_dir: Path) -> None:
        expected = json.loads((rendered_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        actual = json.loads((_RENDERED_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert actual == expected, f"{MANIFEST_FILENAME} is stale. {_RERUN}"

    def test_manifest_hashes_match_the_files_on_disk(self) -> None:
        manifest = json.loads((_RENDERED_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        for name, entry in manifest["commands"].items():
            content = (_RENDERED_DIR / name).read_text(encoding="utf-8")
            assert file_hash(content) == entry["hash"], (
                f".agents/skills/{name} does not match its manifest hash. {_RERUN}"
            )


class TestNoUnrenderedPlaceholders:
    """A placeholder that survives into the rendered tree reaches agents verbatim.

    Mirrors ``tests/test_command_render_drift.py``'s ``TestNoUnrenderedPlaceholders``
    for ``commands/``: the ``skills/`` source tree render_self_skills() also
    renders (via ``materialize_combined_skill_sources()``) had no equivalent
    guard, so a placeholder like ``{{ project_name }}`` could resolve to a
    silently-wrong fallback (or, for a genuinely new placeholder, ship
    unrendered) with nothing to catch it.
    """

    def test_every_used_skill_placeholder_is_resolvable(self) -> None:
        """A new placeholder in skills/ must be one build_variables() can actually fill.

        Checked against build_variables(self_config())'s output keys, not
        SELF_VARIABLES's literal keys: build_variables() always derives a few
        keys (project_name, base_branch, scopes, ...) that are never hand-listed
        in SELF_VARIABLES itself, and those are exactly as "resolvable" as the
        ones that are.
        """
        missing = used_placeholders(_STANDALONE_SKILLS_DIR, "*/SKILL.md") - set(build_variables(self_config()))
        assert not missing, (
            f"skills/ uses placeholders with no resolvable value: {sorted(missing)}. "
            "Add them to SELF_VARIABLES (or build_variables()) in sova/commands/self_render.py "
            "(sova/commands/templates.py)."
        )

    def test_no_rendered_skill_has_a_placeholder(self) -> None:
        managed = _checked_in_managed_names()
        for path in sorted(_RENDERED_DIR.glob("*/SKILL.md")):
            if path.parent.name not in managed:
                continue
            leftovers = PLACEHOLDER_RE.findall(path.read_text(encoding="utf-8"))
            assert not leftovers, (
                f".agents/skills/{path.parent.name}/SKILL.md still contains unrendered placeholders "
                f"{leftovers}. {_RERUN}"
            )


class TestRenderedTreeShape:
    """Pre-existing, unmanaged skill directories must stay outside the managed set."""

    def test_unmanaged_skills_are_not_prefixed_and_not_managed(self) -> None:
        manifest = json.loads((_RENDERED_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        managed = set(manifest["commands"])
        on_disk = {f"{p.name}/SKILL.md" for p in _RENDERED_DIR.iterdir() if p.is_dir()}
        unmanaged = on_disk - managed
        assert unmanaged, "expected at least one pre-existing unmanaged skill in .agents/skills/"
        assert all(not name.startswith(_MANAGED_PREFIX) for name in unmanaged), (
            f"unmanaged entries must not use the {_MANAGED_PREFIX} prefix: {sorted(unmanaged)}"
        )

    def test_unmanaged_skills_match_the_pinned_hand_authored_set(self) -> None:
        """A directory that is neither rendered nor explicitly pinned as hand-authored
        must be investigated, not silently accepted as "just another unmanaged entry":
        that's exactly how a dropped manifest entry (issue #1136 finding) would otherwise
        slip past the weaker truthiness check above."""
        manifest = json.loads((_RENDERED_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        managed = set(manifest["commands"])
        on_disk = {f"{p.name}/SKILL.md" for p in _RENDERED_DIR.iterdir() if p.is_dir()}
        unmanaged = {name.removesuffix("/SKILL.md") for name in on_disk - managed}
        assert unmanaged == _HAND_AUTHORED, f"{unmanaged.symmetric_difference(_HAND_AUTHORED)}"

    def test_no_rendered_skill_is_a_symlink(self) -> None:
        managed = _checked_in_managed_names()
        links = sorted(
            p.name
            for p in _RENDERED_DIR.iterdir()
            if p.is_dir() and p.name in managed and (p / "SKILL.md").is_symlink()
        )
        assert not links, f"rendered skills must be regular files, found symlinks: {links}"

    def test_frontmatter_name_matches_directory_name(self) -> None:
        """A dir/frontmatter mismatch is exactly the collision the sova- prefix (or, for a
        standalone skill, the bare name itself) exists to prevent.

        A runtime that keys skills by the declared ``name:`` rather than the
        directory would otherwise see two differently-named-on-disk packages
        (e.g. a command-derived ``sova-foo`` and a pre-existing hand-authored
        ``foo``) collapse onto the same identity.
        """
        mismatches = []
        for skill_md in sorted(_RENDERED_DIR.glob("*/SKILL.md")):
            dir_name = skill_md.parent.name
            match = re.search(r"^name:\s*(\S+)\s*$", skill_md.read_text(encoding="utf-8"), re.MULTILINE)
            declared = match.group(1) if match else None
            if declared != dir_name:
                mismatches.append(f"{dir_name} declares name: {declared!r}")
        assert not mismatches, f"{mismatches}. {_RERUN}"

    def test_no_two_rendered_skills_declare_the_same_name(self) -> None:
        names: dict[str, str] = {}
        duplicates = []
        for skill_md in sorted(_RENDERED_DIR.glob("*/SKILL.md")):
            match = re.search(r"^name:\s*(\S+)\s*$", skill_md.read_text(encoding="utf-8"), re.MULTILINE)
            if match is None:
                continue
            declared = match.group(1)
            if declared in names:
                duplicates.append(f"{names[declared]} and {skill_md.parent.name} both declare name: {declared!r}")
            else:
                names[declared] = skill_md.parent.name
        assert not duplicates, duplicates


# .claude/skills/ has no render step of its own (make skills-render only regenerates
# .agents/skills/, per the Makefile), so a distributable skill with no SOVA-specific
# override must stay byte-identical to skills/ by hand. issue-template and
# testing-patterns are deliberate SOVA-specific variants (see
# tests/test_issue_template_skill.py and .claude/rules/workflow.md) and are excluded
# here on purpose, not because they're hand-authored like dashboard-design/
# database-patterns/visual-audit above.
_CLAUDE_SKILLS_DIR = _ROOT / ".claude" / "skills"
_IDENTICAL_CLAUDE_SKILLS = frozenset({"design-taste"})


class TestClaudeSkillsNoDrift:
    """.claude/skills/ entries with no SOVA-specific override must mirror skills/ exactly.

    Unlike .agents/skills/, there is no manifest and no render step here, so this is the
    only thing standing between skills/<name>/SKILL.md and silent drift in the Claude Code
    copy (issue #1136 finding; CodeRabbit never reviews .claude/** either, so nothing else
    would catch it).
    """

    @pytest.mark.parametrize("name", sorted(_IDENTICAL_CLAUDE_SKILLS))
    def test_identical_copy_matches_canonical(self, name: str) -> None:
        canonical = (_STANDALONE_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        claude_copy = (_CLAUDE_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert claude_copy == canonical, (
            f".claude/skills/{name}/SKILL.md has drifted from skills/{name}/SKILL.md. Copy the canonical "
            "file over it: these two must stay byte-identical (see .claude/rules/workflow.md)."
        )

    def test_no_leftover_sibling_files(self) -> None:
        """A prior three-file layout (README.md, design-standards.md) must not linger once
        its content is folded into a single SKILL.md (issue #1136)."""
        for name in sorted(_IDENTICAL_CLAUDE_SKILLS):
            extras = sorted(p.name for p in (_CLAUDE_SKILLS_DIR / name).iterdir() if p.name != "SKILL.md")
            assert not extras, f".claude/skills/{name}/ has leftover file(s) {extras} from a prior layout"
