"""Which model each role and step runs on, and why: the Settings page's Models view (#1463).

``build_model_map()`` derives every answer from the resolvers the steps themselves call (``route_model()``,
``route_task_type()``, ``select_model()``, ``resolve_bare_tier_for_provider()`` and
``ProjectConfig.fallback_models_for()``), with the precedence each call site applies, so the page shows what a
run does rather than a copy of the rules. The step table below records, per step, which config the call site
reads; tests/test_role_model_config.py pins each row against a config with a step setting, a role model and the
default.

Short model names (``sonnet``) are listed with the exact ID each runs as today, and ``pin_short_names()`` writes
those IDs (Pin and Pin all).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sova.llm.backends import TIER_NAMES
from sova.llm.client import resolve_bare_tier_for_provider, select_model
from sova.llm.complexity import ComplexityTier
from sova.llm.routing import (
    _DEFAULT_ROUTING,
    _MODEL_FAMILIES,
    TASK_TYPE_KEYS,
    _pin_to_configured_model,
    get_model_family,
    route_model,
    route_task_type,
)
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.model_map")

# The step ids a routing row may name: the task types plus the complexity tiers (``llm.routing.<id>``).
ROUTING_KEYS: frozenset[str] = TASK_TYPE_KEYS | frozenset(tier.value for tier in ComplexityTier)

SHORT_NAMES: frozenset[str] = TIER_NAMES | frozenset(_MODEL_FAMILIES)

# Built-in defaults the call sites fall back to (sova/roles/reviewer.py _DEFAULT_REVIEW_MODEL,
# sova/supervisor/planner.py _DEFAULT_MODEL, sova/llm/routing.py resolve_extraction_model()).
_REVIEW_DEFAULT = "sonnet"
_PLANNER_DEFAULT = "sonnet"
_EXTRACTION_DEFAULT = "haiku"

# Pipelines a developer-role step runs in. Only the developer pipeline has AssessStep, which is the only place
# roles.developer_model and the issue-size routing apply; an address-review run passes agent.model instead.
_DEV = "developer"
_ADDRESS = "address_review"


@dataclass(frozen=True)
class _Step:
    step: str  # the llm.routing key the call site routes by
    what: str
    rule: str  # how the call site picks its model when no route applies (see _base_model)
    role: str | None  # the role whose fallbacks the call walks; None: the default chain
    tools: bool = False  # a requires_tools step: the developer runtime pin applies
    first_fix_attempt_only: bool = False  # validate, monitor_ci: the route applies to fix attempt 1 only
    pipelines: tuple[str, ...] = ()
    note: str = ""


_ROLE_BLOCKS: tuple[tuple[str, str, str, str, tuple[_Step, ...]], ...] = (
    (
        "triage",
        "Triage",
        "Sorts and labels new issues",
        "roles.triage_model",
        (_Step("triage", "Classify an issue and judge if it is ready", "triage", "triage"),),
    ),
    (
        "researcher",
        "Researcher",
        "Reads the code and writes the plan",
        "roles.researcher_model",
        (
            _Step("research", "Investigate before work starts", "researcher", "researcher"),
            _Step("spec", "Write the implementation spec", "researcher", "researcher"),
        ),
    ),
    (
        "developer",
        "Developer",
        "Writes and fixes the code",
        "roles.developer_model",
        (
            _Step("develop", "Write the change", "dev", "developer", tools=True, pipelines=(_DEV,)),
            _Step(
                "develop_fix", "Fix what the develop checks found", "dev", "developer", tools=True, pipelines=(_DEV,)
            ),
            _Step("simplify", "Tidy the change", "dev", "developer", tools=True, pipelines=(_DEV,)),
            _Step("self_review", "Review its own diff", "dev", "developer", tools=True, pipelines=(_DEV,)),
            _Step(
                "validate",
                "Run the checks and fix failures",
                "dev",
                "developer",
                tools=True,
                first_fix_attempt_only=True,
                pipelines=(_DEV, _ADDRESS),
            ),
            _Step(
                "monitor_ci",
                "Fix CI failures",
                "dev",
                "developer",
                tools=True,
                first_fix_attempt_only=True,
                pipelines=(_DEV, _ADDRESS),
            ),
            _Step(
                "address_review",
                "Address review comments and external reviewer findings",
                "dev",
                "developer",
                tools=True,
                pipelines=(_DEV, _ADDRESS),
                note="Also the route for address_external_findings in the developer pipeline.",
            ),
            _Step("rearrange_commits", "Reorganize commits", "dev", "developer", tools=True, pipelines=(_ADDRESS,)),
            _Step(
                "rebase",
                "Resolve rebase conflicts",
                "default",
                "developer",
                pipelines=(_ADDRESS,),
                note="Passes the default model, never the developer model.",
            ),
        ),
    ),
    (
        "reviewer",
        "Reviewer",
        "Reviews pull requests",
        "roles.reviewer_model",
        (
            _Step("review", "Review a PR", "review", "reviewer"),
            _Step("review_panel", "Panel review, one reviewer per dimension", "review_panel", "reviewer"),
        ),
    ),
    (
        "planner",
        "Planner",
        "Plans each supervisor cycle and proposes new issues",
        "roles.planner_model",
        (
            _Step(
                "planner",
                "Decide what the supervisor starts next",
                "supervisor_planner",
                "planner",
                note="The Planner role's issue proposal uses this route too; without it, the provider default.",
            ),
            _Step("generate_tasks", "Propose new issues from a project scan", "provider_default", "planner"),
        ),
    ),
)

_WRITING_STEPS: tuple[_Step, ...] = (
    _Step("commit_message", "Commit messages", "dev_text", "developer", pipelines=(_DEV,)),
    _Step("pr_body", "PR descriptions", "dev_text", "developer", pipelines=(_DEV, _ADDRESS)),
    _Step(
        "extraction",
        "Lessons for agent memory, implementation notes",
        "extraction",
        None,
        note="Walks the fallbacks of the role running it (developer or researcher), else the default fallbacks.",
    ),
    _Step("confidence_score", "Confidence score before handoff", "confidence", "developer", pipelines=(_DEV,)),
    _Step("harden", "Rewrite a thin issue", "provider_default", None),
)

# What an empty role model means, by role: the model each call site passes instead.
_UNSET_LABELS: dict[str, str] = {
    "triage": "Not set: the provider default",
    "researcher": "Not set: the default model",
    "developer": "Not set: by issue size",
    "reviewer": f"Not set: built-in {_REVIEW_DEFAULT}",
    "planner": f"Not set: built-in {_PLANNER_DEFAULT}",
}

_OTHER_MODELS: tuple[tuple[str, str], ...] = (
    ("oversight.analysis_model", "Oversight agent"),
    ("confidence.model", "Confidence scoring"),
)


class _Resolver:
    """Resolves model names for one config, remembering each answer (``resolve_alias()`` logs every mapping)."""

    def __init__(self, cfg: ProjectConfig) -> None:
        self.cfg = cfg
        self._runs_as: dict[str, str | None] = {}

    def runs_as(self, model: str | None) -> str | None:
        """The model ID a name reaches the provider as: ``select_model()``, which applies ``llm.model_aliases``
        and resolves a short tier name for the detected backend. None means the provider's own default."""
        if not model:
            return None
        if model not in self._runs_as:
            self._runs_as[model] = select_model(model, self.cfg)
        return self._runs_as[model]

    def route(self, step: str) -> tuple[str, str] | None:
        """``llm.routing[step]`` pinned to agent.model's family, as ``route_task_type()`` hands it to the call."""
        return route_task_type(step, llm_config=self.cfg.llm, agent_model=self.cfg.agent.model)


