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

// Role colors use these dynamically:
ROLE_COLORS.developer.hex   // reads from SOVA_COLORS.accent
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

### Sidebar Behavior

Sidebar tooltips (`right` position) only appear when the sidebar is collapsed. When expanded, labels are visible so tooltips are suppressed. This is handled automatically by the CSS; any `[data-tooltip]` inside `.sidebar` follows this rule.

### Disabled Buttons

Tooltips are hidden on `disabled` or `aria-disabled="true"` elements.

### Styling

Tooltips match the Catppuccin Mocha theme: `--ctp-surface1` background, `--ctp-text` color, `0.75rem` font, 150ms fade transition. Defined in `style.css` Section 6.

## Confirmation Dialogs

Use `sovaConfirm()` instead of the browser's native `confirm()`. Returns a Promise that resolves `true` (Confirm) or `false` (Cancel/Escape/backdrop click).

### API

```javascript
var ok = await sovaConfirm('Delete this item?', {
  title: 'Confirm',          // optional, default 'Confirm'
  confirmText: 'Delete',     // optional, default 'Confirm'
  cancelText: 'Cancel',      // optional, default 'Cancel'
  confirmClass: 'danger',    // optional, 'danger' for red styling
});
if (!ok) return;
```

### Styling

- Backdrop: `z-200`, `bg-black/50`, 150ms fade
- Dialog: `--color-surface-card` background, `--ctp-surface1` border, 150ms scale transition
- Cancel button: ghost text (`text-gray-400 hover:text-gray-200`)
- Confirm button (default): `bg-accent/20 text-accent`
- Confirm button (danger): `bg-accent-red/20 text-accent-red`

Defined in `style.css` Section 3b, function in `app.js` Section 5.

### Usage Rules

- Always use `confirmClass: 'danger'` for destructive actions (delete, abandon, mark failed)
- Default styling for neutral confirmations (skip, advance, approve)
- The enclosing function must be `async` to use `await`

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
| `sova/dashboard/templates/base.html` | Main layout, sidebar, notification panel |
| `sova/dashboard/static/app.js` | Color system, toasts, confirmation modal, browser notifications, utilities |
| `sova/dashboard/templates/style_guide.html` | Living style guide at `/style-guide` |
