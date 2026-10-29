"""Tests for sova harden command and helpers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from sova.adapters.base import Task, TaskState
from sova.cli.commands.harden import (
    _build_harden_prompt,
    _detect_issue_type,
    _format_issues_summary,
    _load_issue_template,
    _load_project_docs,
    _strip_code_fences,
)
from sova.config.models import ProjectConfig
from sova.db.session import close_db, init_db
from sova.utils.markdown import strip_preamble as _strip_preamble

runner = CliRunner()


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for CLI tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_task.return_value = Task(
        id="42",
        title="Add dark mode",
        body="We need dark mode support.",
        state=TaskState.BACKLOG,
        labels=["type: feature"],
    )
    adapter.list_tasks.return_value = [
        Task(
            id="42",
            title="Add dark mode",
            body="We need dark mode",
            state=TaskState.BACKLOG,
            labels=["type: feature"],
        ),
        Task(
            id="43",
            title="Fix login bug",
            body="Login crashes",
            state=TaskState.BACKLOG,
            labels=["type: bug"],
        ),
    ]
    return adapter


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestDetectIssueType:
    def test_feature_label(self) -> None:
        assert _detect_issue_type(["type: feature", "priority: high"]) == "feature"

    def test_bug_label(self) -> None:
        assert _detect_issue_type(["type: bug"]) == "bug"

    def test_task_label(self) -> None:
        assert _detect_issue_type(["type: task"]) == "task"

    def test_no_type_label_defaults_to_feature(self) -> None:
        assert _detect_issue_type(["priority: high", "area: dashboard"]) == "feature"

    def test_empty_labels(self) -> None:
        assert _detect_issue_type([]) == "feature"


class TestStripCodeFences:
    def test_no_fences(self) -> None:
        assert _strip_code_fences("## Title\nContent") == "## Title\nContent"

    def test_markdown_fences(self) -> None:
        text = "```markdown\n## Title\nContent\n```"
        assert _strip_code_fences(text) == "## Title\nContent"

    def test_plain_fences(self) -> None:
        text = "```\n## Title\nContent\n```"
        assert _strip_code_fences(text) == "## Title\nContent"

    def test_whitespace_around_fences(self) -> None:
        text = "\n  ```markdown\n## Title\n```  \n"
        assert _strip_code_fences(text) == "## Title"

    def test_single_line_fence(self) -> None:
        assert _strip_code_fences("```foo```") == "foo"

    def test_closing_fence_on_last_content_line(self) -> None:
        assert _strip_code_fences('```json\n{"a": 1}```') == '{"a": 1}'

    def test_four_backtick_fence_loses_all_four_closing_backticks(self) -> None:
        assert _strip_code_fences("````markdown\n## Title\n````") == "## Title"

    def test_longer_closing_fence_on_own_line(self) -> None:
        assert _strip_code_fences("```json\n{}\n`````") == "{}"

    def test_shorter_closing_line_still_closes_the_wrapper(self) -> None:
        """The strip always accepted a lone three-backtick last line; LLMs mismatch fence lengths."""
        assert _strip_code_fences("````md\nbody\n```") == "body"

    def test_closing_line_with_surrounding_spaces_closes_the_wrapper(self) -> None:
        assert _strip_code_fences("````md\nbody\n  ``` ") == "body"

    def test_glued_closing_run_shorter_than_the_opener_is_content(self) -> None:
        """The width rule applies only where it disambiguates: a run glued to a content line."""
        assert _strip_code_fences("````md\nuse ```x```") == "use ```x```"

    def test_glued_closing_fence_keeps_the_inline_span_delimiter(self) -> None:
        assert _strip_code_fences("```md\nuse `x````") == "use `x`"

    def test_single_line_glued_closing_fence_keeps_the_inline_span_delimiter(self) -> None:
        assert _strip_code_fences("```use `x````") == "use `x`"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param('```json{"a": 1}```', '{"a": 1}', id="tag-glued-to-object"),
            pytest.param("```json[1, 2]```", "[1, 2]", id="tag-glued-to-array"),
            pytest.param('```json {"a": 1}```', '{"a": 1}', id="json-tag-with-blank"),
            pytest.param("```JSON [1]```", "[1]", id="json-tag-upper-case"),
            pytest.param("```see [docs]```", "see [docs]", id="other-word-before-blank-is-content"),
            pytest.param("```foo```", "foo", id="plain-word"),
        ],
    )
    def test_single_line_fence_drops_a_language_tag_only_in_front_of_json(self, text: str, expected: str) -> None:
        assert _strip_code_fences(text) == expected

    def test_four_backtick_closing_glued_to_last_line(self) -> None:
        assert _strip_code_fences("````json\n{}````") == "{}"

    def test_unclosed_wrapper_keeps_a_trailing_inline_code_span_intact(self) -> None:
        assert _strip_code_fences("```md\nuse ```x```") == "use ```x```"

    def test_unclosed_single_line_wrapper_keeps_a_trailing_inline_code_span_intact(self) -> None:
        assert _strip_code_fences("```use ```x```") == "use ```x```"

    def test_nested_closing_fence_kept_when_outer_fence_on_own_line(self) -> None:
        text = "```markdown\n## Title\n```python\nx = 1\n```\n```"
        assert _strip_code_fences(text) == "## Title\n```python\nx = 1\n```"


class TestStripPreamble:
    def test_no_preamble(self) -> None:
        text = "## Title\nContent"
        assert _strip_preamble(text) == text

    def test_strips_reasoning_preamble(self) -> None:
        text = "Now I have all the context needed.\nLet me produce the output.\n## Title\nContent"
        assert _strip_preamble(text) == "## Title\nContent"

    def test_no_heading_returns_unchanged(self) -> None:
        text = "Just some plain text\nwith no headings at all."
        assert _strip_preamble(text) == text

    def test_heading_inside_code_block_ignored(self) -> None:
        text = "Preamble text\n```python\n## This is a comment\n```\n## Real Heading\nContent"
        assert _strip_preamble(text) == "## Real Heading\nContent"

    def test_heading_only_inside_code_block(self) -> None:
        text = "Preamble\n```\n## Not a heading\n```\nMore text"
        assert _strip_preamble(text) == text

    def test_first_line_is_heading(self) -> None:
        text = "## Title\nContent\nMore"
        assert _strip_preamble(text) == text

    def test_multiple_headings_strips_to_first(self) -> None:
        text = "Reasoning here.\n# First\nBody\n## Second\nMore"
        assert _strip_preamble(text) == "# First\nBody\n## Second\nMore"

    def test_empty_string(self) -> None:
        assert _strip_preamble("") == ""

    def test_issue_number_not_treated_as_heading(self) -> None:
        text = "#123 is closed\n## Real Heading\nContent"
        assert _strip_preamble(text) == "## Real Heading\nContent"

    def test_bare_hash_treated_as_heading(self) -> None:
        text = "Preamble\n#\nContent"
        assert _strip_preamble(text) == "#\nContent"

    def test_multi_hash_without_space_not_heading(self) -> None:
        text = "##no-space\n## Real\nContent"
        assert _strip_preamble(text) == "## Real\nContent"

    def test_indented_heading_not_treated_as_heading(self) -> None:
        text = "Preamble\n  # Indented\n## Real\nBody"
        assert _strip_preamble(text) == "## Real\nBody"


class TestIsAtxHeading:
    def test_valid_headings(self) -> None:
        from sova.utils.markdown import _is_atx_heading

        assert _is_atx_heading("# Title") is True
        assert _is_atx_heading("## Subtitle") is True
        assert _is_atx_heading("### Deep") is True
        assert _is_atx_heading("#") is True

    def test_invalid_headings(self) -> None:
        from sova.utils.markdown import _is_atx_heading

        assert _is_atx_heading("#123") is False
        assert _is_atx_heading("#no-space") is False
        assert _is_atx_heading("##nope") is False
        assert _is_atx_heading("not a heading") is False
        assert _is_atx_heading("") is False


class TestLoadProjectDocs:
    def test_loads_vision_and_templates(self, tmp_path: Path) -> None:
        # Create a vision doc
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "VISION.md").write_text("# Vision\nBuild the future.")

        # Create issue template
        template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
        template_dir.mkdir(parents=True)
        (template_dir / "feature.md").write_text("---\nname: Feature\n---\n## Objective")

        result = _load_project_docs(tmp_path)

        assert "Vision" in result
        assert "Build the future" in result
        assert "Issue Template: feature.md" in result
        assert "## Objective" in result

    def test_empty_project(self, tmp_path: Path) -> None:
        result = _load_project_docs(tmp_path)
        assert result == ""

    def test_truncates_long_docs(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        long_content = "\n".join(f"Line {i}" for i in range(1000))
        (docs_dir / "VISION.md").write_text(long_content)

        result = _load_project_docs(tmp_path)

        # Should only have 500 lines
        lines_in_result = result.strip().split("\n")
        # Header line + 500 content lines
        assert len(lines_in_result) <= 502


class TestLoadIssueTemplate:
    def test_loads_and_strips_frontmatter(self, tmp_path: Path) -> None:
        template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
        template_dir.mkdir(parents=True)
        (template_dir / "feature.md").write_text(
            "---\nname: Feature\nabout: New feature\n---\n\n## Objective\nDescribe here."
        )

        result = _load_issue_template(tmp_path, "feature")

        assert "## Objective" in result
        assert "name: Feature" not in result
        assert "---" not in result

    def test_missing_template_returns_empty(self, tmp_path: Path) -> None:
        result = _load_issue_template(tmp_path, "nonexistent")
        assert result == ""


class TestFormatIssuesSummary:
    def test_formats_tasks(self) -> None:
        tasks = [
            Task(id="1", title="First", labels=["bug"]),
            Task(id="2", title="Second", labels=[]),
        ]
        result = _format_issues_summary(tasks)
        assert "#1: First" in result
        assert "#2: Second" in result
        assert "bug" in result

    def test_empty_list(self) -> None:
        assert _format_issues_summary([]) == "(no open issues)"


class TestBuildHardenPrompt:
    def test_contains_issue_info(self) -> None:
        task = Task(id="42", title="Add dark mode", body="We need dark mode.", labels=["type: feature"])
        prompt = _build_harden_prompt(task, "docs here", "issues here", "## Objective", "feature")

        assert "#42" in prompt
        assert "Add dark mode" in prompt
        assert "We need dark mode" in prompt
        assert "docs here" in prompt
        assert "issues here" in prompt
        assert "## Objective" in prompt
        assert "feature" in prompt

    def test_fallback_structure_when_no_template(self) -> None:
        task = Task(id="1", title="Test", body="body", labels=[])
        prompt = _build_harden_prompt(task, "", "", "", "feature")

        # Should include fallback section structure
        assert "## Objective" in prompt
        assert "## Acceptance Criteria" in prompt


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


class TestHardenCommand:
    def test_harden_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["harden", "--help"])
        assert result.exit_code == 0
        assert "harden" in result.output.lower() or "enrich" in result.output.lower()

    def test_harden_shows_in_app_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "harden" in result.output

    @patch("sova.llm.client.invoke")
    @patch("sova.cli.commands.harden.create_adapter")
    @patch("sova.cli.commands.harden.load_config")
    def test_harden_single_issue(self, mock_config, mock_adapter_factory, mock_invoke) -> None:
        from sova.cli.app import app
        from sova.llm.models import LLMResult

        mock_config.return_value = ProjectConfig(github_repo="owner/repo")
        adapter = _mock_adapter()
        mock_adapter_factory.return_value = adapter
        mock_invoke.return_value = LLMResult(text="## Objective\nEnriched content here.", model="sonnet")

        result = runner.invoke(app, ["harden", "42"])
        assert result.exit_code == 0
        adapter.get_task.assert_called_with("42")
        adapter.edit_body.assert_called_once()
        adapter.post_comment.assert_not_called()

    @patch("sova.llm.client.invoke")
    @patch("sova.cli.commands.harden.create_adapter")
    @patch("sova.cli.commands.harden.load_config")
    def test_harden_dry_run(self, mock_config, mock_adapter_factory, mock_invoke) -> None:
        from sova.cli.app import app
        from sova.llm.models import LLMResult

        mock_config.return_value = ProjectConfig(github_repo="owner/repo")
        adapter = _mock_adapter()
        mock_adapter_factory.return_value = adapter
        mock_invoke.return_value = LLMResult(text="## Objective\nDry run content.", model="sonnet")

        result = runner.invoke(app, ["harden", "42", "--dry-run"])
        assert result.exit_code == 0
        adapter.edit_body.assert_not_called()
        adapter.post_comment.assert_not_called()

    @patch("sova.llm.client.invoke")
    @patch("sova.cli.commands.harden.create_adapter")
    @patch("sova.cli.commands.harden.load_config")
    def test_harden_all_issues(self, mock_config, mock_adapter_factory, mock_invoke) -> None:
        from sova.cli.app import app
        from sova.llm.models import LLMResult

        mock_config.return_value = ProjectConfig(github_repo="owner/repo")
        adapter = _mock_adapter()
        mock_adapter_factory.return_value = adapter
        mock_invoke.return_value = LLMResult(text="## Objective\nBatch content.", model="sonnet")

        result = runner.invoke(app, ["harden"])
        assert result.exit_code == 0
        adapter.list_tasks.assert_called()
        # Should call edit_body for each issue in batch
        assert adapter.edit_body.call_count == 2

    @patch("sova.llm.client.invoke")
    @patch("sova.cli.commands.harden.create_adapter")
    @patch("sova.cli.commands.harden.load_config")
    def test_harden_skip_triage(self, mock_config, mock_adapter_factory, mock_invoke) -> None:
        from sova.cli.app import app
        from sova.llm.models import LLMResult

        mock_config.return_value = ProjectConfig(github_repo="owner/repo")
        adapter = _mock_adapter()
        mock_adapter_factory.return_value = adapter
        mock_invoke.return_value = LLMResult(text="## Objective\nContent.", model="sonnet")

        result = runner.invoke(app, ["harden", "42", "--skip-triage"])
        assert result.exit_code == 0
        adapter.edit_body.assert_called_once()
        # Should not add triage label
        adapter.add_label.assert_not_called()


class TestRetriageTask:
    """Tests for _retriage_task, the harden re-triage helper."""

    async def test_does_not_pass_known_labels(self) -> None:
        """A re-triage snapshot taken before the 300s LLM call must be refetched.

        Round-4 finding: passing `known_labels=task.labels` here violates
        `apply_suitability_label()`'s own documented contract (no known_labels
        across an LLM round trip), making a label change made during the
        enrichment call invisible to the stale-label strip.
        """
        from sova.cli.commands.harden import _retriage_task
        from sova.config.models import ProjectConfig

        task = Task(id="42", title="Test", body="old body", state=TaskState.BACKLOG, labels=["agent:ready"])
        adapter = AsyncMock()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="old body", state=TaskState.BACKLOG, labels=["agent:human-only"]
        )
        config = ProjectConfig(github_repo="owner/repo")

        await _retriage_task(task, "## Objective\nEnriched.\n", config, adapter, skip_triage=False)

        adapter.get_task.assert_awaited_once_with("42")
        adapter.remove_label.assert_not_awaited()
