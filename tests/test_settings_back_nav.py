"""The Settings page's Back control and its browser history (#1566), run under node.

`back-nav.js` takes `history`, `location` and the page's `route()` as arguments, so these tests drive it against a
small simulation of the browser's session history: `pushState` adds an entry and fires no `hashchange`, a traversal
(Back or Forward) restores the entry's state and fires `hashchange` when the hash differs, and assigning
`location.hash` adds an entry and fires `hashchange`. The view is what the page's own `state.layoutView()` returns
for the hash `route()` sees, so a missing `route()` call after `pushState` shows as a stale view.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SETTINGS_JS = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "js" / "pages" / "settings"

_PRELUDE = """
import { pathToFileURL } from 'node:url';
const { createBackNav } = await import(pathToFileURL(process.argv[1]).href);
const state = await import(pathToFileURL(process.argv[2]).href);

function browser(startHash) {
  const entries = [{ hash: startHash, state: null }];
  let index = 0;
  const listeners = [];
  const fire = () => listeners.forEach((fn) => fn());
  const location = {
    pathname: '/settings', search: '',
    get hash() { return entries[index].hash; },
    set hash(h) { entries.splice(index + 1); entries.push({ hash: '#' + h, state: null }); index += 1; fire(); },
  };
  const traverse = (delta) => {
    const from = entries[index].hash;
    index = Math.min(Math.max(index + delta, 0), entries.length - 1);
    if (entries[index].hash !== from) fire();
  };
  const history = {
    get state() { return entries[index].state; },
    get length() { return entries.length; },
    pushState(st, _title, url) {
      entries.splice(index + 1);
      entries.push({ hash: url.includes('#') ? url.slice(url.indexOf('#')) : '', state: st });
      index += 1;
    },
    back() { traverse(-1); },
    forward() { traverse(1); },
  };
  return { location, history, listeners };
}

function page(startHash) {
  const b = browser(startHash);
  const seen = { view: null, scrolls: 0 };
  const route = () => { seen.view = state.layoutView(b.location.hash.slice(1)); };
  b.listeners.push(route);
  route();
  const nav = createBackNav({
    history: b.history, location: b.location, route,
    isHomeView: () => seen.view === 'home',
    scrollTop: () => { seen.scrolls += 1; },
  });
  const now = () => ({ hash: b.location.hash, view: seen.view, entries: b.history.length });
  return { nav, history: b.history, now, seen };
}
"""


def _run(script: str) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run the Settings Back navigation")
        pytest.skip("node is required to run the Settings Back navigation")
    source = _PRELUDE + "\nconst out = {};\n" + script + "\nprocess.stdout.write(JSON.stringify(out));\n"
    proc = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            source,
            str(_SETTINGS_JS / "back-nav.js"),
            str(_SETTINGS_JS / "state.js"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestBackNavigation:
    def test_back_from_a_section_opened_on_home_returns_home_without_a_new_entry(self) -> None:
        out = _run(
            """
            const p = page('');
            out.start = p.now();
            p.nav.go('supervisor');
            out.opened = p.now();
            p.nav.goHome();
            out.home = p.now();
            """
        )
        assert out["start"] == {"hash": "", "view": "home", "entries": 1}
        assert out["opened"] == {"hash": "#supervisor", "view": "section", "entries": 2}
        assert out["home"] == {"hash": "", "view": "home", "entries": 2}

    def test_browser_back_then_forward_then_the_back_control_still_returns_home(self) -> None:
        out = _run(
            """
            const p = page('');
            p.nav.go('supervisor');
            p.history.back();
            out.afterBack = p.now();
            p.history.forward();
            out.afterForward = p.now();
            p.nav.goHome();
            out.home = p.now();
            """
        )
        assert out["afterBack"]["view"] == "home"
        assert out["afterBack"]["hash"] == ""
        assert out["afterForward"]["view"] == "section"
        assert out["afterForward"]["hash"] == "#supervisor"
        # Forward restored the provenance, so Back traversed history: no home entry was pushed on top.
        assert out["home"] == {"hash": "", "view": "home", "entries": 2}

    def test_back_from_a_directly_opened_section_pushes_a_home_entry_and_shows_home(self) -> None:
        out = _run(
            """
            const p = page('#supervisor');
            out.start = p.now();
            p.nav.goHome();
            out.home = p.now();
            out.scrolls = p.seen.scrolls;
            """
        )
        assert out["start"] == {"hash": "#supervisor", "view": "section", "entries": 1}
        assert out["home"] == {"hash": "", "view": "home", "entries": 2}
        assert out["scrolls"] == 1

    def test_a_section_opened_from_another_section_is_not_marked_as_from_home(self) -> None:
        out = _run(
            """
            const p = page('');
            p.nav.go('supervisor');
            p.nav.go('agent');
            out.moved = p.now();
            p.nav.goHome();
            out.home = p.now();
            """
        )
        assert out["moved"] == {"hash": "#agent", "view": "section", "entries": 3}
        # The second section was reached by assigning the hash, so it carries no provenance and Back pushes home.
        assert out["home"] == {"hash": "", "view": "home", "entries": 4}

    def test_following_the_current_section_again_adds_no_entry(self) -> None:
        out = _run(
            """
            const p = page('');
            p.nav.go('supervisor');
            p.nav.go('supervisor');
            out.after = p.now();
            """
        )
        assert out["after"] == {"hash": "#supervisor", "view": "section", "entries": 2}
