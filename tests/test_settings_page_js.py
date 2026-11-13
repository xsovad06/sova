"""Behavior of the Settings page's row module (static/js/pages/settings/rows.js, #1463).

Saving one setting re-renders only the rows bound to that key (its row in a section or in the results, and its
Overview copy), never the page and never a status widget; a save answered with an HTTP error shows the server's
message on the row; Reset to default deletes the stored value and re-renders the row from the answer.

The tests run the shipped app.js and the shipped page modules under node, against a small fake DOM: an element
is found by its attributes (data-key, data-setting-key, ...), and a row's parts (info, control, meta, msg) are
child elements whose innerHTML is recorded rather than parsed. Every fetch is logged, so a widget reload would show.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_STATIC = Path(__file__).parent.parent / "sova" / "dashboard" / "static"
APP_JS = _STATIC / "app.js"
ROWS_JS = _STATIC / "js" / "pages" / "settings" / "rows.js"
STATE_JS = _STATIC / "js" / "pages" / "settings" / "state.js"

_HARNESS = Path(__file__).parent / "fixtures" / "settings_page_harness.mjs"


def _require_node() -> None:
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail("node is required in CI to run the Settings page modules")
    pytest.skip("node is required to run the Settings page modules")


def _setting(key: str, **extra: object) -> dict:
    base = {
        "key": key,
        "label": key,
        "description": "",
        "value": None,
        "value_type": "string",
        "requires_restart": False,
        "options": [],
        "options_source": "",
        "default": None,
        "stored": False,
        "env_var": "",
    }
    return {**base, **extra}


_PAYLOAD = {
    "groups": [
        {
            "id": "agent",
            "label": "Agent",
            "settings": [
                _setting(
                    "agent.max_budget", label="Max budget (USD)", value_type="number", value="10.00", default="10.00"
                ),
                _setting("agent.model", label="Model", value="opus", default="opus", options_source="llm"),
                _setting("llm.provider", label="Provider", value="claude-code", default="claude-code"),
                _setting("max_parallel_agents", label="Agent limit", value_type="number", value=3, default=3),
                _setting("task_source.jira_api_token", label="JIRA API token", value_type="secret", value="••••••••"),
            ],
        },
        {
            "id": "supervisor",
            "label": "Supervisor",
            "settings": [
                _setting(
                    "supervisor.auto_triage", label="Auto-triage", value_type="boolean", value=False, default=False
                ),
                _setting("supervisor.enabled", label="Supervisor", value_type="boolean", value=False, default=False),
            ],
        },
    ],
    "leftover": [],
}


def _run(script: str, *, post: dict | None = None, reset: dict | None = None, **extra: object) -> dict:
    _require_node()
    scenario = {
        "script": script,
        "payload": extra.pop("payload", _PAYLOAD),
        "post": post or {"status": 200, "body": json.dumps({"status": "ok"})},
        "reset": reset or {"status": 200, "body": json.dumps({"status": "ok"})},
        **extra,
    }
    proc = subprocess.run(
        ["node", str(_HARNESS), str(APP_JS), str(ROWS_JS), str(STATE_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# A row for agent.max_budget in its section and in the search results, and a row of another key.
_TEXT_ROWS = """
const sectionRow = el({'data-key': 'agent.max_budget', 'data-ctx': 'row'});
const resultRow = el({'data-key': 'agent.max_budget', 'data-ctx': 'row'});
const other = el({'data-key': 'agent.model', 'data-ctx': 'row'});
const widget = el({'data-widget': 'installation'});
"""


class TestSaveRerendersOnlyItsRows:
    def test_a_saved_text_row_rerenders_every_row_of_its_key_and_nothing_else(self) -> None:
        out = _run(
            _TEXT_ROWS
            + """
            const saved = await rows.saveRow('agent.max_budget', '25');
            return {saved, section: snap(sectionRow), result: snap(resultRow), other: snap(other), widget: snap(widget),
                    value: state.setting('agent.max_budget').value, stored: state.setting('agent.max_budget').stored};
            """,
            readBack=25,
        )
        r = out["result"]
        assert r["saved"]["ok"] is True
        assert (r["value"], r["stored"]) == (25, True)
        for row in (r["section"], r["result"]):
            assert "Changed" in row["parts"]["info"]["html"]
            assert "Reset to default" in row["parts"]["meta"]["html"]
            assert 'value="25"' in row["parts"]["control"]["html"]
            assert row["parts"]["msg"]["text"] == "Saved"
        assert r["other"]["parts"] == {}
        assert r["widget"]["renders"] == 0
        assert out["fetches"] == ["POST /api/settings/config", "GET /api/settings/config"]

    def test_a_switch_saved_in_the_overview_updates_its_row_and_no_widget(self) -> None:
        out = _run(
            """
            const row = el({'data-key': 'supervisor.auto_triage', 'data-ctx': 'row'});
            const overview = el({'data-key': 'supervisor.auto_triage', 'data-ctx': 'overview'});
            overview.querySelector('[data-part="dot"]').hidden = true;
            const host = el({'data-setting-switch': 'supervisor.auto_triage', 'data-label': 'Auto-triage'});
            overview.appendChild(host);
            const rowHost = el({'data-setting-switch': 'supervisor.auto_triage', 'data-label': 'Auto-triage'});
            row.querySelector('[data-part="control"]').appendChild(rowHost);
            const widget = el({'data-widget': 'health'});
            rows.mountControls(overview);
            rows.mountControls(row);
            const overviewInput = host.children[0].children[0];
            const rowInput = rowHost.children[0].children[0];
            await fire(overviewInput, 'change');
            return {row: snap(row), overview: snap(overview), widget: snap(widget), rowChecked: rowInput.checked,
                    stored: state.setting('supervisor.auto_triage').stored};
            """,
        )
        r = out["result"]
        assert r["stored"] is True
        assert r["rowChecked"] is True, "the row's switch did not follow the Overview's"
        assert "Changed" in r["row"]["parts"]["info"]["html"]
        assert r["row"]["parts"]["control"]["renders"] == 0, "a shared switch is told the value, not rebuilt"
        assert r["overview"]["parts"]["dot"]["hidden"] is False
        assert r["widget"]["renders"] == 0
        assert out["fetches"] == ["POST /api/settings/config", "GET /api/settings/config"]


class TestSavesOfOneKeyAreOrdered:
    def test_a_second_save_waits_for_the_first_read_back(self) -> None:
        """Two quick saves of one key must not have their read-backs answered out of order."""
        out = _run(
            """
            const first = saveSetting('max_parallel_agents', 4);
            const second = saveSetting('max_parallel_agents', 5);
            const results = await Promise.all([first, second]);
            return results.map((r) => r.ok);
            """,
            readBack=5,
        )
        assert out["result"] == [True, True]
        assert out["fetches"] == [
            "POST /api/settings/config",
            "GET /api/settings/config",
            "POST /api/settings/config",
            "GET /api/settings/config",
        ]

    def test_a_failed_save_does_not_block_the_next_one(self) -> None:
        out = _run(
            """
            const first = await saveSetting('max_parallel_agents', 'x');
            const second = await saveSetting('max_parallel_agents', 'y');
            return [first.ok, second.ok];
            """,
            post={"status": 422, "body": json.dumps({"detail": "no"})},
        )
        assert out["result"] == [False, False]


class TestSaveErrors:
    def test_an_http_error_with_only_detail_shows_on_the_row(self) -> None:
        detail = "agent.max_budget: Input should be a valid number"
        out = _run(
            _TEXT_ROWS
            + """
            const saved = await rows.saveRow('agent.max_budget', 'abc');
            return {saved, section: snap(sectionRow), result: snap(resultRow),
                    value: state.setting('agent.max_budget').value};
            """,
            post={"status": 422, "body": json.dumps({"detail": detail})},
        )
        r = out["result"]
        assert r["saved"]["ok"] is False
        assert r["value"] == "10.00"
        for row in (r["section"], r["result"]):
            assert row["parts"]["msg"]["text"] == "Not saved. " + detail
            assert "sova-setting-msg-err" in row["parts"]["msg"]["cls"]
            assert "control" not in row["parts"], "a failed save keeps what the user typed"
        assert out["fetches"] == ["POST /api/settings/config"]

    def test_an_error_in_a_200_answer_shows_on_the_row(self) -> None:
        out = _run(
            _TEXT_ROWS + "await rows.saveRow('agent.max_budget', 'abc'); return snap(sectionRow);",
            post={"status": 200, "body": json.dumps({"error": "'agent.max_budget' expects a number, got 'abc'"})},
        )
        assert out["result"]["parts"]["msg"]["text"] == "Not saved. 'agent.max_budget' expects a number, got 'abc'"


class TestListValues:
    def test_an_entry_holding_a_comma_is_shown_as_a_json_array(self) -> None:
        """Splitting the text on commas at save time would cut a regex such as \\d{1,3} in two."""
        out = _run(
            """
            return {
              plain: rows.controlHtml({key: 'a.list', label: 'A', value_type: 'list', value: ['x', 'y']}),
              comma: rows.controlHtml({key: 'a.list', label: 'A', value_type: 'list', value: ['\\\\d{1,3}', 'y']}),
            };
            """,
        )
        assert 'value="x, y"' in out["result"]["plain"]
        assert 'value="[&quot;' in out["result"]["comma"]


class TestModelFields:
    def test_a_provider_change_rebuilds_every_model_field(self) -> None:
        """A dropdown left from the old provider would offer models the new one cannot serve."""
        out = _run(
            """
            const modelRow = el({'data-key': 'agent.model', 'data-ctx': 'row'});
            const other = el({'data-key': 'agent.max_budget', 'data-ctx': 'row'});
            await rows.saveRow('llm.provider', 'openai');
            return {model: snap(modelRow), other: snap(other)};
            """,
        )
        assert out["result"]["model"]["parts"]["control"]["renders"] == 1
        assert out["result"]["other"]["parts"] == {}

    def test_a_model_field_being_edited_keeps_what_the_user_typed(self) -> None:
        """Rebuilding the row from the stored value would discard text typed into another model field."""
        out = _run(
            """
            const modelRow = el({'data-key': 'agent.model', 'data-ctx': 'row'});
            modelRow.classList.add('sova-setting-dirty');
            await rows.saveRow('llm.provider', 'openai');
            return {model: snap(modelRow), dirty: modelRow.classList.contains('sova-setting-dirty')};
            """,
        )
        assert out["result"]["model"]["parts"] == {}
        assert out["result"]["dirty"] is True


class TestSecrets:
    def test_an_empty_secret_is_refused_on_the_row_without_a_request(self) -> None:
        """Saving nothing would delete the stored credential; Reset to default is how to remove one."""
        out = _run(
            """
            const row = el({'data-key': 'task_source.jira_api_token', 'data-ctx': 'row'});
            const saved = await rows.saveRow('task_source.jira_api_token', '  ');
            return {saved, row: snap(row)};
            """,
        )
        r = out["result"]
        assert r["saved"]["ok"] is False
        assert "Reset to default" in r["row"]["parts"]["msg"]["text"]
        assert out["fetches"] == []


class TestReset:
    def test_reset_rerenders_the_row_from_the_answer(self) -> None:
        out = _run(
            _TEXT_ROWS
            + """
            state.recordSaved('agent.max_budget', 25);
            await rows.resetRow('agent.max_budget');
            return {section: snap(sectionRow), value: state.setting('agent.max_budget').value,
                    stored: state.setting('agent.max_budget').stored};
            """,
            reset={"status": 200, "body": json.dumps({"status": "ok", "key": "agent.max_budget", "value": "10.00"})},
        )
        r = out["result"]
        assert (r["value"], r["stored"]) == ("10.00", False)
        assert "Changed" not in r["section"]["parts"]["info"]["html"]
        assert "Reset to default" not in r["section"]["parts"]["meta"]["html"]
        assert r["section"]["parts"]["msg"]["text"] == "Reset. The default applies again."
        assert out["fetches"] == ["POST /api/settings/config/reset"]

    def test_a_refused_reset_shows_the_detail(self) -> None:
        detail = "'agent.max_budget' is set by SOVA_AGENT_MAX_BUDGET in the server environment. Change it there."
        out = _run(
            _TEXT_ROWS + "await rows.resetRow('agent.max_budget'); return snap(sectionRow);",
            reset={"status": 409, "body": json.dumps({"detail": detail})},
        )
        assert out["result"]["parts"]["msg"]["text"] == "Not reset. " + detail


class TestRowMarkup:
    def test_an_environment_value_has_no_control_and_no_reset(self) -> None:
        out = _run(
            """
            const s = Object.assign({}, state.setting('agent.model'),
              {env_var: 'SOVA_AGENT_MODEL', stored: true, value: 'x'});
            return {control: rows.controlHtml(s), badges: rows.badgesHtml(s), row: rows.rowHtml(s, {})};
            """,
        )
        r = out["result"]
        assert "data-act" not in r["control"]
        assert "Environment" in r["badges"]
        assert "SOVA_AGENT_MODEL" in r["row"]
        assert "Reset to default" not in r["row"]

    def test_switches_and_the_agent_limit_use_the_shared_controls(self) -> None:
        out = _run(
            """
            return {
              bool: rows.controlHtml(state.setting('supervisor.auto_triage')),
              limit: rows.controlHtml(
                {key: 'max_parallel_agents', label: 'Max parallel agents', value_type: 'number', value: 2}),
            };
            """,
        )
        assert 'data-setting-switch="supervisor.auto_triage"' in out["result"]["bool"]
        assert 'data-setting-stepper="max_parallel_agents"' in out["result"]["limit"]


_SWITCH = """
const row = el({'data-key': 'supervisor.enabled', 'data-ctx': 'row'});
const overview = el({'data-key': 'supervisor.enabled', 'data-ctx': 'overview'});
const host = el({'data-setting-switch': 'supervisor.enabled', 'data-label': 'Supervisor'});
overview.appendChild(host);
rows.mountControls(overview);
const input = host.children[0].children[0];
await fire(input, 'change');
"""

_STEPPER = """
const row = el({'data-key': 'max_parallel_agents', 'data-ctx': 'row'});
const overview = el({'data-key': 'max_parallel_agents', 'data-ctx': 'overview'});
const host = el({'data-setting-stepper': 'max_parallel_agents', 'data-label': 'Agent limit',
  'data-min': '1', 'data-step': '1'});
