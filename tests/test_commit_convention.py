"""Tests for sova.core.steps.commit_convention: shared commit/PR title validation."""

from __future__ import annotations

from sova.core.steps.commit_convention import (
    MAX_LINE_LENGTH,
    format_type_scope,
    infer_scope_from_paths,
    infer_type_from_paths,
    is_valid_commit_line,
    lowercase_first_letter,
    match_type_prefix,
    resolve_default_scope,
    resolve_default_type,
    strip_type_prefix,
    truncate_commit_line,
)

TYPES = frozenset({"feat", "fix", "refactor", "test", "docs", "chore", "ci"})
SCOPES = frozenset({"core", "dashboard", "cli"})


class TestIsValidCommitLine:
    def test_valid_type_and_scope(self) -> None:
        assert is_valid_commit_line("feat(core): add widget", types=TYPES, scopes=SCOPES)

    def test_ci_is_scopeless(self) -> None:
        assert is_valid_commit_line("ci: add workflow step", types=TYPES, scopes=SCOPES)

    def test_ci_accepts_a_configured_scope(self) -> None:
        """invariants/commit-format.sh accepts "ci(scope): ..." via its
        general type(scope) pattern, so "ci" is scope-optional, not
        scope-forbidden: a validator stricter than the invariant it encodes
        would discard a perfectly pushable LLM proposal."""
        assert is_valid_commit_line("ci(core): pin the runner image", types=TYPES, scopes=SCOPES)

    def test_ci_rejects_an_unconfigured_scope(self) -> None:
        assert not is_valid_commit_line("ci(perf): add workflow step", types=TYPES, scopes=SCOPES)

    def test_non_ci_type_requires_a_scope(self) -> None:
        assert not is_valid_commit_line("feat: add widget", types=TYPES, scopes=SCOPES)

    def test_unconfigured_type_is_rejected(self) -> None:
        assert not is_valid_commit_line("perf(core): speed it up", types=TYPES, scopes=SCOPES)

    def test_unconfigured_scope_is_rejected(self) -> None:
        assert not is_valid_commit_line("feat(perf): speed it up", types=TYPES, scopes=SCOPES)

    def test_uppercase_subject_is_rejected(self) -> None:
        assert not is_valid_commit_line("feat(core): Add widget", types=TYPES, scopes=SCOPES)

    def test_acronym_led_subject_is_accepted(self) -> None:
        """lowercase_first_letter() leaves an all-caps opening word alone, so
        the validator must accept the line it produces, not reject it."""
        assert is_valid_commit_line("feat(core): LLM provider abstraction", types=TYPES, scopes=SCOPES)

    def test_blank_subject_is_rejected(self) -> None:
        assert not is_valid_commit_line("feat(core): ", types=TYPES, scopes=SCOPES)

    def test_line_over_72_chars_is_rejected(self) -> None:
        subject = "a" * 61
        line = f"feat(core): {subject}"
        assert len(line) > 72
        assert not is_valid_commit_line(line, types=TYPES, scopes=SCOPES)

    def test_line_at_exactly_72_chars_is_accepted(self) -> None:
        line = "feat(core): " + "a" * (72 - len("feat(core): "))
        assert len(line) == 72
        assert is_valid_commit_line(line, types=TYPES, scopes=SCOPES)

    def test_malformed_line_is_rejected(self) -> None:
        assert not is_valid_commit_line("just some text with no colon", types=TYPES, scopes=SCOPES)

    def test_empty_configured_scopes_rejects_every_non_ci_type(self) -> None:
        assert not is_valid_commit_line("feat(core): add widget", types=TYPES, scopes=[])


class TestResolveDefaultScope:
    def test_prefers_core_when_configured(self) -> None:
        assert resolve_default_scope(["widgets", "core"]) == "core"

    def test_falls_back_to_first_configured_scope_when_core_is_absent(self) -> None:
        """A project whose scopes are entirely its own (e.g. accounts/billing)
        must get a scope its own commit-format check accepts, not "core"."""
        assert resolve_default_scope(["accounts", "billing"]) == "accounts"

    def test_empty_scopes_returns_core_as_a_last_resort(self) -> None:
        assert resolve_default_scope([]) == "core"


