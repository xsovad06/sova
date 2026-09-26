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
