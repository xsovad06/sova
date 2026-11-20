"""Helpers shared by the files split from the former tests/test_dashboard.py."""

from __future__ import annotations

# httpx merges per-call headers over the client's defaults rather than clearing
# them, so a test proving a route rejects a *missing* CSRF pair must explicitly
# blank out both headers the `client` fixture in tests/conftest.py pre-arms on every request.
NO_CSRF = {"origin": "", "x-csrf-token": ""}
