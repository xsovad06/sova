"""Pages carry their own setting controls (#1461): each renders placeholders that app.js's
mountSettingControls() turns into settingSwitch()/settingStepper() controls, labelled from the
settings registry so they read the same as the Settings page.

The controls' save, pending, error and revert behavior is tested in test_setting_controls.py;
these tests check that each page declares the right keys and drops the old instructions.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.settings_meta import get_meta
from tests.conftest import csrf_request_headers as _csrf_request_headers

pytestmark = pytest.mark.usefixtures("reset_db_rows")

SUPERVISOR_SWITCHES = (
    "supervisor.auto_triage",
    "supervisor.auto_research",
    "supervisor.auto_develop",
    "supervisor.auto_address_review",
    "supervisor.auto_review",
    "supervisor.auto_integrate",
    "supervisor.auto_rebase",
    "supervisor.require_approval",
    "supervisor.respect_dependencies",
    "supervisor.respect_ownership",
    "supervisor.file_overlap_gate",
    "supervisor.llm_planning",
    "supervisor.auto_queue",
)


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=dashboard_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers.update(_csrf_request_headers(ac))
        yield ac


def _switch_keys(html: str) -> list[str]:
    return re.findall(r'data-setting-switch="([^"]+)"', html)


def _stepper(html: str, key: str) -> str:
    match = re.search(r'<span[^>]*data-setting-stepper="' + re.escape(key) + r'"[^>]*>', html)
    assert match, f"no stepper placeholder for {key}"
    return match.group(0)


class TestSupervisorPage:
    async def test_shows_the_on_off_switch_and_all_13_switches(self, client: AsyncClient) -> None:
        html = (await client.get("/supervisor")).text

        keys = _switch_keys(html)
        assert keys[0] == "supervisor.enabled", "the on/off switch sits in the header, before the panel"
        assert sorted(keys[1:]) == sorted(SUPERVISOR_SWITCHES)

    async def test_switch_labels_come_from_the_settings_registry(self, client: AsyncClient) -> None:
        html = (await client.get("/supervisor")).text

        for key in SUPERVISOR_SWITCHES:
            meta = get_meta(key)
            assert meta is not None, key
            assert f'data-setting-switch="{key}" data-label="{meta.label}"' in html

    async def test_on_off_saves_through_the_settings_path_only(self, client: AsyncClient) -> None:
        """The save starts or stops the daemon itself; separate start/stop calls would race it."""
        html = (await client.get("/supervisor")).text

        assert "mountSettingControls(document" in html
        assert "/supervisor/start" not in html
        assert "/supervisor/stop" not in html
        assert "toggleSupervisor" not in html


class TestAgentsPage:
    async def test_shows_running_of_limit_instead_of_free_slots(self, client: AsyncClient) -> None:
        html = (await client.get("/agents")).text

        assert 'id="agent-limit-running"' in html
        assert 'id="agent-limit-control"' in html
        assert "agents running" in html
        assert 'id="slots-info"' not in html
