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

from sova.dashboard.services import work_service
from sova.dashboard.services.work_service import get_work_history
from sova.dashboard.services.work_state import RUN_ACTIONS, VARIANT_ACTIONS, run_action, run_actions_payload
from sova.db.models import StepExecution, TaskRun
from sova.db.session import get_session
from tests.conftest import AGENTS_PAGE_JS_DIR, DASHBOARD_DIR

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
    # An unlisted command named after a registered action takes that action's name and colour.
    ("command:develop", "command", "develop", "Develop", "blue", "Developing"),
    ("command:/develop 12", "command", "develop", "Develop", "blue", "Developing"),
    ("command:review", "command", "review", "Review", "green", "Reviewing"),
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

    def test_a_key_has_one_label_and_colour_whatever_role_names_it(self) -> None:
        roles = [r for r, *_ in _TABLE] + [f"command:{k}" for k in RUN_ACTIONS] + list(RUN_ACTIONS)
        for role in roles:
            for variant in (None, "command", "address_review", "developer"):
                action = run_action(role, variant)
                if action.key in RUN_ACTIONS:
                    assert action == RUN_ACTIONS[action.key], (role, variant)

    def test_every_variant_has_a_sql_twin_for_the_action_filter(self) -> None:
        variants = {v for by_variant in VARIANT_ACTIONS.values() for v in by_variant}
        assert variants <= work_service._VARIANT_CLAUSES.keys()

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
        # Develop ends last (T0 + 20h) but started first, so ordering by end time would put it at the top; the
        # stopped address-pr run started last and must lead.
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

    async def test_action_filter_and_row_action_agree_for_every_kind_of_run(
        self, session: AsyncSession, runs: dict
    ) -> None:
        """Every key a row reports, "auto" included, selects exactly the rows that report it, in one colour."""
        async with session.begin():
            await _add_run(session, role="", hours=10, issue="40")
            await _add_run(session, role="command:develop", hours=11, issue="41")
            await _add_run(session, role="command:/review 12", hours=12, issue="42")
        rows = (await get_work_history(session, limit=100))["tasks"]
        assert {"auto", "develop", "review"} <= {t["action"] for t in rows}
        by_key: dict[str, set[int]] = {}
        for t in rows:
            by_key.setdefault(t["action"], set()).add(t["id"])
        for key, ids in by_key.items():
            assert len({(t["action_label"], t["action_color"]) for t in rows if t["action"] == key}) == 1, key
            filtered = await get_work_history(session, action=key, limit=100)
            assert {t["id"] for t in filtered["tasks"]} == ids, key

    @pytest.mark.parametrize("number", ["11", "#11", " 11 "])
    async def test_number_matches_the_issue_or_the_pr(self, session: AsyncSession, runs: dict, number: str) -> None:
        result = await get_work_history(session, number=number, limit=50)
        ids = {t["id"] for t in result["tasks"]}
        assert ids == {runs[n].id for n in ("address_steps", "review", "address_cmd")}

    async def test_number_matches_a_pr_with_no_issue(self, session: AsyncSession, runs: dict) -> None:
        result = await get_work_history(session, number="524", limit=50)
        assert [t["id"] for t in result["tasks"]] == [runs["legacy_integrate"].id]

    @pytest.mark.parametrize("number", ["RHCLOUD-42", "rhcloud-42", "RhCloud-42"])
    async def test_number_matches_a_tracker_key(self, session: AsyncSession, number: str) -> None:
        async with session.begin():
            run = await _add_run(session, role="developer", issue="RHCLOUD-42")
        result = await get_work_history(session, number=number, limit=50)
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

    async def test_the_role_parameter_still_filters_for_other_callers(self, client: AsyncClient, runs: dict) -> None:
        data = (await client.get("/api/work/history", params={"role": "reviewer"})).json()
        assert [t["id"] for t in data["tasks"]] == [runs["review"].id]

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