overview.appendChild(host);
rows.mountControls(overview);
await fire(host.children[0].children[2], 'click');
"""


class TestSharedControlMessages:
    """A switch and the agent limit are app.js's shared controls: their result lands on the row, not only a toast."""

    @pytest.mark.parametrize("control", [_SWITCH, _STEPPER], ids=["switch", "stepper"])
    def test_an_http_error_with_only_detail_shows_on_the_row_and_the_overview_copy(self, control: str) -> None:
        detail = "Input should be a valid boolean"
        out = _run(
            control + "return {row: snap(row), overview: snap(overview)};",
            post={"status": 422, "body": json.dumps({"detail": detail})},
        )
        for copy in (out["result"]["row"], out["result"]["overview"]):
            assert copy["parts"]["msg"]["text"] == "Not saved. " + detail
            assert "sova-setting-msg-err" in copy["parts"]["msg"]["cls"]

    def test_a_refused_switch_goes_back_to_the_stored_value(self) -> None:
        out = _run(
            _SWITCH + "return {checked: input.checked, value: state.setting('supervisor.enabled').value};",
            post={"status": 422, "body": json.dumps({"detail": "refused"})},
        )
        assert out["result"] == {"checked": False, "value": False}

    def test_a_refused_stepper_goes_back_to_the_stored_value(self) -> None:
        out = _run(
            _STEPPER + "return {shown: host.children[0].children[1].textContent, "
            "value: state.setting('max_parallel_agents').value};",
            post={"status": 422, "body": json.dumps({"detail": "refused"})},
        )
        assert out["result"] == {"shown": "3", "value": 3}

    def test_a_lifecycle_error_in_a_200_answer_is_shown_on_the_row_not_as_saved(self) -> None:
        out = _run(
            _SWITCH + "return {row: snap(row), overview: snap(overview)};",
            post={"status": 200, "body": json.dumps({"status": "ok", "lifecycle_error": "Supervisor failed to start"})},
        )
        for copy in (out["result"]["row"], out["result"]["overview"]):
            assert copy["parts"]["msg"]["text"] == "Saved. Supervisor failed to start"
            assert "sova-setting-msg-warn" in copy["parts"]["msg"]["cls"]

    def test_a_restart_note_comes_from_the_answer_too(self) -> None:
        out = _run(
            _STEPPER + "return snap(row);",
            post={"status": 200, "body": json.dumps({"status": "ok", "restart_required": True})},
        )
        assert out["result"]["parts"]["msg"]["text"] == "Saved. Restart the server to apply it."

    def test_a_plain_save_says_saved(self) -> None:
        out = _run(_SWITCH + "return snap(row);")
        assert out["result"]["parts"]["msg"]["text"] == "Saved"
        assert "sova-setting-msg-ok" in out["result"]["parts"]["msg"]["cls"]


