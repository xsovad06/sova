"""Ratchet guard for five dashboard design-drift patterns (#1053).

Each pattern below is a hand-rolled copy of something the shared components now provide: a native `title=`
tooltip (use `data-tooltip`), an arbitrary tiny font size, a raw stat card, a hex color literal (use a
Catppuccin token or `sovaColor()`), and a hand-written `fixed inset-0` modal shell. The migration issues
(#1055, #1056, #1224) move templates onto the components; this guard makes sure the counts only shrink.

How the ratchet works:
- `BASELINE` holds a count per pattern and file. A file over its baseline fails with the file, the pattern
  and the line numbers. A file under its baseline fails too, telling the author to lower the baseline, so
  freed headroom cannot be reused for a new violation. A file missing from `BASELINE` has a baseline of 0.
- `ALLOWLIST` exempts a (pattern, file) pair outright, with a reason. Only a file that emits the component
  markup belongs there; a file that merely has legacy markup, or that mixes a color table with other code,
  does not. `EXEMPT_BLOCKS` exempts one delimited block of a file (the `SOVA_COLORS` fallback table in
  `app.js`) so a hex literal anywhere else in that file still counts.
- Only first-party `.html` templates and `.js` files are scanned. Minified or vendored files (`*.min.js`,
  `d3.min.js`) and the minified Tailwind bundle are skipped; `style.css` is a stylesheet and is not scanned.

Detection is regex based and the false-positive handling is documented on each pattern below. Files are read
as UTF-8 and the file list is sorted, so line endings, locale and directory order cannot change a count.
For the `title_attr` pattern only, Jinja statements, expressions and comments are blanked out of `.html` files
before matching (newlines are kept, so reported line numbers stay right): a macro keyword argument such as
`card_header(title="x")` is not markup. The other four patterns match the raw text, so a violation written
inside a Jinja tag (`{% set c = '#f38ba8' %}`, `value_class='text-2xl font-bold'`) still counts.

To re-seed after a deliberate scanner change, run `python3 -m tests.test_dashboard_design_drift_guard` and paste
the output into `BASELINE`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import DASHBOARD_DIR

SCAN_ROOTS = (DASHBOARD_DIR / "templates", DASHBOARD_DIR / "static")
SCAN_SUFFIXES = (".html", ".js")

# Pattern name -> compiled regex. Each is matched per file after Jinja blocks are blanked out of templates.
PATTERNS: dict[str, re.Pattern[str]] = {
    # A markup `title=` attribute: preceded by whitespace (so `data-title=`, `aria-label=` and `.title=` do not
    # match, and neither does `<title>` or an object key `title:`), followed by a quote, an escaped quote
    # (a JS string holding markup) or a `${` interpolation. This is the form JS template strings use.
    "title_attr": re.compile(r"(?<=\s)title=(?:[\"'\\]|\$\{)"),
    # The tiny arbitrary font sizes; the type scale has text-xs (12px) as its floor.
    "arbitrary_text_size": re.compile(r"text-\[(?:9|10|11)px\]"),
    # The hand-rolled stat number (`text-2xl font-bold`, `text-xl font-bold`, either order) and any `.sova-stat`
    # class written outside the stat_card() macro (`sova-stat`, `sova-stat-label`, `sova-stat-value-sm`, ...).
    "raw_stat_card": re.compile(r"\b(?:text-(?:2xl|xl) font-bold|font-bold text-(?:2xl|xl))\b|\bsova-stat\b"),
    # `#rgb`, `#rrggbb` and `#rrggbbaa`. The lookbehind skips `&#39;`-style entities and `word#fragment`; a
    # fragment link such as href="#add" is the one known false positive and is counted in the baseline.
    "hex_color": re.compile(r"(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b"),
    # The hand-written modal overlay shell. #1224 owns the modal component; this only counts the shells.
    "modal_shell": re.compile(r"\b(?:fixed inset-0|inset-0 fixed)\b"),
}

# (pattern, path relative to DASHBOARD_DIR) -> why the file is exempt.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("raw_stat_card", "static/app.js"): "statCardHtml() is the JS twin of the stat_card() macro and emits the classes",
    ("raw_stat_card", "templates/_components.html"): "the stat_card() macro is where the classes are written",
}

# (pattern, path relative to DASHBOARD_DIR) -> (start marker, end marker, why). The text from the start marker
# to the next end marker is blanked for that pattern; every other line of the file is still counted.
EXEMPT_BLOCKS: dict[tuple[str, str], tuple[str, str, str]] = {
    (
        "hex_color",
        "static/app.js",
    ): (
        "var _SOVA_COLOR_FALLBACK = {",
        "};",
        "the SOVA_COLORS fallback table is the one place hex values stand in for the style.css tokens",
    ),
}

# pattern -> path relative to DASHBOARD_DIR -> allowed count. Seeded from the scanner itself (see _seed()).
BASELINE: dict[str, dict[str, int]] = {
    "title_attr": {
        "static/role-editor.js": 1,
        "templates/briefing.html": 1,
        "templates/commands.html": 1,
        "templates/fleet.html": 1,
        "templates/home.html": 3,
        "templates/oversight.html": 3,
        "templates/supervisor.html": 5,
    },
    "arbitrary_text_size": {
        "templates/base.html": 2,
        "templates/commands.html": 11,
    },
    "raw_stat_card": {
        "templates/_head.html": 1,
        "templates/costs.html": 7,
        "templates/dependency_health.html": 18,
        "templates/fleet.html": 6,
        "templates/home.html": 5,
        "templates/pr_metrics.html": 6,
        "templates/reliability.html": 1,
        "templates/run_detail.html": 7,
        "templates/setup.html": 1,
        "templates/spec.html": 7,
        "templates/style_guide.html": 15,
        "templates/work.html": 12,
    },
    "hex_color": {
        "static/js/pages/settings/page.js": 1,
        "templates/supervisor.html": 21,  # the milestone PALETTE bg/border tints (20) and the blocker orange-red
        "templates/run_detail.html": 4,
        "templates/spec.html": 8,
    },
    "modal_shell": {},
}

_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.DOTALL)

# Patterns whose matches inside a Jinja tag are not markup (a macro keyword argument such as `title="x"`).
_BLANK_JINJA_FOR = frozenset({"title_attr"})


def _blank(text: str, regex: re.Pattern[str]) -> str:
    """Replace every match with its newlines only, so line numbers stay right."""
    return regex.sub(lambda m: "\n" * m.group().count("\n"), text)


def _blank_block(text: str, start: str, end: str) -> str:
    begin = text.find(start)
    if begin == -1:
        return text
    stop = text.find(end, begin)
    stop = len(text) if stop == -1 else stop + len(end)
    return text[:begin] + "\n" * text.count("\n", begin, stop) + text[stop:]


def scan_text(text: str, *, jinja: bool, rel: str = "") -> dict[str, list[int]]:
    """Line numbers (1-based) of each pattern's matches in one file's text."""
    found: dict[str, list[int]] = {}
    for name, regex in PATTERNS.items():
        view = _blank(text, _JINJA) if jinja and name in _BLANK_JINJA_FOR else text
        block = EXEMPT_BLOCKS.get((name, rel))
        if block:
            view = _blank_block(view, block[0], block[1])
        found[name] = [view.count("\n", 0, m.start()) + 1 for m in regex.finditer(view)]
    return found


def scanned_files() -> list[Path]:
    """Every first-party .html/.js file under the scan roots; fails if a root is gone or holds nothing."""
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            pytest.fail(f"drift guard scan root is missing (renamed or moved?): {root}")
        found = sorted(
            p for p in root.rglob("*") if p.is_file() and p.suffix in SCAN_SUFFIXES and not p.name.endswith(".min.js")
        )
        if not found:
            pytest.fail(f"drift guard scan root holds no .html or .js files: {root}")
        files.extend(found)
    return files


def scan_tree() -> dict[str, dict[str, list[int]]]:
    """pattern -> relative path -> line numbers, for every non-allowlisted match in the tree."""
    result: dict[str, dict[str, list[int]]] = {name: {} for name in PATTERNS}
    for path in scanned_files():
        rel = path.relative_to(DASHBOARD_DIR).as_posix()
        found = scan_text(path.read_text(encoding="utf-8"), jinja=path.suffix == ".html", rel=rel)
        for name, lines in found.items():
            if lines and (name, rel) not in ALLOWLIST:
                result[name][rel] = lines
    return result


def _seed() -> None:
    """Print BASELINE for the current tree."""
    print("BASELINE: dict[str, dict[str, int]] = {")
    for name, files in scan_tree().items():
        print(f'    "{name}": {{')
        for rel, lines in sorted(files.items()):
            print(f'        "{rel}": {len(lines)},')
        print("    },")
    print("}")


@pytest.fixture(scope="module")
def tree_scan() -> dict[str, dict[str, list[int]]]:
    return scan_tree()


class TestRatchet:
    @pytest.mark.parametrize("pattern", sorted(PATTERNS))
    def test_counts_never_exceed_or_hide_below_the_baseline(
        self, pattern: str, tree_scan: dict[str, dict[str, list[int]]]
    ) -> None:
        actual = tree_scan[pattern]
        baseline = BASELINE[pattern]
        problems: list[str] = []
        for rel in sorted(set(actual) | set(baseline)):
            lines = actual.get(rel, [])
            allowed = baseline.get(rel, 0)
            if len(lines) > allowed:
                problems.append(
                    f"{rel}: {len(lines)} `{pattern}` matches, baseline {allowed}; lines {lines}. "
                    "Use the shared component instead of adding another."
                )
            elif len(lines) < allowed:
                problems.append(
                    f"{rel}: {len(lines)} `{pattern}` matches, baseline {allowed}. Lower the baseline in "
                    f"{Path(__file__).name} to {len(lines)} (delete the entry at 0) so the headroom cannot be reused."
                )
        assert not problems, "\n".join(problems)

    def test_baseline_covers_exactly_the_known_patterns(self) -> None:
        assert set(BASELINE) == set(PATTERNS)

    def test_a_baseline_entry_never_sits_on_an_allowlisted_file(self) -> None:
        both = [(name, rel) for name, files in BASELINE.items() for rel in files if (name, rel) in ALLOWLIST]
        assert both == []


class TestAllowlist:
    @pytest.mark.parametrize(("pattern", "rel"), sorted(ALLOWLIST))
    def test_entry_names_a_real_pattern_and_file_and_gives_a_reason(self, pattern: str, rel: str) -> None:
        assert pattern in PATTERNS
        assert (DASHBOARD_DIR / rel).is_file(), f"stale allowlist entry: {rel}"
        assert len(ALLOWLIST[(pattern, rel)].strip()) >= 12

    @pytest.mark.parametrize(("pattern", "rel"), sorted(EXEMPT_BLOCKS))
    def test_exempt_block_names_a_real_pattern_file_and_block_with_a_reason(self, pattern: str, rel: str) -> None:
        start, end, reason = EXEMPT_BLOCKS[(pattern, rel)]
        text = (DASHBOARD_DIR / rel).read_text(encoding="utf-8")
        assert pattern in PATTERNS
        assert start in text, f"stale exempt block start: {start}"
        assert end in text[text.index(start) :], f"stale exempt block end: {end}"
        assert len(reason.strip()) >= 12

    def test_the_color_files_are_not_blanket_exempt_from_hex(self) -> None:
        assert ("hex_color", "static/app.js") not in ALLOWLIST
        assert ("hex_color", "templates/supervisor.html") not in ALLOWLIST


class TestScanner:
    def test_scan_covers_templates_and_scripts_but_not_vendored_or_minified_files(self) -> None:
        rels = {p.relative_to(DASHBOARD_DIR).as_posix() for p in scanned_files()}
        assert "templates/supervisor.html" in rels
        assert "static/app.js" in rels
        assert not any(r.endswith(".min.js") or r.endswith(".css") for r in rels)

    def test_a_missing_scan_root_fails_instead_of_scanning_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(f"{__name__}.SCAN_ROOTS", (tmp_path / "renamed",))
        with pytest.raises(pytest.fail.Exception, match="scan root is missing"):
            scanned_files()

    def test_an_empty_scan_root_fails_instead_of_scanning_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(f"{__name__}.SCAN_ROOTS", (tmp_path,))
        with pytest.raises(pytest.fail.Exception, match="holds no .html or .js files"):
            scanned_files()

    @pytest.mark.parametrize(
        ("text", "jinja", "expected"),
        [
            ('<button title="Stop">', True, 1),
            ('html += `<span class="x" title="${esc(t)}">`;', False, 1),
            ("'<td title=\\'' + x + '\\'>'", False, 1),
            ("<title>SOVA</title>", True, 0),
            ("const item = { title: 'x' };", False, 0),
            ('<button aria-label="Stop" data-tooltip="Stop">', True, 0),
            ('<div data-title="x">', True, 0),
            ('{{ card_header("Runs", title="Recent") }}', True, 0),
            ('{% set t = {"title": "x"} %}{{ btn("Go", title="x") }}', True, 0),
            ("el.title='x'", False, 0),
        ],
    )
    def test_title_attr(self, text: str, jinja: bool, expected: int) -> None:
        assert len(scan_text(text, jinja=jinja)["title_attr"]) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('class="text-[9px]"', 1),
            ('class="md:text-[10px] text-[11px]"', 2),
            ('class="text-[12px] text-xs"', 0),
        ],
    )
    def test_arbitrary_text_size(self, text: str, expected: int) -> None:
        assert len(scan_text(text, jinja=False)["arbitrary_text_size"]) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('class="text-2xl font-bold"', 1),
            ('class="font-bold text-xl"', 1),
            ('class="sova-stat"><p class="sova-stat-value-sm">', 2),
            ('class="sova-work-stat text-2xl"', 0),
            ('class="text-2xl"', 0),
        ],
    )
    def test_raw_stat_card(self, text: str, expected: int) -> None:
        assert len(scan_text(text, jinja=False)["raw_stat_card"]) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("fill: #fff;", 1),
            ("color: #CBA6F7;", 1),
            ("color: #cba6f7cc;", 1),
            ("'#1e1e2e'", 1),
            ("&#39;", 0),
            ('id="graph" href="/x#section"', 0),
            ("#12345", 0),
            ("#1234567", 0),
            ("#GGGGGG", 0),
        ],
    )
    def test_hex_color(self, text: str, expected: int) -> None:
        assert len(scan_text(text, jinja=False)["hex_color"]) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('<div class="fixed inset-0 bg-black/50">', 1),
            ('<div class="inset-0 fixed">', 1),
            ('<div class="absolute inset-0">', 0),
        ],
    )
    def test_modal_shell(self, text: str, expected: int) -> None:
        assert len(scan_text(text, jinja=False)["modal_shell"]) == expected

    @pytest.mark.parametrize(
        ("text", "pattern"),
        [
            ('{% set c = "#ff0000" %}', "hex_color"),
            ('{{ x or "text-[10px]" }}', "arbitrary_text_size"),
            ("{{ stat_card('Cost', 'c', value_class='text-2xl font-bold') }}", "raw_stat_card"),
            ("{{ 'fixed inset-0' if open }}", "modal_shell"),
        ],
    )
    def test_a_violation_written_inside_a_jinja_tag_still_counts(self, text: str, pattern: str) -> None:
        assert len(scan_text(text, jinja=True)[pattern]) == 1

    def test_an_exempt_block_hides_hex_only_inside_the_block(self) -> None:
        text = "var T = {\n  a: '#111111',\n};\nconst x = '#222222';\n"
        monkey = {("hex_color", "f.js"): ("var T = {", "};", "a table of fallback values")}
        original = dict(EXEMPT_BLOCKS)
        EXEMPT_BLOCKS.update(monkey)
        try:
            assert scan_text(text, jinja=False, rel="f.js")["hex_color"] == [4]
            assert scan_text(text, jinja=False, rel="g.js")["hex_color"] == [2, 4]
        finally:
            EXEMPT_BLOCKS.clear()
            EXEMPT_BLOCKS.update(original)

    def test_a_directory_named_like_a_script_is_not_scanned_as_a_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "odd.js").mkdir()
        (tmp_path / "real.js").write_text("x", encoding="utf-8")
        monkeypatch.setattr(f"{__name__}.SCAN_ROOTS", (tmp_path,))
        assert [p.name for p in scanned_files()] == ["real.js"]

    def test_line_numbers_survive_blanked_jinja_blocks_and_crlf(self) -> None:
        text = '{% if a %}\n{# note\n#}\n<div class="fixed inset-0">\n'
        assert scan_text(text, jinja=True)["modal_shell"] == [4]
        assert scan_text(text.replace("\n", "\r\n"), jinja=True)["modal_shell"] == [4]


if __name__ == "__main__":
    _seed()
