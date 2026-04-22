---
name: design
description: Apply the SOVA design system when creating or modifying dashboard UI elements
user-invocable: true
---

# SOVA UI Design Standards

Apply the SOVA design system to the specified UI work. Context: $ARGUMENTS

## 1. Load the Design System

Read these files before making any UI decisions:
- `sova/dashboard/static/style.css` -- CSS variables (Catppuccin Mocha), component classes
- `sova/dashboard/templates/base.html` -- layout, Tailwind config, icon macro, notification panel
- `sova/dashboard/templates/_head.html` -- shared Tailwind config (centralized)
- `sova/dashboard/templates/_icons.html` -- icon macro (Heroicons v2 outline)
- `sova/dashboard/templates/_components.html` -- Jinja2 component macros (stat_card, card_header)
- `sova/dashboard/static/app.js` -- SOVA_COLORS, toast system, browser notifications, shared utilities
- `docs/design-system.md` -- full design system reference

Optionally view the living style guide at `/style-guide` in the running dashboard.

## 2. Mandatory Rules

### Colors
- Every color must come from the Catppuccin Mocha palette defined as CSS variables in `style.css`
- Tailwind classes use RGB vars: `bg-accent`, `text-accent-green`, `border-accent/30`
- JS uses `window.SOVA_COLORS.accent` (populated from CSS vars at init)
- Never hardcode hex values in templates or JS
- Semantic meanings:
  - Blue (accent): primary, links, active state
  - Green: success, done, money
  - Red: error, failed, danger
  - Yellow: warning, running, action needed
  - Purple (mauve): metadata, researcher role

### Buttons (3 main variants)
- Primary: `.sova-btn .sova-btn-primary` or `bg-accent/20 text-accent hover:bg-accent/30`
- Danger: `.sova-btn .sova-btn-danger`
- Success: `.sova-btn .sova-btn-success`
- Warning: `.sova-btn .sova-btn-warning`
- Never invent new button color combinations

### Cards
- Container: `.sova-card` or `bg-surface-card rounded-lg border border-gray-700/50`
- Header: `.sova-card-header` or use the `card_header()` macro
- Stat: `.sova-stat` + `.sova-stat-label` + `.sova-stat-value` or use the `stat_card()` macro

### Typography (6 roles)
- Page heading: `text-2xl font-semibold`
- Section heading: `text-lg font-semibold`
- Label: `text-sm font-medium`
- Body: `text-sm` (default)
- Caption: `text-xs text-gray-500`
- Data/numbers: `font-mono font-semibold`

### Icons
- Use the icon macro from `_icons.html`: `{{ icon("dashboard", "w-5 h-5") }}`
- Default size: `w-5 h-5` (20px)
- Nav size: `w-[18px] h-[18px]`
- Never use ASCII characters for icons

### Notifications
- Ephemeral feedback: `showToast(message, type)` -- types: info, warning, error, success
- Persistent notifications: `_addNotification(message, type)` -- auto-fires toast + browser notification
- Browser notifications fire only when tab is unfocused

### Sizing
- Sidebar: `w-64` (256px), nav items `px-5 py-3`
- Main content: `ml-64 p-8`
- Minimum text: `text-xs` (12px) -- never use `text-[10px]`, `text-[9px]`, `text-[8px]`

## 3. Checklist

Before finishing any dashboard UI work:
- [ ] All colors reference CSS variables (no hardcoded hex in JS or templates)
- [ ] Buttons use standardized `.sova-btn-*` variants
- [ ] Cards use `.sova-card` or equivalent Tailwind pattern
- [ ] Typography follows the 6-role scale
- [ ] Icons use the macro from `_icons.html`
- [ ] No text smaller than `text-xs` (12px)
- [ ] Responsive layout uses grid breakpoints (md, lg)
- [ ] New shared elements are added to the style guide (`/style-guide`)