class TestChangedAndSearch:
    def test_changed_only_lists_exactly_the_stored_settings_and_ignores_the_environment(self) -> None:
        payload = json.loads(json.dumps(_PAYLOAD))
        agent = payload["groups"][0]["settings"]
        agent[0]["stored"] = True
        agent[1]["env_var"] = "SOVA_AGENT_MODEL"
        out = _run(
            """
            state.view.changedOnly = true;
            const listed = state.allSettings().filter(state.isChanged).map((s) => s.key);
            return {listed, totals: state.totals()};
            """,
            payload=payload,
        )
        assert out["result"]["listed"] == ["agent.max_budget"]
        assert out["result"]["totals"]["stored"] == 1
        assert out["result"]["totals"]["env"] == 1

    def test_unreadable_stored_rows_list_nothing_as_changed(self) -> None:
        payload = json.loads(json.dumps(_PAYLOAD))
        payload["groups"][0]["settings"][0]["stored"] = True
        payload["stored_unreadable"] = True
        out = _run(
            f"""
            state.view.changedOnly = true;
            state.setPayload({json.dumps(payload)});
            return {{changed: state.allSettings().filter(state.isChanged).length, only: state.view.changedOnly,
                    totals: state.totals(), unreadable: state.isStoredUnreadable()}};
            """,
            payload=payload,
        )
        r = out["result"]
        assert (r["changed"], r["only"], r["unreadable"]) == (0, False, True)

    def test_a_stored_setting_the_provider_makes_inactive_is_still_listed_and_flagged(self) -> None:
        payload = json.loads(json.dumps(_PAYLOAD))
        payload["groups"][0]["settings"].append(
            _setting("llm.api_key", label="API key", value_type="secret", value="x", stored=True)
        )
        out = _run(
            """
            state.view.changedOnly = true;
            const group = state.groupById('agent');
            return state.resultsIn(group).map((r) => [r.setting.key, r.inactive]);
            """,
            payload=payload,
        )
        assert out["result"] == [["llm.api_key", True]]

    def test_an_unstored_inactive_setting_stays_out_of_the_results(self) -> None:
        payload = json.loads(json.dumps(_PAYLOAD))
        payload["groups"][0]["settings"].append(_setting("llm.api_key", label="API key", value_type="secret"))
        out = _run(
            """
            state.view.query = 'api key';
            return state.resultsIn(state.groupById('agent')).length;
            """,
            payload=payload,
        )
        assert out["result"] == 0

    def test_a_setting_without_a_label_does_not_match_the_word_undefined(self) -> None:
        out = _run(
            """
            return [state.matches({key: 'a.b'}, ['undefined']), state.matches({key: 'a.b'}, ['a.b']),
                    state.matches({label: 'L', key: 'k', description: 'about timeouts'}, ['timeouts'])];
            """
        )
        assert out["result"] == [False, True, True]

    def test_objects_with_the_same_content_in_another_key_order_are_the_same_value(self) -> None:
        out = _run(
            """
            return [state.sameValue({a: 1, b: {c: 2, d: [1, {x: 1, y: 2}]}}, {b: {d: [1, {y: 2, x: 1}], c: 2}, a: 1}),
                    state.sameValue({a: 1}, {a: 2}), state.sameValue([1, 2], [2, 1]), state.sameValue(null, undefined)];
            """
        )
        assert out["result"] == [True, False, False, False]

    def test_every_setting_of_the_real_payload_is_reachable_by_search(self, tmp_path: Path) -> None:
        """Search finds each setting by its key, and one the provider or runtime hides still shows once stored."""
        from fastapi.encoders import jsonable_encoder

        from sova.dashboard.services.settings_service import get_grouped_settings
        from sova.dashboard.settings_meta import GROUP_ORDER, SECTIONS

        assert sorted(g for groups in SECTIONS.values() for g in groups) == sorted(GROUP_ORDER), "one section each"
        payload = jsonable_encoder(get_grouped_settings(tmp_path))
        sections = [{"id": sid, "label": sid, "groups": list(groups)} for sid, groups in SECTIONS.items()]
        out = _run(
            f"""
            state.setSections({json.dumps(sections)});
            const found = (key) => state.getSections().some((sec) => sec.groups.some((gid) => {{
              const g = state.groupById(gid);
              return g && state.resultsIn(g).some((r) => r.setting.key === key);
            }}));
            const hidden = state.hiddenKeys();
            const unreachable = [];
            for (const s of state.allSettings()) {{
              state.view.query = s.key;
              if (!hidden.has(s.key) && !found(s.key)) unreachable.push(s.key);
              if (hidden.has(s.key)) {{
                s.stored = true;
                if (!found(s.key)) unreachable.push(s.key + ' (stored)');
                s.stored = false;
              }}
            }}
            return {{unreachable, count: state.allSettings().length, hidden: [...hidden]}};
            """,
            payload=payload,
        )
        assert out["result"]["unreachable"] == []
        assert out["result"]["count"] > 300
        assert out["result"]["hidden"], "the check of a stored, hidden setting never ran"


