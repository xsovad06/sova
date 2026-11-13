"""Structure of the Settings page (#1463) and source-level guards for its scripts.

settings.html renders the section list, every group's shell, the Overview and the widget slots; the rows are built
by static/js/pages/settings/ (rows.js, page.js, widgets.js), whose save and reset behavior runs under node in
tests/test_settings_page_js.py. The guards here check what is plain in the source: which value types stay
editable, how the fallback list editor works, and the sets mirrored from Python.

Why lists must stay editable: sova.toml was removed in #900, so the dashboard and `sova config set` are the only
ways a human edits configuration. While list-typed settings were read-only, 20 registered settings
(awareness.providers, pipelines.developer, agent.fallback_models, ci.flaky_checks, ...) could only be changed by
hand-editing .claude/sova.db with sqlite3.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.settings_meta import _REGISTRY, GROUP_ORDER, GROUPS, OVERVIEW_KEYS, SECTION_LABELS, SECTIONS
from tests.conftest import agents_page_source

_PAGE_JS = Path(__file__).resolve().parents[1] / "sova" / "dashboard" / "static" / "js" / "pages" / "settings"


@pytest.fixture(scope="module")
def rows_source() -> str:
    return (_PAGE_JS / "rows.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page_source() -> str:
    return (_PAGE_JS / "page.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def state_source() -> str:
    return (_PAGE_JS / "state.js").read_text(encoding="utf-8")


@pytest.fixture
async def settings_html(dashboard_app) -> AsyncIterator[str]:
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as client:
        res = await client.get("/settings")
        assert res.status_code == 200
        yield res.text


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start : source.index("\n}\n", start)]


@pytest.mark.usefixtures("reset_db_rows")
class TestPageStructure:
    async def test_lists_every_section_with_its_groups(self, settings_html: str) -> None:
        links = re.findall(r'data-section-link="([^"]+)"', settings_html)
        assert links == list(SECTIONS)
        for section_id in SECTIONS:
            assert SECTION_LABELS[section_id] in settings_html
        assert sorted(re.findall(r'data-group-link="([^"]+)"', settings_html)) == sorted(GROUP_ORDER)

    async def test_every_group_has_a_shell_in_its_section(self, settings_html: str) -> None:
        for section_id, group_ids in SECTIONS.items():
            if not group_ids:
                continue
            block = settings_html[settings_html.index(f'id="section-{section_id}"') :]
            block = block[: block.index("</section>")]
            assert re.findall(r'data-group="([^"]+)"', block) == list(group_ids), section_id
            for group_id in group_ids:
                assert f"<h4>{GROUPS[group_id]}</h4>" in block.replace("&amp;", "&")

    async def test_no_collapsible_boxes_are_left(self, settings_html: str) -> None:
        main = settings_html[settings_html.index('class="sova-settings-header"') : settings_html.index("</main>")]
        for leftover in ("sova-settings-body", "toggleGroup", "aria-expanded", "group-chevron-", "onclick="):
            assert leftover not in main

    async def test_the_sections_data_matches_settings_meta(self, settings_html: str) -> None:
        raw = re.search(
            r'<script type="application/json" id="settings-sections-data">(.*?)</script>', settings_html, re.S
        )
        assert raw
        data = json.loads(raw.group(1))
        assert [(s["id"], s["label"], s["groups"]) for s in data] == [
            (sid, SECTION_LABELS[sid], list(groups)) for sid, groups in SECTIONS.items()
        ]

    async def test_overview_has_the_everyday_controls(self, settings_html: str) -> None:
        overview = settings_html[settings_html.index('id="section-overview"') :]
        overview = overview[: overview.index("</section>")]
        switches = re.findall(r'data-setting-switch="([^"]+)"', overview)
        steppers = re.findall(r'data-setting-stepper="([^"]+)"', overview)
        # The expectation is written out, not read from OVERVIEW_KEYS: a control dropped from the list must fail.
        supervisor_switches = [k for k in switches if k.startswith("supervisor.") and k != "supervisor.enabled"]
        assert len(supervisor_switches) == 13
        assert "supervisor.enabled" in switches
        assert "oversight.enabled" in switches
        assert steppers == ["max_parallel_agents"]
        assert sorted(switches + steppers) == sorted(
            ["max_parallel_agents", "supervisor.enabled", "oversight.enabled", *supervisor_switches]
        )
        assert sorted(OVERVIEW_KEYS) == sorted(switches + steppers)
        # Each control sits in an Overview copy bound to the same key, which a save re-renders with the row.
        assert sorted(re.findall(r'data-key="([^"]+)" data-ctx="overview"', overview)) == sorted(OVERVIEW_KEYS)
        assert 'data-widget="installation"' in overview
        assert 'data-widget="health"' in overview

    @pytest.mark.parametrize(
        ("widget", "group_id"),
        [
            ("personas", "project"),
            ("labels", "task_source"),
            ("invariants", "validation"),
            ("operations-persona", "oversight"),
        ],
    )
    async def test_widgets_sit_in_their_groups(self, settings_html: str, widget: str, group_id: str) -> None:
        shell = settings_html[settings_html.index(f'id="group-{group_id}"') :]
        shell = shell[: shell.index('data-part="group-rows"')]
        assert f'data-widget="{widget}"' in shell

    async def test_loads_the_page_module(self, settings_html: str) -> None:
        assert "import { init } from '/static/js/pages/settings/page.js';" in settings_html


class TestAnchors:
    def test_a_hash_opens_a_section_or_the_section_holding_a_group(self, page_source: str) -> None:
        body = _function_body(page_source, "function route() {")
        assert "state.sectionById(hash)" in body
        assert "state.sectionForGroup(" in body
        assert "scrollToEl($('group-' + gid))" in body
        assert "window.addEventListener('hashchange', route)" in page_source


class TestEditability:
    def test_only_objects_are_read_only(self, rows_source: str) -> None:
        """Lists get the text box every other plain value gets; objects alone render as a read-only table."""
        body = _function_body(rows_source, "export function controlHtml(s) {")
        assert "if (s.value_type === 'object') return objectControl(s);" in body
        assert "'list'" not in body
        assert body.rstrip().endswith("return textControl(s);")

    def test_list_rows_get_a_format_hint(self, rows_source: str) -> None:
        assert "const LIST_HINT = " in rows_source
        assert "list ? LIST_HINT" in _function_body(rows_source, "function textControl(s) {")


class TestRegistryCoverage:
    def test_list_settings_exist_to_justify_the_gate(self) -> None:
        """If this ever hits zero the gate test above is vacuous."""
        list_keys = [m.key for m in _REGISTRY if m.value_type == "list"]
        assert len(list_keys) > 10
        assert "awareness.providers" in list_keys

    def test_no_registry_description_points_at_sova_toml(self) -> None:
        """sova.toml no longer exists; a description naming it sends users nowhere."""
        offenders = [m.key for m in _REGISTRY if "sova.toml" in m.description]
        assert offenders == [], f"settings descriptions still reference sova.toml: {offenders}"


class TestFallbackOrderControls:
    """agent.fallback_models is an ordered chain; the editor must let it be reordered (#1036)."""

    def test_rows_offer_up_down_and_remove(self, rows_source: str) -> None:
        body = _function_body(rows_source, "function renderFallbackOrder(row) {")
        for act in ('data-act="order-up"', 'data-act="order-down"', 'data-act="order-remove"'):
            assert act in body

    def test_reorder_rewrites_the_text_box_that_gets_saved(self, rows_source: str) -> None:
        """The text box stays the single source of truth for what Save sends."""
        body = _function_body(rows_source, "function changeOrder(row, act, index) {")
        assert "const parts = parseList(input.value);" in body
        assert (
            "input.value = parts.some((p) => p.indexOf(',') !== -1) ? JSON.stringify(parts) : parts.join(', ');" in body
        )

    def test_order_list_rerenders_on_manual_typing(self, rows_source: str) -> None:
        body = _function_body(rows_source, "export function handleInput(target) {")
        assert "renderFallbackOrder(row)" in body

    def test_saved_list_preserves_order(self) -> None:
        from sova.dashboard.services.settings_service import _cast_list

        assert _cast_list("claude-sonnet-5, claude-haiku-4-5, gpt-oss-120b") == [
            "claude-sonnet-5",
            "claude-haiku-4-5",
            "gpt-oss-120b",
        ]
        assert _cast_list('["b", "a"]') == ["b", "a"]


# Module-level rather than a class-scoped fixture method in each class:
# pytest 9.1 deprecates a class-scoped fixture defined as an instance method.
@pytest.fixture(scope="module")
def agents_source() -> str:
    return agents_page_source()


class TestLaunchModalLlmOverride:
    """The launch modal's per-run "Internal LLM model" control (#1036)."""

    def test_labelled_as_this_run_only(self, agents_source: str) -> None:
        assert "Internal LLM model (this run only)" in agents_source

    def test_sourced_from_llm_layer_discovery(self, agents_source: str) -> None:
        assert "/models/available?layer=llm" in agents_source

    def test_defaults_to_no_override_never_a_suggestion(self, agents_source: str) -> None:
        start = agents_source.index("function _prepareLlmOverrideSelect(")
        body = agents_source[start : agents_source.index("\n}\n", start)]
        assert "sel.value = ''" in body
        assert "suggested_model" not in body

    def test_roles_mirror_the_server_side_set(self, agents_source: str) -> None:
        from sova.dashboard.services.agent_lifecycle import LLM_MODEL_OVERRIDE_ROLES

        match = re.search(r"var _LLM_OVERRIDE_ROLES = \{([^}]*)\};", agents_source)
        assert match, "_LLM_OVERRIDE_ROLES not found in the Agents page source"
        client_roles = set(re.findall(r"(\w+): true", match.group(1)))
        assert client_roles == set(LLM_MODEL_OVERRIDE_ROLES)

    def test_disabled_control_is_never_submitted(self, agents_source: str) -> None:
        start = agents_source.index("function confirmLaunch(")
        body = agents_source[start : agents_source.index("\n}\n", start)]
        assert "llmSel.disabled ? null" in body


class TestRuntimeModelSelectors:
    """The runtime-layer model selectors (#1039): settings row plus launch modal.

    Both live in JavaScript, so these are source-level drift guards in the same
    spirit as TestLaunchModalLlmOverride above.
    """

    @staticmethod
    def _runtime_select_body(agents_source: str) -> str:
        start = agents_source.index("function _prepareRuntimeModelSelect(")
        return agents_source[start : agents_source.index("\n}\n", start)]

    def test_registered_as_a_runtime_layer_enumerable_setting(self) -> None:
        meta = next(m for m in _REGISTRY if m.key == "roles.developer_runtime_model")
        assert meta.options_source == "runtime"
        assert not any(m.key == "agent.runtime_model" for m in _REGISTRY)

    def test_launch_modal_sources_the_runtime_layer(self, agents_source: str) -> None:
        assert "/models/available?layer=runtime" in self._runtime_select_body(agents_source)

    def test_launch_modal_defaults_to_no_override_when_suggestion_is_not_in_catalog(self, agents_source: str) -> None:
        """The suggestion is a layer-2 routing alias ("sonnet"), rarely a member
        of the runtime's catalog. Without an explicit no-override entry the
        browser silently selects the catalog's first model and launches that."""
        body = self._runtime_select_body(agents_source)
        assert "no override" in body
        fallback = "if (!appendModelOptgroups(sel, groupModelsByFamily(data.models), suggestedModel))"
        assert fallback + " sel.value = ''" in body

    def test_launch_modal_hides_on_reported_capability_not_a_runtime_name(self, agents_source: str) -> None:
        body = self._runtime_select_body(agents_source)
        assert "data.honours_model_override === false" in body
        assert "'codex'" not in body

    def test_settings_row_hidden_for_runtimes_that_drop_the_override(self, state_source: str) -> None:
        assert "if (NO_MODEL_CHOICE_RUNTIMES[runtime]) hidden.add('roles.developer_runtime_model');" in _function_body(
            state_source, "export function hiddenKeys() {"
        )

    def test_settings_runtime_set_mirrors_honours_model_override(self, state_source: str) -> None:
        """A new runtime that ignores the model override must not keep showing a
        selector whose value it will drop."""
        from sova.ipc.runtime import _RUNTIMES, create_runtime

        match = re.search(r"const NO_MODEL_CHOICE_RUNTIMES = \{([^}]*)\};", state_source)
        assert match, "NO_MODEL_CHOICE_RUNTIMES not found in static/js/pages/settings/state.js"
        client_set = set(re.findall(r"([\w-]+): true", match.group(1)))
        server_set = {name for name in _RUNTIMES if not create_runtime(name).honours_model_override}
        assert client_set == server_set

    def test_pin_roles_mirror_the_server_side_set(self, agents_source: str) -> None:
        from sova.dashboard.services.agent_lifecycle import RUNTIME_MODEL_PIN_ROLES

        match = re.search(r"var _RUNTIME_MODEL_PIN_ROLES = \{([^}]*)\};", agents_source)
        assert match, "_RUNTIME_MODEL_PIN_ROLES not found in the Agents page source"
        assert set(re.findall(r"(\w+): true", match.group(1))) == set(RUNTIME_MODEL_PIN_ROLES)

    def test_pin_mode_never_preselects_the_suggestion(self, agents_source: str) -> None:
        """A preselected pin would override per-step routing on a plain Launch click."""
        assert "if (mode === 'pin') suggestedModel = undefined;" in self._runtime_select_body(agents_source)

    def test_roles_that_ignore_the_choice_get_no_selector(self, agents_source: str) -> None:
        body = self._runtime_select_body(agents_source)
        assert "if (mode === 'ignored') {" in body

    def test_auto_launch_sends_the_suggestion_only_for_non_direct_roles(self, agents_source: str) -> None:
        assert "var autoModel = _runtimeModelMode(role) === 'spawn' ? data.suggested_model : null;" in agents_source
