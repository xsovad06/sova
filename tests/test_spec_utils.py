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

    def test_changes_with_title(self) -> None:
        a = compute_issue_revision_hash("Title A", "Body text")
        b = compute_issue_revision_hash("Title B", "Body text")
        assert a != b

    def test_changes_with_body(self) -> None:
        a = compute_issue_revision_hash("Title", "Body A")
        b = compute_issue_revision_hash("Title", "Body B")
        assert a != b

    def test_is_short_hex(self) -> None:
        digest = compute_issue_revision_hash("Title", "Body")
        assert len(digest) == 12
        int(digest, 16)  # does not raise

    def test_ignores_triage_assessment_section(self) -> None:
        base = compute_issue_revision_hash("Title", "Body text")
        with_triage = compute_issue_revision_hash("Title", "Body text\n\n## Triage Assessment\n\nScore: 8/10\n")
        assert base == with_triage

    def test_ignores_research_section(self) -> None:
        base = compute_issue_revision_hash("Title", "Body text")
        with_research = compute_issue_revision_hash("Title", "Body text\n\n## Research\n\nFindings here.\n")
        assert base == with_research

    def test_triage_and_research_both_stripped_regardless_of_order(self) -> None:
        base = compute_issue_revision_hash("Title", "Body text")
        combined = compute_issue_revision_hash(
            "Title",
            "Body text\n\n## Research\n\nFindings.\n\n## Triage Assessment\n\nScore: 8/10\n",
        )
        assert base == combined

    def test_research_subheading_residue_is_fully_stripped(self) -> None:
        """An LLM-authored Research section containing its own ``## `` subheading must not
        leave residue in the hash: appended sections are stripped by position (first match
        to EOF), not per-heading up to the next ``## ``, which would stop at that subheading."""
        base = compute_issue_revision_hash("Title", "Body text")
        with_subheading = compute_issue_revision_hash(
            "Title",
            "Body text\n\n## Research\n\n### Findings\nSome text\n\n## More stuff\njunk\n",
        )
        assert base == with_subheading

    def test_original_description_writeback_preserves_hash(self) -> None:
        """The ``/spec`` command's optional Step 8 write-back restructures the whole issue
        body as ``## Original Description`` + ``---`` + ``## Implementation Spec``, purely
        cosmetically. That restructuring must not change the hash it is meant to protect."""
        base = compute_issue_revision_hash("Title", "Do the thing.")
        after_writeback = compute_issue_revision_hash(
            "Title",
            "## Original Description\n\nDo the thing.\n\n---\n\n## Implementation Spec\n\nSpec stuff here.\n",
        )
        assert base == after_writeback

    def test_original_description_writeback_still_detects_real_change(self) -> None:
        after_writeback = compute_issue_revision_hash(
            "Title",
            "## Original Description\n\nDo the thing.\n\n---\n\n## Implementation Spec\n\nSpec stuff here.\n",
        )
        changed = compute_issue_revision_hash(
            "Title",
            "## Original Description\n\nDo a completely different thing.\n\n---\n\n"
            "## Implementation Spec\n\nSpec stuff here.\n",
        )
        assert after_writeback != changed

    def test_empty_body(self) -> None:
        # Must not raise.
        compute_issue_revision_hash("Title", "")

    def test_trailing_whitespace_does_not_change_hash_when_section_appended(self) -> None:
        """A body ending in a newline must hash the same before and after an append.

        ``remove_section`` trims its result only when it actually removed a
        section, so a body whose own text ends in whitespace used to hash
        differently once SOVA appended a Triage Assessment section: exactly the
        drift ``_APPENDED_SECTION_HEADINGS`` exists to prevent.
        """
        base = compute_issue_revision_hash("Title", "Body text\n")
        with_triage = compute_issue_revision_hash("Title", "Body text\n\n## Triage Assessment\n\nScore: 8/10\n")
        assert base == with_triage

    def test_surrounding_whitespace_in_body_is_normalized(self) -> None:
        assert compute_issue_revision_hash("Title", "  Body text\n\n") == compute_issue_revision_hash(
            "Title", "Body text"
        )

    def test_surrounding_whitespace_in_title_is_normalized(self) -> None:
        """The legacy title-comparison path in ``spec_is_current`` already trims
        ``task.title``; the hash must normalize the same way or a title edited
        to add/remove surrounding whitespace looks like a real content change
        to one path but not the other."""
        assert compute_issue_revision_hash("  Title  \n", "Body text") == compute_issue_revision_hash(
            "Title", "Body text"
        )


