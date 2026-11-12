"""Health API: read-only diagnostic checks for the settings page panel."""

from __future__ import annotations

from fastapi import APIRouter

from sova.config.context import resolve_project_dir
from sova.dashboard.services import health_service
from sova.dashboard.services.agent_pool import get_default_project_dir

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/checks")
async def checks() -> dict:
    """Run every diagnostic check (sova.utils.diagnostics) and return the results.

    Never 500s: health_service.get_health_checks() converts any internal
    failure into a single failed check row rather than raising.
    """
    project_dir = resolve_project_dir(default_project_dir=get_default_project_dir())
    return await health_service.get_health_checks(project_dir)


@router.get("/project")
async def project() -> dict:
    """The project directory this server serves, so a client can confirm it reached the project it means.

    Read by the MCP operator tools before they send a project-scoped call to a single-project server: the
    configured port can belong to another project's server, or to an unrelated local app. The ``project_dir`` key
    (an absolute, resolved path) is a contract with ``sova/mcp/operator.py``, pinned by
    ``TestHealthChecksAPI.test_project_endpoint_reports_the_served_project_dir``. Like the rest of ``/api/health``
    it is read-only and carries no auth beyond the Host check. This route is a deliberate addition to the
    dashboard routers: no existing route reports the served directory.
    """
    return {"project_dir": str(resolve_project_dir(default_project_dir=get_default_project_dir()).resolve())}
