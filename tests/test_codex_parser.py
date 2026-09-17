"""Tests for sova.ipc.codex: Codex CLI stream event parser."""

from __future__ import annotations

import json

from sova.ipc.codex import (
    _MAX_COMMAND_CHARS,
    _MAX_CONTENT_CHARS,
    _MAX_OUTPUT_HEAD_CHARS,
    CodexStreamParser,
)
from sova.llm.models import LLMResult


def _line(**kwargs) -> str:
    return json.dumps(kwargs)


class TestBlankAndMalformedLines:
    def test_empty_line_returns_none(self) -> None:
        parser = CodexStreamParser()
        assert parser.parse_line("") is None

    def test_whitespace_only_line_returns_none(self) -> None:
        parser = CodexStreamParser()
        assert parser.parse_line("   \n") is None

    def test_non_json_line_becomes_content(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line("plain stray output")
        assert event is not None
        assert event.type == "content"
        assert event.text == "plain stray output"

    def test_json_list_falls_through_to_malformed_path(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps([1, 2, 3]))
        assert event is not None
        assert event.type == "content"

    def test_json_string_falls_through_to_malformed_path(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps("just a string"))
        assert event is not None
        assert event.type == "content"

    def test_json_number_falls_through_to_malformed_path(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps(42))
        assert event is not None
        assert event.type == "content"


class TestThreadStarted:
    def test_thread_started_is_silent(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="thread.started", thread_id="thread-1"))
        assert event is None

    def test_thread_id_carried_to_turn_completed(self) -> None:
        parser = CodexStreamParser()
        parser.parse_line(_line(type="thread.started", thread_id="thread-1"))
        event = parser.parse_line(_line(type="turn.completed", usage={}))
        assert event is not None
        assert event.result is not None
        assert event.result.session_id == "thread-1"

    def test_turn_completed_without_thread_started_uses_empty_session_id(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.completed", usage={}))
        assert event is not None
        assert event.result is not None
        assert event.result.session_id == ""

    def test_thread_started_after_terminal_resets_latch(self) -> None:
        parser = CodexStreamParser()
        parser.parse_line(_line(type="turn.completed", usage={}))
        parser.parse_line(_line(type="thread.started", thread_id="thread-2"))
        event = parser.parse_line(_line(type="turn.completed", usage={"input_tokens": 5}))
        assert event is not None
        assert event.result is not None
        assert event.result.session_id == "thread-2"
        assert event.result.input_tokens == 5


class TestItemLifecycle:
    def test_reasoning_item_completed_produces_no_output(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.completed", item={"item_type": "reasoning", "text": "thinking..."}))
        assert event is None

    def test_agent_message_completed_becomes_content(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(type="item.completed", item={"item_type": "agent_message", "text": "Done with the fix."})
        )
        assert event is not None
        assert event.type == "content"
        assert event.text == "Done with the fix."

    def test_agent_message_accepts_type_key_when_item_type_missing(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.completed", item={"type": "agent_message", "text": "Hi"}))
        assert event is not None
        assert event.text == "Hi"

    def test_item_type_key_preferred_over_type(self) -> None:
        parser = CodexStreamParser()
        # item_type says agent_message (rendered); type says reasoning (would be suppressed).
        event = parser.parse_line(
            _line(type="item.completed", item={"item_type": "agent_message", "type": "reasoning", "text": "Hi"})
        )
        assert event is not None
        assert event.text == "Hi"

    def test_command_execution_started_shows_command(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(type="item.started", item={"item_type": "command_execution", "command": "pytest -q"})
        )
        assert event is not None
        assert event.type == "content"
        assert "pytest -q" in event.text

    def test_non_command_execution_started_is_silent(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.started", item={"item_type": "agent_message", "text": "hi"}))
        assert event is None

    def test_command_execution_completed_shows_exit_code_and_output(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(
                type="item.completed",
                item={
                    "item_type": "command_execution",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "aggregated_output": "5 passed",
                },
            )
        )
        assert event is not None
        assert "pytest -q" in event.text
        assert "exit 0" in event.text
        assert "5 passed" in event.text

    def test_file_change_lists_bounded_paths_with_count(self) -> None:
        parser = CodexStreamParser()
        changes = [{"path": f"file_{i}.py"} for i in range(15)]
        event = parser.parse_line(_line(type="item.completed", item={"item_type": "file_change", "changes": changes}))
        assert event is not None
        assert "15 file(s) changed" in event.text
        assert "+5 more" in event.text
        assert "file_0.py" in event.text
        assert "file_14.py" not in event.text

    def test_file_change_with_no_paths_returns_none(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.completed", item={"item_type": "file_change", "changes": []}))
        assert event is None

    def test_item_updated_always_returns_none(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.updated", item={"item_type": "agent_message", "text": "partial..."}))
        assert event is None

    def test_unknown_item_type_returns_none(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.completed", item={"item_type": "mcp_tool_call"}))
        assert event is None

    def test_missing_item_payload_returns_none(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.completed"))
        assert event is None

    def test_non_dict_item_payload_returns_none(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps({"type": "item.completed", "item": "not a dict"}))
        assert event is None

    def test_agent_message_with_no_text_returns_none(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="item.completed", item={"item_type": "agent_message"}))
        assert event is None


class TestUnknownEvents:
    def test_unknown_top_level_event_returns_none(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="some_future_event", payload={"x": 1}))
        assert event is None

    def test_turn_started_is_silent(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.started"))
        assert event is None

    def test_missing_type_key_treated_as_unknown(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps({"foo": "bar"}))
        assert event is None


class TestTurnCompleted:
    def test_usage_maps_to_llm_result_fields(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(
                type="turn.completed",
                usage={
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 10,
                },
            )
        )
        assert event is not None
        assert event.type == "result"
        result = event.result
        assert isinstance(result, LLMResult)
        assert result.input_tokens == 100
        assert result.cache_read_tokens == 20
        assert result.output_tokens == 50
        assert result.reasoning_output_tokens == 10
        assert result.stop_reason == "end_turn"
        assert not result.is_error

    def test_cost_is_unpriced_zero(self) -> None:
        from sova.llm.models import CostSource

        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.completed", usage={}))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.cost_usd == 0
        assert result.cost_source == CostSource.UNKNOWN

    def test_text_is_most_recent_agent_message(self) -> None:
        parser = CodexStreamParser()
        parser.parse_line(_line(type="item.completed", item={"item_type": "agent_message", "text": "First"}))
        parser.parse_line(_line(type="item.completed", item={"item_type": "agent_message", "text": "Second"}))
        event = parser.parse_line(_line(type="turn.completed", usage={}))
        assert event is not None
        assert event.result is not None
        assert event.result.text == "Second"

    def test_text_is_empty_when_no_agent_message_seen(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.completed", usage={}))
        assert event is not None
        assert event.result is not None
        assert event.result.text == ""

    def test_missing_usage_defaults_all_fields_to_zero(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps({"type": "turn.completed"}))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.cache_read_tokens == 0
        assert result.reasoning_output_tokens == 0

    def test_null_usage_defaults_all_fields_to_zero(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps({"type": "turn.completed", "usage": None}))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.input_tokens == 0

    def test_non_dict_usage_defaults_to_zero(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(json.dumps({"type": "turn.completed", "usage": "bogus"}))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.input_tokens == 0

    def test_negative_usage_value_coerces_to_zero(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.completed", usage={"input_tokens": -5}))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.input_tokens == 0

    def test_string_encoded_usage_value_coerces_to_zero(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.completed", usage={"input_tokens": "100"}))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.input_tokens == 0

    def test_float_usage_value_coerces_to_zero(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.completed", usage={"input_tokens": 1.5}))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.input_tokens == 0

    def test_second_turn_completed_is_suppressed(self) -> None:
        parser = CodexStreamParser()
        first = parser.parse_line(_line(type="turn.completed", usage={}))
        second = parser.parse_line(_line(type="turn.completed", usage={}))
        assert first is not None
        assert second is None


class TestTerminalFailure:
    def test_turn_failed_produces_error_result(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="turn.failed", error={"message": "sandbox denied write"}))
        assert event is not None
        assert event.type == "result"
        result = event.result
        assert result is not None
        assert result.stop_reason == "error"
        assert result.is_error
        assert result.text == "sandbox denied write"

    def test_error_event_produces_error_result(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="error", message="connection reset"))
        assert event is not None
        result = event.result
        assert result is not None
        assert result.stop_reason == "error"
        assert result.text == "connection reset"

    def test_error_before_turn_started_still_produces_result(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(_line(type="error", message="early failure"))
        assert event is not None
        assert event.result is not None
        assert event.result.stop_reason == "error"

    def test_failure_falls_back_to_last_agent_message_when_no_error_text(self) -> None:
        parser = CodexStreamParser()
        parser.parse_line(_line(type="item.completed", item={"item_type": "agent_message", "text": "partial work"}))
        event = parser.parse_line(_line(type="turn.failed", error={}))
        assert event is not None
        assert event.result is not None
        assert event.result.text == "partial work"

    def test_second_terminal_event_after_failure_is_suppressed(self) -> None:
        parser = CodexStreamParser()
        first = parser.parse_line(_line(type="turn.failed", error={"message": "boom"}))
        second = parser.parse_line(_line(type="turn.completed", usage={}))
        assert first is not None
        assert second is None

    def test_error_event_after_failure_is_suppressed(self) -> None:
        parser = CodexStreamParser()
        parser.parse_line(_line(type="turn.failed", error={"message": "boom"}))
        event = parser.parse_line(_line(type="error", message="another error"))
        assert event is None


class TestRedactionAndTruncation:
    def test_agent_message_secret_is_redacted(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(
                type="item.completed",
                item={"item_type": "agent_message", "text": "api_key=abcd1234efgh5678ijkl"},
            )
        )
        assert event is not None
        assert "abcd1234efgh5678ijkl" not in event.text
        assert "REDACTED" in event.text

    def test_command_output_secret_is_redacted(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(
                type="item.completed",
                item={
                    "item_type": "command_execution",
                    "command": "env",
                    "exit_code": 0,
                    "aggregated_output": "api_key=abcd1234efgh5678ijkl",
                },
            )
        )
        assert event is not None
        assert "abcd1234efgh5678ijkl" not in event.text

    def test_extremely_long_agent_message_is_bounded(self) -> None:
        parser = CodexStreamParser()
        huge_text = "x" * 500_000
        event = parser.parse_line(_line(type="item.completed", item={"item_type": "agent_message", "text": huge_text}))
        assert event is not None
        assert len(event.text) <= _MAX_CONTENT_CHARS

    def test_extremely_long_command_output_is_bounded(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(
                type="item.completed",
                item={
                    "item_type": "command_execution",
                    "command": "cat huge.log",
                    "exit_code": 0,
                    "aggregated_output": "y" * 500_000,
                },
            )
        )
        assert event is not None
        assert len(event.text) <= _MAX_OUTPUT_HEAD_CHARS + _MAX_COMMAND_CHARS + 64

    def test_malformed_line_is_redacted(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line("not json but has api_key=abcd1234efgh5678ijkl in it")
        assert event is not None
        assert "abcd1234efgh5678ijkl" not in event.text

    def test_control_characters_do_not_raise(self) -> None:
        parser = CodexStreamParser()
        event = parser.parse_line(
            _line(
                type="item.completed",
                item={
                    "item_type": "command_execution",
                    "command": "printf",
                    "exit_code": 0,
                    "aggregated_output": "line1\x00\x01\x02line2",
                },
            )
        )
        assert event is not None
        assert event.type == "content"