class TestSpecIsCurrent:
    def test_current_when_hash_matches(self) -> None:
        task = _task()
        digest = compute_issue_revision_hash(task.title, task.body)
        spec_text = f"# Spec: Fix the thing\n\n**Issue revision**: {digest}\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is True

    def test_stale_when_hash_mismatches(self) -> None:
        task = _task()
        spec_text = "# Spec: Fix the thing\n\n**Issue revision**: deadbeef0000\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is False

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

    def test_corrupted_hash_treated_as_stale_not_raising(self) -> None:
        """A marker present with a hand-edited, non-hex value is stale, not a legacy fallback.

        Must not fall through to the legacy title comparison even though the
        spec's title here would otherwise match: a spec new enough to carry
        the marker is never compared by title.
        """
        task = _task()
        spec_text = "# Spec: Fix the thing\n\n**Issue revision**: not-a-valid-hash!!\n"
        assert spec_is_current(spec_text, task) is False

    def test_legacy_spec_with_matching_title_is_current(self) -> None:
        task = _task(title="Fix the thing")
        spec_text = "# Spec: Fix the thing\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is True

    def test_legacy_spec_with_mismatched_title_is_stale(self) -> None:
        task = _task(title="Fix the thing")
        spec_text = "# Spec: Fix a different thing\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is False

    def test_legacy_spec_title_match_trims_whitespace(self) -> None:
        task = _task(title="  Fix the thing  ")
        spec_text = "# Spec: Fix the thing\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is True

    def test_legacy_spec_with_no_title_heading_is_current(self) -> None:
        """No revision line and no parseable title: nothing to compare, so don't block."""
        task = _task()
        spec_text = "## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is True

    def test_legacy_spec_title_without_spec_prefix_is_compared_verbatim(self) -> None:
        """A heading that omits the ``Spec: `` prefix compares as the whole title."""
        task = _task(title="Fix the thing")
        assert spec_is_current("# Fix the thing\n\n## Solution\nDo it.\n", task) is True
        assert spec_is_current("# Fix another thing\n\n## Solution\nDo it.\n", task) is False

    def test_marker_value_on_next_line_is_not_captured_as_hash(self) -> None:
        """A marker with no value on its own line must not pick up unrelated text
        from a following line as the hash (``\\s*`` used to span the newline)."""
        task = _task(title="Fix the thing")
        spec_text = "# Spec: Fix the thing\n\n**Issue revision**:\n\nabc\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is False

    def test_hash_of_wrong_length_is_rejected(self) -> None:
        """A value that is hex but not exactly 12 chars must not be accepted as a hash."""
        task = _task()
        digest = compute_issue_revision_hash(task.title, task.body)
        spec_text = f"# Spec: Fix the thing\n\n**Issue revision**: {digest[:10]}\n"
        assert spec_is_current(spec_text, task) is False

    def test_legacy_title_inside_fenced_block_is_not_mistaken_for_title(self) -> None:
        """A ``# `` line inside a fenced example block that precedes the real title
        heading must not be read as the spec's title: the real title (outside
        the fence) is found and compared instead, and it happens to match here."""
        task = _task(title="Fix the thing")
        spec_text = "```bash\n# some note\necho hi\n```\n\n# Spec: Fix the thing\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is True

    def test_legacy_title_behind_fence_mismatch_is_detected(self) -> None:
        """The fix for the case above: a real title behind a leading fence that
        does NOT match the issue's current title must be caught as stale, not
        unconditionally trusted just because the first line isn't a heading."""
        task = _task(title="Fix the thing")
        spec_text = "```bash\n# some note\necho hi\n```\n\n# Spec: Fix a different thing\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is False

    def test_frontmatter_line_before_title_does_not_defeat_the_title_check(self) -> None:
        """A spec whose very first line is frontmatter prose (not a fence, not a
        heading) must still have its real title found and compared."""
        task = _task(title="Fix the thing")
        spec_text = "**Status**: approved\n\n# Spec: Fix a different thing\n\n## Solution\nDo it.\n"
        assert spec_is_current(spec_text, task) is False

    def test_marker_quoted_inside_fenced_block_is_not_read_as_the_stamp(self) -> None:
        """A spec's frontmatter that quotes the ``**Issue revision**:`` marker
        format inside a fenced example (as this repo's own docs do) must not
        have that quoted text read as the spec's real stamp. With no real
        marker present, this legacy spec falls back to the title comparison,
        which matches. Without fence-masking, ``deadbeef0000`` is valid hex of
        the right length and would have been read as a mismatched real stamp,
        incorrectly marking this spec stale."""
        task = _task(title="Fix the thing")
        spec_text = (
            "# Spec: Fix the thing\n"
            "Stamp specs with a line like:\n\n"
            "```\n"
            "**Issue revision**: deadbeef0000\n"
            "```\n\n"
            "## Solution\nDo it.\n"
        )
        assert spec_is_current(spec_text, task) is True

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
