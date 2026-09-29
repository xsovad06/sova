"""Tests for the shared Claude CLI argument builder."""

from __future__ import annotations

import stat
from decimal import Decimal
from pathlib import Path

from sova.llm.cli_args import build_claude_cli_args, write_system_prompt_file


class TestBuildClaudeCliArgs:
    def test_base_args(self) -> None:
        args = build_claude_cli_args()
        assert args[0] == "claude"
        assert "-p" in args
        assert "--output-format" in args
        fmt_idx = args.index("--output-format")
        assert args[fmt_idx + 1] == "json"
        assert "--permission-mode" in args
        pm_idx = args.index("--permission-mode")
        assert args[pm_idx + 1] == "bypassPermissions"

    def test_prompt_never_appears_on_argv(self) -> None:
        """The prompt is sent over stdin, never as a positional value after -p."""
        args = build_claude_cli_args()
        p_idx = args.index("-p")
        assert p_idx == len(args) - 1 or args[p_idx + 1].startswith("--")

    def test_omits_optional_flags_by_default(self) -> None:
        args = build_claude_cli_args()
        assert "--model" not in args
        assert "--fallback-model" not in args
        assert "--max-budget-usd" not in args
        assert "--system-prompt" not in args
        assert "--system-prompt-file" not in args
        assert "--verbose" not in args

    def test_includes_model(self) -> None:
        args = build_claude_cli_args(model="opus")
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "opus"

    def test_includes_fallback_model(self) -> None:
        args = build_claude_cli_args(fallback_model="sonnet")
        fm_idx = args.index("--fallback-model")
        assert args[fm_idx + 1] == "sonnet"

    def test_includes_max_budget_usd_as_string(self) -> None:
        args = build_claude_cli_args(max_budget_usd=Decimal("0"))
        budget_idx = args.index("--max-budget-usd")
        assert args[budget_idx + 1] == "0"

    def test_includes_system_prompt_file(self) -> None:
        args = build_claude_cli_args(system_prompt_file="/tmp/sova-system-prompt-abc.txt")
        sp_idx = args.index("--system-prompt-file")
        assert args[sp_idx + 1] == "/tmp/sova-system-prompt-abc.txt"
        assert "--system-prompt" not in args

    def test_includes_system_prompt_file_accepts_path_object(self, tmp_path: Path) -> None:
        args = build_claude_cli_args(system_prompt_file=tmp_path / "sp.txt")
        sp_idx = args.index("--system-prompt-file")
        assert args[sp_idx + 1] == str(tmp_path / "sp.txt")

    def test_stream_json_adds_verbose(self) -> None:
        args = build_claude_cli_args(output_format="stream-json")
        assert "--verbose" in args

    def test_json_omits_verbose(self) -> None:
        args = build_claude_cli_args(output_format="json")
        assert "--verbose" not in args


class TestWriteSystemPromptFile:
    def test_writes_content(self) -> None:
        path = write_system_prompt_file("You are a planner.")
        try:
            assert path.read_text(encoding="utf-8") == "You are a planner."
        finally:
            path.unlink(missing_ok=True)

    def test_file_is_private(self) -> None:
        path = write_system_prompt_file("secret persona text")
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == stat.S_IRUSR | stat.S_IWUSR
        finally:
            path.unlink(missing_ok=True)

    def test_returns_distinct_paths_across_calls(self) -> None:
        path_a = write_system_prompt_file("a")
        path_b = write_system_prompt_file("b")
        try:
            assert path_a != path_b
        finally:
            path_a.unlink(missing_ok=True)
            path_b.unlink(missing_ok=True)
