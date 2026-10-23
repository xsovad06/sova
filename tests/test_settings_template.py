"""Guards for the settings page's editability gate.

The gate lives in JavaScript (`renderSettingRow()` in settings.html), so there
is no Python seam to call. These are source-level drift guards, in the spirit
of tests/test_model_literal_guard.py: they fail loudly if a refactor silently
takes the edit affordance away from list-typed settings again.

Why this matters: sova.toml was removed in #900, so the dashboard and
`sova config set` are the only ways a human edits configuration. While
`isStructured` covered lists, 20 registered settings (awareness.providers,
pipelines.developer, agent.fallback_models, ci.flaky_checks, ...) could not be
changed anywhere except by hand-editing .claude/sova.db with sqlite3.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sova.dashboard.settings_meta import _REGISTRY

_TEMPLATE = Path(__file__).resolve().parents[1] / "sova" / "dashboard" / "templates" / "settings.html"


@pytest.fixture(scope="module")
def template_source() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _render_setting_row_body(source: str) -> str:
    """The text of renderSettingRow() up to its return, where editability is decided."""
    start = source.index("function renderSettingRow(s) {")
    end = source.index("return '<tr", start)
    return source[start:end]


class TestEditabilityGate:
    def test_only_objects_are_non_editable(self, template_source: str) -> None:
        """The read-only gate must cover 'object' only, never 'list'."""
        body = _render_setting_row_body(template_source)

        assert re.search(r"var editable = !isBool && !isObject;", body), (
            "renderSettingRow() no longer computes editability from isBool/isObject"
        )
        assert "'object'" in body
        assert "'list'" not in body, (
            "list-typed settings are non-editable again: with sova.toml gone, that "
            "leaves sqlite3 as the only way to set them"
        )

    def test_list_rows_get_a_format_hint(self, template_source: str) -> None:
        """A raw JSON array in a text box is unguessable without a hint."""
        assert "_LIST_INPUT_HINT" in template_source

    def test_edit_and_blur_share_one_raw_to_string_helper(self, template_source: str) -> None:
        """Two renderings of the same value would make an untouched list look edited."""
        assert "function _rawToInputString(" in template_source
        # Once in editConfig() to seed the input, once in the blur no-change check.
        assert template_source.count("_rawToInputString(JSON.parse(span.dataset.raw))") == 2


class TestFormatValueKeyParameter:
    def test_key_is_declared_and_passed_when_the_body_uses_it(self, template_source: str) -> None:
        """An undeclared `key` throws on the first boolean row and blanks the whole page.

        loadConfig() wraps rendering in a catch-all that only shows "Failed to load
        configuration", and the ReferenceError never reaches the server log (#1103).
        """
        signature = re.search(r"function formatValue\(([^)]*)\) \{", template_source)
        assert signature, "formatValue() not found in settings.html"
        params = [p.strip() for p in signature.group(1).split(",")]
        body = template_source[signature.end() : template_source.index("\n}\n", signature.end())]

        code = re.sub(r"//[^\n]*", "", body)  # a comment mentioning "key" is not a use of it
        if re.search(r"\bkey\b", code):
            assert "key" in params, "formatValue() uses `key` but does not declare it"
            assert "formatValue(s.value, s.value_type, s.key)" in template_source, (
                "renderSettingRow() does not pass s.key to formatValue()"
            )


class TestEnumerableListPicker:
    """agent.fallback_models' grouped-<select> picker beside the plain list editor."""

    def test_list_input_is_normalized_before_the_picker_operates_on_it(self, template_source: str) -> None:
        """A non-empty list renders as a JSON array literal (_rawToInputString()); the
        picker must never split that literal on ',' directly, or appending a model to an
        already-non-empty agent.fallback_models corrupts the other entries (issue #1162).
        """
        assert "function _parseListInput(" in template_source
        start = template_source.index("function editEnumerableListConfig(")
        end = template_source.index("\n}\n", start)
        body = template_source[start:end]
        assert "_parseListInput(input.value)" in body
        assert "input.value.split(',')" not in body

    def test_picker_commits_only_on_explicit_action_not_bare_change(self, template_source: str) -> None:
        """A native <select> fires 'change' for every option an arrow-key traversal lands
        on, not just the one the user settles on: acting on 'change' alone silently
        appends every model passed over and is unreachable by keyboard without corrupting
        the list.
        """
        start = template_source.index("function editEnumerableListConfig(")
        end = template_source.index("\n}\n", start)
        body = template_source[start:end]
        assert "select.onchange" not in body
        assert "addBtn.onclick" in body


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

    @staticmethod
    def _body(source: str) -> str:
        start = source.index("function editEnumerableListConfig(")
        return source[start : source.index("\n}\n", start)]

    def test_rows_offer_up_down_and_remove(self, template_source: str) -> None:
        body = self._body(template_source)
        for action in ("action: 'up'", "action: 'down'", "action: 'remove'"):
            assert action in body

    def test_reorder_rewrites_the_text_input_that_gets_saved(self, template_source: str) -> None:
        """The text input stays the single source of truth for what saveConfig() sends."""
        start = template_source.index("function applyOrderAction(")
        body = template_source[start : template_source.index("\n  }\n", start)]
        assert "_parseListInput(input.value)" in body
        assert "input.value = parts.join(', ')" in body

    def test_order_list_rerenders_on_manual_typing(self, template_source: str) -> None:
        assert "input.addEventListener('input', renderOrder)" in self._body(template_source)

    def test_blur_no_change_check_compares_parsed_entries_for_lists(self, template_source: str) -> None:
        """The list editor rewrites its own text, so a raw string compare saves on every blur.

        _rawToInputString() renders a non-empty list as a JSON array literal while
        editEnumerableListConfig() normalizes the input to comma-separated text, so
        comparing the two strings never matched and merely opening the editor and
        clicking away POSTed the unchanged list back.
        """
        start = template_source.index("function _isUnchanged(")
        body = template_source[start : template_source.index("\n  }\n", start)]
        assert "_parseListInput(newVal).join(', ') === _parseListInput(origStr).join(', ')" in body
        assert "if (!_isUnchanged(newVal, origStr))" in template_source

    def test_saved_list_preserves_order(self) -> None:
        from sova.dashboard.services.settings_service import _cast_list

        assert _cast_list("claude-sonnet-5, claude-haiku-4-5, gpt-oss-120b") == [
            "claude-sonnet-5",
            "claude-haiku-4-5",
            "gpt-oss-120b",
        ]
        assert _cast_list('["b", "a"]') == ["b", "a"]


