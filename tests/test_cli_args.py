"""Tests for the shared Claude CLI argument builder."""

from __future__ import annotations

from decimal import Decimal

from sova.llm.cli_args import build_claude_cli_args


class TestBuildClaudeCliArgs:
    def test_base_args(self) -> None:
        args = build_claude_cli_args("hello")
        assert args[0] == "claude"
        assert "-p" in args
        p_idx = args.index("-p")
        assert args[p_idx + 1] == "hello"
        assert "--output-format" in args
        fmt_idx = args.index("--output-format")
        assert args[fmt_idx + 1] == "json"
        assert "--permission-mode" in args
        pm_idx = args.index("--permission-mode")
        assert args[pm_idx + 1] == "bypassPermissions"

    def test_omits_optional_flags_by_default(self) -> None:
        args = build_claude_cli_args("hello")
        assert "--model" not in args
        assert "--fallback-model" not in args
        assert "--max-budget-usd" not in args
        assert "--system-prompt" not in args
        assert "--verbose" not in args

    def test_includes_model(self) -> None:
        args = build_claude_cli_args("hello", model="opus")
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "opus"

    def test_includes_fallback_model(self) -> None:
        args = build_claude_cli_args("hello", fallback_model="sonnet")
        fm_idx = args.index("--fallback-model")
        assert args[fm_idx + 1] == "sonnet"

    def test_includes_max_budget_usd_as_string(self) -> None:
        args = build_claude_cli_args("hello", max_budget_usd=Decimal("0"))
        budget_idx = args.index("--max-budget-usd")
        assert args[budget_idx + 1] == "0"

    def test_includes_system_prompt(self) -> None:
        args = build_claude_cli_args("hello", system_prompt="You are a planner.")
        sp_idx = args.index("--system-prompt")
        assert args[sp_idx + 1] == "You are a planner."

    def test_stream_json_adds_verbose(self) -> None:
        args = build_claude_cli_args("hello", output_format="stream-json")
        assert "--verbose" in args

    def test_json_omits_verbose(self) -> None:
        args = build_claude_cli_args("hello", output_format="json")
        assert "--verbose" not in args
