"""Tests for sova.core.spec_utils: issue-revision hashing and spec staleness (#1310)."""

from __future__ import annotations

import pytest

from sova.adapters.base import Task
from sova.core.spec_utils import compute_issue_revision_hash, spec_is_current


def _task(title: str = "Fix the thing", body: str = "Do the thing.") -> Task:
    return Task(id="42", title=title, body=body)


class TestComputeIssueRevisionHash:
    def test_deterministic(self) -> None:
        a = compute_issue_revision_hash("Title", "Body text")
        b = compute_issue_revision_hash("Title", "Body text")
        assert a == b

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(("Title A", "Body text"), ("Title B", "Body text"), id="title-changed"),
            pytest.param(("Title", "Body A"), ("Title", "Body B"), id="body-changed"),
            pytest.param(
                (
                    "Title",
                    "## Original Description\n\nDo the thing.\n\n---\n\n## Implementation Spec\n\nSpec stuff here.\n",
                ),
                (
                    "Title",
                    "## Original Description\n\nDo a completely different thing.\n\n---\n\n"
                    "## Implementation Spec\n\nSpec stuff here.\n",
                ),
                id="real-change-after-original-description-writeback",
            ),
        ],
    )
    def test_changes_with_content(self, first: tuple[str, str], second: tuple[str, str]) -> None:
        assert compute_issue_revision_hash(*first) != compute_issue_revision_hash(*second)

    def test_is_short_hex(self) -> None:
        digest = compute_issue_revision_hash("Title", "Body")
        assert len(digest) == 12
        int(digest, 16)  # does not raise

    @pytest.mark.parametrize(
        ("base", "variant"),
        [
            pytest.param(
                ("Title", "Body text"),
                ("Title", "Body text\n\n## Triage Assessment\n\nScore: 8/10\n"),
                id="triage-assessment-section-ignored",
            ),
            pytest.param(
                ("Title", "Body text"),
                ("Title", "Body text\n\n## Research\n\nFindings here.\n"),
                id="research-section-ignored",
            ),
            pytest.param(
                ("Title", "Body text"),
                ("Title", "Body text\n\n## Research\n\nFindings.\n\n## Triage Assessment\n\nScore: 8/10\n"),
                id="triage-and-research-stripped-in-any-order",
            ),
            # An LLM-authored Research section with its own `## ` subheading must leave no residue: appended
            # sections are stripped by position (first match to EOF), not per heading up to the next `## `.
            pytest.param(
                ("Title", "Body text"),
                ("Title", "Body text\n\n## Research\n\n### Findings\nSome text\n\n## More stuff\njunk\n"),
                id="research-subheading-residue-stripped",
            ),
            # The /spec command's optional Step 8 write-back restructures the body as
            # `## Original Description` + `---` + `## Implementation Spec`, purely cosmetically.
            pytest.param(
                ("Title", "Do the thing."),
                (
                    "Title",
                    "## Original Description\n\nDo the thing.\n\n---\n\n## Implementation Spec\n\nSpec stuff here.\n",
                ),
                id="original-description-writeback-preserves-hash",
            ),
            # `remove_section` trims only when it removed a section, so a body ending in whitespace used to
            # hash differently once SOVA appended a Triage Assessment: the drift the stripping exists to prevent.
            pytest.param(
                ("Title", "Body text\n"),
                ("Title", "Body text\n\n## Triage Assessment\n\nScore: 8/10\n"),
                id="trailing-whitespace-before-appended-section",
            ),
            pytest.param(("Title", "Body text"), ("Title", "  Body text\n\n"), id="body-surrounding-whitespace"),
            # The legacy title comparison in `spec_is_current` already trims `task.title`; the hash must
            # normalize the same way or one path sees a whitespace edit as a real change.
            pytest.param(("Title", "Body text"), ("  Title  \n", "Body text"), id="title-surrounding-whitespace"),
        ],
    )
    def test_hash_unchanged(self, base: tuple[str, str], variant: tuple[str, str]) -> None:
        assert compute_issue_revision_hash(*base) == compute_issue_revision_hash(*variant)

    def test_empty_body(self) -> None:
        # Must not raise.
        compute_issue_revision_hash("Title", "")


