# SOVA Design System

## Overview

The SOVA dashboard uses the **Catppuccin Mocha** dark theme with Tailwind CSS (CDN).
All colors are defined as CSS custom properties in `sova/dashboard/static/style.css`,
enabling centralized theming and runtime access from JavaScript.

## Color System

### Architecture

```
:root (CSS vars)  -->  Tailwind config (rgb() with <alpha-value>)  -->  Template classes
                  -->  window.SOVA_COLORS (JS, via getComputedStyle)  -->  Dynamic rendering
```

### Catppuccin Mocha Palette

| Token | Hex | Role |
|-------|-----|------|
| `--ctp-crust` | `#11111b` | Deepest background |
| `--ctp-mantle` | `#181825` | Page background (`surface`) |
| `--ctp-base` | `#1e1e2e` | Card background (`surface-card`, `sidebar`) |
| `--ctp-surface0` | `#313244` | Hover states, subtle contrast |
| `--ctp-surface1` | `#45475a` | Active states, borders |
| `--ctp-surface2` | `#585b70` | Scrollbar thumb hover |
| `--ctp-overlay0` | `#6c7086` | Faint text, disabled |
| `--ctp-overlay1` | `#7f849c` | Muted text |
| `--ctp-text` | `#cdd6f4` | Primary text |
| `--ctp-blue` | `#89b4fa` | Accent, links, primary actions |
| `--ctp-green` | `#a6e3a1` | Success, done, money |
| `--ctp-red` | `#f38ba8` | Error, failed, danger |
| `--ctp-yellow` | `#f9e2af` | Warning, running, action needed |
| `--ctp-mauve` | `#cba6f7` | Purple, metadata, researcher |

### Semantic Aliases

| Alias | Maps To | Usage |
|-------|---------|-------|
| `--color-accent` | `--ctp-blue` | Primary accent color |
| `--color-accent-green` | `--ctp-green` | Success states |
| `--color-accent-red` | `--ctp-red` | Error states |
| `--color-accent-yellow` | `--ctp-yellow` | Warning states |
| `--color-accent-purple` | `--ctp-mauve` | Metadata |
| `--color-surface` | `--ctp-mantle` | Page background |
| `--color-surface-card` | `--ctp-base` | Card background |
| `--color-border` | `--ctp-surface1` | Default border |
| `--color-text` | `--ctp-text` | Primary text |
| `--color-text-muted` | `--ctp-overlay1` | Secondary text |
| `--color-text-faint` | `--ctp-overlay0` | Tertiary text |

### Tailwind Usage

Colors are defined with RGB channel variables for Tailwind alpha modifier support:

```css
:root { --ctp-blue-rgb: 137, 180, 250; }
```

```javascript
// In _head.html Tailwind config:
accent: { DEFAULT: 'rgb(var(--ctp-blue-rgb) / <alpha-value>)' }
```

This enables: `bg-accent`, `bg-accent/20`, `border-accent/30`, `text-accent`, etc.

### JavaScript Usage

```javascript
// Read from CSS vars (initialized on page load):
window.SOVA_COLORS.accent   // "#89b4fa"
window.SOVA_COLORS.green    // "#a6e3a1"
window.SOVA_COLORS.red      // "#f38ba8"

// Run (role) colors come from the server's run action table (window.SOVA_RUN_ACTIONS):
roleColor('developer').hex  // the action's colour token, read from SOVA_COLORS.accent
runActionColors('teal')     // {bg, text, dot, border, hex} for a colour token
```

## Typography Scale

| Role | Classes | Size | Usage |
|------|---------|------|-------|
| Page heading | `text-2xl font-semibold` | 24px | Page titles (one per page) |
| Section heading | `text-lg font-semibold` | 18px | Card titles, section headers |
| Label | `text-sm font-medium` | 14px | Stat titles, form labels |
| Body | `text-sm` (default) | 14px | General text, table cells |
| Caption | `text-xs text-gray-500` | 12px | Timestamps, secondary info |
| Data | `font-mono font-semibold` | inherit | Monetary amounts, IDs |

**Minimum text size**: `text-xs` (12px). Never use `text-[10px]` or smaller.

## Component Classes

All component classes are defined in `sova/dashboard/static/style.css`.

### Cards

