"""Display lines for Claude Code stream-json events (sova/ipc/claude_stream.py)."""

from __future__ import annotations

import json

import pytest

from sova.ipc.claude_stream import claude_event_lines, summarize_tool_error, summarize_tool_use
from sova.ipc.runtime import ClaudeCodeRuntime


def _assistant(*blocks: object) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _tool_use(name: str, tool_input: object) -> dict:
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}


class TestSummarizeToolUse:
    @pytest.mark.parametrize(
        ("name", "tool_input", "expected"),
        [
            pytest.param("Bash", {"command": "git status"}, "[Bash] git status", id="command"),
            pytest.param("Read", {"file_path": "/a/b.py", "limit": 5}, "[Read] /a/b.py", id="file-path"),
            pytest.param("Grep", {"pattern": "foo", "path": "src"}, "[Grep] foo", id="first-key-wins"),
            pytest.param("Skill", {}, "[Skill]", id="empty-input"),
            pytest.param("Skill", None, "[Skill]", id="no-input"),
            pytest.param("Custom", {"unrelated": 1}, "[Custom]", id="no-main-argument"),
            pytest.param("Bash", {"command": "echo a\n  echo   b"}, "[Bash] echo a echo b", id="multi-line"),
            pytest.param("Task", {"prompt": ["a", {"b": 1}]}, '[Task] ["a", {"b": 1}]', id="non-string"),
        ],
    )
    def test_summary(self, name: str, tool_input: object, expected: str) -> None:
        assert summarize_tool_use(name, tool_input) == expected

    def test_long_input_is_cut_to_one_line_with_ellipsis(self) -> None:
        summary = summarize_tool_use("Bash", {"command": "x" * 500})
        assert summary.endswith("...")
        assert "\n" not in summary
        assert len(summary) < 200


class TestSummarizeToolError:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param("boom\nsecond", "[tool error] boom second", id="string"),
            pytest.param(
                [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "[tool error] a b", id="blocks"
            ),
            pytest.param("", "[tool error]", id="empty-string"),
            pytest.param([], "[tool error]", id="empty-list"),
            pytest.param(None, "[tool error]", id="missing"),
            pytest.param(
                "<tool_use_error>File does not exist.</tool_use_error>",
                "[tool error] File does not exist.",
                id="wrapped",
            ),
            pytest.param("<tool_use_error>cut off", "[tool error] cut off", id="unclosed-wrapper"),
            pytest.param("<tool_use_error></tool_use_error>", "[tool error]", id="empty-wrapper"),
            pytest.param(
                [{"type": "text", "text": "<tool_use_error>No such\nfile</tool_use_error>"}],
                "[tool error] No such file",
                id="wrapped-in-block",
            ),
        ],
    )
    def test_summary(self, content: object, expected: str) -> None:
        assert summarize_tool_error(content) == expected


class TestSummaryRedaction:
    """Tool summaries reach the dashboard, so a credential in an argument or error never survives them."""

    _SECRET = "s3cr3t-Value-1234567890"

    def test_credential_in_a_bash_command_is_redacted(self) -> None:
        summary = summarize_tool_use("Bash", {"command": f"curl -H 'X-Api-Key: {self._SECRET}' https://x.test"})
        assert self._SECRET not in summary
        assert summary.startswith("[Bash] curl -H")
        assert "[REDACTED:" in summary

    def test_credential_in_a_tool_error_is_redacted(self) -> None:
        summary = summarize_tool_error(f"request failed: Authorization: Bearer {self._SECRET}")
        assert self._SECRET not in summary
        assert summary.startswith("[tool error] request failed:")

    def test_credential_past_the_cut_is_redacted_before_truncating(self) -> None:
        summary = summarize_tool_use("Bash", {"command": "x" * 140 + f" X-Api-Key: {self._SECRET}"})
        assert "s3cr3" not in summary  # the fragment a cut before redaction would leave
        assert summary.endswith("...")

    def test_multi_line_private_key_in_a_heredoc_is_redacted(self) -> None:
        command = (
            "cat > key.pem <<'EOF'\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAkeymaterial1234\n"
            "abcdefgh5678keymaterial\n-----END RSA PRIVATE KEY-----\nEOF"
        )
        summary = summarize_tool_use("Bash", {"command": command})
        assert "MIIEpA" not in summary
        assert "keymaterial" not in summary
        assert "[REDACTED:private_key]" in summary

    def test_huge_command_is_redacted_and_cut_without_scanning_all_of_it(self) -> None:
        command = f"curl -H X-Api-Key: {self._SECRET} " + "y" * 1_000_000
        summary = summarize_tool_use("Bash", {"command": command})
        assert self._SECRET not in summary
        assert len(summary) <= len("[Bash] ") + 160
        assert summary.endswith("...")

    def test_whitespace_only_main_argument_has_no_trailing_space(self) -> None:
        assert summarize_tool_use("Bash", {"command": "   "}) == "[Bash]"


class TestClaudeEventLines:
    def test_blocks_come_out_in_order_and_empty_text_is_skipped(self) -> None:
        event = _assistant(
            {"type": "text", "text": "Running it"},
            {"type": "text", "text": "  "},
            _tool_use("Bash", {"command": "make check"}),
            {"type": "thinking", "thinking": ""},
        )
        assert claude_event_lines(event) == ["Running it", "[Bash] make check"]

    def test_bare_string_blocks_are_text_and_malformed_blocks_are_skipped(self) -> None:
        event = _assistant("plain block", 42, None, _tool_use("Read", {"file_path": "a.py"}))
        assert claude_event_lines(event) == ["plain block", "[Read] a.py"]

    def test_tool_only_event(self) -> None:
        assert claude_event_lines(_assistant(_tool_use("Read", {"file_path": "a.py"}))) == ["[Read] a.py"]

    def test_tool_use_with_a_null_name_falls_back_to_tool(self) -> None:
        event = _assistant({"type": "tool_use", "id": "1", "name": None, "input": {"command": "ls"}})
        assert claude_event_lines(event) == ["[tool] ls"]

    def test_only_errored_tool_results_are_reported(self) -> None:
        event = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "1", "content": "fine"},
                    {"type": "tool_result", "tool_use_id": "2", "is_error": True, "content": "no such file"},
                ]
            },
        }
        assert claude_event_lines(event) == ["[tool error] no such file"]

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param({"type": "assistant"}, id="no-message"),
            pytest.param({"type": "assistant", "message": {"content": "plain"}}, id="string-content"),
            pytest.param(
                {"type": "assistant", "content": [{"type": "text", "text": "old shape"}]}, id="top-level-content"
            ),
            pytest.param({"type": "system", "message": {"content": [{"type": "text", "text": "x"}]}}, id="other-type"),
        ],
    )
    def test_unrecognised_shapes_yield_nothing(self, event: dict) -> None:
        assert claude_event_lines(event) == []


class TestRuntimeParseOutput:
    def test_assistant_event_carries_every_line(self) -> None:
        line = json.dumps(_assistant({"type": "text", "text": "Hi"}, _tool_use("Bash", {"command": "ls"})))
        event = ClaudeCodeRuntime().parse_output(line)
        assert event is not None
        assert event.lines == ["Hi", "[Bash] ls"]
        assert event.text == "Hi\n[Bash] ls"

    def test_event_with_no_lines_is_dropped(self) -> None:
        line = json.dumps(_assistant({"type": "thinking", "thinking": ""}))
        assert ClaudeCodeRuntime().parse_output(line) is None
