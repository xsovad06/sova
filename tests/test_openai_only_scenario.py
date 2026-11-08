"""Mocked disposable-project scenario for the OpenAI-only pipeline (#1130).

No network, no process launch, no real model: ``llm.provider = "openai"`` with ``agent.runtime = "codex"``
drives the researcher, developer, PR-handoff and reviewer paths, with a tripwire on every Claude class.
Tool-using steps run on a scripted ``CodexRuntime``; SOVA's own text calls (PR body, review) run through
the real OpenAI provider with only ``litellm`` faked underneath, so routing and cost provenance are real.
The forge and tracker are mocks: nothing is pushed, opened or labelled. The unsupported-capability case
(a budget cap Codex cannot enforce) fails visibly, and three recovery cases are covered: a review change
request that becomes an address-review cycle, a CI failure fixed on Codex, and a cancelled agent turn.
The live counterpart is ``tests/test_live_pilot.py``, which stays opt-in.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.agent_dispatch import dispatch_command
from sova.core.context import ExecutionContext
from sova.core.steps.address_review import AddressReviewStep
from sova.core.steps.create_pr import CreatePRStep
from sova.core.steps.develop import DevelopStep
from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep
from sova.core.steps.monitor_ci import MonitorCIStep
from sova.core.steps.research import ResearchStep
from sova.git.pr import CheckConclusion, CheckStatus, CICheck, PRInfo
from sova.ipc.handoff import read_handoff_file
from sova.ipc.runtime import ClaudeCodeRuntime, CodexRuntime, create_runtime
from sova.llm import client
from sova.llm.models import CostSource
from sova.llm.provider import create_provider
from sova.llm.providers.anthropic_api import AnthropicAPIProvider
from sova.llm.providers.claude_code import ClaudeCodeProvider
from sova.roles.reviewer import ReviewerRole
from sova.utils.diagnostics import claude_model_references
from sova.utils.shell import ShellResult

SOL = "openai/gpt-6.1-sol"
LUNA = "openai/gpt-6-luna"
SECTION = "## Research\n\n**Complexity**: simple\n\n### Affected Files\n\n- `a.py` (modify): the fix"
DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n x = 1\n-y = 2\n+y = 3\n z = 4\n"
CHANGE_REQUEST = {
    "file": "a.py",
    "line": 2,
    "severity": 6,
    "category": "correctness",
    "description": "y is off by one",
    "suggestion": "use 2",
}


def _openai_config(*, allow_unenforced_budget: bool = True) -> ProjectConfig:
    return ProjectConfig(
        llm={
            "provider": "openai",
            "model": SOL,
            "fallback_model": SOL,
            "routing": {
                "develop": SOL,
                "triage": LUNA,
                "review": SOL,
                "research": SOL,
                "pr_body": LUNA,
                "monitor_ci": SOL,
            },
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


class _HangingProcess(_Process):
    """An agent turn that never finishes on its own, so cancellation is the only way out."""

    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()
        self.stop_causes: list[str] = []

    async def stdout_lines(self) -> AsyncIterator[str]:
        self.started.set()
        await asyncio.Event().wait()
        yield ""  # pragma: no cover (never reached)

    async def stop(self, timeout: float = 10.0, *, cause: str = "", requester: str = "") -> None:
        self.stop_causes.append(cause)
        self.returncode = -15


class _FakeLiteLLM:
    """Stands in for the ``litellm`` module under the real OpenAI provider: records each call, replies in order."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.replies: list[str] = []

    async def acompletion(self, *, messages: list[dict], model: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append({"model": model, "prompt": messages[-1]["content"], **kwargs})
        message = SimpleNamespace(content=self.replies.pop(0))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=80),
            model=model,
        )

    def completion_cost(self, completion_response: object) -> float:
        return 0.0014


def _stub_preflight(model: str):
    return patch(
        "sova.core.agent_dispatch.client.prepare_invocation",
        new_callable=AsyncMock,
        return_value=(None, model, 60.0),
    )


def _ok(stdout: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr="")