| Class | Description |
|-------|-------------|
| `.sova-card` | Card container (bg, border, rounded) |
| `.sova-card-header` | Card header with flex layout and bottom border |
| `.sova-card-body` | Card body with padding |
| `.sova-stat` | Stat card container |
| `.sova-stat-label` | Uppercase label in stat card |
| `.sova-stat-value` | Large bold number |
| `.sova-stat-detail` | Small detail text below stat value |

To recolour a stat value or a badge after it renders, call `setColorClasses(el, colorClasses)` (`app.js`): it swaps only the palette colour classes (text and background colours such as `text-accent-red`, `bg-gray-600/30`, `text-white`, plus `border-accent-*`) and keeps every other class, the component's own (`sova-stat-value` and its size, `sova-badge`) and size or alignment utilities (`text-xs`, `text-right`) alike, which a `className` write would drop.

### Buttons

| Class | Color | Usage |
|-------|-------|-------|
| `.sova-btn` | Base | Required on all buttons |
| `.sova-btn-primary` | Blue | Default actions |
| `.sova-btn-success` | Green | Confirmations, approvals |
| `.sova-btn-danger` | Red | Destructive actions |
| `.sova-btn-warning` | Yellow | Caution actions |

### Badges

```html
<span class="sova-badge bg-accent/20 text-accent">developer</span>
```

### Form Inputs

```html
<input type="text" class="sova-input" placeholder="...">
```

### Setting Controls

A page changes a setting with one of two controls from `app.js`, never with its own save code or a second endpoint. Each binds to one setting key and saves through `POST /settings/config`:

- `settingSwitch(key, opts)`: an on/off switch (`.sova-toggle`) for a boolean setting.
- `settingStepper(key, {min, max, step, format})`: a number with minus and plus buttons (`.sova-stepper`). The button that would cross `min` or `max` is disabled.

Both show a pending state (`.sova-setting-pending`) while the save runs, read the stored value back, and on failure return to the stored value and show the server's message, whether it arrives as an HTTP error's `detail` or a 200 answer's `error`. A save answered with `restart_required` shows "Restart required". Every control for the same key still on the page shows the new value after a save (one no longer in the document is dropped), and `sovaSetValue(v)` on the returned element updates it from a poll (held while a save is in flight: a failed save then returns to the held value, not the pre-click one). A poll that overlaps a save compares `settingSaveCount()` before and after its fetch and drops its value for the setting when it changed. When the config cannot be loaded (an HTTP error, or a 200 answer carrying `_error`), the control stays focusable with the failure in its accessible name and tooltip, and activating it (the switch, or the stepper's plus button) tries the load again.

In a template, declare a placeholder and let `mountSettingControls(root, overrides)` build it, with the label and tooltip taken from the settings registry through the `setting_meta(key)` Jinja global:

```html
{% set meta = setting_meta("supervisor.auto_triage") %}
<span class="flex" data-setting-switch="supervisor.auto_triage" data-label="{{ meta.label }}"
      data-tooltip="{{ meta.description }}"></span>
<span class="flex" data-setting-stepper="agent.max_issue_budget" data-label="Per-issue spending limit"
      data-min="5" data-step="5" data-prefix="$"></span>
```

`overrides` maps a key to extra options such as `onSaved(value)` or `onValue(value)`. A control's tooltip moves from the placeholder onto the control, wraps (`data-tooltip-wide`) and opens to the left unless `data-tooltip-pos` says otherwise: controls sit at the right edge of a row or header, where a centered single-line tooltip's hidden box would widen the page. `data-tooltip-wide` tooltips take their natural width up to 320px. A control belongs on a page only for an on/off switch or a number the user changes regularly; everything else stays in Settings.

### Settings Page Rows

The Settings page builds each setting as a row (`static/js/pages/settings/rows.js`): label, badges and key on the left; control, default with Reset to default, and a message line on the right. Badges are `rounded-full` pills: Changed (blue, stored for this project), Stored, same as default (muted), Environment (mauve, a `SOVA_` variable sets it, so the row has no control and no Reset) and Restart. Booleans and the agent limit and per-issue spending limit use the shared Setting Controls above; text, number and list values show an input with Save and Cancel once edited (Enter saves, Escape cancels); model fields become a grouped dropdown once the model list loads (a provider or runtime save rebuilds the other model fields, except one being edited). A save's result, or the server's error, shows on the row's message line, never only in a toast. A setting holding a short model name (`sonnet`) also carries a yellow Short name pill linking to the Models view.

