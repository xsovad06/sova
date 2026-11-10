"""Tests for sova.core.steps.commit_convention: shared commit/PR title validation."""

from __future__ import annotations

import pytest

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
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param("feat(core): add widget", True, id="valid-type-and-scope"),
            pytest.param("ci: add workflow step", True, id="ci-is-scopeless"),
            # invariants/commit-format.sh accepts "ci(scope): ..." via its general type(scope) pattern, so
            # "ci" is scope-optional: a stricter validator would discard a perfectly pushable LLM proposal.
            pytest.param("ci(core): pin the runner image", True, id="ci-accepts-configured-scope"),
            pytest.param("ci(perf): add workflow step", False, id="ci-rejects-unconfigured-scope"),
            pytest.param("feat: add widget", False, id="non-ci-type-requires-scope"),
            pytest.param("perf(core): speed it up", False, id="unconfigured-type"),
            pytest.param("feat(perf): speed it up", False, id="unconfigured-scope"),
            pytest.param("feat(core): Add widget", False, id="uppercase-subject"),
            # lowercase_first_letter() leaves an all-caps opening word alone, so the validator must accept
            # the line it produces.
            pytest.param("feat(core): LLM provider abstraction", True, id="acronym-led-subject"),
            pytest.param("feat(core): ", False, id="blank-subject"),
            pytest.param("just some text with no colon", False, id="malformed-line"),
        ],
    )
    def test_validates(self, line: str, expected: bool) -> None:
        assert is_valid_commit_line(line, types=TYPES, scopes=SCOPES) is expected

    def test_line_over_72_chars_is_rejected(self) -> None:
        subject = "a" * 61
        line = f"feat(core): {subject}"
        assert len(line) > 72
        assert not is_valid_commit_line(line, types=TYPES, scopes=SCOPES)

    def test_line_at_exactly_72_chars_is_accepted(self) -> None:
        line = "feat(core): " + "a" * (72 - len("feat(core): "))
        assert len(line) == 72
        assert is_valid_commit_line(line, types=TYPES, scopes=SCOPES)

    def test_empty_configured_scopes_rejects_every_non_ci_type(self) -> None:
        assert not is_valid_commit_line("feat(core): add widget", types=TYPES, scopes=[])


class TestResolveDefaultScope:
    @pytest.mark.parametrize(
        ("scopes", "expected"),
        [
            pytest.param(["widgets", "core"], "core", id="prefers-core"),
            # A project whose scopes are entirely its own must get a scope its own commit-format check
            # accepts, not "core".
            pytest.param(["accounts", "billing"], "accounts", id="first-configured-when-core-absent"),
            pytest.param([], "core", id="empty-scopes-last-resort"),
        ],
    )
    def test_resolves(self, scopes: list[str], expected: str) -> None:
        assert resolve_default_scope(scopes) == expected


class TestResolveDefaultType:
    @pytest.mark.parametrize(
        ("types", "expected"),
        [
            pytest.param(["fix", "feat"], "feat", id="prefers-feat"),
            # A project whose types omit "feat"/"fix" must get a type its own commit-format check accepts.
            pytest.param(["feature", "bugfix"], "feature", id="first-configured-when-preferred-absent"),
            pytest.param([], "feat", id="empty-types-last-resort"),
            # Both callers append a (scope) segment, which is_valid_commit_line() rejects for "ci":
            # picking it would hand them a type they cannot legally use.
            pytest.param(["ci", "chore"], "chore", id="skips-scopeless-ci"),
            pytest.param(["ci"], "ci", id="only-type-is-scopeless"),
        ],
    )
    def test_resolves(self, types: list[str], expected: str) -> None:
        assert resolve_default_type(types) == expected

    def test_honors_an_explicit_preferred_type(self) -> None:
        assert resolve_default_type(["fix", "feat"], preferred="fix") == "fix"
        assert resolve_default_type(["bugfix"], preferred="fix") == "bugfix"


class TestFormatTypeScope:
    @pytest.mark.parametrize(
        ("commit_type", "expected"),
        [
            pytest.param("feat", "feat(core)", id="ordinary-type-gets-scope"),
            pytest.param("ci", "ci", id="scopeless-ci-omits-scope"),
        ],
    )
    def test_formats(self, commit_type: str, expected: str) -> None:
        assert format_type_scope(commit_type, "core") == expected


