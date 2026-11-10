"""Tests for sova.utils.json module."""

from __future__ import annotations

import json

import pytest

from sova.utils.json import extract_json
from sova.utils.markdown import strip_code_fences


class TestExtractJson:
    """Test extract_json utility function."""

    @pytest.mark.parametrize(
        ("text", "key", "value"),
        [
            pytest.param(
                '{"action_id": "review_pr", "reasoning": "PR needs review"}',
                "action_id",
                "review_pr",
                id="plain-object",
            ),
            pytest.param('```json\n{"action_id": "review_pr"}\n```', "action_id", "review_pr", id="markdown-fenced"),
            pytest.param('```\n{"action_id": "review_pr"}\n```', "action_id", "review_pr", id="fenced-no-lang"),
            # From an llm_suggestion error: valid JSON followed by extra text.
            pytest.param(
                '{"action_id": "review_pr", "reasoning": "PR needs review"}\n\nThis is some extra commentary.',
                "action_id",
                "review_pr",
                id="trailing-text",
            ),
            # Prose with brackets should be skipped.
            pytest.param(
                'Check [this doc] for details. JSON: {"status": "ok"}', "status", "ok", id="prose-brackets-before-json"
            ),
        ],
    )
    def test_extracts_object(self, text: str, key: str, value: str) -> None:
        parsed = json.loads(extract_json(text))
        assert parsed[key] == value

    def test_plain_json_array(self):
        text = '[{"title": "feat"}, {"title": "fix"}]'
        result = extract_json(text)
        parsed = json.loads(result)
        assert len(parsed) == 2

    @pytest.mark.parametrize(
        ("text", "key"),
        [
            # From a planner error: prose before JSON.
            pytest.param(
                'Here is my analysis:\n{"reasoning": "Based on resources", "actions": []}',
                "reasoning",
                id="leading-text",
            ),
            pytest.param('Let me plan:\n{"actions": []}\nThat is my plan.', "actions", id="leading-and-trailing-text"),
        ],
    )
    def test_extracts_object_surrounded_by_prose(self, text: str, key: str) -> None:
        parsed = json.loads(extract_json(text))
        assert key in parsed

    @pytest.mark.parametrize(
        "text",
        [
            # From a planner error: empty response.
            pytest.param("", id="empty-response"),
            pytest.param("   \n\n  ", id="whitespace-only"),
            # Text without any JSON delimiters returns an empty string.
            pytest.param("This is just prose with no JSON at all.", id="no-json-found"),
        ],
    )
    def test_returns_empty_without_json(self, text: str) -> None:
        assert extract_json(text) == ""

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

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(f"```json\n{PAYLOAD}\n```\nHope this helps.", id="prose-after-closing-fence"),
            pytest.param(f"Here is the result:\n```json\n{PAYLOAD}\n```", id="prose-before-fence"),
        ],
    )
    def test_surrounding_prose_is_tolerated(self, text: str) -> None:
        assert json.loads(extract_json(text)) == json.loads(self.PAYLOAD)

    def test_a_fenced_block_that_is_not_json_is_returned_unchanged(self) -> None:
        assert extract_json("```\nnot json at all\n```") == "not json at all"
