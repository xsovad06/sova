"""Mocked disposable-project scenario for the OpenAI-only pipeline (#1130).

No network, no subprocess, no real model: ``llm.provider = "openai"`` with ``agent.runtime = "codex"``
drives the researcher and developer dispatch paths, with a tripwire on every Claude class, and the
unsupported-capability case (a budget cap Codex cannot enforce) is shown to fail visibly. The live
counterpart is ``tests/test_live_pilot.py``, which stays opt-in.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.agent_dispatch import dispatch_command
from sova.core.context import ExecutionContext
from sova.core.steps.develop import DevelopStep
from sova.core.steps.research import ResearchStep
from sova.ipc.runtime import ClaudeCodeRuntime, CodexRuntime, create_runtime
from sova.llm.provider import create_provider
from sova.llm.providers.anthropic_api import AnthropicAPIProvider
from sova.llm.providers.claude_code import ClaudeCodeProvider
from sova.utils.diagnostics import claude_model_references

SOL = "openai/gpt-6.1-sol"
LUNA = "openai/gpt-6-luna"
SECTION = "## Research\n\n**Complexity**: simple\n\n### Affected Files\n\n- `a.py` (modify): the fix"


def _openai_config(*, allow_unenforced_budget: bool = True) -> ProjectConfig:
    return ProjectConfig(
        llm={
            "provider": "openai",
            "model": SOL,
            "fallback_model": SOL,
            "routing": {"develop": SOL, "triage": LUNA, "review": SOL, "research": SOL},
        },
        agent={"model": SOL, "runtime": "codex", "fallback_models": [SOL]},
        roles={
            "triage_model": LUNA,
            "researcher_model": SOL,
            "reviewer_model": SOL,
            "planner_model": SOL,
            "developer_model": SOL,
        },
        oversight={"analysis_model": SOL},
        confidence={"model": SOL},
        codex={"model": "gpt-6.1-sol", "allow_unenforced_budget": allow_unenforced_budget},
    )


def _codex_turn(reply: str) -> list[str]:
    """The JSON Lines a ``codex exec`` turn prints, replayed through the real CodexStreamParser."""
    return [
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        json.dumps({"type": "item.completed", "item": {"item_type": "agent_message", "text": reply}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 45}}),
    ]


class _Process:
    """Minimal AgentProcess double that prints the given lines and exits 0."""

    returncode: int | None = None

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        return
        yield ""  # pragma: no cover (makes this an async generator)

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    async def stop(self, timeout: float = 10.0, *, cause: str = "", requester: str = "") -> None:
        self.returncode = 0


class _ScriptedCodex(CodexRuntime):
    """The real CodexRuntime, minus the CLI: records each spawn and replays a canned final message."""

    def __init__(self, config: object, reply: str) -> None:
        super().__init__(config=config)
        self.spawns: list[dict] = []
        self._reply = reply

    async def check_available(self) -> tuple[bool, str]:
        return True, "API key configured"

    async def spawn(self, prompt, cwd, **kwargs):  # noqa: ANN001, ANN003 (test double)
        self.spawns.append({"prompt": prompt, "cwd": cwd, **kwargs})
        return _Process(_codex_turn(self._reply))


def _stub_preflight(model: str):
    return patch(
        "sova.core.agent_dispatch.client.prepare_invocation",
        new_callable=AsyncMock,
        return_value=(None, model, 60.0),
    )


def _ctx(tmp_path: Path, cfg: ProjectConfig, body: str) -> ExecutionContext:
    adapter = MagicMock()
    adapter.get_task = AsyncMock(return_value=Task(id="42", title="Fix it", body=body, state=TaskState.TRIAGED))
    adapter.get_comments = AsyncMock(return_value=[])
    adapter.edit_body = AsyncMock()
    return ExecutionContext(project_dir=tmp_path, config=cfg, adapter=adapter, issue_number="42", role="researcher")


@pytest.fixture(autouse=True)
def litellm_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI provider is built on litellm, an optional extra CI does not install.

    The scenario only constructs the provider (research runs on the Codex runtime), so the import guard is lifted.
    """
    import sova.llm.litellm_provider as litellm_provider

    monkeypatch.setattr(litellm_provider, "_HAS_LITELLM", True)


@pytest.fixture
def no_claude():
    """Constructing any Claude provider (Claude Code or the Anthropic API) or runtime fails the scenario."""

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the OpenAI-only scenario constructed a Claude class")

    with (
        patch.object(ClaudeCodeProvider, "__init__", boom),
        patch.object(AnthropicAPIProvider, "__init__", boom),
        patch.object(ClaudeCodeRuntime, "__init__", boom),
    ):
        yield