_MODEL_ROW = """
const row = el({'data-key': 'agent.model', 'data-ctx': 'row'});
const input = el({'data-act': 'text'});
row.querySelector('[data-part="control"]').appendChild(input);
const root = el({});
root.appendChild(row);
"""

_LIVE = {
    "status": 200,
    "body": json.dumps({"source": "live", "models": [{"id": "claude-opus-5-5", "family": "claude"}]}),
}


class TestModelFieldsFailOpen:
    def test_a_failed_or_curated_model_list_leaves_the_text_box_and_is_not_cached(self) -> None:
        """Every mount asks again: a failure or a curated answer must not poison the list for the next one."""
        out = _run(
            _MODEL_ROW
            + """
            const kept = [];
            for (let i = 0; i < 2; i++) {
              rows.mountControls(root);
              await settle();
              kept.push(input.replacedWith === undefined);
            }
            rows.mountControls(root);
            await settle();
            return {kept, upgraded: input.replacedWith !== undefined};
            """,
            models=[
                {"status": 500, "body": "{}"},
                {"status": 200, "body": json.dumps({"source": "curated", "models": [{"id": "x", "family": "claude"}]})},
                _LIVE,
            ],
        )
        r = out["result"]
        assert r["kept"] == [True, True]
        assert r["upgraded"] is True
        asked = [f for f in out["fetches"] if "/models/available" in f]
        assert len(asked) == 3, "a failed fetch was served from the cache"

    def test_a_live_list_replaces_the_text_box_with_a_dropdown(self) -> None:
        out = _run(
            _MODEL_ROW + "rows.mountControls(root); await settle(); return input.replacedWith.attrs['data-act'];",
            models=[_LIVE],
        )
        assert out["result"] == "model-select"