_HISTORY_HARNESS = r"""
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STATIC_DIR;
// Freeze "now" in 2026 so the year shown on a timestamp (only when it is not the current year) never depends on
// the calendar year the suite runs in.
const RealDate = Date;
const FROZEN_NOW = new RealDate(2026, 10, 12, 18, 0, 0).getTime();
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [FROZEN_NOW])); }
  static now() { return FROZEN_NOW; }
};
const els = new Map();
function el(id) {
  if (!els.has(id)) {
    const listeners = {};
    const classes = new Set();
    const attrs = {};
    els.set(id, {
      id, innerHTML: '', textContent: '', value: '', disabled: false, attrs,
      classList: {
        add: (...c) => c.forEach((x) => classes.add(x)), remove: (...c) => c.forEach((x) => classes.delete(x)),
        toggle: (c, on) => {
          const want = on === undefined ? !classes.has(c) : on;
          if (want) classes.add(c); else classes.delete(c);
        },
        contains: (c) => classes.has(c),
      },
      setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: (k) => attrs[k] ?? null,
      addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
      fire: (type) => (listeners[type] || []).forEach((fn) => fn({ target: els.get(id) })),
    });
  }
  return els.get(id);
}
const bodyListeners = [];
globalThis.window = globalThis;
globalThis.document = {
  hidden: false,
  getElementById: (id) => el(id),
  querySelector: () => null, querySelectorAll: () => [], createElement: () => el('__created'),
  addEventListener() {}, removeEventListener() {},
  body: { addEventListener: (type, fn) => bodyListeners.push(fn) },
};
globalThis.addEventListener = () => {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

const appSrc = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const grab = (name) => {
  const s = appSrc.indexOf('function ' + name + '(');
  return appSrc.slice(s, appSrc.indexOf('\n}\n', s) + 2);
};
new Function(grab('escapeHtml') + grab('renderIfChanged')
  + '; globalThis.escapeHtml = escapeHtml; globalThis.renderIfChanged = renderIfChanged;')();
const bar = '/* ============================================================\n   13. ';
const start = appSrc.indexOf(bar + 'RUN ACTIONS');
const end = appSrc.indexOf(bar + 'STEP PIPELINE BAR', start);
window.SOVA_COLORS = {};
window.SOVA_RUN_ACTIONS = JSON.parse(process.env.RUN_ACTIONS);
(0, eval)(appSrc.slice(start, end) + '; globalThis.runActionColors = runActionColors;');
globalThis.statusDot = (s) => 'dot-' + s;
globalThis.issueLink = (n) => '<a class="issue">#' + n + '</a>';
globalThis.prLink = (n) => (n ? '<a class="pr">PR #' + n + '</a>' : '--');
globalThis.apiUrl = (p) => '/api' + p;
globalThis.showToast = () => {};
const polls = [];
const pollFns = [];
globalThis.visibilityAwarePoll = (fn, ms) => { polls.push(ms); pollFns.push(fn); fn(); return { id: null }; };
const pending = [];
const urls = [];
globalThis.fetchAPI = (url) => new Promise((resolve) => { urls.push(url); pending.push({ url, resolve }); });
const tick = () => new Promise((r) => setTimeout(r, 0));
async function respond(data) { const p = pending.shift(); p.resolve(data); await tick(); await tick(); return p.url; }
function click(action, args) {
  const attrs = { 'data-action': action, 'data-args': args ? JSON.stringify(args) : null };
  const node = { getAttribute: (n) => attrs[n] ?? null };
  bodyListeners.forEach((fn) => fn({ target: { closest: () => node }, stopPropagation() {} }));
}
const q = (url) => Object.fromEntries(new URL('http://x' + url).searchParams);
const localMidnight = (days) => {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + days);
  return d.toISOString();
};

const dir = path.join(staticDir, 'js', 'pages', 'agents');
const actions = await import(pathToFileURL(path.join(dir, 'actions.js')).href);
const history = await import(pathToFileURL(path.join(dir, 'history.js')).href);
actions.init();
history.init();

const page = (over) => Object.assign({ total: 60, failed_total: 4, total_cost_usd: '412.5', tasks: [
  { id: 9, issue_number: '1452', pr_number: 1457, role: 'developer', action: 'address-review',
    action_label: 'Address review', action_color: 'blue', status: 'done', total_cost_usd: '1234.834787',
    duration_formatted: '11m 11s', error_message: null,
    started_at: '2026-11-12T15:29:13+00:00', ended_at: '2026-11-12T15:40:24+00:00' },
  { id: 8, issue_number: null, pr_number: 1451, role: 'command:integrate-pr', action: 'integrate',
    action_label: 'Integrate',
    action_color: 'teal', status: 'done', total_cost_usd: '0.003', duration_formatted: '3m 37s', error_message: null,
    started_at: '2025-12-31T09:00:00+00:00', ended_at: '2025-12-31T09:03:37+00:00' },
  { id: 7, issue_number: '1453', pr_number: null, role: 'developer', action: 'develop', action_label: 'Develop',
    action_color: 'blue', status: 'failed', total_cost_usd: '0', duration_formatted: null,
    error_message: 'Timed out <after> 1500s', started_at: '2026-11-12T10:00:00+00:00', ended_at: null },
] }, over || {});

const out = {};
out.polls = polls;
out.first = q(await respond(page()));
out.since7 = localMidnight(-6);
const table = el('history-table').innerHTML;
out.table = table;
out.failedCount = el('tab-failed-count').textContent;
out.totals = el('history-totals').innerHTML;
out.pageInfo = el('history-page-info').textContent;
out.actionOptions = el('filter-action').innerHTML;

click('history-next');
out.next = q(await respond(page({ tasks: page().tasks.slice(0, 1) })));
// Changing a filter drops the old total: Next must not advance on it while the new result is pending.
el('filter-action').value = 'integrate'; el('filter-action').fire('change');
out.pagerDisabledWhileLoading = el('history-next').disabled && el('history-prev').disabled;
click('history-next');
out.requestsAfterNextWhileLoading = pending.length;
await respond(page({ total: 1, tasks: page().tasks.slice(0, 1) }));
el('filter-action').value = ''; el('filter-action').fire('change');
await respond(page());
click('history-next');
await respond(page({ tasks: page().tasks.slice(0, 1) }));
const tableOnPage2 = el('history-table').innerHTML;
history.refreshHistoryAfterRun();
await new Promise((r) => setTimeout(r, 400));
out.checkNew = q(await respond(page({ total: 61, tasks: page().tasks.slice(0, 1) })));
out.newRunsHidden = el('history-new-runs').classList.contains('hidden');
out.newRunsText = el('history-new-runs-btn').textContent;
out.page2Unchanged = el('history-table').innerHTML === tableOnPage2;
// The 60 s poll on a later page takes the same probe path: it must not re-fetch the page and shift its rows.
pollFns[0]();
out.pollOnPage2 = q(await respond(page({ total: 61, tasks: page().tasks.slice(0, 1) })));
out.pollPage2Unchanged = el('history-table').innerHTML === tableOnPage2;
out.pollNewRunsVisible = !el('history-new-runs').classList.contains('hidden');
click('history-first-page');
out.backToFirst = q(await respond(page({ total: 61 })));
out.newRunsHiddenAfter = el('history-new-runs').classList.contains('hidden');

history.refreshHistoryAfterRun();
await new Promise((r) => setTimeout(r, 400));
out.refreshPage1 = q(await respond(page({ total: 61 })));

click('switch-tab', ['failed']);
out.failedTab = q(await respond(page({ total: 4, tasks: page().tasks.slice(2) })));
out.statusHiddenOnFailed = el('filter-status').classList.contains('hidden');
out.failedTable = el('history-table').innerHTML;
out.failedTotals = el('history-totals').innerHTML;
click('switch-tab', ['history']);
await respond(page());

el('filter-action').value = 'integrate'; el('filter-action').fire('change');
out.action = q(await respond(page()));
el('filter-number').value = ' #11 '; el('filter-number').fire('input');
await new Promise((r) => setTimeout(r, 400));
out.number = q(await respond(page()));
out.clearShown = !el('history-clear').classList.contains('hidden');

click('history-range', ['today']);
out.today = q(await respond(page()));
out.todayStart = localMidnight(0);
out.todayPressed = el('history-range-today').getAttribute('aria-pressed');
click('history-range', ['all']);
out.all = q(await respond(page()));
click('history-range', ['custom']);
out.customShown = !el('history-custom-range').classList.contains('hidden');
await respond(page());
el('history-from').value = '2026-11-02'; el('history-from').fire('change');
await respond(page());
el('history-to').value = '2026-11-04'; el('history-to').fire('change');
out.custom = q(await respond(page()));
out.customSince = new Date(2026, 10, 2).toISOString();
out.customUntil = new Date(2026, 10, 5).toISOString();

// A slow answer for an old filter must not overwrite the table the newer filter drew.
el('filter-action').value = 'review'; el('filter-action').fire('change');
el('filter-action').value = 'triage'; el('filter-action').fire('change');
const stale = pending.shift();
const relabel = (label, key) => [Object.assign({}, page().tasks[0], { action_label: label, action: key })];
await respond(page({ total: 1, tasks: relabel('Triage', 'triage') }));
stale.resolve(page({ total: 2, tasks: relabel('Review', 'review') }));
await tick(); await tick();
const shown = el('history-table').innerHTML;
out.staleDropped = shown.includes('Triage') && !shown.includes('>Review<');

click('history-clear');
out.cleared = q(await respond(page()));
out.actionAfterClear = el('filter-action').value;
out.pending = pending.length;
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def history_page(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The history.js harness run once: what the page requested and rendered."""
    _require_node()
    env = {
        "STATIC_DIR": str(_STATIC),
        "RUN_ACTIONS": json.dumps(run_actions_payload()),
        "TZ": "Europe/Prague",
        "LC_ALL": "en_US.UTF-8",
    }
    return _run_node(tmp_path_factory.mktemp("history"), _HISTORY_HARNESS, env=env)  # type: ignore[return-value]


