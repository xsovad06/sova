"""Role dispatcher -- routes tasks to the appropriate agent role.

The dispatcher determines which role should handle a task based on its
current tracker state or an explicit role name. It enforces the mandatory
pipeline order: Triage -> Research -> Develop.
"""

from __future__ import annotations

from pathlib import Path

from sova.adapters.base import TaskState
from sova.config.models import RolesConfig
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult
from sova.roles.developer import DeveloperRole
from sova.roles.planner import PlannerRole
from sova.roles.researcher import ResearcherRole
from sova.roles.reviewer import ReviewerRole
from sova.roles.triage import TriageRole
from sova.utils.logging import get_logger

log = get_logger(component="dispatcher")

# Registry of all built-in roles
_ROLES: dict[str, type[AgentRole]] = {
    "triage": TriageRole,
    "researcher": ResearcherRole,
    "developer": DeveloperRole,
    "reviewer": ReviewerRole,
    "planner": PlannerRole,
}

BUILTIN_ROLE_NAMES: frozenset[str] = frozenset(_ROLES.keys())

# Cache for discovered YAML workflows (invalidated on None)
_yaml_workflows_cache: list | None = None

# Maps tracker states to the role that should handle them
_STATE_TO_ROLE: dict[TaskState, str] = {
    TaskState.BACKLOG: "triage",
    TaskState.TRIAGED: "researcher",
    TaskState.RESEARCHED: "developer",
    TaskState.IN_PROGRESS: "developer",
    TaskState.IN_REVIEW: "reviewer",
}


def _resolve_nickname(name: str, config: RolesConfig | None) -> str:
    """Resolve a role nickname to its canonical name."""
    if config and name in config.nicknames:
        return config.nicknames[name]
    return name


def get_role(name: str, *, config: RolesConfig | None = None) -> AgentRole:
    """Get a role instance by name, resolving nicknames if configured.

    Raises ValueError if the role name is not found.
    """
    name = _resolve_nickname(name, config)

    role_cls = _ROLES.get(name)
    if role_cls is None:
        available = ", ".join(sorted(_ROLES.keys()))
        raise ValueError(f"Unknown role: {name!r}. Available: {available}")

    return role_cls()


def resolve_role_for_state(state: TaskState) -> AgentRole:
    """Determine which role should handle an issue in the given state.

    Raises ValueError if no role handles the given state (e.g., DONE).
    """
    role_name = _STATE_TO_ROLE.get(state)
    if role_name is None:
        raise ValueError(f"No role handles issues in {state!r} state")

    return get_role(role_name)


def list_roles() -> list[AgentRole]:
    """Return instances of all registered roles."""
    return [cls() for cls in _ROLES.values()]


async def get_role_async(name: str, *, config: RolesConfig | None = None, project_dir: Path | None = None) -> AgentRole:
    """Get a role by name, falling back to YAML workflows and DB lookup for custom roles.

    Args:
        name: Role name to look up
        config: Optional RolesConfig for nickname resolution
        project_dir: Project root directory (defaults to cwd, but should be passed from context)

    Raises ValueError if the role name is not found in built-ins, YAML, or DB.
    """
    global _yaml_workflows_cache
    name = _resolve_nickname(name, config)

    role_cls = _ROLES.get(name)
    if role_cls is not None:
        return role_cls()

    # Try YAML workflow discovery (with caching)
    from sova.core.yaml_workflow import discover_yaml_workflows
    from sova.roles.custom import CustomRole

    if _yaml_workflows_cache is None:
        resolved_dir = project_dir or Path.cwd()
        yaml_dirs = [
            resolved_dir / ".sova" / "workflows",
            resolved_dir / "commands" / "workflows",
        ]
        _yaml_workflows_cache = discover_yaml_workflows(yaml_dirs)

    for workflow in _yaml_workflows_cache:
        if workflow.name == name:
            log.info("dispatcher.yaml_workflow_found", name=name)
            return CustomRole(workflow)

    # Fall back to DB lookup for custom roles
    from sqlalchemy import select

    from sova.db.models import WorkflowDefinition
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                stmt = select(WorkflowDefinition).where(WorkflowDefinition.name == name)
                result = await session.execute(stmt)
                definition = result.scalar_one_or_none()
                if definition is not None:
                    log.info("dispatcher.db_workflow_found", name=name)
                    return CustomRole(definition)
    except Exception:
        log.warning("dispatcher.custom_lookup_failed", name=name, exc_info=True)
        raise

    available = ", ".join(sorted(_ROLES.keys()))
    raise ValueError(f"Unknown role: {name!r}. Available built-in: {available}")


async def dispatch(
    ctx: ExecutionContext,
    *,
    role_name: str | None = None,
    config: RolesConfig | None = None,
) -> tuple[AgentRole, RoleResult]:
    """Dispatch a task to the appropriate role and execute it.

    If role_name is provided, uses that role explicitly.
    Otherwise, auto-selects the role based on the task's tracker state.

    Returns the role used and its execution result.
    """
    if role_name:
        role = await get_role_async(role_name, config=config, project_dir=ctx.project_dir)
    elif ctx.has_issue:
        # Auto-select based on tracker state
        state = await ctx.adapter.get_state(ctx.issue_number)
        role = resolve_role_for_state(state)
    else:
        raise ValueError("Issue-less runs require an explicit --role")

    log.info("dispatch", issue=ctx.issue_number or "", label=ctx.display_label, role=role.name)
    ctx.role = role.name
    result = await role.execute(ctx)
    return role, result
