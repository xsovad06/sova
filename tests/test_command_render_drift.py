"""Guards against .claude/commands/ drifting from the canonical commands/ templates.

This repository is both the canonical source of the distributable slash commands
and a sync target for them. `commands/*.md` carry `{{ var }}` placeholders;
`.claude/commands/` holds the rendered copy Claude Code loads, and Claude Code has
no templating, so an unrendered `{{ check_cmd }}` reaches the agent verbatim.

Before issue #1088 the rendered tree was hand-maintained: seven commands shipped a
literal `{{ check_cmd }}`, and five had content that existed on only one side. A
later `sova commands sync` overwrote that drift, leaving the primary checkout dirty;
`ensure_claude_artifacts()` mirrored the dirt into every worktree, where
`RearrangeCommitsStep`'s gate read it as the agent's own uncommitted work and paused
the run. These tests fail with an instruction to run `make commands-render` rather
than regenerating anything themselves: the rendered tree is a checked-in build
artifact, and CI must not silently rewrite it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sova.commands.manifest import MANIFEST_FILENAME, file_hash
from sova.commands.self_render import (
    PLACEHOLDER_RE,
    SELF_VARIABLES,
    render_self,
    repo_root,
    used_placeholders,
)

_ROOT = repo_root()
_CANONICAL_DIR = _ROOT / "commands"
_RENDERED_DIR = _ROOT / ".claude/commands"

_RERUN = "Run `make commands-render` and commit the result."

_CANONICAL_NAMES = sorted(p.name for p in _CANONICAL_DIR.glob("*.md"))

# A missing or empty canonical directory would make every set comparison below
# trivially true, so fail loudly at collection time instead.
assert _CANONICAL_NAMES, f"no canonical commands found in {_CANONICAL_DIR}"


@pytest.fixture(scope="module")
def rendered_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Render canonical commands once into an isolated directory.

    Module-scoped because the render writes all 28 files plus the manifest, and
    every comparison below needs the same output; a function-scoped fixture would
    repeat the whole render once per parametrized command.
    """
    target = tmp_path_factory.mktemp("rendered")
    render_self(root=_ROOT, target_dir=target)
    return target


class TestNoDrift:
    """The checked-in rendered tree must equal a fresh render of canonical."""

    def test_every_canonical_command_is_rendered(self, rendered_dir: Path) -> None:
        produced = {p.name for p in rendered_dir.glob("*.md")}
        assert produced == set(_CANONICAL_NAMES)

    @pytest.mark.parametrize("name", _CANONICAL_NAMES)
    def test_rendered_command_matches_canonical(self, name: str, rendered_dir: Path) -> None:
        expected = (rendered_dir / name).read_text(encoding="utf-8")
        checked_in_path = _RENDERED_DIR / name
        assert checked_in_path.is_file(), f".claude/commands/{name} is missing. {_RERUN}"
        assert checked_in_path.read_text(encoding="utf-8") == expected, (
            f".claude/commands/{name} is out of sync with commands/{name}. {_RERUN}"
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
                f".claude/commands/{name} does not match its manifest hash. {_RERUN}"
            )


class TestNoUnrenderedPlaceholders:
    """A placeholder that survives into the rendered tree reaches agents verbatim."""

    @pytest.mark.parametrize("name", _CANONICAL_NAMES)
    def test_rendered_command_has_no_placeholder(self, name: str) -> None:
        content = (_RENDERED_DIR / name).read_text(encoding="utf-8")
        leftovers = PLACEHOLDER_RE.findall(content)
        assert not leftovers, (
            f".claude/commands/{name} still contains unrendered placeholders {leftovers}. "
            f"Add them to SELF_VARIABLES in sova/commands/self_render.py, then {_RERUN.lower()}"
        )

    def test_every_used_placeholder_is_pinned(self) -> None:
        """A new placeholder in canonical must gain a SELF_VARIABLES value.

        Without this, render_command() silently leaves an unknown placeholder
        as-is (its documented behaviour) and the unsubstituted text ships.
        """
        missing = used_placeholders(_CANONICAL_DIR) - set(SELF_VARIABLES)
        assert not missing, (
            f"commands/ uses placeholders with no pinned value: {sorted(missing)}. "
            "Add them to SELF_VARIABLES in sova/commands/self_render.py."
        )


class TestRenderedTreeShape:
    """Project-only commands must stay outside the managed set."""

    def test_project_only_commands_are_not_managed(self) -> None:
        manifest = json.loads((_RENDERED_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        managed = set(manifest["commands"])
        on_disk = {p.name for p in _RENDERED_DIR.glob("*.md")}
        project_only = on_disk - set(_CANONICAL_NAMES)
        assert project_only, "expected at least one project-only command in .claude/commands/"
        assert not (project_only & managed), (
            f"project-only commands must not be manifest-managed: {sorted(project_only & managed)}"
        )

    def test_no_rendered_command_is_a_symlink(self) -> None:
        """A symlinked target makes write_text() clobber the canonical template.

        `_install_files()` writes through a symlink, so a rendered entry pointing
        back at `commands/` would overwrite the template it was rendered from.
        """
        links = sorted(p.name for p in _RENDERED_DIR.glob("*.md") if p.is_symlink())
        assert not links, f"rendered commands must be regular files, found symlinks: {links}"


class TestPinnedVariables:
    """SELF_VARIABLES must name targets this repo actually exposes."""

    @pytest.mark.parametrize("key", sorted(SELF_VARIABLES))
    def test_pinned_make_target_exists(self, key: str) -> None:
        value = SELF_VARIABLES[key]
        prefix = "make "
        if not value.startswith(prefix):
            pytest.skip(f"{key} is not a make target")
        target = value[len(prefix) :].strip()
        makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
        assert any(line.startswith(f"{target}:") for line in makefile.splitlines()), (
            f"SELF_VARIABLES[{key!r}] is {value!r} but the Makefile has no {target!r} target."
        )
