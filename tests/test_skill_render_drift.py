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
from sova.commands.skill_render import SKILL_NAME_PREFIX, render_self_skills
from sova.commands.templates import build_variables

_ROOT = repo_root()
_STANDALONE_SKILLS_DIR = _ROOT / "skills"
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


def _checked_in_managed_names() -> set[str]:
    """Directory names the checked-in manifest marks as SOVA-managed, bare or prefixed alike."""
    manifest = json.loads((_RENDERED_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    return {name.removesuffix("/SKILL.md") for name in manifest["commands"]}


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
