"""Tests for sova/commands/skill_render.py: rendering commands/*.md as Codex SKILL.md packages."""

from __future__ import annotations

from pathlib import Path

import pytest

from sova.commands.catalog import CommandEntry
from sova.commands.manifest import MANIFEST_FILENAME
from sova.commands.skill_render import (
    SKILL_NAME_PREFIX,
    SkillRenderError,
    materialize_combined_skill_sources,
    render_codex_skill,
    render_codex_skills,
)


def _write_command(
    commands_dir: Path,
    filename: str,
    *,
    name: str,
    description: str = "A command.",
    body: str = "Do the thing.",
    runtimes: str | None = None,
) -> None:
    lines = ["---", f"name: {name}", f"description: {description}", "user-invocable: true"]
    if runtimes is not None:
        lines.append(f"runtimes: {runtimes}")
    lines += ["---", body, ""]
    (commands_dir / filename).write_text("\n".join(lines), encoding="utf-8")


class TestRenderCodexSkill:
    def test_basic_rendering_has_prefixed_name_and_quoted_description(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "foo.md", name="foo", description="Does foo things.", body="Do the foo thing.")
        entry = CommandEntry(
            name="foo", description="Does foo things.", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "name: sova-foo" in rendered
        assert 'description: "Does foo things."' in rendered
        assert "Do the foo thing." in rendered

    def test_arguments_idiom_is_substituted(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "foo.md", name="foo", body="Use $ARGUMENTS to find the target.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "$ARGUMENTS" not in rendered
        assert "the arguments provided when this skill is invoked" in rendered

    def test_neutral_arguments_placeholder_is_substituted(self, tmp_path: Path) -> None:
        """{{ arguments }}, the provider-neutral counterpart to $ARGUMENTS, renders to the same prose."""
        _write_command(tmp_path, "foo.md", name="foo", body="Use {{ arguments }} to find the target.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "{{ arguments }}" not in rendered
        assert "the arguments provided when this skill is invoked" in rendered

    def test_in_fence_neutral_arguments_placeholder_renders_as_shell_metavariable(self, tmp_path: Path) -> None:
        body = "Run the checks:\n\n```bash\ngh issue view {{ arguments }} --json number\n```\n"
        _write_command(tmp_path, "foo.md", name="foo", body=body)
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "gh issue view <arguments> --json number" in rendered

    def test_workflow_reference_syntax_is_rewritten_to_the_skill_name(self, tmp_path: Path) -> None:
        """The provider-neutral `name` workflow syntax (no slash) becomes `sova-name` skill."""
        _write_command(tmp_path, "foo.md", name="foo", body="Run the `bar` workflow to continue.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert "`bar` workflow" not in rendered
        assert f"Run the `{SKILL_NAME_PREFIX}bar` skill to continue." in rendered

    def test_unknown_workflow_reference_raises(self, tmp_path: Path) -> None:
        """A `name` workflow reference to a command not in skill_names must fail loudly, not ship."""
        _write_command(tmp_path, "foo.md", name="foo", body="Run the `bar` workflow to continue.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        with pytest.raises(SkillRenderError, match="workflow reference"):
            render_codex_skill(entry, skill_names=[])

    def test_cross_reference_is_rewritten_to_the_other_skill_name(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "foo.md", name="foo", body="Run /bar to continue.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert "/bar" not in rendered
        assert f"the `{SKILL_NAME_PREFIX}bar` skill" in rendered

    def test_cross_reference_inside_a_fence_is_left_verbatim(self, tmp_path: Path) -> None:
        """A /foo inside a code fence is a shell command or path, not a cross-reference."""
        body = "Run /bar first.\n\n```bash\ngit log > /tmp/bar-commits.txt\n```\n"
        _write_command(tmp_path, "foo.md", name="foo", body=body)
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert "/tmp/bar-commits.txt" in rendered
        assert f"Run the `{SKILL_NAME_PREFIX}bar` skill first." in rendered

    def test_path_segment_is_not_mistaken_for_a_cross_reference(self, tmp_path: Path) -> None:
        """build/test/lint names no command: only a /foo at a real boundary is a reference."""
        _write_command(tmp_path, "foo.md", name="foo", body="Document build/test/lint commands.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["test"])

        assert "build/test/lint commands" in rendered
        assert SKILL_NAME_PREFIX + "test" not in rendered

    def test_longer_command_name_is_not_truncated_by_a_shorter_one(self, tmp_path: Path) -> None:
        """/review-pr must be rewritten by its own pattern, never left as "sova-review skill-pr"."""
        _write_command(tmp_path, "foo.md", name="foo", body="Then run /review-pr on it.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["review", "review-pr"])

        assert f"the `{SKILL_NAME_PREFIX}review-pr` skill" in rendered
        assert "skill-pr" not in rendered

    def test_backticked_cross_reference_does_not_nest_backticks(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "foo.md", name="foo", body="Run `/bar` next.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert f"Run the `{SKILL_NAME_PREFIX}bar` skill next." in rendered
        assert "`the `" not in rendered

    def test_backticked_cross_reference_keeps_its_argument_hint(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "foo.md", name="foo", body="Run `/bar <PR_NUMBER>` to resume.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert f"Run the `{SKILL_NAME_PREFIX}bar` skill <PR_NUMBER> to resume." in rendered
        assert "`the `" not in rendered

    def test_self_reference_is_rewritten_too(self, tmp_path: Path) -> None:
        """Codex has no slash commands, so a command's reference to itself needs rewriting as well."""
        _write_command(tmp_path, "foo.md", name="foo", body="The user can re-run `/foo` later.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["foo"])

        assert f"re-run the `{SKILL_NAME_PREFIX}foo` skill later." in rendered

    def test_preceding_article_is_not_doubled(self, tmp_path: Path) -> None:
        """ "Follow the `/bar` workflow" must not render as "Follow the the `sova-bar` skill workflow"."""
        _write_command(tmp_path, "foo.md", name="foo", body="Follow the `/bar` workflow.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert "the the" not in rendered
        assert f"Follow the `{SKILL_NAME_PREFIX}bar` skill workflow." in rendered

    def test_preceding_article_capitalization_is_preserved(self, tmp_path: Path) -> None:
        """A sentence-initial "The `/bar`..." must not become a lowercase "the" mid-sentence."""
        _write_command(tmp_path, "foo.md", name="foo", body="The `/bar` step runs first.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert f"The `{SKILL_NAME_PREFIX}bar` skill step runs first." in rendered

    def test_trailing_command_word_is_absorbed(self, tmp_path: Path) -> None:
        """ "Follow the `/bar` command workflow" must not leave a dangling "skill command"."""
        _write_command(tmp_path, "foo.md", name="foo", body="Follow the `/bar` command workflow.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert f"Follow the `{SKILL_NAME_PREFIX}bar` skill workflow." in rendered
        assert "skill command" not in rendered

    def test_modifier_between_determiner_and_reference_is_preserved_verbatim(self, tmp_path: Path) -> None:
        """ "that a previous `/bar` run" must keep "a previous", not double into "a previous the ... skill"."""
        _write_command(tmp_path, "foo.md", name="foo", body="that a previous `/bar` run found missing")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert f"that a previous `{SKILL_NAME_PREFIX}bar` skill run found missing" in rendered
        assert "a previous the" not in rendered

    def test_determiner_several_words_upstream_does_not_swallow_the_verb_phrase(self, tmp_path: Path) -> None:
        """ "that happens via `/bar`" must still inject "the": "that" here is not a determiner for `/bar`."""
        _write_command(tmp_path, "foo.md", name="foo", body="that happens via `/bar` or `/baz`.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["bar", "baz"])

        assert f"that happens via the `{SKILL_NAME_PREFIX}bar` skill" in rendered

    def test_verify_local_reference_is_rewritten_to_neutral_prose(self, tmp_path: Path) -> None:
        """/verify-local is not a canonical command: it must not ship as a dangling Claude reference."""
        body = (
            "If the project has a `/verify-local` command, follow the `/verify-local` procedure. "
            "Skip if no `/verify-local` command exists."
        )
        _write_command(tmp_path, "foo.md", name="foo", body=body)
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "/verify-local" not in rendered
        assert "a local verification procedure" in rendered
        assert "that procedure" in rendered
        assert "no such procedure exists" in rendered

    def test_approve_merge_reference_is_rewritten_to_neutral_prose(self, tmp_path: Path) -> None:
        """/approve-merge is Claude-only (lives only in .claude/commands/), so it has no Codex skill."""
        _write_command(tmp_path, "foo.md", name="foo", body="That happens via `/integrate-pr` or `/approve-merge`.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=["integrate-pr"])

        assert "/approve-merge" not in rendered
        assert f"the `{SKILL_NAME_PREFIX}integrate-pr` skill" in rendered
        assert "or by merging it directly once the user asks" in rendered

    def test_unknown_placeholder_in_description_raises(self, tmp_path: Path) -> None:
        """A description idiom no substitution covers must fail loudly, not ship into the frontmatter."""
        description = "Does {{ some_new_var }} things."
        _write_command(tmp_path, "foo.md", name="foo", description=description)
        entry = CommandEntry(
            name="foo",
            description=description,
            category="core",
            user_invocable=True,
            path=tmp_path / "foo.md",
        )

        with pytest.raises(SkillRenderError, match="placeholder"):
            render_codex_skill(entry, skill_names=[])

    def test_description_cross_reference_is_rewritten(self, tmp_path: Path) -> None:
        """A description naming another command ("Run before /pr") must not ship a Claude slash command."""
        _write_command(tmp_path, "foo.md", name="foo", description="Does foo. Run before /bar.")
        entry = CommandEntry(
            name="foo",
            description="Does foo. Run before /bar.",
            category="core",
            user_invocable=True,
            path=tmp_path / "foo.md",
        )

        rendered = render_codex_skill(entry, skill_names=["bar"])

        assert f'description: "Does foo. Run before the `{SKILL_NAME_PREFIX}bar` skill."' in rendered

    def test_in_fence_arguments_idiom_renders_as_shell_metavariable(self, tmp_path: Path) -> None:
        """An explanatory sentence inside a ```bash fence would produce an unrunnable command line."""
        body = "Run the checks:\n\n```bash\ngh issue view $ARGUMENTS --json number\n```\n"
        _write_command(tmp_path, "foo.md", name="foo", body=body)
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "gh issue view <arguments> --json number" in rendered
        assert "the arguments provided when this skill is invoked" not in rendered

    def test_in_fence_template_variable_is_left_for_install_time_rendering(self, tmp_path: Path) -> None:
        """{{ check_cmd }} inside a fence must stay a real command, not become an unrunnable metavariable."""
        body = "Run the checks:\n\n```bash\n{{ check_cmd }}\n```\n"
        _write_command(tmp_path, "foo.md", name="foo", body=body)
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "{{ check_cmd }}" in rendered
        assert "<check-command>" not in rendered

    def test_description_already_quoted_in_frontmatter_is_not_double_quoted(self, tmp_path: Path) -> None:
        raw_description = "Does foo: things, and more."
        _write_command(tmp_path, "foo.md", name="foo", description=f'"{raw_description}"', body="Body.")
        entry = CommandEntry(
            name="foo",
            description=f'"{raw_description}"',
            category="core",
            user_invocable=True,
            path=tmp_path / "foo.md",
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert rendered.count('"') == 2
        assert f'description: "{raw_description}"' in rendered

    def test_description_with_interior_quotes_is_not_mangled(self, tmp_path: Path) -> None:
        """A description that merely starts/ends with " (quoting a word in prose) isn't a YAML wrapper."""
        raw_description = '"review" mode for "pr"'
        _write_command(tmp_path, "foo.md", name="foo", description=raw_description, body="Body.")
        entry = CommandEntry(
            name="foo",
            description=raw_description,
            category="core",
            user_invocable=True,
            path=tmp_path / "foo.md",
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert 'description: "\\"review\\" mode for \\"pr\\""' in rendered

    def test_template_variable_is_deferred_to_install_time_rendering(self, tmp_path: Path) -> None:
        """{{ check_cmd }} must survive into the rendered body for render_command() to fill at install time.

        A prose rewrite here (e.g. "the project's CI-equivalent check command")
        would ship a generic description instead of the project's actual
        configured command (`make check`), which install_skills() already
        knows how to substitute, exactly as it does for the Claude Code path.
        """
        _write_command(tmp_path, "foo.md", name="foo", body="Run {{ check_cmd }} before committing.")
        entry = CommandEntry(
            name="foo", description="d", category="core", user_invocable=True, path=tmp_path / "foo.md"
        )

        rendered = render_codex_skill(entry, skill_names=[])

        assert "{{ check_cmd }}" in rendered

    def test_no_frontmatter_raises(self, tmp_path: Path) -> None:
        (tmp_path / "bad.md").write_text("No frontmatter here.\n", encoding="utf-8")
        entry = CommandEntry(
            name="bad", description="d", category="core", user_invocable=True, path=tmp_path / "bad.md"
        )

        with pytest.raises(SkillRenderError):
            render_codex_skill(entry, skill_names=[])


class TestAssertFullyRendered:
    def test_uncovered_arguments_idiom_raises(self) -> None:
        """A $ARGUMENTS reference that somehow survives substitution must fail loudly, never ship."""
        from sova.commands.skill_render import _assert_fully_rendered

        with pytest.raises(SkillRenderError, match=r"\$ARGUMENTS"):
            _assert_fully_rendered("foo", "still has $ARGUMENTS in it")

    def test_uncovered_placeholder_raises(self) -> None:
        from sova.commands.skill_render import _assert_fully_rendered

        with pytest.raises(SkillRenderError, match="placeholder"):
            _assert_fully_rendered("foo", "still has {{ some_new_var }} in it")

    def test_known_deferred_placeholder_does_not_raise(self) -> None:
        """{{ check_cmd }} is left for install-time rendering, not a leftover idiom."""
        from sova.commands.skill_render import _assert_fully_rendered

        _assert_fully_rendered("foo", "run {{ check_cmd }} and {{ lint_cmd }}")

    def test_uncovered_backticked_command_reference_raises(self) -> None:
        """A backticked `/bar` that survived every rewrite pass must fail loudly, never ship."""
        from sova.commands.skill_render import _assert_fully_rendered

        with pytest.raises(SkillRenderError, match="command reference"):
            _assert_fully_rendered("foo", "run `/bar` first")

    def test_uncovered_bare_command_reference_raises(self) -> None:
        """A bare (unbackticked) /bar must fail loudly too: canonical commands use this form too."""
        from sova.commands.skill_render import _assert_fully_rendered

        with pytest.raises(SkillRenderError, match="command reference"):
            _assert_fully_rendered("foo", "Run before /approve-merge")

    def test_bare_reference_inside_inline_code_span_does_not_raise(self) -> None:
        """A longer inline-code example (a URL path, not a command) must not false-positive."""
        from sova.commands.skill_render import _assert_fully_rendered

        _assert_fully_rendered("foo", "see `gh api repos/.../pulls/<N>/comments`")

    def test_bare_path_segment_ending_in_hyphen_does_not_raise(self) -> None:
        """ "the `MERGEABLE`/not-`BEHIND` case": stripping the two code spans must not leave a bare /not-."""
        from sova.commands.skill_render import _assert_fully_rendered

        _assert_fully_rendered("foo", "the `MERGEABLE`/not-`BEHIND` case")

    def test_fully_rendered_body_does_not_raise(self) -> None:
        from sova.commands.skill_render import _assert_fully_rendered

        _assert_fully_rendered("foo", "clean body with no idioms")

    def test_cross_reference_inside_a_fence_does_not_raise(self) -> None:
        """The renderer deliberately leaves in-fence /foo alone, so the guard must not fire on it."""
        from sova.commands.skill_render import _assert_fully_rendered

        _assert_fully_rendered("foo", "```bash\ncat /bar/baz\n```")

    def test_path_segment_does_not_raise(self) -> None:
        from sova.commands.skill_render import _assert_fully_rendered

        _assert_fully_rendered("foo", "document build/bar/lint commands")

    def test_description_label_appears_in_error(self) -> None:
        from sova.commands.skill_render import _assert_fully_rendered

        with pytest.raises(SkillRenderError, match="description"):
            _assert_fully_rendered("foo", "still has $ARGUMENTS in it", label="description")


class TestRenderCodexSkills:
    def test_renders_every_unrestricted_command(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "foo.md", name="foo")
        _write_command(tmp_path, "bar.md", name="bar")

        rendered = render_codex_skills(tmp_path)

        assert set(rendered) == {"foo", "bar"}

    def test_claude_only_command_is_excluded(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "foo.md", name="foo")
        _write_command(tmp_path, "claude-only.md", name="claude-only", runtimes="claude-code")

        rendered = render_codex_skills(tmp_path)

        assert "foo" in rendered
        assert "claude-only" not in rendered

    def test_codex_restricted_command_is_included(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "codex-only.md", name="codex-only", runtimes="codex")

        rendered = render_codex_skills(tmp_path)

        assert "codex-only" in rendered

    def test_duplicate_frontmatter_name_raises(self, tmp_path: Path) -> None:
        """Two different canonical files declaring the same frontmatter `name:` must not silently collide."""
        _write_command(tmp_path, "foo.md", name="dup")
        _write_command(tmp_path, "foo2.md", name="dup")

        with pytest.raises(SkillRenderError, match="duplicate"):
            render_codex_skills(tmp_path)

    def test_supports_predicate_restricts_output(self, tmp_path: Path) -> None:
        """A caller's own restriction (e.g. an adapter's supports_command) takes precedence."""
        _write_command(tmp_path, "foo.md", name="foo")
        _write_command(tmp_path, "bar.md", name="bar")

        rendered = render_codex_skills(tmp_path, supports=lambda cmd: cmd.name == "foo")

        assert set(rendered) == {"foo"}


class TestMaterializeCombinedSkillSources:
    def test_combines_extra_and_standalone(self, tmp_path: Path) -> None:
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: hand-authored\n---\nBody.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(standalone, {"foo": "---\nname: sova-foo\n---\nBody.\n"}, scratch)

        assert (scratch / "foo" / "SKILL.md").is_file()
        assert (scratch / "hand-authored" / "SKILL.md").is_file()

    def test_name_collision_raises(self, tmp_path: Path) -> None:
        standalone = tmp_path / "skills"
        skill_dir = standalone / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: foo\n---\nHand-authored.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        with pytest.raises(SkillRenderError, match="collision"):
            materialize_combined_skill_sources(standalone, {"foo": "---\nname: sova-foo\n---\nBody.\n"}, scratch)

    def test_standalone_skill_squatting_the_reserved_prefix_raises(self, tmp_path: Path) -> None:
        """A standalone directory whose *name already carries the reserved prefix* (e.g.
        `skills/sova-foo/`) is rejected up front, before the ordinary collision check even
        runs: it isn't actually a command-derived skill, it's a standalone one squatting on
        a namespace reserved for command-derived output (issue #1136 finding)."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "sova-foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: sova-foo\n---\nHand-authored.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        with pytest.raises(SkillRenderError, match="reserved"):
            materialize_combined_skill_sources(
                standalone, {"foo": "---\nname: sova-foo\n---\nBody.\n"}, scratch, name_prefix="sova-"
            )

    def test_missing_standalone_dir_is_fine(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(tmp_path / "nonexistent", {"foo": "content"}, scratch)
        assert (scratch / "foo" / "SKILL.md").read_text(encoding="utf-8") == "content"

    def test_skips_standalone_skill_already_hand_authored_at_target(self, tmp_path: Path) -> None:
        """A generic rendered skill must not duplicate a pre-existing hand-authored one under
        the same plain name: installing both left `testing-patterns`/`sova-testing-patterns`
        as two competing skills with overlapping auto-activation triggers in this repo's own
        `.agents/skills/`."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "testing-patterns"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: testing-patterns\n---\nGeneric.\n", encoding="utf-8")

        existing_target = tmp_path / "installed"
        existing_skill = existing_target / "testing-patterns"
        existing_skill.mkdir(parents=True)
        (existing_skill / "SKILL.md").write_text("---\nname: testing-patterns\n---\nHand-authored.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(
            standalone, {}, scratch, name_prefix="sova-", existing_target_dir=existing_target
        )

        assert not (scratch / "testing-patterns").exists()

    def test_missing_manifest_does_not_silently_drop_unchanged_managed_content(self, tmp_path: Path) -> None:
        """A manifest lost to corruption or manual deletion must not be read as positive
        evidence the on-disk content is foreign: that would silently and permanently
        exclude a SOVA-managed skill from every future sync, with no way for even --force
        to recover it, since the exclusion happens before update_skills() ever sees the
        entry (issue #1136 finding). When the on-disk content still matches what would be
        rendered today, there's nothing to distinguish it from SOVA's own prior output, so
        it must flow through rather than being skipped."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "a-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: a-skill\n---\nUnchanged body.\n", encoding="utf-8")

        existing_target = tmp_path / "installed"
        existing_skill = existing_target / "a-skill"
        existing_skill.mkdir(parents=True)
        (existing_skill / "SKILL.md").write_text("---\nname: a-skill\n---\nUnchanged body.\n", encoding="utf-8")
        # No manifest file at all: simulates a deleted/corrupted .sova-manifest.json.

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(
            standalone, {}, scratch, name_prefix="sova-", existing_target_dir=existing_target
        )

        assert (scratch / "a-skill" / "SKILL.md").read_text(encoding="utf-8") == (
            "---\nname: a-skill\n---\nUnchanged body.\n"
        )

    def test_does_not_skip_when_no_existing_sibling(self, tmp_path: Path) -> None:
        """Without a pre-existing hand-authored sibling at the target, the standalone skill
        still renders normally (the skip is specific to an actual on-disk collision)."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "testing-patterns"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: testing-patterns\n---\nGeneric.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(
            standalone, {}, scratch, name_prefix="sova-", existing_target_dir=tmp_path / "installed"
        )

        assert (scratch / "testing-patterns" / "SKILL.md").is_file()

    def test_does_not_skip_a_standalone_skill_sova_already_manages(self, tmp_path: Path) -> None:
        """A bare-named standalone skill lands at the same path on every sync (issue #1136): a
        second sync must not mistake its own prior install for pre-existing hand-authored content,
        or it would never be able to update itself again."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "design-taste"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: design-taste\n---\nUpdated body.\n", encoding="utf-8")

        existing_target = tmp_path / "installed"
        existing_skill = existing_target / "design-taste"
        existing_skill.mkdir(parents=True)
        (existing_skill / "SKILL.md").write_text("---\nname: design-taste\n---\nStale body.\n", encoding="utf-8")
        (existing_target / MANIFEST_FILENAME).write_text(
            '{"version": 1, "commands": {"design-taste/SKILL.md": {"hash": "x", "managed": true}}}',
            encoding="utf-8",
        )

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(
            standalone, {}, scratch, name_prefix="sova-", existing_target_dir=existing_target
        )

        assert (scratch / "design-taste" / "SKILL.md").read_text(encoding="utf-8") == (
            "---\nname: design-taste\n---\nUpdated body.\n"
        )

    def test_name_prefix_does_not_reach_standalone_frontmatter_name(self, tmp_path: Path) -> None:
        """A standalone skill is a different artifact class: it installs bare even when
        extra (command-derived) entries in the same call get name_prefix (issue #1136)."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        skill_dir_name = "hand-authored"
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_dir_name}\n---\nBody.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(standalone, {}, scratch, name_prefix="sova-")

        content = (scratch / "hand-authored" / "SKILL.md").read_text(encoding="utf-8")
        assert "name: hand-authored" in content
        assert "sova-" not in content

    def test_claude_only_frontmatter_keys_are_stripped(self, tmp_path: Path) -> None:
        """Codex has no tool-permission concept: allowed_tools is Claude-only noise, not useful metadata."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: hand-authored\nallowed_tools: Read, Grep\n---\nBody.\n", encoding="utf-8"
        )

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(standalone, {}, scratch, name_prefix="sova-")

        content = (scratch / "hand-authored" / "SKILL.md").read_text(encoding="utf-8")
        assert "allowed_tools" not in content
        assert "name: hand-authored" in content
        assert "Body." in content

    def test_no_name_prefix_leaves_frontmatter_untouched(self, tmp_path: Path) -> None:
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: hand-authored\n---\nBody.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(standalone, {}, scratch)

        content = (scratch / "hand-authored" / "SKILL.md").read_text(encoding="utf-8")
        assert "name: hand-authored" in content

    def test_extra_entry_is_prefixed_in_the_scratch_directory_name(self, tmp_path: Path) -> None:
        """A command-derived entry's scratch directory name carries name_prefix directly: the
        caller installs the combined tree with no further name_prefix of its own."""
        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(
            tmp_path / "nonexistent", {"foo": "---\nname: sova-foo\n---\nBody.\n"}, scratch, name_prefix="sova-"
        )

        assert (scratch / "sova-foo" / "SKILL.md").is_file()
        assert not (scratch / "foo").exists()

    def test_sibling_file_raises_instead_of_being_silently_dropped(self, tmp_path: Path) -> None:
        """A SKILL.md referencing a sibling file must not ship a package missing that file."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "multi-file"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: multi-file\n---\nSee checklist.md.\n", encoding="utf-8")
        (skill_dir / "checklist.md").write_text("The checklist.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        with pytest.raises(SkillRenderError, match="checklist.md"):
            materialize_combined_skill_sources(standalone, {}, scratch)

    def test_sibling_directory_raises_too(self, tmp_path: Path) -> None:
        """A bundled-resource subdirectory (references/, scripts/) must not be silently dropped either."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "multi-dir"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: multi-dir\n---\nSee references/checklist.md.\n")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "checklist.md").write_text("The checklist.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        with pytest.raises(SkillRenderError, match="references"):
            materialize_combined_skill_sources(standalone, {}, scratch)

    def test_quoted_frontmatter_name_is_left_quoted_and_unprefixed(self, tmp_path: Path) -> None:
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text('---\nname: "hand-authored"\n---\nBody.\n', encoding="utf-8")

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(standalone, {}, scratch, name_prefix="sova-")

        content = (scratch / "hand-authored" / "SKILL.md").read_text(encoding="utf-8")
        assert 'name: "hand-authored"' in content

    def test_frontmatter_name_directory_mismatch_raises(self, tmp_path: Path) -> None:
        """A declared name that disagrees with its directory would otherwise reach the installed tree unnoticed."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: something-else\n---\nBody.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        with pytest.raises(SkillRenderError, match="does not match"):
            materialize_combined_skill_sources(standalone, {}, scratch, name_prefix="sova-")

    def test_standalone_skill_body_cross_reference_is_rewritten(self, tmp_path: Path) -> None:
        """A hand-authored skill is as likely to reference a /command as a canonical command body is."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: hand-authored\n---\nRun `/foo` first.\n", encoding="utf-8")

        scratch = tmp_path / "scratch"
        materialize_combined_skill_sources(standalone, {"foo": "---\nname: sova-foo\n---\nBody.\n"}, scratch)

        content = (scratch / "hand-authored" / "SKILL.md").read_text(encoding="utf-8")
        assert "`/foo`" not in content
        assert f"the `{SKILL_NAME_PREFIX}foo` skill" in content

    def test_standalone_skill_unrendered_idiom_raises(self, tmp_path: Path) -> None:
        """A standalone skill using an unresolvable {{ var }} must fail loudly, not ship verbatim."""
        standalone = tmp_path / "skills"
        skill_dir = standalone / "hand-authored"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: hand-authored\n---\nUse {{ some_new_var }} here.\n", encoding="utf-8"
        )

        scratch = tmp_path / "scratch"
        with pytest.raises(SkillRenderError, match="placeholder"):
            materialize_combined_skill_sources(standalone, {}, scratch)


class TestRenderSelfSkills:
    def test_renders_combined_tree_with_manifest(self, tmp_path: Path) -> None:
        """A full self-render produces one manifest covering both command-derived and standalone skills."""
        from sova.commands.skill_render import render_self_skills

        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        _write_command(commands_dir, "foo.md", name="foo")

        standalone_dir = tmp_path / "skills"
        skill_dir = standalone_dir / "bar"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: bar\n---\nBody.\n", encoding="utf-8")

        target = tmp_path / ".agents" / "skills"
        result = render_self_skills(root=tmp_path, target_dir=target)

        assert result.installed == 2
        assert (target / "sova-foo" / "SKILL.md").is_file()
        assert (target / "bar" / "SKILL.md").is_file()
        assert (target / ".sova-manifest.json").is_file()