class TestFallbackPicker:
    def test_choosing_in_the_picker_adds_nothing_until_add_is_clicked(self) -> None:
        """A native select fires change for each option an arrow key passes over: only Add may append."""
        out = _run(
            """
            const input = {value: 'a, b'};
            const pick = {value: 'c', dataset: {act: 'order-pick'}};
            const list = {innerHTML: ''};
            const control = {querySelector: (sel) => {
              if (sel === '[data-act="text"]') return input;
              return sel === '.sova-setting-order' ? list : null;
            }};
            const added = [];
            const row = {
              dataset: {key: 'agent.fallback_models'},
              classList: {add: (c) => added.push(c)},
              querySelector: (sel) => {
                if (sel === '[data-part="control"]') return control;
                if (sel === '[data-part="control"] [data-act="text"]') return input;
                if (sel === '[data-act="order-pick"]') return pick;
                return null;
              },
            };
            pick.closest = (sel) => (sel === '[data-key]' ? row : null);
            const add = {dataset: {act: 'order-add'}, closest: (sel) => {
              if (sel === '[data-act]') return add;
              return sel === '[data-key]' ? row : null;
            }};
            const handled = rows.handleChange(pick);
            const afterChange = input.value;
            rows.handleClick(add);
            return {handled, afterChange, afterAdd: input.value};
            """
        )
        r = out["result"]
        assert r["handled"] is False
        assert r["afterChange"] == "a, b"
        assert r["afterAdd"] == "a, b, c"
        assert out["fetches"] == [], "choosing a model in the picker saved something"


