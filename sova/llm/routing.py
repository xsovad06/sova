"""Model routing rules -- complexity-based and task-type-based.

Maps ComplexityTier values and task-type strings to model aliases.
Supports per-tier and per-task-type config overrides via LLMConfig.routing.

Task-type routing enables local model offloading: lightweight tasks (triage,
extraction, pr_body) can be routed to ``ollama/`` models while heavy tasks
(develop, review) stay on cloud models.

When the routing table would return a generic alias (e.g. ``"opus"``), and the
caller provides ``agent_model`` with a pinned version in the same family
(e.g. ``"claude-opus-4-6"``), the pinned version is returned instead. This
prevents the Claude CLI from resolving the alias to a newer version that may
not be available on the user's deployment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.llm.complexity import ComplexityTier

if TYPE_CHECKING:
    from sova.config.models import LLMConfig

_DEFAULT_ROUTING: dict[ComplexityTier, str] = {
    ComplexityTier.TRIVIAL: "haiku",
    ComplexityTier.SIMPLE: "sonnet",
    ComplexityTier.MODERATE: "sonnet",
    ComplexityTier.COMPLEX: "opus",
    ComplexityTier.EPIC: "opus",
}

_MODEL_FAMILIES: tuple[str, ...] = ("opus", "sonnet", "haiku")

# Known task-type keys (disjoint from complexity-tier keys).
# Used to disambiguate when both namespaces share the routing dict.
# Advisory, not a runtime gate: routing looks the caller's key up in
# ``llm.routing`` directly and never checks membership here. The set is a
# registry for consumers (settings UI, config validation) that need to tell
# task-type keys apart from complexity-tier keys in the shared dict.
TASK_TYPE_KEYS: frozenset[str] = frozenset(
    {
        "triage",
        "extraction",
        "pr_body",
        "develop",
        "develop_fix",
        "simplify",
        "review",
        "review_panel",
        "self_review",
        "validate",
        "monitor_ci",
        "rebase",
        "address_review",
        "rearrange_commits",
        "research",
        "spec",
        "harden",
        "generate_tasks",
        "planner",
    }
)


def get_model_family(model_id: str) -> str | None:
    """Extract the family name from a model ID or alias.

    Returns the family (``"opus"``, ``"sonnet"``, ``"haiku"``) if recognized,
    or ``None`` for unrecognized models (e.g. local/third-party).
    """
    lower = model_id.lower()
    for family in _MODEL_FAMILIES:
        if family in lower:
            return family
    return None


def _is_pinned_version(model_id: str) -> bool:
    """Return True if model_id is a specific version, not a bare alias."""
    return model_id not in _MODEL_FAMILIES


def _pin_to_configured_model(alias: str, agent_model: str | None) -> str:
    """If agent_model is a pinned version in the same family as alias, use it."""
    if not agent_model or not _is_pinned_version(agent_model):
        return alias
    alias_family = get_model_family(alias)
    agent_family = get_model_family(agent_model)
    if alias_family and alias_family == agent_family:
        return agent_model
    return alias


def route_model(
    complexity: ComplexityTier,
    *,
    task_type: str | None = None,
    llm_config: LLMConfig | None = None,
    agent_model: str | None = None,
) -> tuple[str, str]:
    """Select model alias based on task type or complexity.

    Priority: task-type routing > complexity config override > default routing.

    When the resolved model is a generic alias (``"opus"``, ``"sonnet"``,
    ``"haiku"``) and ``agent_model`` is a pinned version in the same family,
    the pinned version is returned instead to prevent the CLI from resolving
    to an unavailable newer version.

    Returns (model_alias, reason) tuple.
    """
    # Task-type routing (most specific)
    routed = route_task_type(task_type, llm_config=llm_config, agent_model=agent_model)
    if routed is not None:
        return routed

    # Complexity config override
    if llm_config is not None:
        override = llm_config.routing.get(complexity.value)
        if override is not None:
            return _apply_pin(override, agent_model, f"config:override->{override}")

    model = _DEFAULT_ROUTING.get(complexity, "sonnet")
    return _apply_pin(model, agent_model, f"complexity:{complexity.value}->{model}")


def route_task_type(
    task_type: str | None,
    *,
    llm_config: LLMConfig | None,
    agent_model: str | None = None,
) -> tuple[str, str] | None:
    """Resolve ``llm.routing[task_type]`` to a pinned (model, reason) pair.

    Returns None when the caller passed no task type or no route is configured
    for it, which is the default state of ``llm.routing``. Shared by
    ``route_model()`` and the client's task-type path so both derive the same
    model and the same reason string from one place.
    """
    if not task_type or llm_config is None:
        return None
    override = llm_config.routing.get(task_type)
    if override is None:
        return None
    return _apply_pin(override, agent_model, f"task_type:{task_type}->{override}")


def _apply_pin(model: str, agent_model: str | None, reason: str) -> tuple[str, str]:
    """Pin model alias to agent_model if same family, appending to reason."""
    pinned = _pin_to_configured_model(model, agent_model)
    if pinned != model:
        return pinned, f"{reason},pinned->{pinned}"
    return model, reason


def resolve_extraction_model(cfg: LLMConfig | None) -> str:
    """Resolve the model for extraction-tier sub-tasks (implementation notes, memory consolidation).

    Returns ``llm.routing["extraction"]`` when configured, else ``"haiku"``.
    """
    if cfg is not None:
        override = cfg.routing.get("extraction")
        if override:
            return override
    return "haiku"
