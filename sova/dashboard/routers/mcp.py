"""MCP router -- HTTP+SSE endpoint for agent self-inspection tools."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from starlette.requests import Request

from sova.config.context import get_project_dir
from sova.dashboard.services import mcp_service
from sova.dashboard.services.mcp_service import validate_mcp_token
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.mcp")

router = APIRouter(tags=["mcp"])


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
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "error": {
                "code": -32700,
                "message": f"Parse error: {e}",
            },
            "id": None,
        }

    req_id = body.get("id", 1)
    method = body.get("method")
    params = body.get("params", {})

    # Handle tools/call method
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        # Inject authenticated run_id into arguments
        arguments["token_run_id"] = token_run_id

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
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {tool_name}",
                },
                "id": req_id,
            }

        try:
            raw_run_id = arguments.get("run_id")
            if raw_run_id is None:
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32602,
                        "message": "Missing required parameter: run_id",
                    },
                    "id": req_id,
                }
            try:
                run_id = int(raw_run_id)
            except (TypeError, ValueError):
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32602,
                        "message": f"Invalid run_id: expected integer, got {type(raw_run_id).__name__}",
                    },
                    "id": req_id,
                }
            if run_id != token_run_id:
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32000,
                        "message": f"Access denied: cannot query run_id {run_id} (token allows {token_run_id})",
                    },
                    "id": req_id,
                }

            # Remove token_run_id from arguments before passing to service function
            service_args = {k: v for k, v in arguments.items() if k != "token_run_id"}

            tool_func = tool_map[tool_name]
            result = await tool_func(**service_args)

            return {
                "jsonrpc": "2.0",
                "result": result,
                "id": req_id,
            }
        except ValueError as e:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": str(e),
                },
                "id": req_id,
            }
        except Exception:
            log.exception("mcp.tool_error", tool=tool_name)
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                },
                "id": req_id,
            }

    # Unsupported method
    return {
        "jsonrpc": "2.0",
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
        "id": req_id,
    }