def _outcome(model: str | None, runs_as: str | None, source: str, why: str) -> dict[str, Any]:
    return {"model": model, "runs_as": runs_as, "source": source, "why": why}


def _by_size(res: _Resolver) -> list[dict[str, Any]]:
    """The developer model AssessStep picks per issue size when roles.developer_model is empty."""
    out = []
    for tier in ComplexityTier:
        model, _reason = route_model(tier, llm_config=res.cfg.llm, agent_model=res.cfg.agent.model)
        routed = res.cfg.llm.routing.get(tier.value)
        out.append(
            {
                "tier": tier.value,
                "model": model,
                "runs_as": res.runs_as(model),
                "source": "route" if routed is not None else "built_in",
            }
        )
    return out


def _developer_model(res: _Resolver, pipelines: tuple[str, ...]) -> dict[str, Any]:
    """``ctx.resolved_model or agent.model``, the model a developer step passes, in the pipelines it runs in."""
    cfg = res.cfg
    agent_model = cfg.agent.model
    address = _outcome(agent_model, res.runs_as(agent_model), "default", "Default model (address-review runs)")
    if _DEV not in pipelines:
        return {**address, "why": "Default model: an address-review run has no assess step"}
    if cfg.roles.developer_model:
        model = cfg.roles.developer_model
        main = _outcome(model, res.runs_as(model), "role", "Developer model")
    else:
        main = {**_outcome(None, None, "size", "Picked by issue size at launch"), "by_size": _by_size(res)}
    if _ADDRESS in pipelines:
        main["address_review"] = address
    return main


