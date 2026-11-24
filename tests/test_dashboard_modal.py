"""The shared modal component (`sovaModalOpen`, `sovaModalClose`, `sovaConfirm` in `app.js`, #1224).

There is no browser here, so the component runs under node against `tests/fixtures/fake_dom.cjs`: the real
`escapeHtml`, `sovaBtn` and modal sections of `app.js`, sliced out by their section banners, in a VM context
whose `document` is the fake DOM. A missing `node` skips locally but fails under CI (the same rule as
`test_dashboard_agents_modules.py`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import DASHBOARD_DIR

_APP_JS = DASHBOARD_DIR / "static" / "app.js"
_FAKE_DOM = Path(__file__).parent / "fixtures" / "fake_dom.cjs"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const { Document, key, clickOn } = require(process.argv[3]);
const statics = path.dirname(process.argv[2]);  // app.js lives in static/

const src = fs.readFileSync(process.argv[2], 'utf8');
// Each slice runs from its section banner's comment to the next banner's: a renamed banner fails here.
function section(startMarker, endMarker) {
  const startAt = src.indexOf(startMarker);
  const endAt = src.indexOf(endMarker);
  if (startAt < 0 || endAt < 0) throw new Error('section not found: ' + (startAt < 0 ? startMarker : endMarker));
  const start = src.lastIndexOf('/*', startAt);
  const end = src.lastIndexOf('/*', endAt);
  if (start < 0 || end <= start) throw new Error('section not found: ' + startMarker);
  return src.slice(start, end);
}
// One function declaration, from its keyword to the first closing brace that ends a slice node can compile, so a
// brace inside the body (a block, an object literal, a string) cannot cut it short.
function declaration(text, name) {
  let start = text.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('function not found: ' + name);
  if (text.slice(start - 6, start) === 'async ') start -= 6;
  for (let end = text.indexOf('}', start); end !== -1; end = text.indexOf('}', end + 1)) {
    const slice = text.slice(start, end + 1);
    try { new vm.Script(slice); return slice + '\n'; } catch (e) { /* not the end yet */ }
  }
  throw new Error('function never closes: ' + name);
}
const code = declaration(src, 'escapeHtml') +
  section('2b. COMPONENT HELPERS', '3. BROWSER NOTIFICATION API') +
  section('5. MODAL DIALOGS', '6. VISIBILITY-AWARE POLLING');

const doc = new Document();
const ctx = { console, document: doc, setTimeout, clearTimeout, Promise, WeakMap, WeakSet, Array, String };
vm.createContext(ctx);
vm.runInContext(code, ctx);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const FADE = 200;  // past the 150 ms fade-out
const out = {};

// A page button that opens dialogs, and a bubble-phase Escape listener standing in for the page's own (the
// Work drawer and the feed panel close on Escape).
doc.body.innerHTML = '<button id="trigger">Open</button><main id="page"></main>';
const trigger = doc.getElementById('trigger');
let pageEscapes = 0;
doc.addEventListener('keydown', (e) => { if (e.key === 'Escape') pageEscapes++; });
const backdrops = () => doc.body.querySelectorAll('.sova-modal-backdrop');
const focusedText = () => doc.activeElement.textContent.trim();

(async () => {
  // Primary confirm: ARIA, Confirm focused, click resolves true, faded out and removed, focus back on the trigger.
  trigger.focus();
  let p = ctx.sovaConfirm('Approve the <spec>?', { title: 'Approve', confirmText: 'Approve' });
  let dialog = doc.body.querySelector('.sova-modal-dialog');
  const backdrop = backdrops()[0];
  out.primary = {
    role: dialog.getAttribute('role'),
    modal: dialog.getAttribute('aria-modal'),
    label: doc.getElementById(dialog.getAttribute('aria-labelledby')).textContent,
    described: doc.getElementById(dialog.getAttribute('aria-describedby')).textContent,
    visible: backdrop.classList.contains('sova-modal-visible'),
    hidden: backdrop.hidden,
    focused: focusedText(),
    confirmClass: dialog.querySelector('.sova-modal-confirm').className,
    cancelClass: dialog.querySelector('.sova-modal-cancel').className,
    isOpen: ctx.sovaModalIsOpen(),
  };
  // A second confirm while one is open resolves false at once and adds nothing.
  out.secondWhileOpen = await ctx.sovaConfirm('Again?');
  out.backdropsWhileOpen = backdrops().length;
  // Enter is not intercepted: with no button to press nothing happens and the dialog stays.
  doc.body.focus();
  const enter = key(doc, 'Enter');
  out.enterNotIntercepted = !enter.defaultPrevented && ctx.sovaModalIsOpen();
  dialog.querySelector('.sova-modal-confirm').click();
  out.primary.result = await p;
  out.primary.fadingOut = !backdrop.classList.contains('sova-modal-visible') && backdrops().length === 1;
  out.primary.focusReturned = doc.activeElement === trigger;
  // The next confirm of an `await sovaConfirm(a); await sovaConfirm(b)` opens while the first still fades out: it
  // shows and answers the user's click, rather than counting the fading backdrop as an open confirm.
  p = ctx.sovaConfirm('And then?', { confirmText: 'Next' });
  const next = backdrops().find((b) => b !== backdrop);
  out.duringFade = { shown: !!next && next.classList.contains('sova-modal-visible'), focused: focusedText() };
  next.querySelector('.sova-modal-confirm').click();
  out.duringFade.result = await p;
  await sleep(FADE);
  out.primary.removedAfterFade = backdrops().length === 0;
  out.primary.closedState = !ctx.sovaModalIsOpen();

  // Danger: Cancel focused on open, so a stray Enter on the focused button cancels.
  trigger.focus();
  p = ctx.sovaConfirm('Delete role "x"?', { confirmText: 'Delete', confirmClass: 'danger' });
  dialog = backdrops()[0].querySelector('.sova-modal-dialog');
  out.danger = { focused: focusedText(), confirmClass: dialog.querySelector('.sova-modal-confirm').className };
  doc.activeElement.click();  // what Enter on a focused button does
  out.danger.result = await p;
  await sleep(FADE);

  // Escape closes the dialog and only the dialog: the page's own Escape listener never sees it.
  trigger.focus();
  pageEscapes = 0;
  p = ctx.sovaConfirm('Abandon?', { confirmClass: 'danger' });
  const esc = key(doc, 'Escape');
  out.escape = { result: await p, pageEscapes, prevented: esc.defaultPrevented };
  out.escape.focusReturned = doc.activeElement === trigger;
  await sleep(FADE);
  key(doc, 'Escape');
  out.escape.pageGetsEscapeWithNoDialog = pageEscapes === 1;

  // Warning with details: each detail is an escaped list item, the button is the warning variant, Cancel is focused.
  trigger.focus();
  p = ctx.sovaConfirm('This PR is not fully ready to merge. Run Integrate PR anyway?', {
    title: 'Not ready to merge', confirmText: 'Integrate PR anyway', confirmClass: 'warning',
    details: ['The SOVA review has not approved this PR.', '<b>CI</b> is failing.', '', null],
  });
  dialog = backdrops()[0].querySelector('.sova-modal-dialog');
  const list = dialog.querySelector('.sova-modal-details');
  out.warning = {
    items: list.querySelectorAll('li').map((li) => li.textContent),
    boldTags: list.querySelectorAll('b').length,
    describedBy: dialog.getAttribute('aria-describedby').split(' ').map((id) => !!doc.getElementById(id)),
    confirmClass: dialog.querySelector('.sova-modal-confirm').className,
    confirmText: dialog.querySelector('.sova-modal-confirm').textContent,
    focused: focusedText(),
  };
  // Tab cycles inside: Cancel -> Integrate -> Cancel, and Shift+Tab goes back around.
  const tabs = [];
  for (let i = 0; i < 3; i++) {
    const ev = key(doc, 'Tab');
    if (!ev.defaultPrevented) {  // the browser's own move to the next element, which the trap lets through
      const items = dialog.querySelectorAll('button');
      items[items.indexOf(doc.activeElement) + 1].focus();
    }
    tabs.push(focusedText());
  }
  out.warning.tabs = tabs;
  doc.getElementById('trigger').focus();  // focus escaped the dialog somehow: Tab brings it back in
  key(doc, 'Tab');
  out.warning.tabFromOutside = focusedText();
  key(doc, 'Tab', { shiftKey: true });
  out.warning.shiftTabFromFirst = focusedText();
  // A press inside the dialog released over the backdrop is not a backdrop click; a real one closes.
  clickOn(backdrops()[0], dialog);
  out.warning.dragOutKeepsOpen = ctx.sovaModalIsOpen();
  clickOn(backdrops()[0]);
  out.warning.result = await p;
  await sleep(FADE);

  // A page modal (template markup, kept in the page): hidden until opened, labelled by its heading, first
  // rendered field focused, onClose gets the page's reason, hidden again after the fade, never removed.
  doc.getElementById('page').innerHTML =
    '<div id="page-modal" class="sova-modal-backdrop" hidden>' +
      '<div class="sova-modal-dialog">' +
        '<div class="sova-modal-header"><h3 class="sova-modal-title">Create Custom Role</h3></div>' +
        '<div class="sova-modal-body">' +
          '<div class="hidden"><input id="skipped"></div>' +
          '<input id="name"><input id="desc" disabled><input id="kind" type="hidden"><select id="sel"></select>' +
        '</div>' +
        '<div class="sova-modal-footer"><button id="cancel">Cancel</button><button id="ok">Create</button></div>' +
      '</div>' +
    '</div>';
  const pageModal = doc.getElementById('page-modal');
  const pageDialog = pageModal.querySelector('.sova-modal-dialog');
  const reasons = [];
  trigger.focus();
  ctx.sovaModalOpen(pageModal, { onClose: (r) => reasons.push(r) });
  ctx.sovaModalOpen(pageModal, { onClose: (r) => reasons.push('second ' + r) });  // already open: ignored
  out.page = {
    hidden: pageModal.hidden,
    role: pageDialog.getAttribute('role'),
    modal: pageDialog.getAttribute('aria-modal'),
    label: doc.getElementById(pageDialog.getAttribute('aria-labelledby')).textContent,
    focused: doc.activeElement.id,
  };
  // Shift+Tab from the first field wraps to the last button, skipping the hidden, disabled and type=hidden ones.
  key(doc, 'Tab', { shiftKey: true });
  out.page.shiftTabWraps = doc.activeElement.id;
  key(doc, 'Tab');
  out.page.tabWraps = doc.activeElement.id;
  ctx.sovaModalClose(pageModal, 'created');
  ctx.sovaModalClose(pageModal, 'twice');  // already closed: ignored
  out.page.reasons = reasons.slice();
  out.page.focusReturned = doc.activeElement === trigger;
  await sleep(FADE);
  out.page.hiddenAfterFade = pageModal.hidden && pageModal.isConnected;

  // Reopened during its fade-out, the modal stays shown: the pending hide is cancelled.
  ctx.sovaModalOpen(pageModal, {});
  ctx.sovaModalClose(pageModal, 'x');
  ctx.sovaModalOpen(pageModal, {});
  await sleep(FADE);
  out.page.reopenDuringFadeStaysShown = !pageModal.hidden && pageModal.classList.contains('sova-modal-visible');
  // closeOnBackdrop: false ignores a backdrop click.
  ctx.sovaModalClose(pageModal, 'x');
  await sleep(FADE);
  ctx.sovaModalOpen(pageModal, { closeOnBackdrop: false });
  clickOn(pageModal);
  out.page.backdropIgnored = ctx.sovaModalIsOpen(pageModal);

  // Stacked: a confirm over the page modal takes the keys; Escape closes only the confirm, focus goes back into
  // the page modal, and the next Escape closes the page modal.
  doc.getElementById('ok').focus();
  p = ctx.sovaConfirm('Discard?');
  key(doc, 'Escape');
  out.stack = { confirmResult: await p, pageStillOpen: ctx.sovaModalIsOpen(pageModal), focus: doc.activeElement.id };
  key(doc, 'Escape');
  out.stack.pageClosed = !ctx.sovaModalIsOpen(pageModal);

  // A trigger the page re-rendered away gets no focus call; focus is simply not restored.
  const gone = doc.createElement('button');
  doc.body.appendChild(gone);
  gone.focus();
  p = ctx.sovaConfirm('Go?');
  gone.remove();
  key(doc, 'Escape');
  const goneResult = await p;
  await sleep(FADE);
  out.goneTrigger = { result: goneResult, active: doc.activeElement.tagName };

  // returnFocus: a modal opened after an async step (focus has since moved) gives focus back to the trigger the
  // page captured beforehand, not to whatever held focus at open time.
  doc.getElementById('page').innerHTML =
    '<div id="late" class="sova-modal-backdrop" hidden><div class="sova-modal-dialog">' +
    '<button id="late-ok">OK</button></div></div>';
  const late = doc.getElementById('late');
  trigger.focus();
  const captured = doc.activeElement;
  const elsewhere = doc.createElement('button');
  doc.body.appendChild(elsewhere);
  elsewhere.focus();
  ctx.sovaModalOpen(late, { returnFocus: captured });
  ctx.sovaModalClose(late, 'x');
  out.returnFocus = { toCaptured: doc.activeElement === trigger };
  await sleep(FADE);
  elsewhere.remove();

  // A dialog with nothing focusable takes focus itself, and Tab stays put.
  doc.getElementById('page').innerHTML =
    '<div id="bare" class="sova-modal-backdrop" hidden><div class="sova-modal-dialog"><p>Loading</p></div></div>';
  const bare = doc.getElementById('bare');
  ctx.sovaModalOpen(bare, {});
  const bareDialog = bare.querySelector('.sova-modal-dialog');
  out.bare = { focusedDialog: doc.activeElement === bareDialog, tabPrevented: key(doc, 'Tab').defaultPrevented };
  ctx.sovaModalClose(bare, 'x');

  // Integrate from a Work drawer: the real actions.js handler opens the warning confirm listing the merge
  // blockers, and the real drawer.js outside-click and selection.js Escape listeners run alongside it, so the
  // drawer must stay open whether the dialog is cancelled with Escape, a backdrop click or confirmed.
  const work = (rel) => fs.readFileSync(path.join(statics, 'js', 'pages', 'work', rel), 'utf8');
  const drawerSrc = work('drawer.js');
  const listenersFrom = (text, marker) => {
    const start = text.indexOf(marker);
    if (start < 0) throw new Error('listener not found: ' + marker);
    return text.slice(start, text.indexOf('\n}', start));
  };
  const drawerSlice = (text) => {
    const from = text.indexOf('var _KEEP_OPEN');
    const to = text.indexOf('export function init()');
    if (from < 0 || to < 0) throw new Error('drawer helpers not found');
    return text.slice(from, to);
  };
  vm.runInContext(
    'var _selected = 42; var drawerCloses = 0; var commands = [];\n' +
    'function getSelected() { return _selected; }\n' +
    'function clearSelection() { _selected = null; drawerCloses++; }\n' +
    'function _runCommand(issue, command, args) { commands.push({ issue: issue, command: command, args: args }); }\n' +
    drawerSlice(drawerSrc) +
    listenersFrom(drawerSrc, "document.addEventListener('pointerdown'") + '\n' +
    listenersFrom(work('selection.js'), "document.addEventListener('keydown'") + '\n' +
    declaration(work('actions.js'), '_runServerAction'), ctx);
  doc.getElementById('page').innerHTML =
    '<aside id="work-drawer"><button id="integrate">Integrate PR</button></aside><p id="outside">Board</p>';
  const integrate = doc.getElementById('integrate');
  // A capture-phase Escape listener below the document stands in for any other page handler.
  let bodyCaptureEscapes = 0;
  doc.body.addEventListener('keydown', (e) => { if (e.key === 'Escape') bodyCaptureEscapes++; }, true);
  const action = {
    label: 'Integrate PR', handler: 'run_command', handler_args: { command: 'integrate-pr', pr: 7 },
    merge_blockers: ['CI checks are failing.', 'Some review threads are still unresolved.'],
  };
  const openIntegrate = () => {
    clickOn(integrate);  // the press and click that open it land inside the drawer
    integrate.focus();
    return ctx._runServerAction('42', action);
  };
  let run = openIntegrate();
  dialog = backdrops()[0].querySelector('.sova-modal-dialog');
  out.integrate = {
    title: doc.getElementById(dialog.getAttribute('aria-labelledby')).textContent,
    message: dialog.querySelector('.text-gray-400').textContent,
    items: dialog.querySelector('.sova-modal-details').querySelectorAll('li').map((li) => li.textContent),
    confirmText: dialog.querySelector('.sova-modal-confirm').textContent,
    confirmClass: dialog.querySelector('.sova-modal-confirm').className,
    focused: focusedText(),
  };
  key(doc, 'Escape');
  await run;
  out.integrate.escape = {
    drawerOpen: ctx.getSelected() === 42, bodyCaptureEscapes, commands: ctx.commands.length,
    focusReturned: doc.activeElement === integrate,
  };
  await sleep(FADE);
  run = openIntegrate();
  clickOn(backdrops()[0]);
  await run;
  out.integrate.backdrop = { drawerOpen: ctx.getSelected() === 42, commands: ctx.commands.length };
  await sleep(FADE);
  run = openIntegrate();
  clickOn(backdrops()[0].querySelector('.sova-modal-confirm'));
  await run;
  out.integrate.confirm = { drawerOpen: ctx.getSelected() === 42, commands: JSON.parse(JSON.stringify(ctx.commands)) };
  await sleep(FADE);
  // The stand-in is live: with no dialog open, a click outside the drawer does close it.
  clickOn(doc.getElementById('outside'));
  out.integrate.outsideClickCloses = ctx.getSelected() === null && ctx.drawerCloses === 1;

  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def modal_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI: it runs the modal component behavior tests")
        pytest.skip("node is required to run the modal component behavior tests")
    script: Path = tmp_path_factory.mktemp("modal") / "harness.cjs"
    script.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script), str(_APP_JS), str(_FAKE_DOM)], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestSovaConfirm:
    def test_primary_confirm_is_an_accessible_dialog_focused_on_confirm(self, modal_run: dict) -> None:
        primary = modal_run["primary"]
        assert primary["role"] == "alertdialog"
        assert primary["modal"] == "true"
        assert primary["label"] == "Approve"
        assert primary["described"] == "Approve the <spec>?"
        assert primary["visible"]
        assert not primary["hidden"]
        assert primary["isOpen"]
        assert primary["focused"] == "Approve"
        assert "sova-btn sova-btn-primary" in primary["confirmClass"]
        assert "sova-btn sova-btn-ghost" in primary["cancelClass"]

    def test_confirm_click_resolves_true_fades_out_and_returns_focus(self, modal_run: dict) -> None:
        primary = modal_run["primary"]
        assert primary["result"] is True
        assert primary["fadingOut"], "the backdrop must stay in the document while it fades out"
        assert primary["removedAfterFade"]
        assert primary["focusReturned"]
        assert primary["closedState"]

    def test_only_one_confirm_at_a_time(self, modal_run: dict) -> None:
        assert modal_run["secondWhileOpen"] is False
        assert modal_run["backdropsWhileOpen"] == 1

    def test_a_confirm_opened_while_the_last_one_fades_out_still_shows(self, modal_run: dict) -> None:
        assert modal_run["duringFade"] == {"shown": True, "focused": "Next", "result": True}

    def test_enter_is_not_intercepted(self, modal_run: dict) -> None:
        assert modal_run["enterNotIntercepted"]

    def test_danger_opens_on_cancel_so_a_stray_enter_cancels(self, modal_run: dict) -> None:
        danger = modal_run["danger"]
        assert danger["focused"] == "Cancel"
        assert "sova-btn-danger" in danger["confirmClass"]
        assert danger["result"] is False

    def test_escape_closes_only_the_dialog(self, modal_run: dict) -> None:
        escape = modal_run["escape"]
        assert escape["result"] is False
        assert escape["pageEscapes"] == 0, "the page's own Escape handler must not also fire"
        assert escape["prevented"]
        assert escape["focusReturned"]
        assert escape["pageGetsEscapeWithNoDialog"]

    def test_warning_lists_escaped_details_and_opens_on_cancel(self, modal_run: dict) -> None:
        warning = modal_run["warning"]
        assert warning["items"] == ["The SOVA review has not approved this PR.", "<b>CI</b> is failing."]
        assert warning["boldTags"] == 0
        assert warning["describedBy"] == [True, True]
        assert "sova-btn-warning" in warning["confirmClass"]
        assert warning["confirmText"] == "Integrate PR anyway"
        assert warning["focused"] == "Cancel"

    def test_tab_is_trapped(self, modal_run: dict) -> None:
        warning = modal_run["warning"]
        assert warning["tabs"] == ["Integrate PR anyway", "Cancel", "Integrate PR anyway"]
        assert warning["tabFromOutside"] == "Cancel"
        assert warning["shiftTabFromFirst"] == "Integrate PR anyway"

    def test_backdrop_click_closes_but_a_drag_out_of_the_dialog_does_not(self, modal_run: dict) -> None:
        assert modal_run["warning"]["dragOutKeepsOpen"]
        assert modal_run["warning"]["result"] is False


def _css_rule(selector: str) -> str:
    css = (DASHBOARD_DIR / "static" / "style.css").read_text(encoding="utf-8")
    start = css.index("\n" + selector + " {") + 1
    return css[start : css.index("}", start)]


class TestModalStyle:
    def test_visibility_switches_at_once_on_open_and_after_the_fade_on_close(self) -> None:
        """sovaModalOpen() focuses inside the dialog in the same task it adds .sova-modal-visible. An animated
        visibility is still hidden on that first frame, and the browser silently ignores focus() on a hidden
        element, so the dialog would open with focus left on the trigger (found in Chrome, #1224)."""
        assert "visibility 0s;" in _css_rule(".sova-modal-backdrop.sova-modal-visible")
        assert "visibility 0s 150ms" in _css_rule(".sova-modal-backdrop")

    def test_a_closed_modal_has_no_layout(self) -> None:
        assert "display: none" in _css_rule(".sova-modal-backdrop[hidden]")


class TestPageModal:
    def test_open_shows_labels_and_focuses_the_first_rendered_field(self, modal_run: dict) -> None:
        page = modal_run["page"]
        assert page["hidden"] is False
        assert page["role"] == "dialog"
        assert page["modal"] == "true"
        assert page["label"] == "Create Custom Role"
        assert page["focused"] == "name"

    def test_tab_skips_hidden_and_disabled_fields(self, modal_run: dict) -> None:
        assert modal_run["page"]["shiftTabWraps"] == "ok"
        assert modal_run["page"]["tabWraps"] == "name"

    def test_close_reports_the_reason_once_and_hides_after_the_fade(self, modal_run: dict) -> None:
        page = modal_run["page"]
        assert page["reasons"] == ["created"]
        assert page["focusReturned"]
        assert page["hiddenAfterFade"]

    def test_return_focus_option_overrides_the_element_focused_at_open(self, modal_run: dict) -> None:
        assert modal_run["returnFocus"]["toCaptured"]

    def test_reopen_during_fade_out_stays_open(self, modal_run: dict) -> None:
        assert modal_run["page"]["reopenDuringFadeStaysShown"]

    def test_close_on_backdrop_can_be_turned_off(self, modal_run: dict) -> None:
        assert modal_run["page"]["backdropIgnored"]

    def test_stacked_modals_close_top_first(self, modal_run: dict) -> None:
        stack = modal_run["stack"]
        assert stack["confirmResult"] is False
        assert stack["pageStillOpen"]
        assert stack["focus"] == "ok"
        assert stack["pageClosed"]

    def test_a_removed_trigger_is_not_refocused(self, modal_run: dict) -> None:
        assert modal_run["goneTrigger"] == {"result": False, "active": "BODY"}

    def test_a_dialog_with_nothing_focusable_takes_focus_itself(self, modal_run: dict) -> None:
        assert modal_run["bare"] == {"focusedDialog": True, "tabPrevented": True}


class TestIntegrateFromWorkDrawer:
    def test_lists_the_merge_blockers_in_a_warning_dialog_opened_on_cancel(self, modal_run: dict) -> None:
        integrate = modal_run["integrate"]
        assert integrate["title"] == "Not ready to merge"
        assert integrate["message"] == "This PR is not fully ready to merge. Run Integrate PR anyway?"
        assert integrate["items"] == ["CI checks are failing.", "Some review threads are still unresolved."]
        assert integrate["confirmText"] == "Integrate PR anyway"
        assert integrate["focused"] == "Cancel"
        assert "sova-btn sova-btn-warning" in integrate["confirmClass"]

    def test_the_drawer_stand_in_does_close_on_a_click_outside(self, modal_run: dict) -> None:
        """Without this the drawer-stays-open checks below could pass against listeners that never run."""
        assert modal_run["integrate"]["outsideClickCloses"]

    def test_escape_cancels_and_leaves_the_drawer_open(self, modal_run: dict) -> None:
        assert modal_run["integrate"]["escape"] == {
            "drawerOpen": True,
            "bodyCaptureEscapes": 0,
            "commands": 0,
            "focusReturned": True,
        }

    def test_a_backdrop_click_cancels_and_leaves_the_drawer_open(self, modal_run: dict) -> None:
        assert modal_run["integrate"]["backdrop"] == {"drawerOpen": True, "commands": 0}

    def test_confirming_runs_the_integrate_command_and_leaves_the_drawer_open(self, modal_run: dict) -> None:
        assert modal_run["integrate"]["confirm"] == {
            "drawerOpen": True,
            "commands": [{"issue": "42", "command": "integrate-pr", "args": {"pr": 7}}],
        }