def _ctx(
    tmp_path: Path, cfg: ProjectConfig, body: str, *, role: str = "researcher", state: TaskState = TaskState.TRIAGED
) -> ExecutionContext:
    """A context on a mocked tracker whose issue state follows ``transition_state``, as a real tracker's would."""
    task = Task(id="42", title="Fix it", body=body, state=state)

    async def transition(_issue: str, new_state: TaskState) -> None:
        task.state = new_state

    adapter = MagicMock()
    adapter.get_task = AsyncMock(return_value=task)
    adapter.get_comments = AsyncMock(return_value=[])
    for name in ("edit_body", "post_pr_review", "post_pr_comment", "add_label", "remove_label"):
        setattr(adapter, name, AsyncMock())
    adapter.transition_state = AsyncMock(side_effect=transition)
    return ExecutionContext(project_dir=tmp_path, config=cfg, adapter=adapter, issue_number="42", role=role)


def _review_ctx(dev_ctx: ExecutionContext) -> ExecutionContext:
    """The reviewer's context, built from the handoff the developer pipeline left, as the auto-handoff does."""
    handoff = read_handoff_file(dev_ctx.project_dir, issue=dev_ctx.issue_number)
    assert handoff is not None
    review = next(action for action in handoff.next_actions if action.id == "review")
    assert review.args["role"] == "reviewer"
    assert review.auto_execute
    ctx = ExecutionContext(
        project_dir=dev_ctx.project_dir,
        config=dev_ctx.config,
        adapter=dev_ctx.adapter,
        issue_number=review.args["issue"],
        role="reviewer",
    )
    ctx.pr_number = review.args["pr"]
    ctx.branch_name = handoff.branch
    return ctx


def _review_reply(*findings: dict) -> str:
    return json.dumps({"findings": list(findings), "summary": f"{len(findings)} finding(s)"})


def _pr_fetches(diff: str = DIFF):
    """The reviewer's PR reads (diff, files, head), which go to GitHub in a real run."""
    return (
        patch("sova.roles.reviewer.get_pr_diff", AsyncMock(return_value=diff)),
        patch("sova.roles.reviewer.get_pr_files", AsyncMock(return_value=["a.py"])),
        patch("sova.roles.reviewer.get_pr_head_sha", AsyncMock(return_value="abc1234")),
    )


@pytest.fixture(autouse=True)
def litellm_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI provider is built on litellm, an optional extra CI does not install.

    The scenario only constructs the provider (research runs on the Codex runtime), so the import guard is lifted.
    """
    import sova.llm.litellm_provider as litellm_provider

    monkeypatch.setattr(litellm_provider, "_HAS_LITELLM", True)


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No process starts: no git, gh, codex or claude, so nothing is pushed, opened or merged.

    Checked again at teardown, so a launch that SOVA's fail-open handlers swallowed still fails the test.
    The dispatch layer's before-and-after worktree fingerprint (two git calls per agent turn) reads as
    unavailable instead; ``tmp_path`` is not a repository anyway.
    """
    launched: list[tuple] = []

    def refuse(*args: object, **_kwargs: object) -> None:
        launched.append(args[:3])
        raise AssertionError(f"the OpenAI-only scenario started a process: {args[:3]}")

    async def refuse_async(*args: object, **kwargs: object) -> None:
        refuse(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_async)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("sova.core.agent_dispatch._worktree_fingerprint", AsyncMock(return_value=None))
    yield
    assert launched == [], f"processes started during the scenario: {launched}"


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


@pytest.fixture
def openai_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reset_db_rows: None) -> Iterator[_FakeLiteLLM]:
    """SOVA's own text calls run through the real OpenAI provider and task-type routing; only litellm is faked.

    ``reset_db_rows`` keeps the reviewer's handoff lookups on the in-memory test database.
    """
    import sova.llm.litellm_provider as litellm_provider

    cfg = _openai_config()
    fake = _FakeLiteLLM()
    monkeypatch.setattr(litellm_provider, "litellm", fake, raising=False)
    monkeypatch.setattr(client, "_try_load_config_async", AsyncMock(return_value=cfg))
    monkeypatch.setattr("sova.core.steps._handoff_helpers.notify", MagicMock())
    client.set_provider(create_provider(cfg.llm, tmp_path))
    yield fake
    client.reset_provider()


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


