"""Role service -- CRUD for workflow definitions and built-in role introspection."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sova.commands.catalog import CommandEntry, discover, get_canonical_dir
from sova.core.steps import get_address_review_step_names, get_developer_step_names
from sova.db.models import WorkflowDefinition
from sova.roles.dispatcher import BUILTIN_ROLE_NAMES
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.roles")


# -- CRUD ---------------------------------------------------------------------


async def list_definitions(session: AsyncSession) -> list[WorkflowDefinition]:
    """List all custom workflow definitions."""
    stmt = select(WorkflowDefinition).order_by(WorkflowDefinition.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_definition(session: AsyncSession, name: str) -> WorkflowDefinition | None:
    """Get a single workflow definition by name."""
    stmt = select(WorkflowDefinition).where(WorkflowDefinition.name == name)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_definition(
    session: AsyncSession,
    *,
    name: str,
    description: str = "",
    graph_json: dict,
    input_states: list[str] | None = None,
    output_state: str = "",
) -> WorkflowDefinition | None:
    """Create a new custom workflow definition. Returns None if name conflicts."""
    if name in BUILTIN_ROLE_NAMES:
        return None

    existing = await get_definition(session, name)
    if existing is not None:
        return None

    definition = WorkflowDefinition(
        name=name,
        description=description,
        graph_json=graph_json,
        input_states=input_states or [],
        output_state=output_state,
    )
    session.add(definition)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None
    return definition


async def update_definition(
    session: AsyncSession,
    name: str,
    *,
    description: str | None = None,
    graph_json: dict | None = None,
    input_states: list[str] | None = None,
    output_state: str | None = None,
) -> WorkflowDefinition | None:
    """Update an existing custom workflow definition."""
    definition = await get_definition(session, name)
    if definition is None or definition.is_builtin:
        return None

    if description is not None:
        definition.description = description
    if graph_json is not None:
        definition.graph_json = graph_json
    if input_states is not None:
        definition.input_states = input_states
    if output_state is not None:
        definition.output_state = output_state

    definition.version += 1
    await session.flush()
    return definition


async def delete_definition(session: AsyncSession, name: str) -> bool:
    """Delete a custom workflow definition. Returns False for built-in or not found."""
    definition = await get_definition(session, name)
    if definition is None or definition.is_builtin:
        return False

    await session.delete(definition)
    await session.flush()
    return True


# -- Built-in role introspection -----------------------------------------------


@lru_cache(maxsize=1)
def get_builtin_roles() -> list[dict]:
    """Return built-in roles as dicts with graph_json representations (cached)."""
    return [
        _builtin_developer(),
        _builtin_address_review(),
        _builtin_reviewer(),
        _builtin_triage(),
        _builtin_researcher(),
    ]


def _steps_to_linear_graph(step_names: list[str], role_name: str) -> dict:
    """Convert a list of step names to a linear DAG graph_json."""
    nodes = []
    edges = []
    for i, name in enumerate(step_names):
        nodes.append(
            {
                "id": f"{role_name}-{i}",
                "command": name,
                "label": name.replace("_", " ").title(),
                "position": {"x": 200, "y": 80 + i * 100},
                "params": {},
            }
        )
        if i > 0:
            edges.append(
                {
                    "id": f"{role_name}-e{i}",
                    "source": f"{role_name}-{i - 1}",
                    "target": f"{role_name}-{i}",
                    "condition": None,
                }
            )
    return {"nodes": nodes, "edges": edges}


def _builtin_developer() -> dict:
    steps = get_developer_step_names()
    return {
        "name": "developer",
        "description": "Develop features and fixes using TDD workflow",
        "graph_json": _steps_to_linear_graph(steps, "dev"),
        "input_states": ["researched", "in_progress"],
        "output_state": "in_review",
        "is_builtin": True,
    }


def _builtin_address_review() -> dict:
    steps = get_address_review_step_names()
    return {
        "name": "address-review",
        "description": "Address reviewer findings and push fixes",
        "graph_json": _steps_to_linear_graph(steps, "addr"),
        "input_states": ["in_review"],
        "output_state": "in_review",
        "is_builtin": True,
    }


def _builtin_reviewer() -> dict:
    return {
        "name": "reviewer",
        "description": "Review PRs and provide structured feedback",
        "graph_json": {
            "nodes": [
                {
                    "id": "rev-0",
                    "command": "review_pr",
                    "label": "Review PR",
                    "position": {"x": 200, "y": 80},
                    "params": {},
                },
                {
                    "id": "rev-1",
                    "command": "handoff_to_developer",
                    "label": "Handoff to Developer",
                    "position": {"x": 100, "y": 200},
                    "params": {},
                },
                {
                    "id": "rev-2",
                    "command": "handoff_to_user",
                    "label": "Handoff to User",
                    "position": {"x": 300, "y": 200},
                    "params": {},
                },
            ],
            "edges": [
                {"id": "rev-e1", "source": "rev-0", "target": "rev-1", "condition": "has_findings == true"},
                {"id": "rev-e2", "source": "rev-0", "target": "rev-2", "condition": "has_findings != true"},
            ],
        },
        "input_states": ["in_review"],
        "output_state": "in_review",
        "is_builtin": True,
    }


def _builtin_triage() -> dict:
    return {
        "name": "triage",
        "description": "Assess issues for agent suitability",
        "graph_json": {
            "nodes": [
                {
                    "id": "tri-0",
                    "command": "assess_task",
                    "label": "Assess Task",
                    "position": {"x": 200, "y": 80},
                    "params": {},
                },
                {
                    "id": "tri-1",
                    "command": "write_assessment",
                    "label": "Write Assessment",
                    "position": {"x": 200, "y": 180},
                    "params": {},
                },
            ],
            "edges": [
                {"id": "tri-e1", "source": "tri-0", "target": "tri-1", "condition": None},
            ],
        },
        "input_states": ["backlog"],
        "output_state": "triaged",
        "is_builtin": True,
    }


def _builtin_researcher() -> dict:
    return {
        "name": "researcher",
        "description": "Research issues to gather context before development",
        "graph_json": {
            "nodes": [
                {
                    "id": "res-0",
                    "command": "research",
                    "label": "Research",
                    "position": {"x": 200, "y": 80},
                    "params": {},
                },
                {
                    "id": "res-1",
                    "command": "write_findings",
                    "label": "Write Findings",
                    "position": {"x": 200, "y": 180},
                    "params": {},
                },
            ],
            "edges": [
                {"id": "res-e1", "source": "res-0", "target": "res-1", "condition": None},
            ],
        },
        "input_states": ["triaged"],
        "output_state": "researched",
        "is_builtin": True,
    }


# -- Command contracts ---------------------------------------------------------


@lru_cache(maxsize=1)
def get_available_commands() -> list[dict]:
    """Return all discovered commands with their contracts (cached)."""
    commands_dir = get_canonical_dir()
    entries = discover(commands_dir)
    return [_command_entry_to_dict(e) for e in entries]


def _command_entry_to_dict(entry: CommandEntry) -> dict:
    return {
        "name": entry.name,
        "description": entry.description,
        "category": entry.category,
        "user_invocable": entry.user_invocable,
        "inputs": entry.inputs,
        "outputs": entry.outputs,
    }


# -- Serialization helpers -----------------------------------------------------


def definition_to_dict(d: WorkflowDefinition) -> dict:
    """Convert a WorkflowDefinition to an API-friendly dict."""
    return {
        "id": d.id,
        "name": d.name,
        "description": d.description,
        "graph_json": d.graph_json,
        "input_states": d.input_states,
        "output_state": d.output_state,
        "version": d.version,
        "is_builtin": d.is_builtin,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }
