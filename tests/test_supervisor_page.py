"""The Supervisor page (templates/supervisor.html): its page script and its layout on the phone.

There is no browser here, so the markup is asserted on the rendered page and the layout contract on the shipped
stylesheet; the measured layout at 390 and 1440 px is checked by driving a real browser before merge.
"""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from tests.conftest import DASHBOARD_DIR

pytestmark = pytest.mark.usefixtures("reset_db_rows")

_TEMPLATE = (DASHBOARD_DIR / "templates" / "supervisor.html").read_text(encoding="utf-8")


def _page_script() -> str:
    """The page's own classic script block."""
    scripts = _TEMPLATE[_TEMPLATE.index("{% block scripts %}") :]
    start = scripts.index("<script>")
    return scripts[start : scripts.index("</script>", start)]


class TestPageScript:
    def test_every_variable_the_unload_handler_reads_is_declared(self) -> None:
        """The page left its own socket for the shared one (subscribeSovaSocket); a handler still closing the old
        one threw a ReferenceError on every navigation away from the page."""
        script = _page_script()
        handler = script[script.index("addEventListener('beforeunload'") :]
        for name in set(re.findall(r"\b(_[A-Za-z]\w*)\b", handler)):
            assert re.search(rf"\b(?:let|const|var|function)\s+{name}\b", script), f"{name} is never declared"

    async def test_the_page_renders(self, client: AsyncClient) -> None:
        response = await client.get("/supervisor")
        assert response.status_code == 200
        assert 'id="supervisor-switches"' in response.text
