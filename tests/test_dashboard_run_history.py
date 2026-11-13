"""Run history on the Agents page (#1462): one action name and colour per kind of run, and the history API's
date, action and number filters, totals and ordering."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services.work_state import RUN_ACTIONS, run_action, run_actions_payload
from sova.db.session import get_session
from tests.conftest import DASHBOARD_DIR

pytestmark = pytest.mark.usefixtures("reset_db_rows")

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with await get_session() as s:
        yield s


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as ac:
        yield ac


# The issue's name table, row by row: (stored role, pipeline variant) -> (action, label, colour, running form).
_TABLE = [
    ("developer", "developer", "develop", "Develop", "blue", "Developing"),
    ("developer", None, "develop", "Develop", "blue", "Developing"),
    ("developer", "address_review", "address-review", "Address review", "blue", "Addressing"),
    ("command:address-pr", "command", "address-review", "Address review", "blue", "Addressing"),
    ("reviewer", "command", "review", "Review", "green", "Reviewing"),
    ("command:review-pr", "command", "review", "Review", "green", "Reviewing"),
    ("command:integrate-pr", "command", "integrate", "Integrate", "teal", "Integrating"),
    ("command:/integrate-pr 524", "command", "integrate", "Integrate", "teal", "Integrating"),
    ("researcher", "researcher", "research", "Research", "mauve", "Researching"),
    ("supervisor:rebase", "address_review", "rebase", "Rebase", "gray", "Rebasing"),
    ("triage", None, "triage", "Triage", "yellow", "Triaging"),
    ("planner", None, "plan", "Plan", "mauve", "Planning"),
    ("command:spec", "command", "spec", "Spec", "gray", "Writing Spec"),
    ("command:after-merge", "command", "after-merge", "After merge", "gray", "Cleaning up"),
    ("command:health-audit", "command", "health-audit", "Health audit", "gray", "Running"),
    ("command:/health-audit --deep", "command", "health-audit", "Health audit", "gray", "Running"),
    ("custom", None, "custom", "custom", "gray", "Running"),
]


class TestRunActionTable:
    @pytest.mark.parametrize(("role", "variant", "key", "label", "color", "running"), _TABLE)
    def test_each_kind_of_run_has_one_name_and_colour(
        self, role: str, variant: str | None, key: str, label: str, color: str, running: str
    ) -> None:
        action = run_action(role, variant)
        assert (action.key, action.label, action.color, action.running) == (key, label, color, running)

    def test_a_run_without_a_role_reads_as_auto(self) -> None:
        assert run_action(None).label == "Auto"
        assert run_action("").color == "gray"

    def test_every_colour_is_a_token_the_page_maps(self) -> None:
        assert {a.color for a in RUN_ACTIONS.values()} <= {"blue", "green", "mauve", "yellow", "teal", "gray"}

    def test_the_payload_carries_the_whole_table(self) -> None:
        payload = run_actions_payload()
        assert set(payload) == {"actions", "roles", "variants"}
        assert payload["actions"]["integrate"] == {"label": "Integrate", "running": "Integrating", "color": "teal"}
        assert payload["roles"]["command:integrate-pr"] == "integrate"
        assert payload["variants"] == {"developer": {"address_review": "address-review"}}
        json.dumps(payload)

    def test_running_agent_label_comes_from_the_table(self) -> None:
        from sova.dashboard.services.work_item_service import _format_running_agent

        assert _format_running_agent({"role": "supervisor:rebase"})["role_label"] == "Rebasing"
        assert _format_running_agent({"role": "command:/integrate-pr 7"})["role_label"] == "Integrating"
        assert _format_running_agent({"role": "custom"})["role_label"] == "Running"

    def test_running_agent_label_follows_the_pipeline_variant(self) -> None:
        from sova.dashboard.services.work_item_service import _format_running_agent

        running = {"role": "developer", "pipeline_variant": "address_review"}
        assert _format_running_agent(running)["role_label"] == "Addressing"
        assert (
            _format_running_agent({"role": "developer", "pipeline_variant": "developer"})["role_label"] == "Developing"
        )


class TestRunActionsReachThePage:
    async def test_every_page_receives_the_table_once(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert resp.status_code == 200
        match = re.search(r"window\.SOVA_RUN_ACTIONS = (\{.*?\});\n", resp.text)
        assert match, "window.SOVA_RUN_ACTIONS is not emitted"
        assert json.loads(match.group(1)) == run_actions_payload()
        assert resp.text.count("window.SOVA_RUN_ACTIONS =") == 1


# ---------------------------------------------------------------------------------------------------------------
# Browser side: app.js resolves names and colours from window.SOVA_RUN_ACTIONS, and history.js drives the table.
# ---------------------------------------------------------------------------------------------------------------

_STATIC = DASHBOARD_DIR / "static"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_RUN_ACTIONS_START = "/* ============================================================\n   13. RUN ACTIONS"
_RUN_ACTIONS_END = "/* ============================================================\n   13. STEP PIPELINE BAR"


def _require_node() -> None:
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail("node is required in CI to run the dashboard scripts")
    pytest.skip("node is required to run the dashboard scripts")


def _run_actions_block() -> str:
    start = _APP_JS.index(_RUN_ACTIONS_START)
    return _APP_JS[start : _APP_JS.index(_RUN_ACTIONS_END, start)]


def _run_node(tmp_path: Path, script: str, *, env: dict[str, str] | None = None) -> object:
    path = tmp_path / "run.mjs"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, **(env or {})},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_TEXT_CLASS = {
    "blue": "text-accent",
    "green": "text-accent-green",
    "mauve": "text-accent-purple",
    "yellow": "text-accent-yellow",
    "teal": "text-accent-teal",
    "gray": "text-gray-400",
}


class TestPageResolvesRunActionsFromTheTable:
    def test_format_role_and_role_color_match_the_server_table(self, tmp_path: Path) -> None:
        _require_node()
        cases = [[role, variant] for role, variant, *_ in _TABLE] + [[None, None]]
        script = (
            f"globalThis.window = globalThis;\nwindow.SOVA_RUN_ACTIONS = {json.dumps(run_actions_payload())};\n"
            f"window.SOVA_COLORS = {{}};\n{_run_actions_block()}\n"
            f"const cases = {json.dumps(cases)};\n"
            "const out = cases.map(([r, v]) => [runActionFor(r, v).key, formatRole(r, v), roleColor(r, v).text,"
            " typeof roleColor(r, v).hex]);\n"
            "process.stdout.write(JSON.stringify(out));\n"
        )
        result = _run_node(tmp_path, script)
        expected = [[key, label, _TEXT_CLASS[color], "string"] for _, _, key, label, color, _ in _TABLE]
        expected.append(["auto", "Auto", _TEXT_CLASS["gray"], "string"])
        assert result == expected

    def test_a_page_without_the_table_still_names_the_run(self, tmp_path: Path) -> None:
        """home.html and setup.html do not extend base.html; a helper reached there must not throw."""
        _require_node()
        script = (
            f"globalThis.window = globalThis;\n{_run_actions_block()}\n"
            "process.stdout.write(JSON.stringify([formatRole('command:/integrate-pr 5'), formatRole('developer'),"
            " roleColor('reviewer').text]));\n"
        )
        assert _run_node(tmp_path, script) == ["integrate-pr", "developer", _TEXT_CLASS["gray"]]

    def test_app_js_keeps_no_name_or_role_table_of_its_own(self) -> None:
        block = _run_actions_block()
        for action in RUN_ACTIONS.values():
            assert f"'{action.label}'" not in block, action.label
            assert f"'{action.running}'" not in block, action.running
        for role in ("developer", "reviewer", "researcher", "triage", "planner", "address-pr", "integrate-pr"):
            assert f"'{role}'" not in block, role
        assert "ROLE_COLORS" not in _APP_JS
        assert "_commandToRole" not in _APP_JS
        assert "address pr" not in _APP_JS