_AGENTS_TEMPLATE = _TEMPLATE.parent / "agents.html"


# Module-level rather than a class-scoped fixture method in each class:
# pytest 9.1 deprecates a class-scoped fixture defined as an instance method.
@pytest.fixture(scope="module")
def agents_source() -> str:
    return _AGENTS_TEMPLATE.read_text(encoding="utf-8")


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
        assert match, "_LLM_OVERRIDE_ROLES not found in agents.html"
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

    def test_settings_row_hidden_for_runtimes_that_drop_the_override(self, template_source: str) -> None:
        assert (
            "_setConfigRowVisible('roles.developer_runtime_model', !_NO_MODEL_CHOICE_RUNTIMES[runtime])"
            in template_source
        )

    def test_settings_runtime_set_mirrors_honours_model_override(self, template_source: str) -> None:
        """A new runtime that ignores the model override must not keep showing a
        selector whose value it will drop."""
        from sova.ipc.runtime import _RUNTIMES, create_runtime

        match = re.search(r"var _NO_MODEL_CHOICE_RUNTIMES = \{([^}]*)\};", template_source)
        assert match, "_NO_MODEL_CHOICE_RUNTIMES not found in settings.html"
        client_set = set(re.findall(r"([\w-]+): true", match.group(1)))
        server_set = {name for name in _RUNTIMES if not create_runtime(name).honours_model_override}
        assert client_set == server_set

    def test_pin_roles_mirror_the_server_side_set(self, agents_source: str) -> None:
        from sova.dashboard.services.agent_lifecycle import RUNTIME_MODEL_PIN_ROLES

        match = re.search(r"var _RUNTIME_MODEL_PIN_ROLES = \{([^}]*)\};", agents_source)
        assert match, "_RUNTIME_MODEL_PIN_ROLES not found in agents.html"
        assert set(re.findall(r"(\w+): true", match.group(1))) == set(RUNTIME_MODEL_PIN_ROLES)

    def test_pin_mode_never_preselects_the_suggestion(self, agents_source: str) -> None:
        """A preselected pin would override per-step routing on a plain Launch click."""
        assert "if (mode === 'pin') suggestedModel = undefined;" in self._runtime_select_body(agents_source)

    def test_roles_that_ignore_the_choice_get_no_selector(self, agents_source: str) -> None:
        body = self._runtime_select_body(agents_source)
        assert "if (mode === 'ignored') {" in body

    def test_auto_launch_sends_the_suggestion_only_for_non_direct_roles(self, agents_source: str) -> None:
        assert "var autoModel = _runtimeModelMode(role) === 'spawn' ? data.suggested_model : null;" in agents_source
