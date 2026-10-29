"""Tests for sova.utils.json module."""

from __future__ import annotations

import json

import pytest

from sova.utils.json import extract_json
from sova.utils.markdown import strip_code_fences


class TestExtractJson:
    """Test extract_json utility function."""

    def test_plain_json_object(self):
        text = '{"action_id": "review_pr", "reasoning": "PR needs review"}'
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["action_id"] == "review_pr"

    def test_plain_json_array(self):
        text = '[{"title": "feat"}, {"title": "fix"}]'
        result = extract_json(text)
        parsed = json.loads(result)
        assert len(parsed) == 2

    def test_markdown_fenced_json(self):
        text = '```json\n{"action_id": "review_pr"}\n```'
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["action_id"] == "review_pr"

    def test_markdown_fenced_no_lang(self):
        text = '```\n{"action_id": "review_pr"}\n```'
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["action_id"] == "review_pr"

    def test_json_with_trailing_text(self):
        """Test case from llm_suggestion error: valid JSON followed by extra text."""
        text = '{"action_id": "review_pr", "reasoning": "PR needs review"}\n\nThis is some extra commentary.'
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["action_id"] == "review_pr"

    def test_json_with_leading_text(self):
        """Test case from planner error: prose before JSON."""
        text = 'Here is my analysis:\n{"reasoning": "Based on resources", "actions": []}'
        result = extract_json(text)
        parsed = json.loads(result)
        assert "reasoning" in parsed

    def test_json_with_leading_and_trailing_text(self):
        text = 'Let me plan:\n{"actions": []}\nThat is my plan.'
        result = extract_json(text)
        parsed = json.loads(result)
        assert "actions" in parsed

    def test_empty_response(self):
        """Test case from planner error: empty response."""
        text = ""
        result = extract_json(text)
        assert result == ""

    def test_whitespace_only(self):
        text = "   \n\n  "
        result = extract_json(text)
        assert result == ""

    def test_no_json_found(self):
        """Text without any JSON delimiters returns empty string."""
        text = "This is just prose with no JSON at all."
        result = extract_json(text)
        assert result == ""

    def test_prose_brackets_before_json(self):
        """Prose with brackets should be skipped."""
        text = 'Check [this doc] for details. JSON: {"status": "ok"}'
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["status"] == "ok"

    def test_multiple_json_objects_returns_first(self):
        """When multiple JSON objects exist, extract the first valid one."""
        text = '{"first": 1} some text {"second": 2}'
        result = extract_json(text)
        parsed = json.loads(result)
        assert "first" in parsed
        assert "second" not in parsed

    def test_nested_json_in_strings(self):
        """Brackets inside JSON strings should not confuse the parser."""
        text = '{"msg": "Use [this] format", "value": 42}'
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["msg"] == "Use [this] format"
        assert parsed["value"] == 42


class TestExtractJsonAgreesWithStripCodeFences:
    """extract_json() and strip_code_fences() (triage's stripper) accept the same wrapped output."""

    PAYLOAD = '{"suitability": "ready", "items": [1, 2]}'

    WRAPPERS = [
        pytest.param(f"```json\n{PAYLOAD}\n```", id="three-backticks"),
        pytest.param(f"```\n{PAYLOAD}\n```", id="bare-fence"),
        pytest.param(f"  \n```json\n{PAYLOAD}\n```\n\n", id="surrounding-whitespace"),
        pytest.param(f"```json\n{PAYLOAD}\n```  ", id="trailing-blanks-after-fence"),
        pytest.param(f"```json\n{PAYLOAD}\n", id="unclosed"),
        pytest.param(f"````json\n{PAYLOAD}\n````", id="four-backticks"),
        pytest.param(f"```json\n{PAYLOAD}\n`````", id="closing-wider-than-opening"),
        pytest.param(f"````json\n{PAYLOAD}\n```", id="closing-narrower-than-opening"),
        pytest.param(f"```json\n{PAYLOAD}```", id="closing-glued-to-last-line"),
        pytest.param(f"````json\n{PAYLOAD}````", id="four-backticks-glued-to-last-line"),
        pytest.param(f"```json{PAYLOAD}```", id="one-line-tag-glued-to-json"),
        pytest.param(f"```json {PAYLOAD}```", id="one-line-tag-then-blank"),
        pytest.param(f"```{PAYLOAD}```", id="one-line-no-tag"),
    ]

    @pytest.mark.parametrize("text", WRAPPERS)
    def test_both_helpers_yield_the_same_json(self, text: str) -> None:
        assert json.loads(extract_json(text)) == json.loads(strip_code_fences(text))
        assert json.loads(extract_json(text)) == json.loads(self.PAYLOAD)

    @pytest.mark.parametrize("text", [w for w in WRAPPERS if "\n" in w.values[0].strip()])
    def test_a_multi_line_wrapper_is_returned_exactly_as_stripped(self, text: str) -> None:
        assert extract_json(text) == strip_code_fences(text)

    def test_prose_after_the_closing_fence_is_tolerated(self) -> None:
        text = f"```json\n{self.PAYLOAD}\n```\nHope this helps."
        assert json.loads(extract_json(text)) == json.loads(self.PAYLOAD)

    def test_prose_before_the_fence_is_tolerated(self) -> None:
        text = f"Here is the result:\n```json\n{self.PAYLOAD}\n```"
        assert json.loads(extract_json(text)) == json.loads(self.PAYLOAD)

    def test_a_fenced_block_that_is_not_json_is_returned_unchanged(self) -> None:
        assert extract_json("```\nnot json at all\n```") == "not json at all"
