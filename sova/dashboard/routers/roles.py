"""Roles API router -- CRUD for workflow definitions and role introspection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.core.dag import validate_dag
from sova.dashboard.services import role_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.roles")

router = APIRouter(prefix="/roles", tags=["roles"])


class CreateRoleRequest(BaseModel):
    name: str
    description: str = ""
    graph_json: dict
    input_states: list[str] = []
    output_state: str = ""


class UpdateRoleRequest(BaseModel):
    description: str | None = None
    graph_json: dict | None = None
    input_states: list[str] | None = None
    output_state: str | None = None


class ValidateRequest(BaseModel):
    graph_json: dict


# -- List / Read ---------------------------------------------------------------


@router.get("")
async def list_roles() -> dict[str, list[dict[str, Any]]]:
    """List all roles (built-in + custom)."""
    builtins = role_service.get_builtin_roles()

    async with await get_session() as session:
        async with session.begin():
            customs = await role_service.list_definitions(session)
            custom_dicts = [role_service.definition_to_dict(d) for d in customs]

    return {"roles": builtins + custom_dicts}


@router.get("/commands")
async def list_commands() -> dict[str, list[dict[str, Any]]]:
    """List available commands with their input/output contracts."""
    commands = role_service.get_available_commands()
    return {"commands": commands}


@router.get("/{name}")
async def get_role(name: str) -> dict[str, Any]:
    """Get role detail + DAG definition."""
    # Check built-in first
    for builtin in role_service.get_builtin_roles():
        if builtin["name"] == name:
            return builtin

    # Check custom
    async with await get_session() as session:
        async with session.begin():
            definition = await role_service.get_definition(session, name)
            if definition is None:
                raise HTTPException(status_code=404, detail=f"Role not found: {name}")
            return role_service.definition_to_dict(definition)


# -- Create / Update / Delete --------------------------------------------------


@router.post("")
async def create_role(req: CreateRoleRequest) -> dict[str, Any]:
    """Create a custom role (draft -- DAG validated on save, not create)."""
    async with await get_session() as session:
        async with session.begin():
            definition = await role_service.create_definition(
                session,
                name=req.name,
                description=req.description,
                graph_json=req.graph_json,
                input_states=req.input_states,
                output_state=req.output_state,
            )
            if definition is None:
                raise HTTPException(status_code=409, detail=f"Role name '{req.name}' already exists or is reserved")
            return role_service.definition_to_dict(definition)


@router.put("/{name}")
async def update_role(name: str, req: UpdateRoleRequest) -> dict[str, Any]:
    """Update a custom role."""
    if req.graph_json is not None:
        errors, _ = validate_dag(req.graph_json)
        if errors:
            detail = "; ".join(errors)
            raise HTTPException(status_code=400, detail=f"Invalid DAG: {detail}")

    async with await get_session() as session:
        async with session.begin():
            definition = await role_service.update_definition(
                session,
                name,
                description=req.description,
                graph_json=req.graph_json,
                input_states=req.input_states,
                output_state=req.output_state,
            )
            if definition is None:
                raise HTTPException(status_code=404, detail=f"Role '{name}' not found or is built-in (immutable)")
            return role_service.definition_to_dict(definition)


@router.delete("/{name}")
async def delete_role(name: str) -> dict[str, str]:
    """Delete a custom role."""
    async with await get_session() as session:
        async with session.begin():
            ok = await role_service.delete_definition(session, name)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Role '{name}' not found or is built-in (immutable)")
            return {"status": "deleted", "name": name}


# -- Validation ----------------------------------------------------------------


@router.post("/{name}/validate")
async def validate_role(name: str, req: ValidateRequest) -> dict[str, Any]:
    """Validate DAG structure."""
    errors, _ = validate_dag(req.graph_json)
    return {"valid": len(errors) == 0, "errors": errors}