def _base_model(res: _Resolver, step: _Step) -> dict[str, Any]:
    """The model the call site passes when no route and no pin apply."""
    cfg = res.cfg
    rule = step.rule
    if rule in ("dev", "dev_text"):
        return _developer_model(res, step.pipelines)
    if rule == "default":
        return _outcome(cfg.agent.model, res.runs_as(cfg.agent.model), "default", "Default model")
    if rule == "researcher":
        if cfg.roles.researcher_model:
            model = cfg.roles.researcher_model
            return _outcome(model, res.runs_as(model), "role", "Researcher model")
        return _outcome(cfg.agent.model, res.runs_as(cfg.agent.model), "default", "Default model")
    if rule == "triage":
        if cfg.roles.triage_model:
            model = cfg.roles.triage_model
            return _outcome(model, res.runs_as(model), "role", "Triage model")
        return _outcome(None, None, "provider_default", "Provider default (no triage model)")
    if rule in ("review", "review_panel"):
        review = _review_model(res)
        if rule == "review_panel" and cfg.review.panel.dimension_models:
            review["dimension_models"] = {
                dim: res.runs_as(model) for dim, model in cfg.review.panel.dimension_models.items()
            }
            review["why"] += "; dimensions with their own model use it"
        return review
    if rule == "supervisor_planner":
        if cfg.roles.planner_model:
            model = cfg.roles.planner_model
            return _outcome(model, res.runs_as(model), "role", "Planner model")
        return _outcome(_PLANNER_DEFAULT, res.runs_as(_PLANNER_DEFAULT), "built_in", "Built-in default")
    if rule == "confidence":
        model = cfg.confidence.model
        if model:
            return _outcome(model, res.runs_as(model), "setting", "Confidence model (confidence.model)")
        return _outcome(None, None, "provider_default", "Provider default (no confidence model)")
    if rule == "extraction":
        return _outcome(_EXTRACTION_DEFAULT, res.runs_as(_EXTRACTION_DEFAULT), "built_in", "Built-in default")
    return _outcome(None, None, "provider_default", "Provider default")


def _review_model(res: _Resolver) -> dict[str, Any]:
    """ReviewerRole._resolve_review_model(): the reviewer model, else the built-in default, made servable for a
    provider that cannot expand a short name. A standalone review has no issue size, so size routing is unused."""
    cfg = res.cfg
    if cfg.roles.reviewer_model:
        model, source, why = cfg.roles.reviewer_model, "role", "Reviewer model"
    else:
        model, source, why = _REVIEW_DEFAULT, "built_in", "Built-in default"
    tier_model = resolve_bare_tier_for_provider(model, cfg.llm)
    runs_as = res.runs_as(tier_model[0] if tier_model else model)
    return _outcome(model, runs_as, source, why)