class TestResolveDefaultType:
    def test_prefers_feat_when_configured(self) -> None:
        assert resolve_default_type(["fix", "feat"]) == "feat"

    def test_falls_back_to_first_configured_type_when_preferred_is_absent(self) -> None:
        """A project whose types omit "feat"/"fix" entirely must get a type
        its own commit-format check accepts, not a hardcoded "feat"."""
        assert resolve_default_type(["feature", "bugfix"]) == "feature"

    def test_empty_types_returns_preferred_as_a_last_resort(self) -> None:
        assert resolve_default_type([]) == "feat"

    def test_honors_an_explicit_preferred_type(self) -> None:
        assert resolve_default_type(["fix", "feat"], preferred="fix") == "fix"
        assert resolve_default_type(["bugfix"], preferred="fix") == "bugfix"

    def test_skips_the_scopeless_ci_type_when_preferred_is_absent(self) -> None:
        """Both callers of this function unconditionally append a (scope)
        segment, which is_valid_commit_line() rejects for "ci": picking it
        here would hand them a type they cannot legally use."""
        assert resolve_default_type(["ci", "chore"]) == "chore"

    def test_returns_the_only_type_when_every_type_is_scopeless(self) -> None:
        assert resolve_default_type(["ci"]) == "ci"


class TestFormatTypeScope:
    def test_appends_the_scope_for_an_ordinary_type(self) -> None:
        assert format_type_scope("feat", "core") == "feat(core)"

    def test_omits_the_scope_for_the_scopeless_ci_type(self) -> None:
        assert format_type_scope("ci", "core") == "ci"


class TestMatchTypePrefix:
    def test_matches_a_bare_type_prefix(self) -> None:
        match = match_type_prefix("fix: broken login", ["feat", "fix"])
        assert match is not None
        assert match.group("type") == "fix"

    def test_matches_a_scoped_type_prefix(self) -> None:
        match = match_type_prefix("fix(auth): broken login", ["feat", "fix"])
        assert match is not None
        assert match.group("type") == "fix"

    def test_matches_a_configured_non_sova_type(self) -> None:
        match = match_type_prefix("perf: speed it up", ["perf", "feat"])
        assert match is not None
        assert match.group("type") == "perf"

    def test_no_match_for_an_unconfigured_type(self) -> None:
        assert match_type_prefix("perf: speed it up", ["feat", "fix"]) is None

    def test_no_match_for_empty_types(self) -> None:
        assert match_type_prefix("feat: add widget", []) is None


class TestStripTypePrefix:
    def test_strips_a_configured_prefix(self) -> None:
        assert strip_type_prefix("perf: speed it up", ["perf", "feat"]) == "speed it up"

    def test_leaves_an_unconfigured_prefix_alone(self) -> None:
        assert strip_type_prefix("perf: speed it up", ["feat", "fix"]) == "perf: speed it up"


class TestInferScopeFromPaths:
    def test_matches_a_directory_segment(self) -> None:
        assert infer_scope_from_paths(["apps/accounts/models.py"], ["accounts", "billing"]) == "accounts"

    def test_matches_a_top_level_directory(self) -> None:
        assert infer_scope_from_paths(["docs/pricing.md"], ["accounts", "docs"]) == "docs"

    def test_matches_a_filename_stem_when_no_directory_matches(self) -> None:
        """A single-file change outside any configured directory still names
        a configured scope when the filename itself does (e.g. a project
        with a top-level app/billing.py and a "billing" scope)."""
        assert infer_scope_from_paths(["app/billing.py"], ["accounts", "billing"]) == "billing"

    def test_most_touched_scope_wins(self) -> None:
        files = ["apps/accounts/a.py", "apps/accounts/b.py", "apps/billing/c.py"]
        assert infer_scope_from_paths(files, ["accounts", "billing"]) == "accounts"

    def test_a_single_unmatched_path_falls_back_to_default(self) -> None:
        """A change that is mostly "accounts" must still fall back to the
        *default* scope (first configured) when any path is unmatched, not
        to whatever scope happens to match: ordering "billing" first here
        means the assertion only holds if the unmatched-path rule fired."""
        files = ["apps/accounts/a.py", "README.md"]
        assert infer_scope_from_paths(files, ["billing", "accounts"]) == "billing"  # first configured, via default

    def test_without_the_unmatched_path_the_matched_scope_wins(self) -> None:
        files = ["apps/accounts/a.py"]
        assert infer_scope_from_paths(files, ["billing", "accounts"]) == "accounts"

    def test_empty_changed_files_falls_back_to_default(self) -> None:
        assert infer_scope_from_paths([], ["accounts", "billing"]) == "accounts"
        assert infer_scope_from_paths(None, ["accounts", "billing"]) == "accounts"

    def test_a_deep_path_is_never_mis_scoped_by_its_own_filename(self) -> None:
        """sova/utils/db.py must not match scope "db" (which names sova/db/):
        the filename-stem fallback only applies to a single-directory-segment
        path (app/billing.py), never a deeper one where the filename is
        incidental to its real location."""
        files = ["sova/utils/db.py"]
        assert infer_scope_from_paths(files, ["accounts", "db"]) == "accounts"  # falls back to default, not "db"


