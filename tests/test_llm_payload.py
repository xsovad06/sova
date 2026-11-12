"""Tests for sova.llm.payload.trim_payload and the call sites that use it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import structlog

from sova.llm.payload import TRUNCATION_MARKER, trim_payload


class TestTrimPayloadTail:
    def test_at_limit_untouched(self) -> None:
        text = "x" * 100
        assert trim_payload(text, 100, label="t") == text

    def test_one_over_limit_trimmed(self) -> None:
        result = trim_payload("a" + "x" * 100, 100, label="t")
        assert result == f"{TRUNCATION_MARKER}\n" + "x" * 100

    def test_keeps_tail(self) -> None:
        result = trim_payload("A" * 50 + "B" * 20, 20, label="t")
        assert result.endswith("B" * 20)
        assert "A" not in result

    def test_marker_not_counted_against_limit(self) -> None:
        result = trim_payload("x" * 500, 100, label="t")
        assert len(result) == 100 + len(TRUNCATION_MARKER) + 1

    def test_empty_passes_through_without_log(self) -> None:
        with structlog.testing.capture_logs() as logs:
            assert trim_payload("", 10, label="t") == ""
        assert logs == []

    def test_multibyte_counted_as_characters(self) -> None:
        text = "é中" * 10
        result = trim_payload(text, 5, label="t")
        assert result == f"{TRUNCATION_MARKER}\n" + text[-5:]

    def test_retrim_does_not_stack_marker(self) -> None:
        once = trim_payload("x" * 500, 100, label="t")
        assert trim_payload(once, 100, label="t") == once

    def test_retrim_to_smaller_limit_keeps_one_marker(self) -> None:
        once = trim_payload("x" * 500, 100, label="t")
        twice = trim_payload(once, 50, label="t")
        assert twice == f"{TRUNCATION_MARKER}\n" + "x" * 50
        assert twice.count(TRUNCATION_MARKER) == 1

    def test_zero_limit_keeps_nothing(self) -> None:
        assert trim_payload("abc", 0, label="t") == f"{TRUNCATION_MARKER}\n"

    def test_logs_label_and_sizes_when_trimmed(self) -> None:
        with structlog.testing.capture_logs() as logs:
            trim_payload("x" * 300, 100, label="ci_failure_logs")
        events = [e for e in logs if e["event"] == "llm.payload.trimmed"]
        assert len(events) == 1
        assert events[0]["label"] == "ci_failure_logs"
        assert events[0]["original_chars"] == 300
        assert events[0]["kept_chars"] == 100
        assert events[0]["limit"] == 100

    def test_no_log_when_untouched(self) -> None:
        with structlog.testing.capture_logs() as logs:
            trim_payload("x" * 100, 100, label="t")
        assert logs == []


class TestTrimPayloadHead:
    def test_keeps_head_and_suffixes_marker(self) -> None:
        result = trim_payload("A" * 20 + "B" * 50, 20, label="t", keep="head")
        assert result == "A" * 20 + f"\n{TRUNCATION_MARKER}"

    def test_at_limit_untouched(self) -> None:
        text = "x" * 100
        assert trim_payload(text, 100, label="t", keep="head") == text

    def test_retrim_does_not_stack_marker(self) -> None:
        once = trim_payload("x" * 500, 100, label="t", keep="head")
        assert trim_payload(once, 100, label="t", keep="head") == once


def _write_spec(tmp_path: Path, solution: str, questions: str = "last") -> None:
    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    (specs_dir / "42-feature.md").write_text(
        f"# Spec: Test task\n**Status**: approved\n\n## Solution\n{solution}\n\n## Open Questions\n{questions}\n"
    )


def _spec_ctx(tmp_path: Path) -> MagicMock:
    from tests.test_spec_provenance import _make_ctx

    return _make_ctx(project_dir=tmp_path, working_dir=tmp_path)


class TestSpecContextCap:
    def test_oversized_spec_trimmed_keeping_head(self, tmp_path: Path) -> None:
        from sova.core.steps.develop import _SPEC_CONTEXT_LIMIT, _load_spec_for_develop

        _write_spec(tmp_path, "START" + "s" * (_SPEC_CONTEXT_LIMIT + 5000))
        content, _ = _load_spec_for_develop(_spec_ctx(tmp_path))
        assert "START" in content
        assert TRUNCATION_MARKER in content
        assert len(content) <= _SPEC_CONTEXT_LIMIT

    def test_oversized_spec_keeps_open_questions(self, tmp_path: Path) -> None:
        from sova.core.steps.develop import _SPEC_CONTEXT_LIMIT, _load_spec_for_develop

        _write_spec(tmp_path, "START" + "s" * (_SPEC_CONTEXT_LIMIT + 5000), questions="DECISION: use sqlite")
        content, _ = _load_spec_for_develop(_spec_ctx(tmp_path))
        assert "START" in content
        assert TRUNCATION_MARKER in content
        assert content.endswith("DECISION: use sqlite")
        assert len(content) <= _SPEC_CONTEXT_LIMIT

    def test_oversized_open_questions_capped(self, tmp_path: Path) -> None:
        from sova.core.steps.develop import _SPEC_CONTEXT_LIMIT, _load_spec_for_develop

        _write_spec(tmp_path, "START plan", questions="q" * (_SPEC_CONTEXT_LIMIT * 2))
        content, _ = _load_spec_for_develop(_spec_ctx(tmp_path))
        assert "START plan" in content
        assert len(content) <= _SPEC_CONTEXT_LIMIT

    def test_open_questions_over_own_cap_kept_whole_when_context_fits(self) -> None:
        from sova.core.steps.develop import _SPEC_QUESTIONS_LIMIT, _cap_spec_context

        questions = "q" * (_SPEC_QUESTIONS_LIMIT + 100) + "ANSWER"
        result = _cap_spec_context("short plan", questions)
        assert TRUNCATION_MARKER not in result
        assert result == f"short plan\n\n{questions}"

    def test_normal_spec_untouched(self, tmp_path: Path) -> None:
        from sova.core.steps.develop import _load_spec_for_develop

        _write_spec(tmp_path, "Plan the thing.")
        content, _ = _load_spec_for_develop(_spec_ctx(tmp_path))
        assert TRUNCATION_MARKER not in content
        assert "Open Questions" in content

    def test_caveat_survives_trim(self, tmp_path: Path) -> None:
        from sova.core.spec_utils import SpecFreshness, spec_freshness_caveat
        from sova.core.steps.develop import _SPEC_CONTEXT_LIMIT, _load_spec_for_develop

        _write_spec(tmp_path, "s" * (_SPEC_CONTEXT_LIMIT + 5000))
        ctx = _spec_ctx(tmp_path)
        ctx.task = None
        content, note = _load_spec_for_develop(ctx)
        assert note
        assert content.startswith(spec_freshness_caveat(SpecFreshness.UNVERIFIABLE)[:40])
        assert TRUNCATION_MARKER in content


class TestDevelopFixPromptCap:
    async def test_fix_prompt_keeps_failing_tail_with_marker(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.core.steps.develop import _CHECK_OUTPUT_PROMPT_CHARS, DevelopStep

        check_output = "noise\n" * 2000 + "FAILED tests/test_foo.py::test_bar"
        with patch("sova.core.steps.develop.dispatch_prompt", new_callable=AsyncMock) as mock_dispatch:
            await DevelopStep()._invoke_fix_llm(_spec_ctx(tmp_path), "make check", check_output, 1)

        prompt = mock_dispatch.call_args.args[1]
        assert f"```\n{TRUNCATION_MARKER}\n" in prompt
        assert check_output[-_CHECK_OUTPUT_PROMPT_CHARS:] in prompt
