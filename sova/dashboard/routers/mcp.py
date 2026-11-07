"""MCP router: HTTP+SSE endpoint for agent self-inspection tools."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from starlette.requests import Request

from sova.config.context import get_project_dir
from sova.dashboard.services import mcp_service
from sova.dashboard.services.mcp_service import validate_mcp_token
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.mcp")

router = APIRouter(tags=["mcp"])

# Server-owned context a tool resolves for itself (from the authenticated request),
# never from the client's JSON-RPC arguments. A caller passing its own project_dir
# here would let a token scoped to one run query an unrelated project's DB.
_CONTEXT_ONLY_PARAMS = frozenset({"project_dir"})


class JsonRpcParams(BaseModel):
    """`params` of a JSON-RPC request: only `tools/call` reads `name` and `arguments`."""

    model_config = ConfigDict(extra="allow")

    name: StrictStr | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request envelope: the one place that defines what a body may contain.

    `jsonrpc` defaults to "2.0" when absent but any other value is rejected. `id` defaults
    to 1 when absent (a notification still gets a reply, as before) and must be a string,
    an integer or null. A bool, float, list or object `id` is rejected.
    """

    model_config = ConfigDict(extra="ignore")

    jsonrpc: Literal["2.0"] = "2.0"
    id: StrictInt | StrictStr | None = 1
    method: StrictStr
    # The envelope stays permissive about `params`: positional (array) and null params are valid
    # JSON-RPC, and only the tools/call branch validates the object form (-32602), so an unknown
    # method keeps its -32601 whatever `params` holds.
    params: dict[str, Any] | list[Any] | None = Field(default_factory=dict)


def _jsonrpc_error(code: int, message: str, req_id: int | str | None) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}


def _detectable_id(body: object) -> int | str | None:
    """Return the request `id` for an error reply when it is a usable JSON-RPC id, else None.

    JSON-RPC 2.0 says the error `id` is null only when the id could not be detected, so a
    well-formed id on an otherwise malformed body is echoed back. A bool, float, list or
    object id is not a valid id and yields None.
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("id")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    return raw


def _filter_tool_arguments(tool_func: Callable[..., Any], arguments: dict) -> dict:
    """Keep only the keys `tool_func`'s own signature declares as client-facing.

    Built per tool from `inspect.signature` rather than one global "safe keys"
    list, so a tool whose signature legitimately differs (an optional parameter
    another tool doesn't have) is never broken by a list sized for a different
    tool, while a key the function's signature does not declare at all (most
    importantly `project_dir`, injected by the server) is always dropped.
    """
    allowed = set(inspect.signature(tool_func).parameters) - _CONTEXT_ONLY_PARAMS
    return {k: v for k, v in arguments.items() if k in allowed}


def _get_mcp_secret(project_dir: Path | None = None) -> str:
    """Load MCP token secret from config."""
    return mcp_service.get_or_generate_secret(project_dir)


def _validate_mcp_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> int:
    """FastAPI dependency: validate MCP token and return authenticated run_id.

    Raises:
        HTTPException: 401 if token is missing or invalid
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")

    token = authorization[7:]  # Strip "Bearer "
    secret = _get_mcp_secret(get_project_dir())

    try:
        run_id = validate_mcp_token(token, secret)
        return run_id
    except ValueError as e:
        log.warning("mcp.auth_failed", error=str(e))
        raise HTTPException(status_code=401, detail=str(e)) from e


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    token_run_id: Annotated[int, Depends(_validate_mcp_auth)],
) -> dict:
    """MCP JSON-RPC 2.0 endpoint (HTTP POST).

    Handles tool invocations from authenticated agents. The token_run_id
    is injected into tool context to enforce cross-run access denial.
    """
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001 (JSON-RPC boundary: any body-read/parse failure becomes a -32700 error)
        log.warning("mcp.request_parse_failed", exc_info=True)
        return _jsonrpc_error(-32700, f"Parse error: {e}", None)

    try:
        rpc = JsonRpcRequest.model_validate(body)
    except ValidationError:
        log.warning("mcp.invalid_request")
        return _jsonrpc_error(-32600, "Invalid Request", _detectable_id(body))

    req_id = rpc.id
    method = rpc.method

    # Handle tools/call method
    if method == "tools/call":
        if not isinstance(rpc.params, dict):
            return _jsonrpc_error(-32602, "Invalid params: tools/call expects an object", req_id)
        try:
            params = JsonRpcParams.model_validate(rpc.params)
        except ValidationError:
            log.warning("mcp.invalid_params")
            return _jsonrpc_error(-32602, "Invalid params: name must be a string and arguments an object", req_id)
        tool_name = params.name
        # The authenticated run_id is the only identity: a client-supplied token_run_id
        # is dropped here and never forwarded.
        arguments = {k: v for k, v in params.arguments.items() if k != "token_run_id"}

        # Dispatch to the appropriate tool
        tool_map = {
            "get_run_status": mcp_service.get_run_status,
            "get_budget": mcp_service.get_budget,
            "get_gate_results": mcp_service.get_gate_results,
            "get_pr_status": mcp_service.get_pr_status,
            "get_issue_context": mcp_service.get_issue_context,
            "list_run_history": mcp_service.list_run_history,
        }

        if tool_name not in tool_map:
            return _jsonrpc_error(-32601, f"Method not found: {tool_name}", req_id)

        try:
            raw_run_id = arguments.get("run_id")
            if raw_run_id is None:
                return _jsonrpc_error(-32602, "Missing required parameter: run_id", req_id)
            try:
                run_id = int(raw_run_id)
            except (TypeError, ValueError):
                return _jsonrpc_error(
                    -32602, f"Invalid run_id: expected integer, got {type(raw_run_id).__name__}", req_id
                )
            if run_id != token_run_id:
                return _jsonrpc_error(
                    -32000, f"Access denied: cannot query run_id {run_id} (token allows {token_run_id})", req_id
                )

            tool_func = tool_map[tool_name]
            service_args = _filter_tool_arguments(tool_func, arguments)
            result = await tool_func(**service_args)

            return {
                "jsonrpc": "2.0",
                "result": result,
                "id": req_id,
            }
        except ValueError as e:
            return _jsonrpc_error(-32000, str(e), req_id)
        except Exception:  # noqa: BLE001 (JSON-RPC boundary: any tool failure becomes an error response)
            log.exception("mcp.tool_error", tool=tool_name)
            return _jsonrpc_error(-32603, "Internal error", req_id)

    # Unsupported method
    return _jsonrpc_error(-32601, f"Method not found: {method}", req_id)