The Models view ("Which model runs where", `static/js/pages/settings/models.js`) lays each role out as a card (`.sova-mm-role`) of rows on a three-column grid (`.sova-mm-grid`: role or step, the model set here, the model it runs on with its source and fallbacks). The source line is blue for a step setting and mauve for the developer pin; a yellow banner (`.sova-mm-pinall`) lists the short model names with Pin all. Messages show on the row, as in the rows above. Below 1024px the grid collapses to one column.

### Tables

```html
<table class="sova-table">
  <thead><tr><th>Column</th></tr></thead>
  <tbody><tr><td>Data</td></tr></tbody>
</table>
```

## Jinja2 Macros

Defined in `sova/dashboard/templates/_components.html`:

```jinja
{% from "_components.html" import stat_card, card_header %}

{{ stat_card("Active Agents", "card-active", detail_id="card-detail") }}
{{ stat_card("Cycle Time", "metric-cycle-time", detail="median hours") }}  {# fixed detail text, rendered server-side #}
{{ card_header("Recent Runs", action_text="View all", action_href="/work") }}
```

## Icons

Defined in `sova/dashboard/templates/_icons.html` as a Jinja2 macro:

```jinja
{% from "_icons.html" import icon %}
{{ icon("dashboard", "w-5 h-5") }}
```

Available icons: `dashboard`, `agents`, `work`, `queue`, `costs`, `logs`,
`memory`, `plus-circle`, `settings`, `bell`, `check`, `warning`, `error`, `info`.

All icons are Heroicons v2 Outline (24x24 viewBox, stroke-width 1.5).

## Notification System

### Toast Notifications

```javascript
showToast('Agent completed', 'info');     // Blue left border
showToast('Action required', 'warning');  // Yellow left border
showToast('Agent failed', 'error');       // Red left border
showToast('PR merged', 'success');        // Green left border
```

Auto-dismiss after 6 seconds, max 3 visible, stacked top-right.

### Browser Notifications

Fire via `_addNotification()` when tab is unfocused. Uses the Web Notification API
with SOVA favicon as icon. Permission is requested via a banner on first visit.

## Tooltips

Pure CSS tooltips using `::after` pseudo-elements. No JavaScript required.

### Usage

Add `data-tooltip="text"` to any element:

```html
<button data-tooltip="Save changes">Save</button>
```

### Position Variants

Control position with `data-tooltip-pos` (default: `top`):

| Value | Position | When to use |
|-------|----------|-------------|
| `top` (default) | Above element | Most buttons |
| `bottom` | Below element | Top-of-page buttons, tab bars |
| `left` | Left of element | Right-aligned buttons |
| `right` | Right of element | Sidebar items |

```html
<button data-tooltip="Filter" data-tooltip-pos="bottom">ALL</button>
```

### Long and Multi-line Text

`data-tooltip-wide` wraps long text (an error message) at 320px. `data-tooltip-multiline` keeps the newlines in the text, one fact per line, such as a run's start and end time in the run history:

```html
<span data-tooltip="Started: ...&#10;Ended: ..." data-tooltip-pos="right" data-tooltip-multiline>Nov 12, 16:29</span>
```