def _effective(res: _Resolver, step: _Step, *, runtime_pin: str) -> dict[str, Any]:
    """The model *step* runs on and why: the developer pin, then the step's route, then the call site's model."""
    if step.tools and runtime_pin:
        return _outcome(runtime_pin, runtime_pin, "pin", "Developer pin (code-editing steps)")
    routed = res.route(step.step)
    if routed is not None:
        model = routed[0]
        outcome = _outcome(model, res.runs_as(model), "route", "Set for this step")
        if step.first_fix_attempt_only:
            later = _base_model(res, step)
            outcome["why"] = "Set for this step (fix attempt 1); later attempts run the developer model"
            outcome["later_attempts"] = later
        return outcome
    return _base_model(res, step)


def _chain(res: _Resolver, role: str | None, primary: str | None) -> list[str]:
    """The fallbacks a call walks after *primary*, as ``_build_candidate_chain()`` builds them (duplicates
    dropped, an entry equal to the model already tried skipped)."""
    seen = {primary} if primary else set()
    chain: list[str] = []
    for entry in res.cfg.fallback_models_for(role):
        candidate = res.runs_as(entry)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        chain.append(candidate)
    return chain


def _step_entry(res: _Resolver, step: _Step, *, runtime_pin: str) -> dict[str, Any]:
    effective = _effective(res, step, runtime_pin=runtime_pin)
    if effective.get("by_size"):
        # A run skips the fallback equal to the model it picked, which differs by issue size.
        for size in effective["by_size"]:
            size["fallbacks"] = _chain(res, step.role, size["runs_as"])
    return {
        "step": step.step,
        "what": step.what,
        "route": res.cfg.llm.routing.get(step.step, ""),
        "pin_applies": step.tools,
        "fallback_role": step.role,
        "fallbacks": _chain(res, step.role, effective.get("runs_as")),
        "note": step.note,
        **effective,
    }


def _role_fallbacks(cfg: ProjectConfig, role: str) -> dict[str, Any]:
    own = cfg.roles.fallback_models_for(role)
    return {
        "key": f"roles.{role}_fallback_models",
        "own": own is not None,
        "models": list(own) if own is not None else list(cfg.agent.fallback_models),
    }


def build_model_map(cfg: ProjectConfig, *, runtime_honours_model: bool = True) -> dict[str, Any]:
    """The Models view's payload: the default model and fallbacks, one block per role with its steps, the writing
    tasks, the issue-size models and the other model settings, each step with the model it runs on and why.

    *runtime_honours_model* is False for a runtime that drops a caller's model (Codex), where the developer pin
    has no effect, as ``resolve_runtime_model_pin()`` decides.
    """
    res = _Resolver(cfg)
    runtime_pin = cfg.roles.developer_runtime_model if runtime_honours_model else ""
    roles = []
    for role_id, label, what, model_key, steps in _ROLE_BLOCKS:
        block: dict[str, Any] = {
            "id": role_id,
            "label": label,
            "what": what,
            "model_key": model_key,
            "model": getattr(cfg.roles, model_key.split(".", 1)[1]),
            "unset_label": _UNSET_LABELS[role_id],
            "fallbacks": _role_fallbacks(cfg, role_id),
            "steps": [_step_entry(res, step, runtime_pin=runtime_pin) for step in steps],
        }
        if role_id == "developer":
            block["pin"] = {
                "key": "roles.developer_runtime_model",
                "model": cfg.roles.developer_runtime_model,
                "applies": runtime_honours_model,
            }
        roles.append(block)
    return {
        "default": {"key": "agent.model", "model": cfg.agent.model, "runs_as": res.runs_as(cfg.agent.model)},
        "fallbacks": {"key": "agent.fallback_models", "models": list(cfg.agent.fallback_models)},
        "roles": roles,
        "writing": [_step_entry(res, step, runtime_pin=runtime_pin) for step in _WRITING_STEPS],
        "tiers": [
            {
                **entry,
                "built_in": _DEFAULT_ROUTING[ComplexityTier(entry["tier"])],
                "route": cfg.llm.routing.get(entry["tier"], ""),
            }
            for entry in _by_size(res)
        ],
        "other": [
            {"key": key, "label": label, "model": _dotted(cfg, key), "runs_as": res.runs_as(_dotted(cfg, key))}
            for key, label in _OTHER_MODELS
        ],
        "routing_keys": sorted(ROUTING_KEYS),
    }