_RESET_CLICK = """
const row = el({'data-key': KEY, 'data-ctx': 'row'});
const btn = el({'data-act': 'reset'});
btn.textContent = 'Reset to default';
btn.closest = (sel) => (sel === '[data-act]' ? btn : (sel === '[data-key]' ? row : null));
rows.handleClick(btn);
await settle();
"""


class TestResetConfirmation:
    """Reset deletes a stored secret for good (it shows masked, so it cannot be copied first): a click arms it."""

    def test_one_click_on_a_secrets_reset_only_asks_again(self) -> None:
        out = _run(
            _RESET_CLICK.replace("KEY", "'task_source.jira_api_token'") + "return {text: btn.textContent};",
        )
        assert out["result"]["text"] == "Click again to remove"
        assert out["fetches"] == [], "a single click on a secret's Reset sent a reset"

    def test_a_second_click_resets_the_secret(self) -> None:
        click_twice = _RESET_CLICK.replace("KEY", "'task_source.jira_api_token'")
        out = _run(click_twice + "rows.handleClick(btn); await settle(); return 1;")
        assert out["fetches"] == ["POST /api/settings/config/reset"]

    def test_a_plain_setting_resets_on_the_first_click(self) -> None:
        out = _run(_RESET_CLICK.replace("KEY", "'agent.max_budget'") + "return {text: btn.textContent};")
        assert out["fetches"] == ["POST /api/settings/config/reset"]
        assert out["result"]["text"] == "Reset to default"

    def test_a_leftover_removal_needs_a_second_click(self) -> None:
        script = """
        const leftover = el({'data-leftover': 'old.removed_field'});
        const btn = el({'data-act': 'drop-leftover'});
        btn.textContent = 'Remove stored value';
        btn.closest = (sel) => (sel === '[data-act]' ? btn : (sel === '[data-leftover]' ? leftover : null));
        rows.handleClick(btn);
        await settle();
        const afterFirst = btn.textContent;
        STEP
        return {afterFirst};
        """
        once = _run(script.replace("STEP", ""))
        assert once["result"]["afterFirst"] == "Click again to remove"
        assert once["fetches"] == []
        twice = _run(script.replace("STEP", "rows.handleClick(btn); await settle();"))
        assert twice["fetches"] == ["POST /api/settings/config/reset"]


class TestSectionCounts:
    def test_a_section_counts_the_rows_it_shows(self) -> None:
        """A key hidden for the active provider is not counted, in a search or out of one, unless it is stored."""
        payload = json.loads(json.dumps(_PAYLOAD))
        payload["groups"][0]["settings"].append(_setting("llm.api_key", label="API key", value_type="secret"))
        out = _run(
            """
            const sec = {id: 'agent', groups: ['agent']};
            const plain = state.sectionCount(sec);
            state.view.query = 'api key';
            const searching = state.sectionCount(sec);
            state.view.query = '';
            state.setting('llm.api_key').stored = true;
            return {plain, searching, stored: state.sectionCount(sec)};
            """,
            payload=payload,
        )
        assert out["result"] == {"plain": 5, "searching": 0, "stored": 6}