class TestHistoryTableInTheBrowser:
    def test_first_load_asks_for_the_last_seven_days_twenty_five_rows(self, history_page: dict) -> None:
        assert history_page["first"] == {"limit": "25", "offset": "0", "since": history_page["since7"]}
        assert history_page["polls"] == [60000]

    def test_columns_and_no_steps(self, history_page: dict) -> None:
        table = history_page["table"]
        headers = re.findall(r"<th[^>]*>([^<]*)</th>", table)
        assert headers == ["When", "Issue", "Action", "Status", "PR", "Duration", "Cost", ""]
        assert "Steps" not in table

    def test_when_shows_local_time_and_hovering_shows_start_and_end(self, history_page: dict) -> None:
        table = history_page["table"]
        assert "Nov 12, 16:29" in table
        assert "Dec 31, 2025, 10:00" in table
        tip = re.search(r'data-tooltip="(Started[^"]*)"', table)
        assert tip
        assert "Ended" in tip.group(1)
        assert "16:40:24" in tip.group(1)
        assert "data-tooltip-multiline" in table
        assert "Ended: still running" not in table
        assert "Ended: not recorded" in table

    def test_costs_have_two_decimals_and_sub_cent_reads_less_than_a_cent(self, history_page: dict) -> None:
        table = history_page["table"]
        assert "$1,234.83" in table
        assert "&lt;$0.01" in table
        assert "$0.00" in table
        assert "1234.83" not in table

    def test_action_is_a_pill_in_its_colour_and_status_is_a_dot(self, history_page: dict) -> None:
        table = history_page["table"]
        assert 'text-accent-teal">Integrate</a>' in table
        assert 'text-accent">Address review</a>' in table
        assert "dot-done" in table
        assert '<a href="/work/9" data-action="stop"' in table
        assert re.search(r'text-accent-red[^"]*">Failed<', table)

    def test_the_failed_error_row_wraps_a_long_unbroken_token_inside_the_table(self) -> None:
        """`break-words` does not lower an auto-layout cell's min-content width; `anywhere` does."""
        source = (_STATIC / "js" / "pages" / "agents" / "history.js").read_text(encoding="utf-8")
        assert "[overflow-wrap:anywhere]" in source
        assert "break-words" not in source
        css = (_STATIC / "tailwind.min.css").read_text(encoding="utf-8")
        assert "overflow-wrap:anywhere" in css

    def test_history_shows_the_error_on_hover_escaped(self, history_page: dict) -> None:
        assert 'data-tooltip="Timed out &lt;after&gt; 1500s"' in history_page["table"]

    def test_rerun_only_on_the_failed_row(self, history_page: dict) -> None:
        assert history_page["table"].count('data-action="rerun"') == 1

    def test_totals_failed_count_and_pager_on_first_load(self, history_page: dict) -> None:
        assert history_page["failedCount"] == "(4)"
        assert "60" in history_page["totals"]
        assert "$412.50" in history_page["totals"]
        assert history_page["pageInfo"] == "1 to 3 of 60"

    def test_action_filter_options_come_from_the_table(self, history_page: dict) -> None:
        for action in RUN_ACTIONS.values():
            assert f'<option value="{action.key}">{action.label}</option>' in history_page["actionOptions"]

    def test_a_later_page_does_not_move_when_a_run_finishes(self, history_page: dict) -> None:
        assert history_page["next"]["offset"] == "25"

    def test_changing_a_filter_disables_the_pager_until_the_new_total_arrives(self, history_page: dict) -> None:
        assert history_page["pagerDisabledWhileLoading"] is True
        assert history_page["requestsAfterNextWhileLoading"] == 1
        assert history_page["checkNew"]["limit"] == "1"
        assert history_page["checkNew"]["offset"] == "0"
        assert history_page["page2Unchanged"] is True
        assert history_page["pollOnPage2"]["limit"] == "1"
        assert history_page["pollOnPage2"]["offset"] == "0"
        assert history_page["pollPage2Unchanged"] is True
        assert history_page["pollNewRunsVisible"] is True
        assert history_page["newRunsHidden"] is False
        assert history_page["newRunsText"] == "1 run finished since this page loaded, back to page 1"
        assert history_page["backToFirst"]["offset"] == "0"
        assert history_page["newRunsHiddenAfter"] is True

    def test_page_one_refreshes_when_a_run_finishes(self, history_page: dict) -> None:
        assert history_page["refreshPage1"]["offset"] == "0"
        assert history_page["refreshPage1"]["limit"] == "25"

    def test_failed_tab_uses_the_same_filters_and_prints_errors(self, history_page: dict) -> None:
        assert history_page["failedTab"]["status"] == "failed"
        assert history_page["failedTab"]["since"] == history_page["since7"]
        assert history_page["statusHiddenOnFailed"] is True
        failed_table = history_page["failedTable"]
        assert "Timed out &lt;after&gt; 1500s" in failed_table
        assert "<after>" not in failed_table
        assert "failed runs" in history_page["failedTotals"]

    def test_filters_reach_the_query(self, history_page: dict) -> None:
        assert history_page["action"]["action"] == "integrate"
        assert history_page["action"]["offset"] == "0"
        assert history_page["number"]["number"] == "11"
        assert history_page["clearShown"] is True

    def test_date_presets_and_custom_range_use_local_midnights(self, history_page: dict) -> None:
        assert history_page["today"]["since"] == history_page["todayStart"]
        assert history_page["todayPressed"] == "true"
        assert "since" not in history_page["all"]
        assert history_page["customShown"] is True
        assert history_page["custom"]["since"] == history_page["customSince"]
        assert history_page["custom"]["until"] == history_page["customUntil"]

    def test_a_stale_answer_is_dropped(self, history_page: dict) -> None:
        assert history_page["staleDropped"] is True

    def test_clear_filters_returns_to_the_last_seven_days(self, history_page: dict) -> None:
        assert history_page["cleared"] == {"limit": "25", "offset": "0", "since": history_page["since7"]}
        assert history_page["actionAfterClear"] == ""
        assert history_page["pending"] == 0


