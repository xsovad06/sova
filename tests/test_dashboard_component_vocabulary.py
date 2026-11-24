"""The shared dashboard component vocabulary (#1053): the Jinja macros in `_components.html`, their JS twins in
`app.js`, the CSS they rely on, and the Catppuccin color table.

A variant added on one side but not the other, or a color table that drifts from `style.css`, fails here.
The JS half runs under `node` (a missing `node` skips locally and fails under CI, as in the other dashboard
script tests).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sova.dashboard.services import work_state
from tests.conftest import DASHBOARD_DIR
from tests.test_dashboard_agents_modules import _require_node

_STATIC = DASHBOARD_DIR / "static"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")

_ENV = Environment(
    loader=FileSystemLoader(DASHBOARD_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _macro(name: str, *args: Any, **kwargs: Any) -> str:
    return str(getattr(_ENV.get_template("_components.html").module, name)(*args, **kwargs))


def _slice(start: str, end: str) -> str:
    assert start in _APP_JS, f"app.js no longer contains the marker {start!r}; update this test's slicing"
    begin = _APP_JS.index(start)
    assert end in _APP_JS[begin:], f"app.js has no {end!r} after {start!r}; update this test's slicing"
    return _APP_JS[begin : _APP_JS.index(end, begin)]


def _function(name: str) -> str:
    """A top-level `function name(` declaration, up to and including its closing brace at column 0."""
    return _slice(f"function {name}(", "\n}\n") + "\n}\n"


def _variable(name: str) -> str:
    """A top-level `var name = ...;` declaration, a one-line value or a table closed by `};` / `];`."""
    return _slice(f"var {name} = ", ";\n") + ";\n"


# The app.js declarations the JS twins need, loaded into node one by one (never as a text range, so reordering
# app.js or reformatting a banner cannot produce a half-sliced script).
_PRELUDE_FUNCTIONS = (
    "escapeHtml",
    "_attrsHtml",
    "_extraClass",
    "_sovaBtnClass",
    "sovaColor",
    "stateBadgeClasses",
    "sovaBtn",
    "badgeHtml",
    "emptyStateHtml",
    "statCardHtml",
    "statusBadge",
)
_PRELUDE_VARIABLES = (
    "_SOVA_COLOR_FALLBACK",
    "STATE_BADGE_CLASSES",
    "STATUS_BADGE_CLASSES",
    "SOVA_BTN_VARIANTS",
    "SOVA_BTN_SIZES",
    "SOVA_STAT_SIZES",
)


def _js_prelude() -> str:
    parts = [_variable(name) for name in _PRELUDE_VARIABLES] + [_function(name) for name in _PRELUDE_FUNCTIONS]
    return "globalThis.window = globalThis;\n" + "\n".join(parts)


def _node(expression: str, *, extra: str = "") -> Any:
    """Evaluate a JS expression with the component helpers and color table loaded, and return it as JSON."""
    _require_node("node is required to run the component helpers")
    script = f"{_js_prelude()}\n{extra}\nconsole.log(JSON.stringify({expression}));"
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _classes(html: str, tag: str) -> str:
    """The class attribute of the first `<tag ...>` in html."""
    match = re.search(rf"<{tag}\b[^>]*\bclass=\"([^\"]*)\"", html)
    assert match, html
    return match.group(1)


def _value_classes(html: str) -> list[str]:
    match = re.search(r'class="(sova-stat-value[^"]*)"', html)
    assert match, html
    return match.group(1).split()


def _squash(html: str) -> str:
    return re.sub(r"\s+", " ", html).strip()


class TestButtonVocabulary:
    def test_variants_and_sizes_are_the_ones_style_css_defines(self) -> None:
        sizes = set(_node("SOVA_BTN_SIZES"))
        css_classes = set(re.findall(r"^\.sova-btn-([a-z]+)\s*\{", _CSS, flags=re.MULTILINE))
        assert sizes == {"xs", "md", "icon"}
        assert sizes <= css_classes
        assert set(_node("SOVA_BTN_VARIANTS")) == css_classes - sizes
        assert set(_node("SOVA_BTN_VARIANTS")) == {
            "primary",
            "success",
            "danger",
            "warning",
            "purple",
            "secondary",
            "ghost",
        }

    @pytest.mark.parametrize("size", ["", "xs", "md", "icon"])
    def test_macro_and_helper_emit_the_same_classes_for_every_variant(self, size: str) -> None:
        variants = [*_node("SOVA_BTN_VARIANTS"), "nonsense"]
        js = _node(f"{json.dumps(variants)}.map(v => sovaBtn('Go', {{variant: v, size: {json.dumps(size)}}}))")
        for variant, js_html in zip(variants, js, strict=True):
            assert _classes(_macro("btn", "Go", variant=variant, size=size), "button") == _classes(js_html, "button")

    def test_unknown_variant_and_size_render_as_the_default_button(self) -> None:
        assert _classes(_macro("btn", "Go", variant="nonsense", size="huge"), "button") == "sova-btn sova-btn-primary"
        assert (
            _node("sovaBtn('Go', {variant: 'nonsense', size: 'huge'})").count('class="sova-btn sova-btn-primary"') == 1
        )

    def test_no_implicit_tabindex_and_attributes_are_rendered_when_given(self) -> None:
        macro = _macro("btn", "Go", id="go", tooltip="Run", disabled=True, attrs={"data-action": "go"})
        js = _node("sovaBtn('Go', {id: 'go', tooltip: 'Run', disabled: true, attrs: {'data-action': 'go'}})")
        assert "tabindex" not in macro
        assert "tabindex" not in js
        assert _squash(macro) == js

    def test_label_and_attribute_values_are_escaped_by_both(self) -> None:
        hostile = '<img src=x onerror="alert(1)">'
        macro = _macro("btn", hostile, tooltip=hostile, attrs={"data-x": hostile})
        js = _node("sovaBtn(h, {tooltip: h, attrs: {'data-x': h}})", extra=f"var h = {json.dumps(hostile)};")
        for html in (macro, js):
            assert "<img" not in html
            assert 'onerror="alert' not in html

    def test_extra_classes_are_appended_by_both_and_a_class_attr_is_merged_not_duplicated(self) -> None:
        macro = _macro("btn", "Go", size="xs", cls="ml-auto hidden")
        js = _node("sovaBtn('Go', {size: 'xs', className: 'ml-auto hidden'})")
        assert (
            _classes(macro, "button")
            == _classes(js, "button")
            == "sova-btn sova-btn-primary sova-btn-xs ml-auto hidden"
        )
        merged_macro = _macro("btn", "Go", cls="a", attrs={"class": "b", "type": "submit", "data-x": "1"})
        merged_js = _node("sovaBtn('Go', {className: 'a', attrs: {'class': 'b', 'type': 'submit', 'data-x': '1'}})")
        for html in (merged_macro, merged_js):
            assert html.count("class=") == 1
            assert html.count("type=") == 1
            assert 'type="button"' in html
            assert 'class="sova-btn sova-btn-primary a b"' in html
            assert 'data-x="1"' in html
        assert _squash(merged_macro) == merged_js

    def test_extra_classes_are_escaped_by_both(self) -> None:
        hostile = '"><script>alert(1)</script>'
        macro = _macro("btn", "Go", cls=hostile)
        js = _node("sovaBtn('Go', {className: h})", extra=f"var h = {json.dumps(hostile)};")
        for html in (macro, js):
            assert "<script>" not in html

    def test_icon_html_is_trusted_markup_placed_before_the_escaped_label(self) -> None:
        js = _node(
            "sovaBtn('<b>', {size: 'icon', iconHtml: '<svg></svg>', attrs: {'aria-label': 'Run'}})",
        )
        assert js.endswith("><svg></svg>&lt;b&gt;</button>")
        assert "<svg" not in _node("sovaBtn('<svg>')")


class TestBadgeVocabulary:
    def test_macro_and_helper_emit_the_same_classes_for_every_color(self) -> None:
        colors = [*_node("Object.keys(STATE_BADGE_CLASSES)"), "nonsense"]
        js = _node(f"{json.dumps(colors)}.map(c => badgeHtml('x', {{color: c}}))")
        for color, js_html in zip(colors, js, strict=True):
            assert _classes(_macro("badge", "x", color=color), "span") == _classes(js_html, "span")

    def test_unknown_color_renders_as_gray(self) -> None:
        assert _classes(_macro("badge", "x", color="nonsense"), "span") == "sova-badge bg-gray-600/30 text-gray-400"
        assert _node("badgeHtml('x', {color: 'toString'})").startswith('<span class="sova-badge bg-gray-600/30')

    def test_label_and_attribute_values_are_escaped_by_both(self) -> None:
        hostile = '<script>alert("x")</script>'
        macro = _macro("badge", hostile, attrs={"title-x": hostile})
        js = _node("badgeHtml(h, {attrs: {'title-x': h}})", extra=f"var h = {json.dumps(hostile)};")
        for html in (macro, js):
            assert "<script>" not in html

    def test_extra_classes_are_appended_by_both_and_a_class_attr_is_merged_not_duplicated(self) -> None:
        macro = _macro("badge", "x", color="green", cls="shrink-0", attrs={"class": "ml-2", "data-x": "1"})
        js = _node("badgeHtml('x', {color: 'green', className: 'shrink-0', attrs: {'class': 'ml-2', 'data-x': '1'}})")
        for html in (macro, js):
            assert html.count("class=") == 1
            assert _classes(html, "span") == "sova-badge bg-accent-green/20 text-accent-green shrink-0 ml-2"
        assert _squash(macro) == js

    def test_badge_has_the_shape_status_badge_renders(self) -> None:
        """`.sova-badge` is the CSS form of statusBadge()'s `text-xs px-2 py-0.5 rounded`: same size, padding and
        radius, so moving a call site from one to the other changes only the color."""
        rule = re.search(r"^\.sova-badge\s*\{([^}]*)\}", _CSS, flags=re.MULTILINE)
        assert rule
        body = rule.group(1)
        assert "font-size: 0.75rem;" in body  # text-xs
        assert "padding: 0.125rem 0.5rem;" in body  # py-0.5 px-2
        assert "border-radius: 0.25rem;" in body  # rounded
        status = _node("statusBadge('x', {})")
        assert 'class="text-xs px-2 py-0.5 rounded ' in status
        assert "rounded-full" not in status

    def test_the_token_set_is_the_one_work_state_emits(self) -> None:
        """work_state.py decides the state-to-color token; the macro table, STATE_BADGE_CLASSES and the hex lookup
        must cover exactly those tokens, so a token added in Python cannot render as gray on one side only."""
        emitted = (
            set(work_state._STATE_COLORS.values())
            | {color for _, color in work_state._PR_STATE_META.values()}
            | {color for _, color in work_state._TASK_STATE_META.values()}
            | {work_state._FALLBACK_COLOR}
        )
        assert set(_node("Object.keys(STATE_BADGE_CLASSES)")) == emitted
        assert set(re.findall(r"'([\w-]+)':\s*'\w+'", _slice("var _STATE_HEX_KEYS", "};"))) == emitted
        gray = _classes(_macro("badge", "x", color="nonsense"), "span")
        for token in sorted(emitted - {"gray"}):
            assert _classes(_macro("badge", "x", color=token), "span") != gray, token

    def test_badge_text_is_12px(self) -> None:
        rule = re.search(r"^\.sova-badge\s*\{([^}]*)\}", _CSS, flags=re.MULTILINE)
        assert rule
        assert "font-size: 0.75rem;" in rule.group(1)


class TestStatCardVocabulary:
    def test_default_output_is_unchanged_for_existing_callers(self) -> None:
        html = _macro("stat_card", "Last Run", "stat-last-run", detail_id="stat-last-run-detail")
        assert '<p class="sova-stat-value " id="stat-last-run">--</p>' in html

    @pytest.mark.parametrize("size", ["lg", "md", "sm", "", "huge"])
    def test_macro_and_helper_emit_the_same_value_classes(self, size: str) -> None:
        macro = _macro("stat_card", "Cost", "stat-cost", value_class="text-accent-green", size=size)
        js = _node(f"statCardHtml('Cost', 'stat-cost', {{size: {json.dumps(size)}, valueClass: 'text-accent-green'}})")
        assert _value_classes(macro) == _value_classes(js)

    @pytest.mark.parametrize(("size", "extra"), [("md", "sova-stat-value-md"), ("sm", "sova-stat-value-sm")])
    def test_a_size_adds_its_class(self, size: str, extra: str) -> None:
        assert extra in _macro("stat_card", "x", "x-id", size=size)
        assert extra in _node(f"statCardHtml('x', 'x-id', {{size: {json.dumps(size)}}})")
        assert re.search(rf"^\.{extra}\s*\{{", _CSS, flags=re.MULTILINE)

    @pytest.mark.parametrize("unknown", ["huge", "", "toString"])
    def test_an_unknown_size_adds_no_class(self, unknown: str) -> None:
        macro = _macro("stat_card", "x", "x-id", size=unknown)
        js = _node(f"statCardHtml('x', 'x-id', {{size: {json.dumps(unknown)}}})")
        for html in (macro, js):
            assert "sova-stat-value-" not in html
            assert 'class="sova-stat-value' in html

    def test_label_is_escaped_by_both(self) -> None:
        hostile = "<b>x</b>"
        assert "<b>" not in _macro("stat_card", hostile, "id")
        assert "<b>" not in _node("statCardHtml(h, 'id')", extra=f"var h = {json.dumps(hostile)};")


class TestEmptyState:
    def test_macro_and_helper_agree_and_escape(self) -> None:
        hostile = "<i>none</i>"
        macro = _macro("empty_state", hostile, hint="<b>hint</b>")
        js = _node("emptyStateHtml(t, hint)", extra=f"var t = {json.dumps(hostile)}, hint = '<b>hint</b>';")
        for html in (macro, js):
            assert "<i>" not in html
            assert "<b>" not in html
            assert 'class="sova-empty"' in html
            assert 'class="sova-empty-hint"' in html

    def test_hint_is_optional(self) -> None:
        assert "sova-empty-hint" not in _macro("empty_state", "Nothing here")
        assert "sova-empty-hint" not in _node("emptyStateHtml('Nothing here')")


class TestTooltipFocus:
    def test_tooltip_shows_on_keyboard_focus_as_well_as_hover(self) -> None:
        assert re.search(
            r"\[data-tooltip\]:hover::after,\s*\[data-tooltip\]:focus-visible::after\s*\{\s*opacity: 1;", _CSS
        )


class TestColorTable:
    @staticmethod
    def _init_colors_from_css() -> dict[str, str]:
        variables = dict(re.findall(r"(--ctp-[\w-]+):\s*(#[0-9a-fA-F]{6})", _CSS))
        init = _slice("function initColors(", "\n}\n") + "\n}\n"
        return _node(
            "window.SOVA_COLORS",
            extra=(
                f"const vars = {json.dumps(variables)};\nfunction getCSSVar(n) {{ return vars[n] || ''; }}\n"
                f"{init}\ninitColors();"
            ),
        )

    def test_fallback_table_equals_what_initcolors_reads_from_style_css(self) -> None:
        assert _node("_SOVA_COLOR_FALLBACK") == self._init_colors_from_css()

    def test_every_remaining_catppuccin_token_is_exposed(self) -> None:
        tokens = set(re.findall(r"^\s*--ctp-([a-z0-9-]+):\s*#", _CSS, flags=re.MULTILINE))
        init = _slice("function initColors(", "\n}\n")
        read = set(re.findall(r"getCSSVar\('--ctp-([a-z0-9-]+)'\)", init))
        assert tokens - read == set()

    def test_a_missing_token_falls_back_to_hex_never_undefined(self) -> None:
        got = _node(
            "[sovaColor('sapphire'), sovaColor('text'), sovaColor('nope'), sovaColor('red')]",
            extra="window.SOVA_COLORS = {red: '#123456', sapphire: ''};",
        )
        assert got == ["#74c7ec", "#cdd6f4", "#6c7086", "#123456"]

    def test_every_token_name_a_caller_passes_exists(self) -> None:
        """sovaColor() falls back to gray for a name it does not know, so a typo would draw the wrong color with
        no error: every literal name passed to it, or mapped to it via a hexKey or _STATE_HEX_KEYS, must be real."""
        known = set(_node("Object.keys(_SOVA_COLOR_FALLBACK)"))
        used: dict[str, set[str]] = {}
        sources = [*_STATIC.rglob("*.js"), *(DASHBOARD_DIR / "templates").rglob("*.html")]
        assert len(sources) > 10
        for path in sources:
            text = path.read_text(encoding="utf-8")
            names = set(re.findall(r"""\bsovaColor\(\s*['"]([\w-]+)['"]\s*\)""", text))
            names |= set(re.findall(r"""\bhexKey:\s*['"]([\w-]+)['"]""", text))
            if path.name == "app.js":
                names |= set(re.findall(r"'[\w-]+':\s*'(\w+)'", _slice("var _STATE_HEX_KEYS", "};")))
            if names:
                used[path.name] = names
        assert "supervisor.html" in used
        unknown = {file: sorted(names - known) for file, names in used.items() if names - known}
        assert unknown == {}

    def test_sovacolor_works_before_initcolors_has_run(self) -> None:
        assert _node("sovaColor('accent')", extra="delete window.SOVA_COLORS;") == "#89b4fa"