Never put a tooltip on an element that clips its own overflow (`truncate`): the `::after` is clipped with it. Put it on a non-clipping wrapper around the truncated element. `data-tooltip-align="start"` opens the bubble from the wrapper's left edge and wraps it, capped by the host's own width (`max-width: min(320px, max(100%, 14rem))`, never by the host's position in the viewport). The bubble is `display: none` until hover or focus, because a transparent bubble still widens the scrollable area. The run history uses it for a truncated title or result line and for the pills. On hover or focus the page sets `data-tooltip-flip` on a host whose bubble would pass the right edge of a narrow screen, which opens it from the host's right edge instead.

### Sidebar Behavior

Sidebar tooltips (`right` position) only appear when the sidebar is collapsed. When expanded, labels are visible so tooltips are suppressed. This is handled automatically by the CSS. Any `[data-tooltip]` inside `.sidebar` follows this rule, except the unreadable-handoff banner (`#handoff-unreadable-banner`), whose label truncates in the expanded sidebar too, so its full file paths show either way. Inside `.sidebar` they are `position: fixed` (to the right of the collapsed sidebar, centred on the hovered element) rather than absolute, because the nav link list scrolls on short viewports and would clip an absolute one; `data-tooltip-pos` has no effect there.

### Disabled Buttons

Tooltips are hidden on `disabled` or `aria-disabled="true"` elements.

### Tooltip Styling

Tooltips match the Catppuccin Mocha theme: `--ctp-surface1` background, `--ctp-text` color, `0.75rem` font, 150ms fade transition. Defined in `style.css` Section 6.

## Modal Dialogs

Every dashboard dialog runs on one component in `app.js` Section 5 (#1224), styled by the `.sova-modal-*` classes in `style.css` Section 3b. Never use the browser's native `confirm()`, `alert()` or `prompt()`: use `sovaConfirm()` for a question and `showToast()` for a message. `tests/test_dashboard_design_drift_guard.py` fails on a native dialog call (`native_dialog`: bare, on `window`, `globalThis`, `self` or `top`, or by bracket access) or a hand-written `fixed inset-0` overlay (`modal_shell`) anywhere in the templates or static JS, and `tests/test_dashboard_modal.py` runs the component under node (with the Work page's Integrate handler and the drawer on the real side panel, so the drawer is shown to stay open behind the dialog) and checks that every page modal is closed with the `hidden` attribute and that its id is passed to both `sovaModalOpen()` and `sovaModalClose()`.

### Behavior

- **Open** (`sovaModalOpen(backdrop, opts)`): clears the backdrop's `hidden` attribute and fades it in (150 ms), sets `role="dialog"` (unless the markup set one, such as `alertdialog`), `aria-modal="true"` and `aria-labelledby` (the dialog's first heading, or the element marked `data-modal-title`), and focuses `opts.initialFocus` or the first rendered focusable element.
- **Keyboard**: while a modal is the topmost open one it owns the keyboard through a capture-phase listener. Escape closes it and stops there, so the Work drawer and the feed panel, which also close on Escape, stay open behind it. Tab and Shift+Tab cycle inside it. Enter is never intercepted: it presses the focused button.
- **Backdrop click** closes it, unless `opts.closeOnBackdrop` is `false`. A press that starts inside the dialog and is released over the backdrop (selecting text in a field) does not count.
- **Close** (`sovaModalClose(backdrop, reason)`): fades out, sets `hidden` again (or removes the backdrop, with `opts.removeOnClose`), returns focus to the element that had it on open when that element is still in the page, and calls `opts.onClose(reason)` once. Escape and the backdrop pass `'escape'` and `'backdrop'`.
- Modals stack: a confirm opened over a page modal closes first, and focus goes back into the page modal.
- `sovaModalIsOpen(backdrop)` reports one modal, `sovaModalIsOpen()` whether any is open.

### Page Modals

A page modal lives in its template, closed with the `hidden` attribute, and its script opens and closes it with the functions above. The header, scrolling body and footer layout is shared:

```html
<div id="example-modal" class="sova-modal-backdrop" hidden>
  <div class="sova-modal-dialog sova-modal-dialog-md">
    <div class="sova-modal-header">
      <div class="sova-modal-icon">{{ icon("roles", "w-4 h-4") }}</div>
      <div class="min-w-0">
        <h3 class="sova-modal-title">Create Custom Role</h3>
        <p class="sova-modal-subtitle">Optional one-line context</p>
      </div>
    </div>
    <div class="sova-modal-body">...</div>
    <div class="sova-modal-footer">
      {{ btn("Cancel", variant="ghost", attrs={"data-act": "close"}) }}
      {{ btn("Create", attrs={"data-act": "create"}) }}
    </div>
  </div>
</div>
```

The dialog is 24rem wide by default (a confirm); `.sova-modal-dialog-md` (28rem), `-lg` (42rem) and `-xl` (56rem) suit forms and lists. The dialog caps at 85vh and the body scrolls. A `.sova-btn` hides with the `hidden` attribute or the `hidden` class: `style.css` restores both (`.sova-btn[hidden], .sova-btn.hidden`), since `.sova-btn`'s display would otherwise override them.

### Confirmations

`sovaConfirm(message, opts)` builds a modal per call and returns a Promise that resolves `true` (Confirm) or `false` (Cancel, Escape or a backdrop click).

```javascript
var ok = await sovaConfirm('This PR is not fully ready to merge. Run Integrate PR anyway?', {
  title: 'Not ready to merge',         // optional, default 'Confirm'
  details: blockers,                   // optional, plain strings listed under the message
  confirmText: 'Integrate PR anyway',  // optional, default 'Confirm'
  cancelText: 'Cancel',                // optional, default 'Cancel'
  confirmClass: 'warning',             // optional: 'danger' or 'warning'; anything else is primary
});
if (!ok) return;
```

- `confirmClass: 'danger'` (red) for a destructive action: delete, abandon, mark failed, override a budget.
- `confirmClass: 'warning'` (yellow) for an action that proceeds past a known problem, such as Integrate with merge blockers.
- No `confirmClass` for a neutral confirmation: skip, advance, approve.
- Danger and warning open with Cancel focused, so an Enter carried over from the trigger cancels; the primary style opens with Confirm focused.
- The message, title, button labels and every `details` entry are text, never markup: the dialog is built with `textContent`.
- One confirm at a time: a call while one is open resolves `false` at once.
- The dialog is a `role="alertdialog"` described by the message and the details list. Its buttons carry the `sovaBtn()` classes (`_sovaBtnClass()`): a ghost Cancel and the chosen variant, both `md`.

## Side Panels

The activity feed and the Work ticket detail are side panels: one component, `static/js/components/side-panel.js` (#1563), styled by `.sova-side-panel` in `style.css` Section 7c. A new panel over the right edge of the page uses it rather than a hand-written fixed panel.

```html
<aside id="example-panel" class="sova-side-panel" aria-label="Example details">
  <div class="...header...">... <button type="button" data-panel-close aria-label="Close">...</button></div>
  <div class="sova-side-panel-body">...the part that scrolls...</div>
</aside>
```

```javascript
import { createSidePanel } from '/static/js/components/side-panel.js';
var panel = createSidePanel(document.getElementById('example-panel'), {
  name: 'example',          // the body's data-side-panel value while it is open
  floats: 'shift',          // 'shift': the owl button and agents widget move left of it; 'hide': they hide behind it
  keepOpen: '#example-search',       // optional: targets outside it whose click must not close it
  trigger: button,                   // optional: its aria-expanded follows the panel
  returnFocus: function() { return button; },   // optional: where Escape and the close button send focus
  onOpen: function() {}, onClose: function(reason) {},
});
panel.open(); panel.close('deselected'); panel.toggle(); panel.isOpen();
```

- **One at a time**: opening a panel closes the open one first (its `onClose` gets `'replaced'`). Opening the feed closes the ticket detail, and selecting a card closes the feed.
- **Closing**: Escape (unless a dialog has the key: a shared modal stops it first, and any other rendered `[role="dialog"]` blocks it), any element inside marked `data-panel-close`, and a click outside. Outside is decided in the capture phase, before the clicked element's own handler runs. Not outside: the panel itself, a pointer press that began inside it (a text selection; a keyboard click, `detail` 0, had no press, so it never counts as one), the panel's own `trigger` (its own handler calls `toggle()`, so a second click closes the panel with `'toggle'` rather than closing and reopening it), a modal or confirm, a toast, any `[role="dialog"]` or `[aria-modal="true"]`, an element marked `data-keeps-panel`, and the panel's own `keepOpen` (for the ticket detail: the cards, so another card switches the ticket and the selected card closes it, the search, and the log viewer). `onClose(reason)` reports `'escape'`, `'button'`, `'outside'`, `'replaced'`, `'toggle'` or the caller's reason.
- **Focus**: Escape and the close button return focus to `returnFocus()` (called after `onClose`, so a page that redraws on close can hand over the new element), else to what had focus outside the panel when it last opened or switched content (an `open()` on the open panel, such as another ticket chosen from a card or the search; one made while focus is inside the panel, a redraw, keeps the earlier element). Focus in a page field outside the panel stays there. A `'toggle'` close moves no focus: it stays on the trigger the click put it on.
- **Scroll containment**: a wheel over the panel never scrolls the page. The module (a non-passive wheel listener) stops a wheel that nothing between its target and the panel can take (the header, or a list already at that end) and leaves the rest to the browser; `overscroll-behavior: contain` on the panel and on `.sova-side-panel-body` covers the momentum of a trackpad at either end. A pinch (ctrlKey) is left to the browser.
- **Motion**: it slides in with `--ease-out` over `--motion-panel-in` (260 ms) and out with `--ease-in` over `--motion-panel-out` (200 ms). A closed panel waits off the right edge with `visibility: hidden`, so it takes no focus and leaves the accessibility tree once it has slid out (`aria-hidden="true"` too). It is also `inert` from the moment it closes, so its content stays visible while it slides out but no Tab or click reaches it; the templates render both panels with `aria-hidden="true"` and `inert`, so they are closed before the module runs. Under `prefers-reduced-motion: reduce` nothing animates: the panel, the feed scrim, the floats and the batch bar lose their transitions in one `@media` block at the end of `style.css`, after every rule it overrides (at equal specificity the later rule wins).
- **The body**: while a panel is open the body carries `data-side-panel="<name>"`, `data-side-panel-floats` and `--side-panel-width` (the panel's laid-out width, kept current by a ResizeObserver). The floats read them to move or hide (a float shifted by one panel slides back while it fades behind the other), and the global batch bar ends at the panel's left edge, its edge moving with the panel's own curves. The feed also dims the page behind it with a scrim (`.feed-scrim`), a click on which is a click outside.
- **Width**: `min(26rem, 100%)` unless the panel sets its own. The feed keeps its drag-resizable width (`--feed-panel-width`, 400 to 800 px, remembered as `sova.feed.width`).

`tests/test_dashboard_feed_panel.py` runs the module under node over `tests/fixtures/fake_dom.cjs`, and `tests/test_work_page.py` runs the ticket detail on it.

## Layer Order

Fixed and floating elements stack by the `--z-*` tokens on `:root` in `style.css`, lowest first. Use a token, never a new number, for anything fixed to the window.

| Token | Value | Layer |
|-------|-------|-------|
| `--z-sticky` | 20 | A sticky page header (Settings) |
| `--z-dropdown` | 30 | A menu or result list inside the page (the Work search) |
| `--z-float` | 40 | The floating owl button and agents widget (`.sova-float`) |
| `--z-scrim` | 44 | The page dimmed behind the activity feed |
| `--z-panel` | 45 | A side panel: the activity feed, the Work ticket detail |
| `--z-nav` | 50 | The sidebar and the global batch bar (Tailwind `z-50` in `base.html`) |
| `--z-tooltip` | 60 | Tooltips |
| `--z-toast` | 100 | Toasts |
| `--z-modal` | 200 | Modals and confirms |

The floats sit under the panels on purpose: they never overlap an open panel, because they move left of one that leaves the page usable (`floats: 'shift'`, the ticket detail) and hide behind one that takes it over (`floats: 'hide'`, the feed), and where a narrow window leaves no room the panel covers them rather than the other way round.

## Owl Avatar

The SOVA owl (`static/owl-avatar.png`, transparent, ears at the top corners) is never clipped into a circle. Render it with the `owl_avatar(size)` macro or its twin `owlAvatarHtml(size)` in `app.js` (`sm` 30 px, `md` 36 px, `lg` 52 px): a `.sova-owl` circle that is only a backdrop, with the image at 90% of it, `object-fit: contain`, no `overflow` on the circle and no radius on the image, so the ears stick out past the circle. The feed's owl button, the feed header and the morning briefing card all use it; the owl button is itself the circle, so its avatar has no backdrop of its own. Never `object-fit: cover` with a round clip. `tests/test_dashboard_component_vocabulary.py` checks the macro and the twin emit the same markup.

## Layout Tokens

| Token | Value | Usage |
|-------|-------|-------|
| `--sidebar-width` | `256px` | Sidebar width (Tailwind `w-64`) |
| Main margin | `ml-64` | Main content left margin |
| Main padding | `p-8` | Main content padding |
| Nav item padding | `px-5 py-3` | Sidebar navigation items |

## File Reference

| File | Purpose |
|------|---------|
| `sova/dashboard/static/style.css` | CSS variables, component classes, toast styles |
| `sova/dashboard/templates/_head.html` | Shared Tailwind config + favicon links |
| `sova/dashboard/templates/_icons.html` | SVG icon macro (Heroicons v2) |
| `sova/dashboard/templates/_components.html` | Jinja2 component macros |
| `sova/dashboard/templates/base.html` | Main layout, sidebar, activity feed panel and owl button |
| `sova/dashboard/static/js/components/side-panel.js` | The shared side panel (feed, Work ticket detail) |
| `sova/dashboard/static/app.js` | Color system, toasts, confirmation modal, browser notifications, utilities |
| `sova/dashboard/templates/style_guide.html` | Living style guide at `/style-guide` |
