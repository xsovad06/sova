"""Run history on the Agents page (#1462): one action name and colour per kind of run, and the history API's
date, action and number filters, totals and ordering."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services.work_service import get_work_history
from sova.dashboard.services.work_state import RUN_ACTIONS, run_action, run_actions_payload
from sova.db.models import StepExecution, TaskRun
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


async def _add_run(
    session: AsyncSession,
    *,
    role: str,
    status: str = "done",
    hours: float = 0,
    issue: str | None = None,
    pr: int | None = None,
    cost: str = "0",
    current_step: str | None = "complete",
    steps: tuple[str, ...] = (),
    error: str | None = None,
) -> TaskRun:
    started = T0 + timedelta(hours=hours)
    run = TaskRun(
        role=role,
        status=status,
        issue_number=issue,
        pr_number=pr,
        total_cost_usd=Decimal(cost),
        current_step=current_step,
        started_at=started,
        ended_at=None if status == "running" else started + timedelta(minutes=10),
        error_message=error,
    )
    session.add(run)
    await session.flush()
    for name in steps:
        session.add(StepExecution(task_run_id=run.id, step_name=name, status="done", started_at=started))
    return run


@pytest.fixture
async def runs(session: AsyncSession) -> dict[str, TaskRun]:
    """One run per kind of work, including both ways a developer run reads as Address review."""
    async with session.begin():
        made = {
            "develop": await _add_run(session, role="developer", hours=0, issue="10", cost="1.234", steps=("develop",)),
            "address_steps": await _add_run(
                session, role="developer", hours=1, issue="11", pr=20, cost="0.004", steps=("address_review",)
            ),
            "address_pr_only": await _add_run(
                session, role="developer", status="failed", hours=2, issue="12", pr=21, current_step=None, error="boom"
            ),
            "develop_research_steps": await _add_run(
                session, role="developer", hours=2.5, issue="16", pr=23, steps=("research",)
            ),
            "review": await _add_run(session, role="reviewer", hours=3, issue="11", pr=20, cost="0.50"),
            "legacy_integrate": await _add_run(session, role="command:/integrate-pr 524", hours=-48, pr=524),
            "other_command": await _add_run(session, role="command:health-audit", status="failed", hours=4),
            "rebase": await _add_run(session, role="supervisor:rebase", status="failed", hours=5, issue="13", pr=22),
            "research": await _add_run(session, role="researcher", hours=6, issue="14", steps=("research",)),
            "triage": await _add_run(session, role="triage", hours=7, issue="15"),
            "address_cmd": await _add_run(session, role="command:address-pr", status="stopped", hours=8, issue="11"),
            "running": await _add_run(session, role="developer", status="running", hours=9, issue="17"),
        }
    return made


class TestHistoryRows:
    async def test_rows_carry_the_action_name_and_colour(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, limit=50)
        by_id = {t["id"]: t for t in result["tasks"]}
        expect = {
            "develop": ("develop", "Develop", "blue"),
            "address_steps": ("address-review", "Address review", "blue"),
            "address_pr_only": ("address-review", "Address review", "blue"),
            "develop_research_steps": ("develop", "Develop", "blue"),
            "review": ("review", "Review", "green"),
            "legacy_integrate": ("integrate", "Integrate", "teal"),
            "other_command": ("health-audit", "Health audit", "gray"),
            "rebase": ("rebase", "Rebase", "gray"),
            "research": ("research", "Research", "mauve"),
            "triage": ("triage", "Triage", "yellow"),
            "address_cmd": ("address-review", "Address review", "blue"),
        }
        for name, (key, label, color) in expect.items():
            row = by_id[runs[name].id]
            assert (row["action"], row["action_label"], row["action_color"]) == (key, label, color), name
        assert runs["running"].id not in by_id

    async def test_rows_are_newest_first_by_start_time(self, session: AsyncSession, runs: dict) -> None:
        # The reviewer run started after the develop run but ended at the same offset, and the stopped
        # address-pr run started last: ordering by end time would not give this sequence.
        async with session.begin():
            runs["develop"].ended_at = T0 + timedelta(hours=20)
        result = await get_work_history(session, limit=50)
        starts = [t["started_at"] for t in result["tasks"]]
        assert starts == sorted(starts, reverse=True)
        assert result["tasks"][0]["id"] == runs["address_cmd"].id
        assert result["tasks"][-1]["id"] == runs["legacy_integrate"].id

    async def test_rows_no_longer_carry_step_counts(self, session: AsyncSession, runs: dict) -> None:
        row = (await get_work_history(session, limit=1))["tasks"][0]
        assert "steps_completed" not in row
        assert "total_steps_possible" not in row


class TestHistoryFilters:
    async def test_since_includes_a_run_that_starts_exactly_then(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, since=T0 + timedelta(hours=3), limit=50)
        ids = {t["id"] for t in result["tasks"]}
        assert runs["review"].id in ids
        assert runs["address_pr_only"].id not in ids
        assert result["total"] == 6

    async def test_until_excludes_a_run_that_starts_exactly_then(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, since=T0, until=T0 + timedelta(hours=3), limit=50)
        ids = {t["id"] for t in result["tasks"]}
        assert runs["review"].id not in ids
        assert ids == {runs[n].id for n in ("develop", "address_steps", "address_pr_only", "develop_research_steps")}

    async def test_bounds_in_another_time_zone_mean_the_same_instant(self, session: AsyncSession, runs: dict) -> None:
        cet = timezone(timedelta(hours=1))
        since = (T0 + timedelta(hours=3)).astimezone(cet)
        result = await get_work_history(session, since=since, limit=50)
        assert result["total"] == 6

    @pytest.mark.parametrize(
        ("action", "names"),
        [
            ("develop", {"develop", "develop_research_steps"}),
            ("address-review", {"address_steps", "address_pr_only", "address_cmd"}),
            ("review", {"review"}),
            ("integrate", {"legacy_integrate"}),
            ("rebase", {"rebase"}),
            ("research", {"research"}),
            ("triage", {"triage"}),
            ("health-audit", {"other_command"}),
            ("plan", set()),
        ],
    )
    async def test_action_filter_matches_the_row_action(
        self, session: AsyncSession, runs: dict, action: str, names: set[str]
    ) -> None:
        """The filter runs in SQL and the row's action in Python; they must agree for every kind of run."""
        result = await get_work_history(session, action=action, limit=50)
        assert {t["id"] for t in result["tasks"]} == {runs[n].id for n in names}
        assert all(t["action"] == action for t in result["tasks"])
        assert result["total"] == len(names)

    async def test_action_filter_includes_a_command_named_after_a_registered_key(self, session: AsyncSession) -> None:
        """``command:develop`` reports the registered key "develop", so the Develop filter must return it too."""
        async with session.begin():
            command = await _add_run(session, role="command:develop", hours=1, issue="30")
            developer = await _add_run(session, role="developer", hours=2, issue="31", steps=("develop",))
        result = await get_work_history(session, action="develop", limit=50)
        assert {t["id"] for t in result["tasks"]} == {command.id, developer.id}
        assert all(t["action"] == "develop" for t in result["tasks"])

    @pytest.mark.parametrize("number", ["11", "#11", " 11 "])
    async def test_number_matches_the_issue_or_the_pr(self, session: AsyncSession, runs: dict, number: str) -> None:
        result = await get_work_history(session, number=number, limit=50)
        ids = {t["id"] for t in result["tasks"]}
        assert ids == {runs[n].id for n in ("address_steps", "review", "address_cmd")}

    async def test_number_matches_a_pr_with_no_issue(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, number="524", limit=50)
        assert [t["id"] for t in result["tasks"]] == [runs["legacy_integrate"].id]

    async def test_number_matches_a_tracker_key(self, session: AsyncSession) -> None:
        async with session.begin():
            run = await _add_run(session, role="developer", issue="RHCLOUD-42")
        result = await get_work_history(session, number="RHCLOUD-42", limit=50)
        assert [t["id"] for t in result["tasks"]] == [run.id]

    @pytest.mark.parametrize("number", ["²", "9" * 30, "#", "   "])
    async def test_odd_number_text_never_errors(self, session: AsyncSession, runs: dict, number: str) -> None:
        result = await get_work_history(session, number=number, limit=50)
        expected = 11 if not number.strip().lstrip("#") else 0
        assert result["total"] == expected

    async def test_role_filter_still_works(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, role="reviewer", limit=50)
        assert [t["id"] for t in result["tasks"]] == [runs["review"].id]