class TestMatchTypePrefix:
    @pytest.mark.parametrize(
        ("text", "types", "expected"),
        [
            pytest.param("fix: broken login", ["feat", "fix"], "fix", id="bare-type-prefix"),
            pytest.param("fix(auth): broken login", ["feat", "fix"], "fix", id="scoped-type-prefix"),
            pytest.param("perf: speed it up", ["perf", "feat"], "perf", id="configured-non-sova-type"),
        ],
    )
    def test_matches(self, text: str, types: list[str], expected: str) -> None:
        match = match_type_prefix(text, types)
        assert match is not None
        assert match.group("type") == expected

    @pytest.mark.parametrize(
        ("text", "types"),
        [
            pytest.param("perf: speed it up", ["feat", "fix"], id="unconfigured-type"),
            pytest.param("feat: add widget", [], id="empty-types"),
        ],
    )
    def test_no_match(self, text: str, types: list[str]) -> None:
        assert match_type_prefix(text, types) is None


class TestStripTypePrefix:
    @pytest.mark.parametrize(
        ("types", "expected"),
        [
            pytest.param(["perf", "feat"], "speed it up", id="configured-prefix-stripped"),
            pytest.param(["feat", "fix"], "perf: speed it up", id="unconfigured-prefix-kept"),
        ],
    )
    def test_strips(self, types: list[str], expected: str) -> None:
        assert strip_type_prefix("perf: speed it up", types) == expected


class TestInferScopeFromPaths:
    @pytest.mark.parametrize(
        ("files", "scopes", "expected"),
        [
            pytest.param(["apps/accounts/models.py"], ["accounts", "billing"], "accounts", id="directory-segment"),
            pytest.param(["docs/pricing.md"], ["accounts", "docs"], "docs", id="top-level-directory"),
            # A single-file change outside any configured directory still names a configured scope when the
            # filename itself does (a top-level app/billing.py and a "billing" scope).
            pytest.param(["app/billing.py"], ["accounts", "billing"], "billing", id="filename-stem"),
            pytest.param(
                ["apps/accounts/a.py", "apps/accounts/b.py", "apps/billing/c.py"],
                ["accounts", "billing"],
                "accounts",
                id="most-touched-scope-wins",
            ),
            # Any unmatched path falls back to the *default* scope (first configured), not whatever scope
            # matched: ordering "billing" first means this only holds if the unmatched-path rule fired.
            pytest.param(
                ["apps/accounts/a.py", "README.md"],
                ["billing", "accounts"],
                "billing",
                id="unmatched-path-uses-default",
            ),
            pytest.param(["apps/accounts/a.py"], ["billing", "accounts"], "accounts", id="matched-scope-wins"),
            pytest.param([], ["accounts", "billing"], "accounts", id="empty-files-uses-default"),
            pytest.param(None, ["accounts", "billing"], "accounts", id="no-files-uses-default"),
            # sova/utils/db.py must not match scope "db" (which names sova/db/): the filename-stem fallback
            # applies only to a single-directory-segment path, so this falls back to the default.
            pytest.param(["sova/utils/db.py"], ["accounts", "db"], "accounts", id="deep-path-not-scoped-by-filename"),
        ],
    )
    def test_infers(self, files: list[str] | None, scopes: list[str], expected: str) -> None:
        assert infer_scope_from_paths(files, scopes) == expected


class TestInferTypeFromPaths:
    @pytest.mark.parametrize(
        ("files", "types", "expected"),
        [
            pytest.param(["docs/guide.md"], ["feat", "docs"], "docs", id="docs-only"),
            # A project whose commit.types omits "docs" must not get it anyway.
            pytest.param(["docs/guide.md"], ["feat", "fix"], "feat", id="docs-type-requires-configuration"),
            pytest.param(["tests/test_x.py"], ["feat", "test"], "test", id="tests-only"),
            pytest.param(["docs/guide.md", "sova/core/x.py"], ["feat", "docs"], "feat", id="mixed-uses-default"),
            pytest.param([], ["feat", "docs"], "feat", id="empty-uses-default"),
            # Docs living outside SOVA's own "docs/" layout must still be recognized.
            pytest.param(["documentation/guide.md"], ["feat", "docs"], "docs", id="documentation-directory"),
            pytest.param(["__tests__/widget.test.ts"], ["feat", "test"], "test", id="jest-tests-directory"),
            pytest.param(["pkg/widget_test.go"], ["feat", "test"], "test", id="go-test-suffix"),
            pytest.param(["src/widget.spec.ts"], ["feat", "test"], "test", id="js-spec-suffix"),
        ],
    )
    def test_infers(self, files: list[str], types: list[str], expected: str) -> None:
        assert infer_type_from_paths(files, types) == expected


class TestLowercaseFirstLetter:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("Add widget", "add widget", id="first-char-only"),
            pytest.param("LLM provider abstraction", "LLM provider abstraction", id="all-caps-word-preserved"),
            pytest.param("", "", id="empty-string"),
            pytest.param("add widget", "add widget", id="already-lowercase"),
        ],
    )
    def test_lowercases(self, text: str, expected: str) -> None:
        assert lowercase_first_letter(text) == expected


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