class TestSpecIsCurrent:
    def test_current_when_hash_matches(self) -> None:
        task = _task()
        digest = compute_issue_revision_hash(task.title, task.body)
        spec_text = f"# Spec: Fix the thing\n\n**Issue revision**: {digest}\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is True

    @pytest.mark.parametrize(
        ("title", "spec_text", "expected"),
        [
            pytest.param(
                "Fix the thing",
                "# Spec: Fix the thing\n\n**Issue revision**: deadbeef0000\n\n## Solution\nDo it.\n",
                False,
                id="stale-when-hash-mismatches",
            ),
            # A marker with a hand-edited, non-hex value is stale and must not fall through to the legacy
            # title comparison (which would match here): a spec carrying the marker is never compared by title.
            pytest.param(
                "Fix the thing",
                "# Spec: Fix the thing\n\n**Issue revision**: not-a-valid-hash!!\n",
                False,
                id="corrupted-hash-is-stale",
            ),
            pytest.param(
                "Fix the thing", "# Spec: Fix the thing\n\n## Solution\nDo it.\n", True, id="legacy-matching-title"
            ),
            pytest.param(
                "Fix the thing",
                "# Spec: Fix a different thing\n\n## Solution\nDo it.\n",
                False,
                id="legacy-mismatched-title",
            ),
            pytest.param(
                "  Fix the thing  ",
                "# Spec: Fix the thing\n\n## Solution\nDo it.\n",
                True,
                id="legacy-title-match-trims-whitespace",
            ),
            # No revision line and no parseable title: nothing to compare, so don't block.
            pytest.param("Fix the thing", "## Solution\nDo it.\n", True, id="legacy-no-title-heading"),
            # A marker with no value must not capture text from a following line as the hash
            # (`\s*` used to span the newline).
            pytest.param(
                "Fix the thing",
                "# Spec: Fix the thing\n\n**Issue revision**:\n\nabc\n\n## Solution\nDo it.\n",
                False,
                id="marker-value-on-next-line-not-captured",
            ),
            # A `# ` line inside a fenced block before the real title is not the title: the real one is
            # found and compared instead, and it matches here.
            pytest.param(
                "Fix the thing",
                "```bash\n# some note\necho hi\n```\n\n# Spec: Fix the thing\n\n## Solution\nDo it.\n",
                True,
                id="legacy-title-inside-fence-ignored",
            ),
            # The fix for the case above: a real title behind a leading fence that does NOT match must be
            # caught as stale, not trusted just because the first line isn't a heading.
            pytest.param(
                "Fix the thing",
                "```bash\n# some note\necho hi\n```\n\n# Spec: Fix a different thing\n\n## Solution\nDo it.\n",
                False,
                id="legacy-title-behind-fence-mismatch",
            ),
            # A first line of frontmatter prose (not a fence, not a heading) must not hide the real title.
            pytest.param(
                "Fix the thing",
                "**Status**: approved\n\n# Spec: Fix a different thing\n\n## Solution\nDo it.\n",
                False,
                id="frontmatter-before-title",
            ),
            # Frontmatter quoting the marker format inside a fence (as this repo's docs do) is not the real
            # stamp; without fence-masking `deadbeef0000` read as a mismatched stamp and marked the spec stale.
            pytest.param(
                "Fix the thing",
                "# Spec: Fix the thing\n"
                "Stamp specs with a line like:\n\n"
                "```\n"
                "**Issue revision**: deadbeef0000\n"
                "```\n\n"
                "## Solution\nDo it.\n",
                True,
                id="marker-quoted-in-fence-not-the-stamp",
            ),
        ],
    )
    def test_spec_text_without_a_matching_stamp(self, title: str, spec_text: str, expected: bool) -> None:
        assert spec_is_current(spec_text, _task(title=title)) is expected

    def test_stale_when_body_changes_after_spec_written(self) -> None:
        task = _task(body="Original body.")
        digest = compute_issue_revision_hash(task.title, task.body)
        spec_text = f"# Spec: Fix the thing\n\n**Issue revision**: {digest}\n"
        changed_task = _task(body="Body was edited after the spec was written.")
        assert spec_is_current(spec_text, changed_task) is False

    def test_hash_comparison_is_case_insensitive(self) -> None:
        task = _task()
        digest = compute_issue_revision_hash(task.title, task.body).upper()
        spec_text = f"# Spec: Fix the thing\n\n**Issue revision**: {digest}\n"
        assert spec_is_current(spec_text, task) is True

    def test_legacy_spec_title_without_spec_prefix_is_compared_verbatim(self) -> None:
        """A heading that omits the ``Spec: `` prefix compares as the whole title."""
        task = _task(title="Fix the thing")
        assert spec_is_current("# Fix the thing\n\n## Solution\nDo it.\n", task) is True
        assert spec_is_current("# Fix another thing\n\n## Solution\nDo it.\n", task) is False

    def test_hash_of_wrong_length_is_rejected(self) -> None:
        """A value that is hex but not exactly 12 chars must not be accepted as a hash."""
        task = _task()
        digest = compute_issue_revision_hash(task.title, task.body)
        spec_text = f"# Spec: Fix the thing\n\n**Issue revision**: {digest[:10]}\n"
        assert spec_is_current(spec_text, task) is False

    def test_marker_quoted_in_fence_does_not_shadow_the_real_stamp(self) -> None:
        """A fenced example quoting the marker format, placed before the real
        stamp within the spec's frontmatter, must not be read instead of it."""
        task = _task()
        digest = compute_issue_revision_hash(task.title, task.body)
        spec_text = (
            "# Spec: Fix the thing\n"
            "```\n"
            "**Issue revision**: deadbeef0000\n"
            "```\n"
            f"**Issue revision**: {digest}\n\n"
            "## Solution\nDo it.\n"
        )
        assert spec_is_current(spec_text, task) is True

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            (
                "complexity_line_anchor",
                "# Spec: Fix the thing\n**Status**: approved\n**Complexity**: simple\n\n## Solution\nDo it.\n",
            ),
            (
                "title_heading_anchor",
                "# Spec: Fix the thing\n\n## Solution\nDo it.\n",
            ),
            (
                "bare_prepend_no_heading",
                "## Solution\nDo it.\n",
            ),
            (
                "fenced_block_before_real_anchors",
                "```bash\n# note\n```\n\n# Spec: Fix the thing\n\n## Problem\nx\n",
            ),
            (
                "complexity_line_after_first_heading",
                "# Spec: Fix the thing\n\n## Metadata\n\n**Complexity**: simple\n\n## Problem\nx\n",
            ),
        ],
    )
    def test_round_trip_stamped_spec_is_current_until_body_changes(self, label: str, content: str) -> None:
        """The writer (``_stamp_issue_revision``) and the checker (``spec_is_current``) must stay
        mutually compatible across every placement branch the writer can take (the complexity-line
        anchor, the title-heading fallback, the bare-prepend fallback, and both adversarial shapes
        that used to place the stamp outside the frontmatter span the checker reads): a spec stamped
        for a task is current for that task, and goes stale the moment the task's body changes."""
        from sova.core.steps.spec import _stamp_issue_revision

        task = _task(title="Fix the thing", body="Original body.")
        stamped = _stamp_issue_revision(content, task.title, task.body)

        assert spec_is_current(stamped, task) is True, label

        task.body = "Body was edited after the spec was written."
        assert spec_is_current(stamped, task) is False, label
