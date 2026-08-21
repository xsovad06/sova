"""Tests for multi-model consensus conflict resolution in sova.git.rebase."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.git.rebase import (
    _build_file_resolution_prompt,
    _configure_diff3,
    _count_non_marker_lines,
    _create_providers,
    _find_prompt_template,
    _has_conflict_markers,
    _is_valid_resolution,
    _load_consensus_config,
    _normalize_resolution,
    _resolve_file_with_consensus,
    _select_consensus,
    _try_consensus_resolution,
    rebase_with_conflict_resolution,
)
from sova.llm.models import LLMResult


class TestNormalizeResolution:
    def test_strips_trailing_whitespace_per_line(self) -> None:
        text = "line1   \nline2  \nline3\n"
        assert _normalize_resolution(text) == "line1\nline2\nline3"

    def test_normalizes_crlf(self) -> None:
        text = "line1\r\nline2\r\nline3"
        assert _normalize_resolution(text) == "line1\nline2\nline3"

    def test_strips_single_trailing_newline(self) -> None:
        text = "content\n"
        assert _normalize_resolution(text) == "content"

    def test_empty_string(self) -> None:
        assert _normalize_resolution("") == ""

    def test_preserves_internal_structure(self) -> None:
        text = "  indented\n    more\n"
        assert _normalize_resolution(text) == "  indented\n    more"


class TestHasConflictMarkers:
    def test_detects_opening_marker(self) -> None:
        assert _has_conflict_markers("some code\n<<<<<<< HEAD\nours\n") is True

    def test_detects_closing_marker(self) -> None:
        assert _has_conflict_markers("some code\n>>>>>>> branch\n") is True

    def test_clean_file(self) -> None:
        assert _has_conflict_markers("clean code\nno markers\n") is False

    def test_detects_separator_marker(self) -> None:
        assert _has_conflict_markers("code\n=======\n") is True

    def test_detects_base_marker(self) -> None:
        assert _has_conflict_markers("code\n||||||| base\n") is True

    def test_empty_string(self) -> None:
        assert _has_conflict_markers("") is False


class TestIsValidResolution:
    def test_rejects_conflict_markers(self) -> None:
        text = "code\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        assert _is_valid_resolution(text, original_non_marker_lines=10) is False

    def test_rejects_too_short(self) -> None:
        assert _is_valid_resolution("line1\nline2\n", original_non_marker_lines=10) is False

    def test_accepts_valid(self) -> None:
        lines = "\n".join(f"line{i}" for i in range(8))
        assert _is_valid_resolution(lines, original_non_marker_lines=10) is True

    def test_accepts_with_zero_originals(self) -> None:
        assert _is_valid_resolution("some content", original_non_marker_lines=0) is True

    def test_empty_resolution_rejected_when_original_has_content(self) -> None:
        assert _is_valid_resolution("", original_non_marker_lines=5) is False


class TestSelectConsensus:
    def test_all_agree(self) -> None:
        resolutions = ["same\ncontent", "same\ncontent", "same\ncontent"]
        result = _select_consensus(resolutions, threshold=0.67)
        assert result == "same\ncontent"

    def test_majority_agree(self) -> None:
        resolutions = ["same\ncontent", "same\ncontent", "different\ncontent"]
        result = _select_consensus(resolutions, threshold=0.6)
        assert result == "same\ncontent"

    def test_two_of_three_at_default_threshold(self) -> None:
        resolutions = ["same\ncontent", "same\ncontent", "different\ncontent"]
        result = _select_consensus(resolutions, threshold=0.66)
        assert result == "same\ncontent"

    def test_two_of_three_below_threshold(self) -> None:
        resolutions = ["same\ncontent", "same\ncontent", "different\ncontent"]
        result = _select_consensus(resolutions, threshold=0.67)
        assert result is None

    def test_no_consensus(self) -> None:
        resolutions = ["aaa", "bbb", "ccc"]
        result = _select_consensus(resolutions, threshold=0.67)
        assert result is None

    def test_two_agree_of_two(self) -> None:
        resolutions = ["same", "same"]
        result = _select_consensus(resolutions, threshold=0.67)
        assert result == "same"

    def test_two_disagree(self) -> None:
        resolutions = ["aaa", "bbb"]
        result = _select_consensus(resolutions, threshold=0.67)
        assert result is None

    def test_empty_list(self) -> None:
        result = _select_consensus([], threshold=0.67)
        assert result is None

    def test_single_item_below_threshold(self) -> None:
        result = _select_consensus(["only one"], threshold=0.67)
        assert result is None

    def test_normalization_applied(self) -> None:
        resolutions = ["same  \ncontent\n", "same\ncontent"]
        result = _select_consensus(resolutions, threshold=0.67)
        assert result is not None


class TestBuildFileResolutionPrompt:
    def test_contains_file_content(self) -> None:
        prompt = _build_file_resolution_prompt("test.py", "conflicted content", template=None)
        assert "test.py" in prompt
        assert "conflicted content" in prompt

    def test_instructs_complete_file(self) -> None:
        prompt = _build_file_resolution_prompt("test.py", "content", template=None)
        assert "complete" in prompt.lower() or "entire" in prompt.lower()

    def test_custom_template(self) -> None:
        template = "Resolve {filename}: {content}"
        prompt = _build_file_resolution_prompt("a.py", "abc", template=template)
        assert prompt == "Resolve a.py: abc"


class TestConfigureDiff3:
    @pytest.mark.asyncio
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_sets_diff3_config(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = MagicMock(success=True)
        await _configure_diff3(Path("/fake"))
        mock_run.assert_awaited_once()
        args = mock_run.call_args
        cmd_parts = [args[0][i] for i in range(len(args[0]))]
        assert "diff3" in cmd_parts
        assert "--local" in cmd_parts

    @pytest.mark.asyncio
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_failure_logged_not_raised(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = MagicMock(success=False, stderr="error")
        await _configure_diff3(Path("/fake"))


class TestResolveFileWithConsensus:
    @pytest.mark.asyncio
    async def test_zero_models_returns_none(self) -> None:
        result, cost = await _resolve_file_with_consensus(
            "test.py",
            "conflicted",
            models=[],
            providers={},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert result is None
        assert cost == Decimal("0")

    @pytest.mark.asyncio
    async def test_single_model_returns_none(self) -> None:
        provider = AsyncMock()
        result, cost = await _resolve_file_with_consensus(
            "test.py",
            "conflicted",
            models=["only-one"],
            providers={"only-one": provider},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert result is None
        assert cost == Decimal("0")
        provider.invoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_models_fail(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.invoke = AsyncMock(side_effect=RuntimeError("api down"))
        result, cost = await _resolve_file_with_consensus(
            "test.py",
            "line1\nline2\nline3\nline4\nline5\n",
            models=["model-a", "model-b"],
            providers={"model-a": mock_provider, "model-b": mock_provider},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert result is None
        assert cost == Decimal("0")

    @pytest.mark.asyncio
    async def test_consensus_reached(self) -> None:
        resolved_text = "\n".join(f"line{i}" for i in range(10))
        mock_result = MagicMock()
        mock_result.text = resolved_text
        mock_result.cost_usd = Decimal("0.01")
        mock_provider = AsyncMock()
        mock_provider.invoke = AsyncMock(return_value=mock_result)
        result, cost = await _resolve_file_with_consensus(
            "test.py",
            "<<<<<<< HEAD\nours\n||||||| base\norig\n=======\ntheirs\n>>>>>>> branch\n",
            models=["model-a", "model-b"],
            providers={"model-a": mock_provider, "model-b": mock_provider},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert result == resolved_text
        assert cost == Decimal("0.02")

    @pytest.mark.asyncio
    async def test_conflict_markers_in_output_filtered(self) -> None:
        bad_text = "code\n<<<<<<< HEAD\nstuff\n>>>>>>> branch"
        good_text = "\n".join(f"line{i}" for i in range(10))
        bad_result = MagicMock()
        bad_result.text = bad_text
        bad_result.cost_usd = Decimal("0.01")
        good_result = MagicMock()
        good_result.text = good_text
        good_result.cost_usd = Decimal("0.01")

        provider_bad = AsyncMock()
        provider_bad.invoke = AsyncMock(return_value=bad_result)
        provider_good = AsyncMock()
        provider_good.invoke = AsyncMock(return_value=good_result)

        result, cost = await _resolve_file_with_consensus(
            "test.py",
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nother_line\n",
            models=["bad", "good-a", "good-b"],
            providers={"bad": provider_bad, "good-a": provider_good, "good-b": provider_good},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert result == good_text
        assert cost == Decimal("0.03")

    @pytest.mark.asyncio
    async def test_partial_failure_with_consensus(self) -> None:
        resolved_text = "\n".join(f"line{i}" for i in range(10))
        mock_result = MagicMock()
        mock_result.text = resolved_text
        mock_result.cost_usd = Decimal("0.01")
        good_provider = AsyncMock()
        good_provider.invoke = AsyncMock(return_value=mock_result)
        bad_provider = AsyncMock()
        bad_provider.invoke = AsyncMock(side_effect=RuntimeError("timeout"))

        result, cost = await _resolve_file_with_consensus(
            "test.py",
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n",
            models=["good-a", "good-b", "bad"],
            providers={"good-a": good_provider, "good-b": good_provider, "bad": bad_provider},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert result == resolved_text
        assert cost == Decimal("0.02")

    @pytest.mark.asyncio
    async def test_budget_split_across_models(self) -> None:
        resolved_text = "\n".join(f"line{i}" for i in range(10))
        mock_result = MagicMock()
        mock_result.text = resolved_text
        mock_result.cost_usd = Decimal("0.01")
        mock_provider = AsyncMock()
        mock_provider.invoke = AsyncMock(return_value=mock_result)

        await _resolve_file_with_consensus(
            "test.py",
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n",
            models=["m1", "m2"],
            providers={"m1": mock_provider, "m2": mock_provider},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=Decimal("1.00"),
        )

        assert len(mock_provider.invoke.call_args_list) == 2
        for call in mock_provider.invoke.call_args_list:
            assert call.kwargs.get("max_budget_usd") == Decimal("0.50")

    @pytest.mark.asyncio
    async def test_prompt_template_selected_by_prefix(self) -> None:
        resolved_text = "\n".join(f"line{i}" for i in range(10))
        mock_result = MagicMock()
        mock_result.text = resolved_text
        mock_result.cost_usd = Decimal("0.01")
        mock_provider_a = AsyncMock()
        mock_provider_a.invoke = AsyncMock(return_value=mock_result)
        mock_provider_b = AsyncMock()
        mock_provider_b.invoke = AsyncMock(return_value=mock_result)

        await _resolve_file_with_consensus(
            "test.py",
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n",
            models=["gemini/gemini-pro", "gemini/gemini-flash"],
            providers={"gemini/gemini-pro": mock_provider_a, "gemini/gemini-flash": mock_provider_b},
            consensus_threshold=0.67,
            prompt_templates={"gemini": "Custom: {filename}\n{content}"},
            max_budget_usd=None,
        )

        prompt_arg = mock_provider_a.invoke.call_args[0][0]
        assert prompt_arg.startswith("Custom: test.py")


class TestCountNonMarkerLines:
    def test_counts_normal_lines(self) -> None:
        content = "line1\nline2\nline3\n"
        assert _count_non_marker_lines(content) == 3

    def test_excludes_conflict_markers(self) -> None:
        content = "code\n<<<<<<< HEAD\nours\n||||||| base\norig\n=======\ntheirs\n>>>>>>> branch\nmore code\n"
        assert _count_non_marker_lines(content) == 5

    def test_empty_string(self) -> None:
        assert _count_non_marker_lines("") == 0

    def test_all_markers(self) -> None:
        content = "<<<<<<< HEAD\n=======\n>>>>>>> branch\n"
        assert _count_non_marker_lines(content) == 0


class TestFindPromptTemplate:
    def test_exact_prefix_match(self) -> None:
        templates = {"gemini": "Gemini template", "claude": "Claude template"}
        assert _find_prompt_template("gemini/gemini-pro", templates) == "Gemini template"

    def test_slash_prefix_match(self) -> None:
        templates = {"gemini-pro": "Pro template"}
        assert _find_prompt_template("vertex_ai/gemini-pro", templates) == "Pro template"

    def test_no_match(self) -> None:
        templates = {"gemini": "Gemini template"}
        assert _find_prompt_template("claude-sonnet-4-6", templates) is None

    def test_empty_templates(self) -> None:
        assert _find_prompt_template("any-model", {}) is None

    def test_longest_prefix_wins(self) -> None:
        templates = {"claude": "Generic", "claude-sonnet": "Specific"}
        assert _find_prompt_template("claude-sonnet-4-6", templates) == "Specific"


class TestLoadConsensusConfig:
    @patch("sova.config.loader.load_config")
    def test_returns_config_values(self, mock_load: MagicMock) -> None:
        from sova.config.models import ConflictResolutionConfig, LLMConfig

        mock_cfg = MagicMock()
        mock_cfg.conflict_resolution = ConflictResolutionConfig(
            models=["m1", "m2"], consensus_threshold=0.8, prompt_templates={"m1": "tmpl"}
        )
        mock_cfg.llm = LLMConfig(cli_timeout=600)
        mock_load.return_value = mock_cfg
        models, threshold, templates, timeout = _load_consensus_config(Path("/fake"))
        assert models == ["m1", "m2"]
        assert threshold == 0.8
        assert templates == {"m1": "tmpl"}
        assert timeout == 600.0

    @patch("sova.config.loader.load_config", side_effect=Exception("bad config"))
    def test_returns_defaults_on_failure(self, mock_config: MagicMock) -> None:
        models, threshold, templates, timeout = _load_consensus_config(Path("/fake"))
        assert models == []
        assert threshold == 0.66
        assert templates == {}
        assert timeout is None


class TestCreateProviders:
    @patch("sova.llm.litellm_provider.LiteLLMProvider")
    @patch("sova.llm.litellm_provider._HAS_LITELLM", True)
    def test_creates_provider_per_model(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = MagicMock()
        result = _create_providers(["m1", "m2"], timeout=600.0)
        assert result is not None
        assert len(result) == 2
        assert "m1" in result
        assert "m2" in result
        for call in mock_cls.call_args_list:
            assert call.kwargs["timeout"] == 600.0

    @patch("sova.llm.litellm_provider._HAS_LITELLM", False)
    def test_returns_none_without_litellm(self) -> None:
        result = _create_providers(["m1"])
        assert result is None

    def test_returns_none_on_import_error(self) -> None:
        with patch.dict("sys.modules", {"sova.llm.litellm_provider": None}):
            result = _create_providers(["m1"])
        assert result is None


class TestTryConsensusResolution:
    @pytest.mark.asyncio
    async def test_resolves_all_files(self, tmp_path: Path) -> None:
        file1 = tmp_path / "a.py"
        file1.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nextra\n")
        resolved_text = "\n".join(f"line{i}" for i in range(10))
        mock_result = MagicMock()
        mock_result.text = resolved_text
        mock_result.cost_usd = Decimal("0.01")
        mock_provider = AsyncMock()
        mock_provider.invoke = AsyncMock(return_value=mock_result)
        mock_run = AsyncMock(return_value=MagicMock(success=True))

        with patch("sova.git.rebase.run", mock_run):
            success, cost = await _try_consensus_resolution(
                ["a.py"],
                cwd=tmp_path,
                models=["m1", "m2"],
                providers={"m1": mock_provider, "m2": mock_provider},
                consensus_threshold=0.67,
                prompt_templates={},
                max_budget_usd=None,
            )
        assert success is True
        assert cost == Decimal("0.02")
        assert file1.read_text() == resolved_text

    @pytest.mark.asyncio
    async def test_returns_false_on_no_consensus(self, tmp_path: Path) -> None:
        file1 = tmp_path / "b.py"
        file1.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nextra\n")
        result_a = MagicMock()
        result_a.text = "\n".join(f"lineA{i}" for i in range(10))
        result_a.cost_usd = Decimal("0.01")
        result_b = MagicMock()
        result_b.text = "\n".join(f"lineB{i}" for i in range(10))
        result_b.cost_usd = Decimal("0.01")
        provider_a = AsyncMock()
        provider_a.invoke = AsyncMock(return_value=result_a)
        provider_b = AsyncMock()
        provider_b.invoke = AsyncMock(return_value=result_b)

        success, _cost = await _try_consensus_resolution(
            ["b.py"],
            cwd=tmp_path,
            models=["m1", "m2"],
            providers={"m1": provider_a, "m2": provider_b},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_returns_false_on_file_read_error(self, tmp_path: Path) -> None:
        success, cost = await _try_consensus_resolution(
            ["nonexistent.py"],
            cwd=tmp_path,
            models=["m1", "m2"],
            providers={"m1": AsyncMock(), "m2": AsyncMock()},
            consensus_threshold=0.67,
            prompt_templates={},
            max_budget_usd=None,
        )
        assert success is False
        assert cost == Decimal("0")

    @pytest.mark.asyncio
    async def test_returns_false_on_stage_failure(self, tmp_path: Path) -> None:
        file1 = tmp_path / "c.py"
        file1.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nextra\n")
        resolved_text = "\n".join(f"line{i}" for i in range(10))
        mock_result = MagicMock()
        mock_result.text = resolved_text
        mock_result.cost_usd = Decimal("0.01")
        mock_provider = AsyncMock()
        mock_provider.invoke = AsyncMock(return_value=mock_result)
        mock_run = AsyncMock(return_value=MagicMock(success=False, stderr="stage error"))

        with patch("sova.git.rebase.run", mock_run):
            success, _cost = await _try_consensus_resolution(
                ["c.py"],
                cwd=tmp_path,
                models=["m1", "m2"],
                providers={"m1": mock_provider, "m2": mock_provider},
                consensus_threshold=0.67,
                prompt_templates={},
                max_budget_usd=None,
            )
        assert success is False


class TestConflictResolutionConfig:
    def test_default_values(self) -> None:
        from sova.config.models import ConflictResolutionConfig

        cfg = ConflictResolutionConfig()
        assert cfg.models == []
        assert cfg.consensus_threshold == 0.66
        assert cfg.prompt_templates == {}

    def test_custom_values(self) -> None:
        from sova.config.models import ConflictResolutionConfig

        cfg = ConflictResolutionConfig(
            models=["claude-sonnet-4-6", "gemini/gemini-pro"],
            consensus_threshold=0.8,
            prompt_templates={"gemini": "custom template"},
        )
        assert len(cfg.models) == 2
        assert cfg.consensus_threshold == 0.8

    def test_threshold_validation(self) -> None:
        from pydantic import ValidationError

        from sova.config.models import ConflictResolutionConfig

        with pytest.raises(ValidationError):
            ConflictResolutionConfig(consensus_threshold=1.5)


class TestConflictResolutionInProjectConfig:
    def test_nested_in_project_config(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(conflict_resolution={"models": ["model-a"]})
        assert cfg.conflict_resolution.models == ["model-a"]

    def test_default_factory(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        assert cfg.conflict_resolution.models == []


class TestConflictResolutionLoaderIntegration:
    def test_in_nested_sections(self) -> None:
        from sova.config.loader import _NESTED_SECTIONS

        assert "conflict_resolution" in _NESTED_SECTIONS


class TestRebaseWithConsensusIntegration:
    """Tests for consensus paths inside rebase_with_conflict_resolution()."""

    @pytest.mark.asyncio
    @patch("sova.git.rebase._create_providers")
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock)
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_consensus_disabled_with_fewer_than_two_models(
        self,
        mock_run: AsyncMock,
        mock_diff3: AsyncMock,
        mock_config: MagicMock,
        mock_providers: MagicMock,
    ) -> None:
        mock_config.return_value = (["single-model"], 0.67, {}, None)
        mock_run.return_value = MagicMock(success=True, stdout="", stderr="")

        result, _cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"))
        assert result.success is True
        mock_providers.assert_not_called()
        mock_diff3.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("sova.git.rebase._resolve_conflicts_with_llm", new_callable=AsyncMock)
    @patch("sova.git.rebase._try_consensus_resolution", new_callable=AsyncMock)
    @patch("sova.git.rebase._create_providers")
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock)
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_consensus_disabled_when_providers_unavailable(
        self,
        mock_run: AsyncMock,
        mock_diff3: AsyncMock,
        mock_config: MagicMock,
        mock_providers: MagicMock,
        mock_consensus: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        mock_config.return_value = (["m1", "m2"], 0.67, {}, None)
        mock_providers.return_value = None
        mock_llm.return_value = MagicMock(text="resolved", cost_usd=Decimal("0.01"))

        call_count = 0

        async def side_effect(*args: str, **kwargs: object) -> MagicMock:
            nonlocal call_count
            cmd = " ".join(args)
            if "fetch" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            if "stash" in cmd and "--include-untracked" in cmd:
                return MagicMock(success=True, stdout="No local changes to save")
            if "rebase" in cmd and "origin/" in cmd and "--continue" not in cmd and "--abort" not in cmd:
                return MagicMock(success=False, stdout="", stderr="CONFLICT")
            if "diff" in cmd and "--diff-filter=U" in cmd:
                call_count += 1
                if call_count <= 1:
                    return MagicMock(success=True, stdout="file.py\n")
                return MagicMock(success=True, stdout="")
            if "rebase" in cmd and "--continue" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            return MagicMock(success=True, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result, _cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"))
        assert result.success is True
        mock_consensus.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("sova.git.rebase._try_consensus_resolution", new_callable=AsyncMock)
    @patch("sova.git.rebase._create_providers")
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock)
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_consensus_success_resolves_conflicts(
        self,
        mock_run: AsyncMock,
        mock_diff3: AsyncMock,
        mock_config: MagicMock,
        mock_providers: MagicMock,
        mock_consensus: AsyncMock,
    ) -> None:
        mock_config.return_value = (["m1", "m2"], 0.67, {}, None)
        mock_providers.return_value = {"m1": AsyncMock(), "m2": AsyncMock()}
        mock_consensus.return_value = (True, Decimal("0.05"))

        call_count = 0

        async def side_effect(*args: str, **kwargs: object) -> MagicMock:
            nonlocal call_count
            cmd = " ".join(args)
            if "fetch" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            if "stash" in cmd and "--include-untracked" in cmd:
                return MagicMock(success=True, stdout="No local changes to save")
            if "rebase" in cmd and "origin/" in cmd and "--continue" not in cmd and "--abort" not in cmd:
                return MagicMock(success=False, stdout="", stderr="CONFLICT")
            if "diff" in cmd and "--diff-filter=U" in cmd:
                call_count += 1
                if call_count <= 1:
                    return MagicMock(success=True, stdout="file.py\n")
                return MagicMock(success=True, stdout="")
            if "rebase" in cmd and "--continue" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            return MagicMock(success=True, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"))
        assert result.success is True
        assert cost == Decimal("0.05")
        assert result.conflicts_resolved == 1
        mock_consensus.assert_awaited_once()
        kwargs = mock_consensus.await_args.kwargs
        assert kwargs["models"] == ["m1", "m2"]
        assert kwargs["consensus_threshold"] == 0.67
        assert kwargs["prompt_templates"] == {}

    @pytest.mark.asyncio
    @patch("sova.git.rebase._resolve_conflicts_with_llm", new_callable=AsyncMock)
    @patch("sova.git.rebase._try_consensus_resolution", new_callable=AsyncMock)
    @patch("sova.git.rebase._create_providers")
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock)
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_consensus_failure_falls_back_to_single_model(
        self,
        mock_run: AsyncMock,
        mock_diff3: AsyncMock,
        mock_config: MagicMock,
        mock_providers: MagicMock,
        mock_consensus: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        mock_config.return_value = (["m1", "m2"], 0.67, {}, None)
        mock_providers.return_value = {"m1": AsyncMock(), "m2": AsyncMock()}
        mock_consensus.return_value = (False, Decimal("0.03"))
        mock_llm.return_value = LLMResult(text="fixed", model="test", cost_usd=Decimal("0.02"))

        diff_call = 0

        async def side_effect(*args: str, **kwargs: object) -> MagicMock:
            nonlocal diff_call
            cmd = " ".join(args)
            if "fetch" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            if "stash" in cmd and "--include-untracked" in cmd:
                return MagicMock(success=True, stdout="No local changes to save")
            if "rebase" in cmd and "origin/" in cmd and "--continue" not in cmd and "--abort" not in cmd:
                return MagicMock(success=False, stdout="", stderr="CONFLICT")
            if "diff" in cmd and "--diff-filter=U" in cmd:
                diff_call += 1
                if diff_call <= 2:
                    return MagicMock(success=True, stdout="file.py\n")
                return MagicMock(success=True, stdout="")
            if "rebase" in cmd and "--continue" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            return MagicMock(success=True, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"))
        assert result.success is True
        assert cost == Decimal("0.05")
        mock_consensus.assert_awaited_once()
        mock_llm.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("sova.git.rebase._create_providers")
    @patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock)
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_no_consensus_config_uses_single_model_only(
        self,
        mock_run: AsyncMock,
        mock_config: MagicMock,
        mock_diff3: AsyncMock,
        mock_providers: MagicMock,
    ) -> None:
        mock_config.return_value = ([], 0.67, {}, None)
        mock_run.return_value = MagicMock(success=True, stdout="", stderr="")

        result, _cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"))
        assert result.success is True
        mock_providers.assert_not_called()
        mock_diff3.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_fetch_failure_returns_error(
        self,
        mock_run: AsyncMock,
        mock_config: MagicMock,
    ) -> None:
        mock_config.return_value = ([], 0.67, {}, None)
        mock_run.return_value = MagicMock(success=False, stderr="network error")

        result, _cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"))
        assert result.success is False
        assert "Fetch failed" in result.error

    @pytest.mark.asyncio
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_stash_and_restore_on_clean_rebase(
        self,
        mock_run: AsyncMock,
        mock_config: MagicMock,
    ) -> None:
        mock_config.return_value = ([], 0.67, {}, None)

        calls: list[str] = []

        async def side_effect(*args: str, **kwargs: object) -> MagicMock:
            cmd = " ".join(args)
            calls.append(cmd)
            if "stash" in cmd and "--include-untracked" in cmd:
                return MagicMock(success=True, stdout="Saved working directory")
            if "stash" in cmd and "pop" in cmd:
                return MagicMock(success=True, stdout="")
            return MagicMock(success=True, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result, _cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"))
        assert result.success is True
        assert any("stash pop" in c for c in calls)

    @pytest.mark.asyncio
    @patch("sova.git.rebase._resolve_conflicts_with_llm", new_callable=AsyncMock)
    @patch("sova.git.rebase._try_consensus_resolution", new_callable=AsyncMock)
    @patch("sova.git.rebase._create_providers")
    @patch("sova.git.rebase._load_consensus_config")
    @patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock)
    @patch("sova.git.rebase.run", new_callable=AsyncMock)
    async def test_consensus_resolved_but_remaining_conflicts_falls_back(
        self,
        mock_run: AsyncMock,
        mock_diff3: AsyncMock,
        mock_config: MagicMock,
        mock_providers: MagicMock,
        mock_consensus: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """When consensus resolves but _get_conflicted_files still returns files,
        consensus_resolved is set to False and fallback single-model path runs."""
        mock_config.return_value = (["m1", "m2"], 0.67, {}, None)
        mock_providers.return_value = {"m1": AsyncMock(), "m2": AsyncMock()}
        mock_consensus.return_value = (True, Decimal("0.02"))
        mock_llm.return_value = LLMResult(text="fixed", model="test", cost_usd=Decimal("0.01"))

        diff_call = 0

        async def side_effect(*args: str, **kwargs: object) -> MagicMock:
            nonlocal diff_call
            cmd = " ".join(args)
            if "fetch" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            if "stash" in cmd:
                return MagicMock(success=True, stdout="No local changes to save")
            if "rebase" in cmd and "origin/" in cmd and "--continue" not in cmd and "--abort" not in cmd:
                return MagicMock(success=False, stdout="", stderr="CONFLICT")
            if "diff" in cmd and "--diff-filter=U" in cmd:
                diff_call += 1
                if diff_call <= 2:
                    return MagicMock(success=True, stdout="a.py\n")
                if diff_call == 3:
                    return MagicMock(success=True, stdout="a.py\n")
                return MagicMock(success=True, stdout="")
            if "rebase" in cmd and "--continue" in cmd:
                return MagicMock(success=True, stdout="", stderr="")
            return MagicMock(success=True, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/fake"), max_attempts=1)
        assert result.success is True
        assert cost == Decimal("0.03")
        mock_consensus.assert_awaited_once()
        mock_llm.assert_awaited_once()


class TestTryConsensusResolutionMultiFile:
    @pytest.mark.asyncio
    async def test_budget_split_across_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nextra\n")
        f2.write_text("<<<<<<< HEAD\nours2\n=======\ntheirs2\n>>>>>>> branch\nextra2\n")
        resolved = "\n".join(f"line{i}" for i in range(10))
        mock_result = MagicMock()
        mock_result.text = resolved
        mock_result.cost_usd = Decimal("0.01")
        mock_provider = AsyncMock()
        mock_provider.invoke = AsyncMock(return_value=mock_result)
        mock_run = AsyncMock(return_value=MagicMock(success=True))

        with patch("sova.git.rebase.run", mock_run):
            success, cost = await _try_consensus_resolution(
                ["a.py", "b.py"],
                cwd=tmp_path,
                models=["m1", "m2"],
                providers={"m1": mock_provider, "m2": mock_provider},
                consensus_threshold=0.67,
                prompt_templates={},
                max_budget_usd=Decimal("2.00"),
            )
        assert success is True
        assert cost == Decimal("0.04")
        assert len(mock_provider.invoke.call_args_list) == 4
        for call in mock_provider.invoke.call_args_list:
            assert call.kwargs.get("max_budget_usd") == Decimal("0.50")

    @pytest.mark.asyncio
    async def test_second_file_no_consensus_returns_false(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nextra\n")
        f2.write_text("<<<<<<< HEAD\nours2\n=======\ntheirs2\n>>>>>>> branch\nextra2\n")

        resolved = "\n".join(f"line{i}" for i in range(10))
        good_result = MagicMock(text=resolved, cost_usd=Decimal("0.01"))
        diff_result_a = MagicMock(text="different_a\n" * 10, cost_usd=Decimal("0.01"))
        diff_result_b = MagicMock(text="different_b\n" * 10, cost_usd=Decimal("0.01"))

        call_count = 0

        async def route_invoke(*args: str, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return good_result
            if call_count == 3:
                return diff_result_a
            return diff_result_b

        mixed_provider_1 = AsyncMock()
        mixed_provider_1.invoke = AsyncMock(side_effect=route_invoke)
        mixed_provider_2 = AsyncMock()
        mixed_provider_2.invoke = mixed_provider_1.invoke

        mock_run = AsyncMock(return_value=MagicMock(success=True))

        with patch("sova.git.rebase.run", mock_run):
            success, _cost = await _try_consensus_resolution(
                ["a.py", "b.py"],
                cwd=tmp_path,
                models=["m1", "m2"],
                providers={"m1": mixed_provider_1, "m2": mixed_provider_2},
                consensus_threshold=0.67,
                prompt_templates={},
                max_budget_usd=None,
            )
        assert success is False
