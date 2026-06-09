"""Multi-project routing middleware.

Extracts project slug from /p/{slug}/ URL prefix and sets the
per-request context so services resolve the correct project.
"""

from __future__ import annotations

import re

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from sova.config.registry import get_project_path
from sova.dashboard.project_context import clear_project_context, set_project_context

_SAFE_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


class ProjectContextMiddleware(BaseHTTPMiddleware):
    """Parse /p/{slug}/ from URL and set project context."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        if path.startswith("/p/"):
            parts = path.split("/", 3)
            if len(parts) >= 3:
                slug = parts[2]
                if _SAFE_SLUG_RE.match(slug):
                    project_path = get_project_path(slug)
                    if project_path and project_path.is_dir():
                        set_project_context(project_path, slug)
                        request.state.project_slug = slug
                        request.state.project_path = project_path
                        request.state.project_name = project_path.name

        try:
            return await call_next(request)
        finally:
            clear_project_context()