class TestStylesheetCoverage:
    """The checked-in tailwind.min.css is rebuilt by hand (`make css`), so a class a helper can emit that the bundle
    lacks would ship unstyled with no failure."""

    _TAILWIND = (_STATIC / "tailwind.min.css").read_text(encoding="utf-8")

    @staticmethod
    def _tokens(html: str, tag: str) -> list[str]:
        return _classes(html, tag).split()

    def _emitted_classes(self) -> set[str]:
        tokens: set[str] = set()
        for variant in [*_node("SOVA_BTN_VARIANTS"), "nonsense"]:
            for size in ["", *_node("SOVA_BTN_SIZES")]:
                tokens.update(self._tokens(_macro("btn", "Go", variant=variant, size=size), "button"))
        for color in [*_node("Object.keys(STATE_BADGE_CLASSES)"), "nonsense"]:
            tokens.update(self._tokens(_macro("badge", "x", color=color), "span"))
            tokens.update(self._tokens(_node(f"badgeHtml('x', {{color: {json.dumps(color)}}})"), "span"))
        for value in _node("Object.values(STATE_BADGE_CLASSES)"):
            tokens.update(value.split())
        for size in ("md", "sm"):
            tokens.update(_value_classes(_macro("stat_card", "x", "x-id", size=size)))
        return tokens

    def test_every_class_a_helper_can_emit_is_styled(self) -> None:
        tokens = self._emitted_classes()
        assert "sova-btn" in tokens
        assert "bg-accent-green/20" in tokens
        missing: list[str] = []
        for token in sorted(tokens):
            if token.startswith("sova-"):
                if not re.search(rf"^\.{re.escape(token)}\b", _CSS, flags=re.MULTILINE):
                    missing.append(f"{token} (style.css)")
            elif f".{re.sub(r'([^a-zA-Z0-9_-])', r'\\\1', token)}" not in self._TAILWIND:
                missing.append(f"{token} (tailwind.min.css: run `make css`)")
        assert missing == []