class TestOpenAIOnlyHandoffAndRecovery:
    """The PR-handoff and reviewer path, and the recovery cases, with forge and tracker mocked."""

    async def _open_pr(self, ctx: ExecutionContext, fake: _FakeLiteLLM) -> AsyncMock:
        """Run create_pr and handoff_to_reviewer; returns the mocked ``gh pr create``."""
        fake.replies.append("## Summary\n\nSets y to 3 in a.py.")
        create = AsyncMock(return_value=PRInfo(number=7, url="https://github.test/o/r/pull/7"))
        with (
            patch("sova.core.steps.create_pr.run", AsyncMock(return_value=_ok("abc1234 fix(core): set y\n"))),
            patch("sova.git.operations.find_pr_for_issue", AsyncMock(return_value=None)),
            patch("sova.git.operations.create_pr", create),
            patch("sova.git.operations.assign_pr", AsyncMock()),
        ):
            created = await CreatePRStep().execute(ctx)
        assert created.success, created.error
        assert (await CreatePRStep().validate_output(ctx)).passed
        handed = await HandoffToReviewerStep().execute(ctx)
        assert handed.success
        return create

    async def test_pr_handoff_and_review_run_on_openai_only(
        self, no_claude: None, openai_text: _FakeLiteLLM, tmp_path: Path
    ) -> None:
        cfg = _openai_config()
        dev = _ctx(tmp_path, cfg, "Fix the thing in a.py.", role="developer", state=TaskState.IN_PROGRESS)
        dev.branch_name = "feat/issue-42"
        create = await self._open_pr(dev, openai_text)

        # The PR body came from the OpenAI call routed for pr_body, not from the no-LLM fallback body.
        assert openai_text.calls[0]["model"] == LUNA
        assert "Sets y to 3" in create.await_args.kwargs["body"]
        assert dev.pr_number == 7
        assert dev.cost_source == CostSource.PRICED

        reviewer = _review_ctx(dev)
        openai_text.replies.append(_review_reply())
        fetch_diff, fetch_files, fetch_head = _pr_fetches()
        with fetch_diff, fetch_files, fetch_head:
            result = await ReviewerRole().execute(reviewer)

        assert result.success, result.error
        assert [call["model"] for call in openai_text.calls] == [LUNA, SOL]
        reviewer.adapter.post_pr_review.assert_awaited_once()
        reviewer.adapter.add_label.assert_awaited_once_with("42", "sova:approved")
        verdict = read_handoff_file(tmp_path, issue="42")
        assert verdict is not None
        assert verdict.details["next_action"] == "approve"
        assert [action.id for action in verdict.next_actions] == ["integrate"]

    async def test_change_request_becomes_an_address_review_cycle_on_codex(
        self, no_claude: None, openai_text: _FakeLiteLLM, tmp_path: Path
    ) -> None:
        """Recovery: a blocking review finding hands back to the developer, whose fix runs on Codex."""
        cfg = _openai_config()
        reviewer = _ctx(tmp_path, cfg, "Fix the thing in a.py.", role="reviewer", state=TaskState.IN_REVIEW)
        reviewer.pr_number, reviewer.branch_name = 7, "feat/issue-42"
        openai_text.replies.append(_review_reply(CHANGE_REQUEST))
        fetch_diff, fetch_files, fetch_head = _pr_fetches()
        with fetch_diff, fetch_files, fetch_head:
            reviewed = await ReviewerRole().execute(reviewer)

        assert reviewed.success, reviewed.error
        reviewer.adapter.add_label.assert_awaited_once_with("42", "sova:revise")
        verdict = read_handoff_file(tmp_path, issue="42")
        assert verdict is not None
        assert verdict.details["next_action"] == "address_review"
        address = next(action for action in verdict.next_actions if action.id == "address_review")
        assert address.args["role"] == "developer"
        assert address.auto_execute is cfg.pipeline.auto_address_review

        dev = _ctx(tmp_path, cfg, "Fix the thing in a.py.", role="developer", state=TaskState.IN_REVIEW)
        dev.pr_number, dev.branch_name, dev.pipeline_variant = 7, "feat/issue-42", "address_review"
        runtime = _ScriptedCodex(cfg.codex, "Fixed y")
        with (
            patch("sova.core.steps.address_review.run", AsyncMock(return_value=_ok("abc1234\n"))),
            patch("sova.core.steps.address_review._load_coderabbit_findings", AsyncMock(return_value=([], []))),
            patch("sova.adapters.external_reviews.fetch_review_threads", AsyncMock(return_value=[])),
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
        ):
            addressed = await AddressReviewStep().execute(dev)
        assert addressed.success, addressed.error
        (spawn,) = runtime.spawns
        assert "y is off by one" in spawn["prompt"]
        assert not spawn.get("read_only")
        assert dev.addressed_review_findings and dev.addressed_review_findings[0]["description"] == "y is off by one"

        rereview = await HandoffToReviewerStep().execute(dev)
        assert rereview.success
        handoff = read_handoff_file(tmp_path, issue="42")
        assert handoff is not None
        assert "re-review" in next(action for action in handoff.next_actions if action.id == "review").description

    async def test_ci_failure_is_fixed_on_codex_and_repolled(
        self, no_claude: None, openai_text: _FakeLiteLLM, tmp_path: Path
    ) -> None:
        """Recovery: a red check is handed to Codex, the fix is pushed (mocked) and the new head polls green."""
        cfg = _openai_config()
        ctx = _ctx(tmp_path, cfg, "Fix the thing in a.py.", role="developer", state=TaskState.IN_REVIEW)
        ctx.pr_number, ctx.branch_name = 7, "feat/issue-42"
        runtime = _ScriptedCodex(cfg.codex, "Fixed the failing test")
        red = CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.FAILURE, details_url="")
        green = CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url="")
        checks = AsyncMock(side_effect=[[red], [green]])
        push = AsyncMock()
        with (
            patch.object(MonitorCIStep, "_get_local_head_sha", AsyncMock(side_effect=["sha-red", "sha-green"])),
            patch.object(MonitorCIStep, "_find_pr_head_mismatch", AsyncMock(return_value=None)),
            patch.object(MonitorCIStep, "_validate_fix", AsyncMock(return_value=(False, None))),
            patch.object(MonitorCIStep, "_fetch_sonarcloud_coverage_prompt", AsyncMock(return_value="")),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", checks),
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", AsyncMock(return_value="FAILED test_a.py::test_y")),
            patch("sova.utils.shell.run", AsyncMock(return_value=_ok("abc1234 fix(core): set y\n"))),
            patch("sova.git.operations.push", push),
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
        ):
            result = await MonitorCIStep().execute(ctx)

        assert result.success, result.error
        assert "after 1 fix attempt" in result.summary
        (spawn,) = runtime.spawns
        assert "FAILED test_a.py::test_y" in spawn["prompt"]
        push.assert_awaited_once()
        assert [call.args[0] for call in checks.await_args_list] == ["sha-red", "sha-green"]
        assert openai_text.calls == []

    async def test_cancelled_agent_turn_stops_codex_and_does_not_degrade(self, no_claude: None, tmp_path: Path) -> None:
        """Recovery: cancelling a run mid-turn stops the Codex process and propagates; nothing retries elsewhere."""
        cfg = _openai_config()
        ctx = _ctx(tmp_path, cfg, "Fix the thing in a.py.", role="developer", state=TaskState.IN_PROGRESS)
        runtime = _ScriptedCodex(cfg.codex, "")
        proc = _HangingProcess()
        runtime.spawn = AsyncMock(return_value=proc)  # type: ignore[method-assign]
        (tmp_path / ".claude" / "commands").mkdir(parents=True)
        (tmp_path / ".claude" / "commands" / "develop.md").write_text(
            "---\nname: develop\ndescription: test\n---\nDevelop $ARGUMENTS\n", encoding="utf-8"
        )

        with patch("sova.core.agent_dispatch.get_runtime", return_value=runtime), _stub_preflight(SOL):
            turn = asyncio.create_task(
                dispatch_command(DevelopStep(), "/develop", args="42", model=SOL, cwd=tmp_path, ctx=ctx, timeout=60)
            )
            await asyncio.wait_for(proc.started.wait(), timeout=5)
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn

        assert proc.stop_causes == ["agent_dispatch_cancelled"]
        runtime.spawn.assert_awaited_once()
