"""Dispatch boundary between the text-only LLMProvider path and AgentRuntime.

A step that only needs a text response (triage, research, review) goes
through ``sova.llm.client.invoke_command()``, which calls the configured
``LLMProvider``. A step that needs to actually edit files
(``BaseStep.requires_tools = True``, e.g. ``DevelopStep``) must instead go
through the configured ``AgentRuntime`` (``sova.ipc.runtime.get_runtime()``):
some ``LLMProvider`` implementations (LiteLLM, the Anthropic API) are pure
text-completion backends with no mechanism to edit a working tree at all, so
routing a tool-using step to one of them does not fail, it just silently
produces a chat response with zero file changes, discovered only later by
``validate_output()``'s diff check (wasting the request in the meantime).

``dispatch_command()`` (for a slash command) and ``dispatch_prompt()`` (for a
step-assembled prompt) are the chokepoints every tool-using step must call
instead of ``invoke_command()``/``invoke()`` directly. Neither ever falls back
from AgentRuntime to LLMProvider: when the configured runtime's CLI is not
installed or not runnable, they raise immediately rather than silently
degrading to a text-only call that cannot do the work (Design Decision 3 in
issue #1125, the no-implicit-fallback rule already established for model
selection in docs/model-selection-architecture.md). This check does not probe
authentication: see ``_require_runtime()``.

Both tool paths run the same pre-flight the ``invoke*`` entry points run
(``sova.llm.client.prepare_invocation()``), so bypassing the LLMProvider does
not also bypass the runaway call guard, ``llm.routing`` task-type routing,
alias resolution, or the configured timeout default. They likewise reuse the
``invoke*`` path's own model fallback walk
(``sova.llm.client.walk_model_chain()``) and its budget-cap pre-check, so
``agent.fallback_models`` still applies and an already-exhausted budget still
raises ``BillingError`` instead of spawning an uncapped agent.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import weakref
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sova.commands.catalog import parse_frontmatter
from sova.core.check_cache import PIPELINE_STAGE_ENV_KEY
from sova.ipc.runtime import AgentRuntime, get_runtime, resolve_runtime_model_pin
from sova.llm import client
from sova.llm.cli_output_parsing import extract_claude_cli_failure_detail
from sova.llm.egress import scan_and_redact
from sova.llm.errors import BillingError, LLMInvocationError, LLMTimeoutError, classify_error
from sova.llm.models import LLMResult, accumulate_llm_result
from sova.llm.provider import _assert_command_exists
from sova.llm.routing import task_type_model_compatible
from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig
    from sova.core.context import ExecutionContext
    from sova.core.steps.base import BaseStep
    from sova.ipc.control import AgentProcess, FileAgentProcess

log = get_logger(component="core.agent_dispatch")

# The Claude Code CLI's own argument placeholder, as it appears in a rendered
# command body under .claude/commands/. See _assemble_prompt(). Excludes a
# backtick-quoted occurrence, which is a prose reference back to the slot
# (e.g. "where issue-number matches `$ARGUMENTS`"), not the slot itself: a
# command body can refer to its own placeholder more than once, and only the
# bare, unquoted occurrence is the substitution point.
_ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"
_ARGUMENTS_PLACEHOLDER_RE = re.compile(r"(?<!`)" + re.escape(_ARGUMENTS_PLACEHOLDER) + r"(?!`)")

# litellm vendor-prefix dialect. "vertex_ai/" is the one case where the bare
# id under the prefix is still a real Claude model id (Claude Code's own
# Vertex routing is env-based, not prefix-based), so it is stripped rather
# than rejected. Every other recognized prefix here names a model the Claude
# CLI cannot serve under any spelling: llm.provider and agent.runtime are
# independently configurable, so client.prepare_invocation()'s
# provider-scoped resolution can hand this path a litellm-dialect id (e.g.
# llm.provider="vertex" with the default agent.runtime="claude-code", or an
# llm.routing entry naming a local model).
_LITELLM_VERTEX_PREFIX = "vertex_ai/"
_LITELLM_FOREIGN_VENDOR_PREFIXES = ("openai/", "ollama/", "gemini/", "mistral/", "cohere/", "bedrock/", "azure/")

# Task types _ensure_task_type_model_compatible() applies to: only the fix
# loops that have a dev-model escalation path to fall back to (issue #780).
# Every other tool-using task type (develop, self_review, simplify, ...)
# stays on the pre-existing fail-loud _sanitize_model_for_runtime() check
# inside _dispatch_to_runtime(): an operator who misroutes one of those to a
# model the runtime cannot serve gets an immediate, actionable
# AgentRuntimeUnavailableError naming the config keys to fix, rather than a
# silent downgrade to the dev model buried in a log line.
_TASK_TYPE_MODEL_COMPATIBILITY_SCOPE: frozenset[str] = frozenset({"validate", "monitor_ci"})


class AgentRuntimeUnavailableError(RuntimeError):
    """Raised when a tool-using step has no usable AgentRuntime to dispatch to.

    Distinct from a generic RuntimeError so callers that want to tell this
    configuration failure apart from an in-flight agent error (a timeout, a
    bad exit code) can do so without string matching.
    """


def _resolve_command_body(command: str, cwd: Path) -> str:
    """Read the canonical markdown body for *command* out of ``cwd``.

    Strips YAML frontmatter when present, since that is metadata for command
    discovery/rendering, not instructions for the agent. A runtime with no
    slash-command concept of its own (Codex) needs the literal body text, not
    a bare ``"/develop"`` reference it cannot resolve on its own.

    Locating and validating the file is delegated to the same
    ``_assert_command_exists()`` the LLMProvider path uses, so the tool path
    gets its name validation and worktree ``.claude/`` restore attempt rather
    than a second copy of the ``.claude/commands/`` layout.
    """
    cmd_path = _assert_command_exists(command, cwd)
    content = read_text_or_none(cmd_path)
    if content is None:
        raise RuntimeError(f"Command file {cmd_path} exists but could not be read as UTF-8 text")
    parsed = parse_frontmatter(content)
    return parsed[1].strip() if parsed is not None else content.strip()


def _assemble_prompt(body: str, args: str) -> str:
    """Substitute *args* into *body* the way the Claude Code CLI would.

    A rendered command body carries the CLI's own ``$ARGUMENTS`` placeholder at
    the point the task belongs (``.claude/commands/develop.md`` puts it under
    "**Task to develop**:", and later steps refer back to it), and the CLI
    substitutes it when it expands a ``/command`` reference. The AgentRuntime
    path spawns the body as a literal prompt, so it has to do that
    substitution itself: appending the args instead would leave the agent
    reading a bare ``$ARGUMENTS`` where its task should be, and every
    instruction referring back to that placeholder would dangle.

    A body with no placeholder (not every command takes arguments inline)
    keeps the appended form, which is what the CLI's own prompt assembly
    produces for those.

    Only the bare, unquoted placeholder is substituted
    (``_ARGUMENTS_PLACEHOLDER_RE``): a backtick-quoted occurrence is prose
    referring back to the real slot, not a second slot, and substituting it
    too would duplicate the (potentially multi-KB) args payload into the
    instruction text and garble the sentence around it.
    """
    if _ARGUMENTS_PLACEHOLDER_RE.search(body):
        return _ARGUMENTS_PLACEHOLDER_RE.sub(lambda _m: args, body).strip()
    return f"{body}\n\n{args}".strip() if args else body


def _sanitize_model_for_claude_cli(model: str | None) -> str | None:
    """Make *model* servable by a bare ``claude --model`` invocation, or raise.

    ``client.prepare_invocation()`` resolves a model through ``llm.provider``
    (e.g. a Vertex-routed ``agent.fallback_models`` entry, or an
    ``llm.routing`` route naming a litellm-prefixed local model), which is
    independently configurable from ``agent.runtime``. Handing a
    litellm-dialect id straight to the Claude CLI fails inside the spawned
    process with an opaque model error instead of a clear configuration one.
    """
    if model is None or "/" not in model:
        return model
    prefix, _, rest = model.partition("/")
    vendor = f"{prefix}/"
    if vendor == _LITELLM_VERTEX_PREFIX:
        return rest
    if vendor in _LITELLM_FOREIGN_VENDOR_PREFIXES:
        raise AgentRuntimeUnavailableError(
            f"Resolved model {model!r} is routed to {prefix!r} via llm.provider/llm.routing, but "
            'agent.runtime="claude-code" cannot serve it. Configure a Claude-compatible model for '
            "this step (llm.model_aliases / agent.fallback_models / llm.routing), or set "
            "agent.runtime to a runtime that can reach that vendor."
        )
    return model


def _sanitize_model_for_runtime(runtime: AgentRuntime, model: str | None) -> str | None:
    """Scope ``_sanitize_model_for_claude_cli()`` to the runtime it actually applies to.

    Only ``ClaudeCodeRuntime`` spawns a bare ``claude --model`` CLI that
    cannot parse a litellm vendor prefix; a different runtime either ignores
    this argument entirely (see ``AgentRuntime.honours_model_override``) or
    accepts a different model dialect.
    """
    if runtime.name != "claude-code":
        return model
    return _sanitize_model_for_claude_cli(model)


async def _worktree_fingerprint(cwd: Path) -> str | None:
    """Return a cheap fingerprint of *cwd*'s git state, or ``None`` if unavailable.

    Used by ``_dispatch_to_runtime()`` to detect whether a side-effecting
    agent process edited the worktree before failing. Unlike the LLMProvider
    path, where a failed attempt has no effect on the working tree, a failed
    attempt here is a real coding agent that may have already edited,
    staged, or committed files, so retrying the identical prompt on the next
    fallback model would run against a half-modified tree with no snapshot
    or reset.
    """
    from sova.utils.shell import run

    head = await run("git", "rev-parse", "HEAD", cwd=cwd)
    status = await run("git", "status", "--porcelain", cwd=cwd)
    if not head.success or not status.success:
        return None
    return f"{head.stdout.strip()}\n{status.stdout}"


# Seconds an AgentRuntime.check_available() answer is trusted before being
# re-probed. check_available() spawns a subprocess (e.g. `claude --version`,
# up to a several-second timeout); a multi-attempt fix loop (DevelopStep,
# ValidateStep, MonitorCIStep) calls _require_runtime() once per attempt, so
# without this a single step could pay that probe cost many times over for an
# answer that does not change within one step's lifetime.
_RUNTIME_AVAILABILITY_TTL_SECONDS = 30.0

# Keyed by the runtime instance itself, not id(runtime): reload_runtime() swaps
# the module-level singleton to a new instance on a config change, and a plain
# id()-keyed dict risks a stale hit after the old instance is garbage
# collected and CPython reuses its id for an unrelated object (observed in
# practice with short-lived fake runtimes across tests). A WeakKeyDictionary
# ties each entry's lifetime to the actual runtime object, so that collision
# cannot happen, and entries for a discarded runtime are reclaimed for free.
_runtime_availability_cache: weakref.WeakKeyDictionary[AgentRuntime, tuple[float, bool, str]] = (
    weakref.WeakKeyDictionary()
)


async def _require_runtime(step: BaseStep) -> AgentRuntime:
    """Return the configured runtime, or raise if it cannot actually run.

    The no-implicit-fallback rule: a tool-using step whose runtime is not
    installed or not runnable fails here, rather than degrading to an
    LLMProvider that has no way to edit files.

    This does not probe authentication. ``check_available()`` is implemented
    per runtime: today only ``CodexRuntime`` checks login state (``codex
    login status``); ``ClaudeCodeRuntime`` and ``AiderRuntime`` only run
    ``<cli> --version``, so an installed-but-unauthenticated Claude Code CLI
    passes this gate and fails later, inside the spawned process, which is
    outside this function's reach.
    """
    runtime = get_runtime()
    cached = _runtime_availability_cache.get(runtime)
    if cached is not None and cached[0] > time.monotonic():
        _, available, detail = cached
    else:
        available, detail = await runtime.check_available()
        _runtime_availability_cache[runtime] = (
            time.monotonic() + _RUNTIME_AVAILABILITY_TTL_SECONDS,
            available,
            detail,
        )
    if not available:
        raise AgentRuntimeUnavailableError(
            f"Step {step.name!r} requires tool execution but agent runtime {runtime.name!r} is "
            f"not installed or not runnable: {detail}. Configure 'agent.runtime' to a working "
            "coding-agent CLI; a text-only LLM provider cannot execute this step's file edits."
        )
    return runtime


def _guard_prompt_or_raise(prompt: str) -> None:
    """Run ``guard_prompt()`` and re-raise a rejection as a classified ``LLMError``.

    ``invoke()``/``invoke_command()`` let ``PromptInjectionError`` (a bare
    ``Exception``, not a ``RuntimeError``) propagate uncaught, which is fine
    there because nothing downstream of them catches ``RuntimeError``
    specifically. Every migrated step's ``except RuntimeError`` handler
    (``AddressReviewStep.execute()`` and peers) does catch specifically that,
    so on the tool path a guard rejection must arrive as an ``LLMError``
    (itself a ``RuntimeError``) or it escapes the step's own error handling
    entirely instead of becoming an ordinary failed ``StepResult``.
    """
    from sova.llm.guard import PromptInjectionError, guard_prompt

    try:
        guard_prompt(prompt)
    except PromptInjectionError as exc:
        raise LLMInvocationError(str(exc)) from exc


def _raise_if_budget_cap_unsupported(runtime: AgentRuntime, max_budget_usd: Decimal | None) -> None:
    """Raise before spawning a runtime that cannot enforce ``max_budget_usd`` itself.

    The runtime-path twin of ``sova.llm.client._raise_if_budget_cap_unsupported``,
    and it has to be its own check rather than a reuse: capability is the
    *runtime's* (``AgentRuntime.enforces_budget_cap``), and the configured
    LLMProvider's answer is unrelated here, so a cap-supporting provider must
    not vouch for a runtime that logs "budget not enforced" and spawns anyway
    (``CodexRuntime``, ``AiderRuntime``).

    Every step passes ``max_budget - ctx.cost_usd``, so a non-positive value
    means the run is already over budget and there is nothing left to spend.

    This only covers the already-exhausted case. ``CodexRuntime.spawn()``
    layers its own, stricter check for a non-exhausted budget on top: it
    refuses *any* ``max_budget_usd`` at all (not just <= 0) unless
    ``codex.allow_unenforced_budget`` is set. That check lives inside
    ``CodexRuntime`` rather than here because today it is Codex-specific
    (``AiderRuntime``, the other non-enforcing runtime, still proceeds on a
    positive budget with only a logged warning, matching this function's own
    behavior); keep the two in sync if a second runtime needs the same
    opt-in gate, rather than letting a third one-off check appear.
    """
    if max_budget_usd is None or max_budget_usd > 0:
        return
    if runtime.enforces_budget_cap:
        return
    raise BillingError(
        f"Remaining budget ${max_budget_usd} is exhausted and agent runtime "
        f"{runtime.name!r} cannot enforce a budget cap natively"
    )


async def _dispatch_to_runtime(
    runtime: AgentRuntime,
    prompt: str,
    *,
    cfg: ProjectConfig | None,
    cwd: Path,
    model: str | None,
    fallback_model: str | None,
    max_budget_usd: Decimal | None,
    timeout: float,
    ctx: ExecutionContext | None = None,
) -> LLMResult:
    """Spawn *prompt* on *runtime*, walking the configured model fallback chain.

    The shared tail of ``dispatch_command()`` and ``dispatch_prompt()``. Both
    reuse ``sova.llm.client.walk_model_chain()`` rather than spawning the
    resolved model once, so ``agent.fallback_models`` keeps working for a
    tool-using step: without it, a rate-limited or unavailable primary model
    failed the whole step while the provider path would have hopped to the next
    candidate. Each hop is handed the following candidate as its
    runtime-level ``fallback_model``, exactly as the provider path hands it to
    the CLI, so a runtime with its own inner fallback agrees with this one.

    Two divergences from the LLMProvider path's walk, both because the
    executor here is a side-effecting coding agent rather than a stateless
    completion call:

    - ``runtime.honours_model_override`` gates whether the walk runs at all.
      Codex ignores ``spawn(model=...)`` entirely, so walking
      ``agent.fallback_models`` for it would re-run a byte-identical agent
      invocation once per candidate for a model that never changes.
    - ``_is_eligible()`` blocks a fallback hop once a failed attempt is found
      to have already changed the worktree (see ``_worktree_fingerprint()``):
      unlike the provider path, a failed attempt here may have already
      edited, staged, or committed files, and retrying the identical prompt
      on the next model would run against that half-modified tree with no
      snapshot or reset.
    """
    _raise_if_budget_cap_unsupported(runtime, max_budget_usd)
    tree_changed = False

    async def _attempt(
        candidate: str | None,
        next_hop: str | None,
        attempt_timeout: float,
        attempt_budget: Decimal | None,
    ) -> LLMResult:
        nonlocal tree_changed
        baseline = await _worktree_fingerprint(cwd)
        try:
            return await _run_agent_process(
                runtime,
                prompt,
                cwd=cwd,
                model=_sanitize_model_for_runtime(runtime, candidate),
                fallback_model=_sanitize_model_for_runtime(runtime, next_hop),
                max_budget_usd=attempt_budget,
                timeout=attempt_timeout,
                ctx=ctx,
            )
        except Exception:
            if baseline is not None:
                after = await _worktree_fingerprint(cwd)
                tree_changed = after is not None and after != baseline
            raise

    if not runtime.honours_model_override:
        result = await _attempt(model, fallback_model, timeout, max_budget_usd)
        # ``ctx.resolved_model`` was set from ``agent.model`` (a Claude model
        # id) back in AssessStep, long before this runtime was chosen; for a
        # runtime that ignores the model override entirely, that id is simply
        # wrong, not just stale, as a description of what THIS runtime ran.
        # But resolved_model is shared across the whole pipeline run and read
        # by every later text-only LLMProvider step (create_pr, spec,
        # research) to pick the model THEY ask for, which has no
        # relationship to what this step's runtime happened to run.
        # Overwriting it here would leak e.g. "gpt-5-codex" into a later
        # Claude-CLI invocation. The runtime's own report instead goes on the
        # step-scoped ``runtime_model``/``runtime_model_reason`` fields,
        # which WorkflowEngine resets before every step attempt and prefers
        # over resolved_model only when writing that step's CostRecord.
        # Issue #1128.
        #
        # Always set (never left None), even when the stream named no model:
        # on today's default Codex config, ``result.model`` is "" (no model
        # field in the JSONL and ``codex.model`` unset), and leaving
        # runtime_model at None here would fall the CostRecord writer through
        # to resolved_model/"claude", mislabeling Codex spend as Claude spend.
        # Falling back to the runtime's own name is honest instead of borrowed.
        #
        # The tag routes this result's usage and cost provenance to the step's
        # runtime CostRecord when the step passes it to ctx.add_usage(), which
        # also combines runtime_cost_source across every dispatch in the step
        # rather than letting the last one overwrite it.
        result.runtime_name = runtime.name
        if ctx is not None:
            if result.model:
                ctx.runtime_model = result.model
                ctx.runtime_model_reason = f"runtime:{runtime.name} reported model"
            else:
                ctx.runtime_model = runtime.name
                ctx.runtime_model_reason = f"runtime:{runtime.name} reported no model"
        return result

    def _is_eligible(exc: Exception) -> bool:
        if tree_changed:
            log.warning(
                "agent_dispatch.fallback_blocked_worktree_changed",
                runtime=runtime.name,
                error_type=type(exc).__name__,
            )
        return not tree_changed

    return await client.walk_model_chain(
        _attempt,
        primary=model,
        cfg=cfg,
        caller_fallback=fallback_model,
        timeout=timeout,
        max_budget_usd=max_budget_usd,
        # Scoped by runtime name, not client.provider_identity(cfg): the
        # executor here is runtime.spawn(), independently configurable from
        # llm.provider (agent.runtime="codex" with llm.provider="claude-code"
        # is a valid combination), so a model marked unavailable on one must
        # never suppress it on the other.
        identity=f"runtime:{runtime.name}:{client.provider_identity(cfg)}",
        is_eligible=_is_eligible,
        # The identity function, not client's default (get_provider()'s own
        # normalize_model_name): the executor here is runtime.spawn(), not
        # the configured LLMProvider, so de-duplicating/availability-filtering
        # the chain must never construct a provider the runtime path has no
        # other use for (and which can fail to construct on its own, e.g. a
        # litellm provider with a missing optional dependency, failing a
        # tool-using step whose runtime is perfectly healthy).
        normalize=lambda m: m,
    )


def _ensure_task_type_model_compatible(
    runtime: AgentRuntime,
    cfg: ProjectConfig | None,
    task_type: str | None,
    dev_model: str | None,
    routed_model: str | None,
) -> tuple[str | None, bool]:
    """Fall back *routed_model* to *dev_model* when *runtime* cannot honour a configured task-type route.

    Catches what ``_sanitize_model_for_runtime()`` would otherwise raise or
    silently no-op on: a route naming a model family the runtime's own
    (already-cached) catalog does not carry, or a runtime whose
    ``honours_model_override`` is False, for which a configured route would
    never actually change what runs anyway. Scoped to the models an operator
    actually routed: the default empty ``llm.routing`` (and a *cfg* that
    failed to load) leaves every dispatch byte-identical to today.

    Compatibility is judged on the form the spawn will actually receive, not
    the raw routed id: *routed_model* is run through
    ``_sanitize_model_for_runtime()`` first (the same sanitizer
    ``_dispatch_to_runtime()`` applies right before spawning), so a
    ``vertex_ai/``-prefixed route is compared to the runtime's catalog with
    that prefix already stripped, matching what the Claude CLI is actually
    handed. A route the sanitizer itself rejects (a foreign vendor prefix
    ``agent.runtime="claude-code"`` cannot serve at all) is incompatible by
    construction, with no catalog lookup needed.

    Never probes the runtime's catalog: ``AgentRuntime.cached_models()``, not
    ``list_available_models()``, so this hot path pays no probe. A pipeline
    process never warms that cache, so the answer there comes from the
    runtime's static ``declared_models()``; a runtime that declares nothing
    reports no catalog at all and *routed_model* fails open as compatible.

    The fallback itself is resolved through ``client.select_model()``, the
    same ``llm.model_aliases``/backend-aware chokepoint every other model on
    this path goes through: *dev_model* is the raw ``model`` argument passed
    into ``dispatch_command()``/``dispatch_prompt()``, which can be a bare,
    unresolved alias (e.g. ``agent.model = "opus"``), and handing that
    straight to a Vertex/Bedrock-routed backend is the #1033 failure mode
    ``client.prepare_invocation()`` exists to prevent.

    Scoped to ``_TASK_TYPE_MODEL_COMPATIBILITY_SCOPE``: a task type outside
    it (every tool-using step other than the validate/monitor_ci fix loops)
    falls through unchanged here and keeps hitting the fail-loud
    ``_sanitize_model_for_runtime()`` check downstream in
    ``_dispatch_to_runtime()`` instead of silently downgrading to the dev
    model.

    Returns ``(model_to_dispatch, route_honoured)``. *route_honoured* is the
    single source of truth ``dispatch_command()``/``dispatch_prompt()`` tag
    onto the eventual ``LLMResult.routed_task_type_model`` for
    ``ExecutionContext.add_usage()`` to split spend on (issue #780 review):
    comparing the *reported* model id against ``resolved_model`` after the
    fact cannot tell a route this function already swapped back to
    *dev_model* from one that merely resolves to *dev_model* verbatim under
    a route/agent-model family pin (``sova.llm.routing._pin_to_configured_model``),
    so it is False in both of those cases, not just the swapped-back one.
    """
    if (
        cfg is None
        or not task_type
        or task_type not in _TASK_TYPE_MODEL_COMPATIBILITY_SCOPE
        or cfg.llm.routing.get(task_type) is None
    ):
        return routed_model, False
    if runtime.honours_model_override:
        try:
            spawn_candidate = _sanitize_model_for_runtime(runtime, routed_model)
        except AgentRuntimeUnavailableError:
            spawn_candidate = None
        if spawn_candidate is not None and task_type_model_compatible(spawn_candidate, runtime.cached_models()):
            return routed_model, routed_model != dev_model
    log.warning(
        "agent_dispatch.task_type_model_incompatible",
        runtime=runtime.name,
        task_type=task_type,
        routed_model=routed_model,
        dev_model=dev_model,
    )
    return client.select_model(dev_model, cfg), False


def _apply_runtime_model_pin(
    runtime: AgentRuntime,
    cfg: ProjectConfig | None,
    ctx: ExecutionContext | None,
    routed_model: str | None,
) -> tuple[str | None, tuple[str, str] | None]:
    """Return the model to hand *runtime*, and the ``(model, reason)`` pin when one applied.

    The role comes from *ctx*; without one (a direct test call) there is no
    role to pin for. *cfg* is ``prepare_invocation()``'s fresh load, so a pin
    saved mid-run applies from the next step, the same as an ``llm.routing``
    change; ``ctx.config`` (the run-start snapshot) covers a failed reload.
    """
    if ctx is None:
        return routed_model, None
    pin = resolve_runtime_model_pin(runtime, cfg if cfg is not None else ctx.config, ctx.role)
    if pin is None:
        return routed_model, None
    log.info("agent_dispatch.runtime_model_pin", runtime=runtime.name, model=pin[0], routed=routed_model, reason=pin[1])
    return pin[0], pin


def _attribute_pinned_result(
    result: LLMResult, runtime: AgentRuntime, ctx: ExecutionContext | None, pin: tuple[str, str] | None
) -> None:
    """Route a pinned dispatch's spend to the runtime CostRecord, under the model that ran.

    The provider CostRecord is labelled ``ctx.resolved_model``, which a pin
    deliberately leaves alone (later text-only steps read it), so without the
    ``runtime_name`` tag the pinned model's tokens would be recorded under the
    routed model's name. Same mechanism as the model-ignoring branch of
    ``_dispatch_to_runtime()`` (issue #1128).
    """
    if pin is None or ctx is None:
        return
    result.runtime_name = runtime.name
    ctx.runtime_model = result.model or pin[0]
    ctx.runtime_model_reason = pin[1]


# Cap on how many non-result stdout lines _run_agent_process() retains for a
# failure-detail fallback. A develop/address-review agent can run for tens of
# minutes and emit far more content than any failure message needs; bounding
# this keeps the buffer itself from becoming a memory concern on a runaway
# turn that never reaches a terminal result event.
_STDOUT_FAILURE_BUFFER_MAX_LINES = 200


async def _run_agent_process(
    runtime: AgentRuntime,
    prompt: str,
    *,
    cwd: Path,
    model: str | None,
    fallback_model: str | None,
    max_budget_usd: Decimal | None,
    timeout: float | None,
    ctx: ExecutionContext | None = None,
) -> LLMResult:
    """Spawn *prompt* on *runtime* and wait for its terminal result.

    Mirrors ``LLMProvider.invoke()``'s contract: raises a classified
    ``LLMError`` subclass on failure (so existing ``except RuntimeError``
    fix-loop handling in step code needs no AgentRuntime-specific branch),
    and raises ``LLMTimeoutError`` distinctly on timeout (matching the
    "fix_llm_timeout" vs "fix_llm_failed" marker convention in
    ``sova.llm.errors.format_fix_llm_failure``).

    Every exit from the wait, not just a timeout or a cancellation, stops the
    spawned process before propagating: a parser raising on a malformed line,
    or any other error out of the drain tasks, would otherwise leave the agent
    running and still editing the worktree after this call has already failed
    the step, with nothing left holding a handle to kill it.

    A terminal result event that reports a failed turn (``LLMResult.is_error``,
    i.e. ``stop_reason == "error"``) is raised as a classified error rather
    than returned: the agent told us it failed, and returning it as a success
    would hand the step an empty-but-"successful" turn to act on.

    *ctx*, when given, feeds every "content" stream event to
    ``ctx.output_writer`` as it arrives: without it, a long tool-using step
    produces no incremental output anywhere (content events were parsed and
    discarded), leaving the dashboard run log silent until the whole step
    finishes.
    """
    # Marks the spawned agent as an autonomous pipeline dispatch, distinct
    # from a human's own interactive session or the dashboard's one-off
    # start_command() spawn (neither goes through this module). Lets a
    # command's full-check instruction (develop.md, review.md, pr.md) reuse
    # an identical check an earlier call site in this cycle already ran; see
    # sova.core.check_cache.should_skip().
    proc = await runtime.spawn(
        prompt,
        cwd,
        env={**os.environ, PIPELINE_STAGE_ENV_KEY: "1"},
        model=model,
        fallback_model=fallback_model,
        max_budget_usd=max_budget_usd,
    )
    parser = runtime.create_stream_parser()
    parse_line = parser.parse_line if parser is not None else runtime.parse_output
    result: LLMResult | None = None
    stdout_lines: list[str] = []

    async def _drain_stdout() -> None:
        nonlocal result
        async for line in proc.stdout_lines():
            event = parse_line(line)
            if event is None:
                continue
            if event.type == "result" and event.result is not None:
                # accumulate, not overwrite: a thread that runs several turns
                # (today only Codex) reports each turn's own token usage, not
                # a thread-cumulative total, so a later turn.completed would
                # otherwise silently drop every earlier turn's spend.
                result = accumulate_llm_result(result, event.result)
                continue
            if event.text and len(stdout_lines) < _STDOUT_FAILURE_BUFFER_MAX_LINES:
                stdout_lines.append(event.text)
            if event.type == "content" and event.text and ctx is not None and ctx.output_writer is not None:
                ctx.output_writer.write_line(event.text)
                if ctx.output_writer.should_flush():
                    await ctx.output_writer.flush()

    stderr_lines: list[str] = []

    async def _drain_stderr() -> None:
        async for line in proc.stderr_lines():
            stderr_lines.append(line)

    try:
        async with asyncio.timeout(timeout):
            # TaskGroup, not asyncio.gather(): when one drain task raises (the
            # parser-error case this function handles below), a TaskGroup
            # cancels its sibling and waits for it before propagating, so the
            # other task is never left pending past this block. gather() alone
            # leaves a failed sibling running until something else happens to
            # await it (here, only incidentally via _stop_quietly() killing the
            # process), which otherwise surfaces as an unawaited pending task.
            async with asyncio.TaskGroup() as tg:
                tg.create_task(_drain_stdout())
                tg.create_task(_drain_stderr())
            await proc.wait()
    except TimeoutError as exc:
        await _stop_quietly(proc, runtime, cause="agent_dispatch_timeout")
        raise LLMTimeoutError(f"Agent runtime {runtime.name!r} timed out after {timeout}s") from exc
    except asyncio.CancelledError:
        await _stop_quietly(proc, runtime, cause="agent_dispatch_cancelled")
        raise
    except ExceptionGroup as eg:
        # A real failure inside _drain_stdout()/_drain_stderr() (a parser
        # error, not a CancelledError) surfaces from the TaskGroup wrapped in
        # an ExceptionGroup, even though only one of the two tasks ever raises
        # in practice. Unwrapped back to that original exception so
        # downstream classification (classify_error/resolve_error_category,
        # which inspect the exception's own message) sees the real error
        # rather than ExceptionGroup's own generic "N sub-exception(s)" text.
        await _stop_quietly(proc, runtime, cause="agent_dispatch_error")
        raise eg.exceptions[0] from eg
    except BaseException:
        # Any other way out of the wait (e.g. proc.wait() itself failing) is
        # still an abandoned subprocess if it is not killed here.
        await _stop_quietly(proc, runtime, cause="agent_dispatch_error")
        raise

    if result is not None and not result.is_error:
        return result

    if result is not None:
        detail = result.text.strip() or "agent reported a terminal failure with no detail"
    else:
        # Stdout-first, matching the documented "Failure capture must prefer
        # stdout's structured error over stderr" decision
        # (sova/llm/providers/claude_code.py:_extract_failure_detail()): the
        # CLI writes its real error to stdout, while stderr often carries
        # only an incidental warning. "\n".join(stdout_lines) covers content
        # events parse_line() already decoded as text; extract_claude_cli_failure_detail()
        # additionally tries to parse that joined text as a structured JSON
        # payload the way the provider's own stdout fallback does.
        stdout_text = "\n".join(stdout_lines).strip()
        stderr_text = "\n".join(stderr_lines).strip()
        detail = extract_claude_cli_failure_detail(stdout=stdout_text, stderr=stderr_text)
        if detail in ("(no error detail captured)", "(no error detail captured beyond warnings)"):
            detail = "agent process exited without a terminal result event"
    detail = scan_and_redact(detail).redacted_text[:500]
    message = f"Agent runtime {runtime.name!r} failed (exit {proc.returncode}): {detail}"
    raise classify_error(detail)(message)


# Bound for the shielded cleanup kill in _stop_quietly(). Generous relative to
# a SIGTERM/SIGKILL round-trip, but still much shorter than any step timeout,
# so a wedged stop() cannot block cleanup indefinitely.
_STOP_QUIETLY_TIMEOUT_SECONDS = 15.0


async def _stop_quietly(proc: AgentProcess | FileAgentProcess, runtime: AgentRuntime, *, cause: str) -> None:
    """Stop *proc*, never letting the stop itself replace the real failure.

    ``_run_agent_process()`` calls this from exception handlers, where a raise
    out of ``proc.stop()`` would discard the timeout, cancellation, or parser
    error that is the actual cause and report a kill failure instead.

    Shielded and bounded: called from an ``except asyncio.CancelledError``
    branch, ``proc.stop()`` is itself a cancellable await inside an
    already-cancelling task. A second cancellation (the ``sova run`` SIGTERM
    handler's grace timer, or an outer cancel scope that does not wait for
    cleanup) would otherwise interrupt the kill before SIGTERM/SIGKILL is
    sent, leaving the spawned agent (started with ``start_new_session=True``,
    so it does not die with the parent) running and still editing the
    worktree, the exact outcome this handler exists to prevent. A failure or
    timeout inside the shielded stop is logged and suppressed; a
    ``CancelledError`` from this function's own await (once the shield/timeout
    itself is cancelled) is deliberately not swallowed, so an outer
    cancellation is never silently absorbed.
    """
    try:
        await asyncio.shield(
            asyncio.wait_for(
                proc.stop(cause=cause, requester="agent_dispatch"),
                timeout=_STOP_QUIETLY_TIMEOUT_SECONDS,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 (cleanup must never mask the failure that triggered it)
        log.warning("agent_dispatch.stop_failed", runtime=runtime.name, cause=cause, exc_info=True)


async def dispatch_command(
    step: BaseStep,
    command: str,
    args: str = "",
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = None,
    ctx: ExecutionContext | None = None,
) -> LLMResult:
    """Run a slash command, routing through AgentRuntime when *step* needs tools.

    ``step.requires_tools`` is the capability flag (``BaseStep``'s own
    semantics: does this step edit files), so a text-only step keeps calling
    ``sova.llm.client.invoke_command()`` through the configured
    ``LLMProvider`` unchanged. A tool-using step always goes through
    ``sova.ipc.runtime.get_runtime()`` instead, regardless of which
    ``LLMProvider`` the project configured, and fails immediately (no silent
    fallback) when that runtime is not actually usable.

    *ctx*, when passed, feeds the spawned agent's incremental stdout into
    ``ctx.output_writer`` (see ``_run_agent_process()``), so a long tool-using
    step is observable in the dashboard run log before it finishes rather than
    silent until it does. Optional because not every caller holds a full
    ``ExecutionContext`` (e.g. a direct test call); omitting it only disables
    the live feed, not dispatch itself.
    """
    if not step.requires_tools:
        return await client.invoke_command(
            command,
            args,
            model=model,
            fallback_model=fallback_model,
            task_type=task_type,
            cwd=cwd,
            max_budget_usd=max_budget_usd,
            timeout=timeout,
        )

    runtime = await _require_runtime(step)
    resolved_cwd = Path(cwd) if cwd is not None else Path.cwd()
    body = _resolve_command_body(command, resolved_cwd)

    cfg, resolved_model, resolved_timeout = await client.prepare_invocation(
        model=model, task_type=task_type, timeout=timeout, cwd=cwd
    )
    original_args = args
    if args:
        # Guarded and compressed exactly as invoke_command() does it, so moving
        # a step to the tool path does not drop the injection check or restore
        # the uncompressed payload (develop's args carry a whole spec section).
        _guard_prompt_or_raise(f"{command} {args}".strip())
        args = client.maybe_compress(args, cwd, cfg=cfg)

    prompt = _assemble_prompt(body, args)
    # Pin resolved first: a role-level roles.developer_runtime_model pin
    # outranks a task-type route for a tool-using step (design decision 6,
    # issue #780), so when one applies the compatibility check never runs
    # and never logs a warning about a route that had no bearing on what
    # actually gets spawned (issue #780 review).
    resolved_model, pin = _apply_runtime_model_pin(runtime, cfg, ctx, resolved_model)
    route_honoured = False
    if pin is None:
        resolved_model, route_honoured = _ensure_task_type_model_compatible(
            runtime, cfg, task_type, model, resolved_model
        )
    log.info("agent_dispatch.invoke", step=step.name, command=command, runtime=runtime.name)
    result = await _dispatch_to_runtime(
        runtime,
        prompt,
        cfg=cfg,
        cwd=resolved_cwd,
        model=resolved_model,
        fallback_model=fallback_model,
        max_budget_usd=max_budget_usd,
        timeout=resolved_timeout,
        ctx=ctx,
    )
    _attribute_pinned_result(result, runtime, ctx, pin)
    if route_honoured:
        result.routed_task_type_model = resolved_model
    # original_args vs args (not the assembled prompt): compression ran only
    # on args, and record_compression_savings()'s identity check needs the
    # exact pre/post pair maybe_compress() saw, not the whole templated body.
    client.record_compression_savings(result, original_args, args)
    return result


async def dispatch_prompt(
    step: BaseStep,
    prompt: str,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = None,
    ctx: ExecutionContext | None = None,
) -> LLMResult:
    """Run a raw *prompt*, routing through AgentRuntime when *step* needs tools.

    The prompt-level sibling of ``dispatch_command()``, for a tool-using step
    that assembles its own prompt rather than naming a slash command: today
    ``DevelopStep``'s inner fix loop, which asks the agent to edit source
    files until the project's check command passes. That loop is as
    tool-dependent as the ``/develop`` invocation it follows, so leaving it on
    ``sova.llm.client.invoke()`` would reintroduce the exact silent
    degradation ``requires_tools`` exists to prevent, one call later.

    See ``dispatch_command()`` for *ctx*.
    """
    if not step.requires_tools:
        return await client.invoke(
            prompt,
            model=model,
            fallback_model=fallback_model,
            task_type=task_type,
            cwd=cwd,
            max_budget_usd=max_budget_usd,
            timeout=timeout,
        )

    runtime = await _require_runtime(step)
    cfg, resolved_model, resolved_timeout = await client.prepare_invocation(
        model=model, task_type=task_type, timeout=timeout, cwd=cwd
    )

    _guard_prompt_or_raise(prompt)
    original_prompt = prompt
    prompt = client.maybe_compress(prompt, cwd, cfg=cfg)

    # Pin resolved first: see the matching comment in dispatch_command().
    resolved_model, pin = _apply_runtime_model_pin(runtime, cfg, ctx, resolved_model)
    route_honoured = False
    if pin is None:
        resolved_model, route_honoured = _ensure_task_type_model_compatible(
            runtime, cfg, task_type, model, resolved_model
        )
    log.info("agent_dispatch.invoke_prompt", step=step.name, runtime=runtime.name, prompt_len=len(prompt))
    result = await _dispatch_to_runtime(
        runtime,
        prompt,
        cfg=cfg,
        cwd=Path(cwd) if cwd is not None else Path.cwd(),
        model=resolved_model,
        fallback_model=fallback_model,
        max_budget_usd=max_budget_usd,
        timeout=resolved_timeout,
        ctx=ctx,
    )
    _attribute_pinned_result(result, runtime, ctx, pin)
    if route_honoured:
        result.routed_task_type_model = resolved_model
    client.record_compression_savings(result, original_prompt, prompt)
    return result