class TestOpenAIOnlyScenario:
    def test_config_names_no_claude_model(self) -> None:
        assert claude_model_references(_openai_config()) == []

    def test_provider_and_runtime_are_not_claude(self, no_claude: None, tmp_path: Path) -> None:
        cfg = _openai_config()
        provider = create_provider(cfg.llm, tmp_path)
        assert not provider.capabilities.runs_commands_with_tools
        assert type(provider).__name__ != "ClaudeCodeProvider"
        assert isinstance(create_runtime(cfg.agent.runtime, codex=cfg.codex), CodexRuntime)

    async def test_researcher_then_developer_run_on_codex_only(self, no_claude: None, tmp_path: Path) -> None:
        cfg = _openai_config()
        runtime = _ScriptedCodex(cfg.codex, SECTION)
        ctx = _ctx(tmp_path, cfg, "Fix the thing in a.py.")
        (tmp_path / ".claude" / "commands").mkdir(parents=True)
        (tmp_path / ".claude" / "commands" / "develop.md").write_text(
            "---\nname: develop\ndescription: test\n---\nDevelop $ARGUMENTS\n", encoding="utf-8"
        )

        with (
            patch("sova.core.steps.research.get_provider", return_value=create_provider(cfg.llm, tmp_path)),
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(SOL),
        ):
            research = await ResearchStep().execute(ctx)
            runtime._reply = "Developed"
            developed = await dispatch_command(
                DevelopStep(), "/develop", args="42", model=SOL, cwd=tmp_path, ctx=ctx, timeout=60
            )

        assert research.success
        ctx.adapter.edit_body.assert_awaited_once()
        assert SECTION in ctx.adapter.edit_body.await_args.args[1]
        assert developed.text == "Developed"
        # What the live pilot records per step: the model Codex reported, its tokens, and the cost provenance.
        assert (developed.model, developed.input_tokens, developed.output_tokens) == ("gpt-6.1-sol", 120, 45)
        assert str(developed.cost_source) != "priced"
        research_spawn, develop_spawn = runtime.spawns
        assert research_spawn["read_only"] is True
        assert not develop_spawn.get("read_only")
        # CodexRuntime ignores the model SOVA passes to spawn() and runs codex.model, so the argument proves
        # nothing; test_codex_exec_runs_the_configured_model pins the model that reaches the CLI.
        assert runtime.honours_model_override is False

    async def test_codex_exec_runs_the_configured_model(self, no_claude: None, tmp_path: Path) -> None:
        """The argv Codex is launched with carries codex.model, whatever model SOVA passes to spawn()."""
        cfg = _openai_config()
        runtime = CodexRuntime(config=cfg.codex)
        spawned = AsyncMock(return_value=_Process([]))

        with (
            patch("sova.ipc.runtime._spawn_agent_process", spawned),
            patch.object(CodexRuntime, "_resolve_stdin_capable", AsyncMock(return_value=True)),
        ):
            await runtime.spawn("do it", tmp_path, model="openai/something-else", max_budget_usd=None)

        argv = spawned.await_args.args[0]
        assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
        assert "something-else" not in " ".join(argv)

    async def test_unsupported_budget_cap_fails_visibly(self, no_claude: None, tmp_path: Path) -> None:
        """Codex cannot enforce a budget cap; without codex.allow_unenforced_budget the step fails, loudly."""
        cfg = _openai_config(allow_unenforced_budget=False)
        runtime = CodexRuntime(config=cfg.codex)
        ctx = _ctx(tmp_path, cfg, "Fix the thing in a.py.")

        with (
            patch("sova.core.steps.research.get_provider", return_value=create_provider(cfg.llm, tmp_path)),
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch.object(CodexRuntime, "check_available", AsyncMock(return_value=(True, "ok"))),
            _stub_preflight(SOL),
        ):
            result = await ResearchStep().execute(ctx)

        assert not result.success
        assert "allow_unenforced_budget" in (result.error or "")
        ctx.adapter.edit_body.assert_not_awaited()

    async def test_unavailable_runtime_fails_instead_of_degrading(self, no_claude: None, tmp_path: Path) -> None:
        cfg = _openai_config()
        runtime = _ScriptedCodex(cfg.codex, SECTION)
        ctx = _ctx(tmp_path, cfg, "Fix the thing in a.py.")

        with (
            patch("sova.core.steps.research.get_provider", return_value=create_provider(cfg.llm, tmp_path)),
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch.object(_ScriptedCodex, "check_available", AsyncMock(return_value=(False, "codex not logged in"))),
            _stub_preflight(SOL),
        ):
            result = await ResearchStep().execute(ctx)

        assert not result.success
        assert "codex not logged in" in (result.error or "")
        assert runtime.spawns == []
        ctx.adapter.edit_body.assert_not_awaited()