class TestHistoryTotals:
    async def test_total_cost_sums_every_filtered_run_not_just_the_page(
        self, session: AsyncSession, runs: dict
    ) -> None:
        result = await get_work_history(session, limit=1)
        assert len(result["tasks"]) == 1
        assert result["total"] == 11
        assert result["total_cost_usd"] == "1.738000"

    async def test_failed_total_ignores_the_status_filter(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, status="done", limit=50)
        assert result["failed_total"] == 3
        assert all(t["status"] == "done" for t in result["tasks"])

    async def test_failed_total_follows_the_other_filters(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, since=T0 + timedelta(hours=3), limit=50)
        assert result["failed_total"] == 2
        result = await get_work_history(session, action="address-review", limit=50)
        assert result["failed_total"] == 1

    async def test_an_empty_window_reports_zero(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, since=T0 + timedelta(days=30), limit=50)
        assert result == {"tasks": [], "total": 0, "total_cost_usd": "0.000000", "failed_total": 0}


class TestHistoryEndpoint:
    async def test_query_parameters_reach_the_service(self, client: AsyncClient, runs: dict) -> None:
        since = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        until = (T0 + timedelta(hours=4)).isoformat()
        resp = await client.get(
            "/api/work/history", params={"since": since, "until": until, "action": "review", "number": "#11"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [t["id"] for t in data["tasks"]] == [runs["review"].id]
        assert data["total"] == 1
        assert data["failed_total"] == 0
        assert data["total_cost_usd"] == "0.500000"

    async def test_default_page_is_twenty_five_rows(self, client: AsyncClient, session: AsyncSession) -> None:
        async with session.begin():
            for i in range(30):
                await _add_run(session, role="triage", hours=i, issue=str(i))
        data = (await client.get("/api/work/history")).json()
        assert len(data["tasks"]) == 25
        assert data["total"] == 30

    async def test_an_unparseable_since_is_a_422(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/history", params={"since": "yesterday"})
        assert resp.status_code == 422


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
