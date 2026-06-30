"""Model routing rules -- complexity-based and task-type-based.

Maps ComplexityTier values and task-type strings to model aliases.
Supports per-tier and per-task-type config overrides via LLMConfig.routing.

Task-type routing enables local model offloading: lightweight tasks (triage,
extraction, pr_body) can be routed to ``ollama/`` models while heavy tasks
(develop, review) stay on cloud models.
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

# Known task-type keys (disjoint from complexity-tier keys).
# Used to disambiguate when both namespaces share the routing dict.
# Not referenced in routing logic yet -- serves as a registry for consumers
# (settings UI, config validation) to distinguish task-type keys from
# complexity-tier keys in the shared ``llm.routing`` dict.
TASK_TYPE_KEYS: frozenset[str] = frozenset(
    {
        "triage",
        "extraction",
        "pr_body",
        "develop",
        "review",
        "self_review",
        "address_review",
        "harden",
        "validate",
        "monitor_ci",
        "generate_tasks",
        "planner",
    }
)


def route_model(
    complexity: ComplexityTier,
    *,
    task_type: str | None = None,
    llm_config: LLMConfig | None = None,
) -> tuple[str, str]:
    """Select model alias based on task type or complexity.

    Priority: task-type routing > complexity config override > default routing.
    Returns (model_alias, reason) tuple.
    """
    # Task-type routing (most specific)
    if task_type and llm_config is not None:
        override = llm_config.routing.get(task_type)
        if override is not None:
            return override, f"task_type:{task_type}->{override}"

    # Complexity config override
    if llm_config is not None:
        override = llm_config.routing.get(complexity.value)
        if override is not None:
            return override, f"config:override->{override}"

    model = _DEFAULT_ROUTING.get(complexity, "sonnet")
    return model, f"complexity:{complexity.value}->{model}"
