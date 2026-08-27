"""Tests for sova.utils.json module."""

from __future__ import annotations

import json

from sova.utils.json import extract_json


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
