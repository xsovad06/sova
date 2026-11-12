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


class TestOversightPage:
    async def test_header_switch_replaces_the_disabled_banner(self, client: AsyncClient) -> None:
        html = (await client.get("/oversight")).text

        assert _switch_keys(html) == ["oversight.enabled"]
        assert 'id="disabled-banner"' not in html
        assert "Enable it in Settings" not in html
        assert "/settings#oversight" in html, "the page keeps its Settings link"

    async def test_starting_label_is_bounded_to_a_window_after_the_enable_save(self, client: AsyncClient) -> None:
        html = (await client.get("/oversight")).text

        assert "STARTING_WINDOW_MS" in html
        assert "'Not running'" in html, "an enabled agent that is not running is shown, not left as Starting"


class TestDependenciesPage:
    async def test_switch_replaces_the_instruction_and_turning_on_starts_a_scan(self, client: AsyncClient) -> None:
        html = (await client.get("/dependency-health")).text

        assert _switch_keys(html) == ["dependency_health.enabled"]
        assert "in Settings to scan" not in html
        assert "if (on) refreshSnapshot();" in html

    async def test_a_scan_started_by_the_switch_shows_scanning_runs_once_and_reports_failure(
        self, client: AsyncClient
    ) -> None:
        html = (await client.get("/dependency-health")).text

        assert 'id="loading-state"' in html
        assert "showState('loading-state')" in html, "the off panel is replaced while the scan runs"
        assert "if (_scanInFlight) return _scanInFlight;" in html, "a second trigger joins the running scan"
        assert "!snapshot.enabled || !!_scanInFlight" in html, "loadAll keeps Rescan disabled mid-scan"
        assert "showToast('Scan failed: '" in html, "a failed scan is visible, not only in the console"


class TestPageKeysSave:
    """Every key a page control writes is saved by POST /settings/config and applies without a restart.

    The service is mocked as in test_dashboard.py's settings route tests: in this harness the write and the
    config read use different stores. The read-back after a save is covered by test_setting_controls.py.
    """

    @pytest.fixture
    def saved(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
        from unittest.mock import AsyncMock, MagicMock

        calls: list[tuple[str, str]] = []

        async def update_config(_project_dir: object, *, key: str, value: str, provider: object = None) -> dict:
            calls.append((key, value))
            return {"status": "ok", "key": key, "value": value}

        monkeypatch.setattr(
            "sova.dashboard.routers.settings.settings_service",
            MagicMock(update_config=AsyncMock(side_effect=update_config)),
        )
        monkeypatch.setattr("sova.dashboard.services.agent_pool.sync_max_concurrent", lambda *a, **kw: None)
        return calls

    @pytest.mark.parametrize(
        ("key", "value", "sent"),
        [
            ("max_parallel_agents", 3, "3"),
            ("agent.max_issue_budget", 80, "80"),
            ("external_reviews.coderabbit.trigger_review", True, "true"),
            ("dependency_health.enabled", True, "true"),
            ("supervisor.auto_triage", True, "true"),
            ("supervisor.enabled", False, "false"),
            ("oversight.enabled", False, "false"),
        ],
    )
    async def test_saves_and_needs_no_restart(
        self, client: AsyncClient, saved: list[tuple[str, str]], key: str, value: object, sent: str
    ) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.routers.settings._reload_all_configs", return_value=None):
            resp = await client.post("/api/settings/config", json={"key": key, "value": value})

        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"
        assert "restart_required" not in resp.json(), f"{key} applies live, so its control must not warn"
        assert saved == [(key, sent)]

    async def test_oversight_switch_starts_and_stops_the_agent(
        self, client: AsyncClient, saved: list[tuple[str, str]]
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.config.models import ProjectConfig

        agent = MagicMock()
        agent.running = False
        agent.stop = AsyncMock(side_effect=lambda: setattr(agent, "running", False))
        agent.start = MagicMock(side_effect=lambda: setattr(agent, "running", True))
        agent.reload_config = MagicMock(side_effect=lambda cfg: setattr(agent, "_config", cfg.oversight))

        def config_with(enabled: bool) -> ProjectConfig:
            cfg = ProjectConfig()
            cfg.oversight.enabled = enabled
            return cfg

        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=agent):
            with patch("sova.config.loader.load_config", return_value=config_with(True)):
                on = await client.post("/api/settings/config", json={"key": "oversight.enabled", "value": True})
            assert agent.running is True
            with patch("sova.config.loader.load_config", return_value=config_with(False)):
                off = await client.post("/api/settings/config", json={"key": "oversight.enabled", "value": False})

        assert agent.running is False
        agent.start.assert_called_once()
        agent.stop.assert_awaited_once()
        for resp in (on, off):
            assert resp.json().get("status") == "ok"
            assert "lifecycle_error" not in resp.json()


def test_a_spending_limit_of_zero_is_refused_with_a_message(tmp_path) -> None:
    """agent.max_issue_budget must be above 0; the Costs stepper shows the message and returns to the stored value."""
    from decimal import Decimal

    from sova.dashboard.services.settings_service import _validate_config_consistency

    assert _validate_config_consistency(tmp_path, "agent.max_issue_budget", Decimal("0"))
    assert _validate_config_consistency(tmp_path, "agent.max_issue_budget", Decimal("5")) is None


class TestStatusPollsDoNotOverwriteASave:
    """A status poll that starts before a save and answers after it carries the pre-save value. The header switch
    must skip that answer (as the Agents page's limit does), or it flips back until the next poll."""

    @pytest.mark.parametrize(
        ("page", "setter"),
        [
            ("oversight", r"enabledSwitch\.sovaSetValue"),
            ("supervisor", r"enabledSwitch\.sovaSetValue"),
            ("dependency_health", r"scanSwitch\.sovaSetValue"),
        ],
    )
    async def test_the_poll_compares_the_save_count_before_pushing_its_value(
        self, client: AsyncClient, page: str, setter: str
    ) -> None:
        from pathlib import Path

        source = (Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / f"{page}.html").read_text(
            encoding="utf-8"
        )

        assert re.search(r"(const|var) savesBefore = settingSaveCount\(\);", source)
        assert re.search(r"settingSaveCount\(\) === savesBefore\)\s*" + setter, source)
        assert source.index("savesBefore = settingSaveCount()") < source.index("sovaSetValue")