class TestInferTypeFromPaths:
    def test_docs_only_change_is_docs(self) -> None:
        assert infer_type_from_paths(["docs/guide.md"], ["feat", "docs"]) == "docs"

    def test_docs_type_requires_configuration(self) -> None:
        """A project whose commit.types omits "docs" must not get it anyway."""
        assert infer_type_from_paths(["docs/guide.md"], ["feat", "fix"]) == "feat"

    def test_tests_only_change_is_test(self) -> None:
        assert infer_type_from_paths(["tests/test_x.py"], ["feat", "test"]) == "test"

    def test_mixed_change_falls_back_to_default_type(self) -> None:
        assert infer_type_from_paths(["docs/guide.md", "sova/core/x.py"], ["feat", "docs"]) == "feat"

    def test_empty_changed_files_falls_back_to_default_type(self) -> None:
        assert infer_type_from_paths([], ["feat", "docs"]) == "feat"

    def test_documentation_directory_is_recognized_as_docs(self) -> None:
        """A project whose docs live outside SOVA's own "docs/" layout must
        still be recognized, not silently fall back to the default type."""
        assert infer_type_from_paths(["documentation/guide.md"], ["feat", "docs"]) == "docs"

    def test_jest_style_tests_directory_is_recognized(self) -> None:
        assert infer_type_from_paths(["__tests__/widget.test.ts"], ["feat", "test"]) == "test"

    def test_go_style_test_suffix_is_recognized(self) -> None:
        assert infer_type_from_paths(["pkg/widget_test.go"], ["feat", "test"]) == "test"

    def test_js_style_spec_suffix_is_recognized(self) -> None:
        assert infer_type_from_paths(["src/widget.spec.ts"], ["feat", "test"]) == "test"


class TestLowercaseFirstLetter:
    def test_lowercases_first_char_only(self) -> None:
        assert lowercase_first_letter("Add widget") == "add widget"

    def test_preserves_rest_of_string_case(self) -> None:
        assert lowercase_first_letter("LLM provider abstraction") == "LLM provider abstraction"

    def test_empty_string_is_unchanged(self) -> None:
        assert lowercase_first_letter("") == ""

    def test_already_lowercase_is_unchanged(self) -> None:
        assert lowercase_first_letter("add widget") == "add widget"


GWYM_TITLE = (
    "Make the pricing claims match the app: drop tax optimization, gate the health score trend, drop the plan parameter"
)


class TestTruncateCommitLine:
    def test_short_line_is_unchanged(self) -> None:
        assert truncate_commit_line("feat(core): add a widget") == "feat(core): add a widget"

    def test_gwym_title_is_trimmed_to_a_valid_line(self) -> None:
        line = truncate_commit_line(f"docs(core): {lowercase_first_letter(GWYM_TITLE)}")

        assert len(line) <= MAX_LINE_LENGTH
        assert is_valid_commit_line(line, types=["docs"], scopes=["core"])

    def test_cuts_at_a_word_boundary(self) -> None:
        line = truncate_commit_line("feat(core): " + " ".join(["word"] * 30))

        assert len(line) <= MAX_LINE_LENGTH
        assert not line.endswith("wor")

    def test_prefix_is_never_cut(self) -> None:
        line = truncate_commit_line("refactor(dashboard): " + "x" * 200)

        assert line.startswith("refactor(dashboard): ")
        assert len(line) <= MAX_LINE_LENGTH

    def test_exact_boundary_fit_keeps_the_whole_last_word(self) -> None:
        """When the cut lands exactly on a word boundary, the last word that
        fits must survive: room for "aaa bbb" (7 chars) must not drop "bbb"
        just because a boundary-trim always ran regardless of need."""
        line = truncate_commit_line("feat(core): aaa bbb ccc", limit=19)

        assert line == "feat(core): aaa bbb"
