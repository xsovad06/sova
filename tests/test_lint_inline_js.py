"""Tests for .github/scripts/lint_inline_js.py, the inline-template half of `make lint-js` (#1388)."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "lint_inline_js.py"
TEMPLATES = REPO_ROOT / "sova" / "dashboard" / "templates"

_spec = importlib.util.spec_from_file_location("lint_inline_js", SCRIPT)
assert _spec is not None and _spec.loader is not None
lint_inline_js = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_inline_js)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _check(tmp_path: Path, template: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "page.html"
    path.write_text(template, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.parametrize(
    ("attrs", "kind"),
    [
        pytest.param("", "script", id="plain"),
        pytest.param(' type="text/javascript"', "script", id="text-javascript"),
        pytest.param(' type="module"', "module", id="module"),
        pytest.param(' type="application/json" id="data"', None, id="json"),
        pytest.param(' type="text/template"', None, id="template"),
        pytest.param(' src="/static/app.js"', None, id="src"),
    ],
)
def test_script_kind(attrs: str, kind: str | None) -> None:
    assert lint_inline_js.script_kind(attrs) == kind


def test_strip_jinja_keeps_every_line_in_place() -> None:
    source = "const a = {{ x\n | tojson }};\n{% if y %}\nf();\n{# note\n #}\n{% endif %}\n"
    stripped = lint_inline_js.strip_jinja(source)
    assert stripped.count("\n") == source.count("\n")
    assert stripped.splitlines()[3] == "f();"
    assert not re.search(r"\{\{|\{%|\{#", stripped)


@needs_node
def test_dashboard_templates_pass() -> None:
    """The real tree is the false-positive check: its Jinja-in-JS patterns must all parse."""
    assert lint_inline_js.main([str(TEMPLATES)]) == 0


@needs_node
@pytest.mark.parametrize(
    "template",
    [
        pytest.param(
            "<script>\nconst prefix = {{ prefix|default('')|tojson }};\nconst n = {{ n|int }} + 1;\n"
            "const label = 'Issue #{{ issue }}';\nconst o = { key: {{ v|tojson }} };\n</script>\n",
            id="expressions-as-values-and-in-strings",
        ),
        pytest.param(
            "<script>\nconst items = [{% for i in items %}{{ i|tojson }},{% endfor %}];\n"
            "{% if debug %}\nconsole.log(items);\n{% else %}\nconsole.info(items);\n{% endif %}\n</script>\n",
            id="loops-and-conditionals",
        ),
        pytest.param("<script></script>\n<script>\n</script>\n", id="empty-blocks"),
        pytest.param("{% macro icon(name) %}<svg></svg>{% endmacro %}\n", id="no-scripts"),
        pytest.param('<script type="application/json">{"a": {{ a|tojson }}, }</script>\n', id="json-skipped"),
        pytest.param(
            '<script type="module">\nimport { x } from "/static/js/x.js";\nawait x();\n</script>\n', id="module"
        ),
    ],
)
def test_valid_templates_pass(tmp_path: Path, template: str) -> None:
    result = _check(tmp_path, template)
    assert result.returncode == 0, result.stderr


@needs_node
def test_syntax_error_fails_with_the_template_line(tmp_path: Path) -> None:
    template = "<div>\n{% block content %}\n<script>\nconst a = {{ a|tojson }};\nconst b = ;\n</script>\n"
    result = _check(tmp_path, template)
    assert result.returncode == 1
    assert f"{tmp_path / 'page.html'}:5" in result.stderr
    assert "SyntaxError" in result.stderr
    assert "    at " not in result.stderr


@needs_node
def test_module_syntax_is_rejected_in_a_classic_script(tmp_path: Path) -> None:
    """A classic block is checked as a script, so `import` there is the error a browser would raise."""
    result = _check(tmp_path, '<script>\nimport { x } from "/static/js/x.js";\n</script>\n')
    assert result.returncode == 1