class TestHistoryMarkup:
    async def test_new_runs_button_hides_through_a_wrapper(self, client: AsyncClient) -> None:
        """.sova-btn sets display: inline-flex, which beats Tailwind's .hidden on the same element, so the
        empty button showed as a blank pill; the class that hides it sits on a wrapper."""
        html = (await client.get("/agents")).text
        wrapper = html[html.index('id="history-new-runs"') :]
        assert 'class="hidden"' in wrapper[: wrapper.index(">")]
        button = html[html.index('id="history-new-runs-btn"') :]
        assert "hidden" not in button[: button.index(">")]


class TestHistoryWiring:
    def test_a_finished_run_refreshes_the_history(self) -> None:
        source = (AGENTS_PAGE_JS_DIR / "live-status.js").read_text(encoding="utf-8")
        assert "import { refreshHistoryAfterRun } from './history.js';" in source
        branch = source[source.index("if (msg.type === 'agent_finished') {") :]
        branch = branch[: branch.index("    return;\n  }")]
        assert "refreshHistoryAfterRun();" in branch

    def test_multiline_tooltip_variant_exists(self) -> None:
        css = (_STATIC / "style.css").read_text(encoding="utf-8")
        assert re.search(r"\[data-tooltip\]\[data-tooltip-multiline\]::after\s*\{[^}]*white-space:\s*pre", css)