def _dotted(cfg: ProjectConfig, key: str) -> Any:
    value: Any = cfg
    for part in key.split("."):
        value = getattr(value, part)
    return value


# --- Short model names: Pin and Pin all -------------------------------------------------------------------


def is_short_name(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in SHORT_NAMES


def _model_settings() -> tuple[list[str], list[str]]:
    """The LLM-layer model settings: (single-model keys, list keys). Runtime-layer ones name the coding agent's
    own catalog, which select_model() does not resolve, so they are left out."""
    from sova.dashboard.settings_meta import _REGISTRY

    scalars = [m.key for m in _REGISTRY if m.options_source == "llm" and m.value_type != "list"]
    lists = [m.key for m in _REGISTRY if m.options_source == "llm" and m.value_type == "list"]
    return scalars, lists


def _exact_id(cfg: ProjectConfig, name: str, catalog: list[str] | None, *, pin_family: bool) -> str | None:
    """The exact model ID *name* runs as today, or None when that is ambiguous.

    A routing entry is first pinned to agent.model's family, as ``route_task_type()`` does. Then
    ``select_model()`` (``resolve_alias()``): the alias map, else the backend's ID for a tier name. When that
    leaves the short name unresolved, the single model of that family in *catalog* (the available-models list)
    is used; none or several is ambiguous.
    """
    if pin_family:
        pinned = _pin_to_configured_model(name, cfg.agent.model)
        if pinned != name:
            return pinned
    resolved = select_model(name, cfg)
    if resolved and resolved != name and not is_short_name(resolved):
        return resolved
    family = get_model_family(name)
    if family is None or not catalog:
        return None
    matches = sorted({m for m in catalog if get_model_family(m) == family and not is_short_name(m)})
    return matches[0] if len(matches) == 1 else None


def short_name_settings(
    cfg: ProjectConfig, catalog: list[str] | None = None, *, environ: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Every model setting whose value in effect is a short name, with the exact ID Pin would write.

    One entry per setting: a single-model setting, a list setting (``entries`` names the short entries) or one
    routing row (``llm.routing.<step>``). ``pin_to`` is None when the exact ID is ambiguous, and ``env_var``
    names the ``SOVA_`` variable that sets the value, which Pin cannot change.
    """
    from sova.config.loader import env_override_var

    env = environ if environ is not None else {name.upper(): value for name, value in os.environ.items()}
    scalars, lists = _model_settings()
    found: list[dict[str, Any]] = []

    def add(key: str, value: Any, pin_to: Any, entries: list[str] | None = None) -> None:
        item = {"key": key, "value": value, "pin_to": pin_to, "env_var": env_override_var(key, env) or ""}
        if entries is not None:
            item["entries"] = entries
        found.append(item)

    for key in scalars:
        value = _dotted(cfg, key)
        if is_short_name(value):
            add(key, value, _exact_id(cfg, value, catalog, pin_family=False))
    for key in lists:
        value = _dotted(cfg, key)
        short = [entry for entry in (value or []) if is_short_name(entry)]
        if not short:
            continue
        pinned = [
            entry if not is_short_name(entry) else _exact_id(cfg, entry, catalog, pin_family=False) for entry in value
        ]
        add(key, list(value), None if None in pinned else pinned, entries=short)
    routing_env = env_override_var("llm.routing", env) or ""
    for step, value in sorted(cfg.llm.routing.items()):
        if is_short_name(value):
            add(f"llm.routing.{step}", value, _exact_id(cfg, value, catalog, pin_family=True))
            found[-1]["env_var"] = routing_env
    return found
