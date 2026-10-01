"""Shared Google Cloud ADC bearer-token helper for Vertex AI REST callers.

Several independent call sites need a short-lived OAuth2 bearer token for
Vertex AI's REST API. This is credential-handling code, so a hand-rolled copy
per caller is a real hazard (see the ``read_text_or_none()``/
``_write_rendered()`` precedents in .claude/rules/architecture.md).

Routed through here: batch submission
(``sova/llm/providers/anthropic_batch.py``) and model enumeration
(``sova/llm/litellm_provider.py``). Still hand-rolled, and the next thing to
fold in: ``_get_vertex_token()`` in
``sova/dashboard/services/llm_suggestion_service.py``, whose tests patch its
module-level ``_vertex_credentials`` global and ``asyncio.to_thread`` directly,
so converting it is a test rewrite rather than a drop-in substitution.

``google-auth`` is an optional dependency (``pip install sova[vertex]`` /
``sova[batch]``); every caller must handle the ``ImportError`` this raises.
"""

from __future__ import annotations

import asyncio

_GCP_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class VertexTokenProvider:
    """Fetches and caches a Google Cloud ADC bearer token.

    Credentials are resolved once via ``google.auth.default()`` and refreshed
    in place behind a lock, so concurrent callers on the same instance share
    one refresh instead of each triggering their own.
    """

    def __init__(self) -> None:
        self._credentials: object | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Return a valid bearer token, refreshing credentials if needed.

        Raises ``ImportError`` if ``google-auth`` is not installed.
        """
        try:
            import google.auth
            import google.auth.transport.requests
        except ImportError:
            raise ImportError(
                "google-auth is required for Vertex AI. Install it with: pip install google-auth"
            ) from None

        async with self._lock:
            if self._credentials is None:
                self._credentials, _ = await asyncio.to_thread(google.auth.default, scopes=_GCP_SCOPES)

            creds = self._credentials
            if not getattr(creds, "token", "") or getattr(creds, "expired", False):
                await asyncio.to_thread(creds.refresh, google.auth.transport.requests.Request())

            return str(creds.token)
