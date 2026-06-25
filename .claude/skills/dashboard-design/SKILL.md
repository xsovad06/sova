---
name: dashboard-design
description: SOVA dashboard UI design system -- Catppuccin Mocha dark theme, Tailwind CSS, Jinja2 templates, component macros. Auto-activates when creating or modifying dashboard templates, CSS, or JavaScript.
allowed_tools: Read, Grep, Glob, Bash, Edit, Write
---

# SOVA Dashboard Design System

When working on any file under `sova/dashboard/templates/` or `sova/dashboard/static/`, read these references first:

1. `sova/dashboard/static/style.css` -- CSS variables (Catppuccin Mocha palette), component classes
2. `sova/dashboard/templates/_head.html` -- shared Tailwind config (centralized)
3. `sova/dashboard/templates/_icons.html` -- SVG icon macro (Heroicons v2 outline)
4. `sova/dashboard/templates/_components.html` -- Jinja2 component macros (stat_card, card_header)
5. `docs/design-system.md` -- full design system reference

## Mandatory Rules

### Colors
- Every color comes from Catppuccin Mocha CSS variables in `style.css`
- Tailwind uses RGB vars: `bg-accent`, `text-accent-green`, `border-accent/30`
- JS uses `window.SOVA_COLORS.accent` (populated from CSS vars)
- Never hardcode hex values
- Semantic: blue=primary, green=success, red=error, yellow=warning/running, purple=metadata

### Components
- Buttons: `.sova-btn .sova-btn-primary|danger|success|warning` -- never invent variants
- Cards: `.sova-card` container, `.sova-card-header` or `card_header()` macro
- Stats: `.sova-stat` + `.sova-stat-label` + `.sova-stat-value` or `stat_card()` macro
- Icons: `{{ icon("name", "w-5 h-5") }}` macro -- never use ASCII characters for icons

### Typography (6 roles only)
- Page heading: `text-2xl font-semibold`
- Section heading: `text-lg font-semibold`
- Label: `text-sm font-medium`
- Body: `text-sm`
- Caption: `text-xs text-gray-500`
- Data/numbers: `font-mono font-semibold`
- Minimum: `text-xs` (12px) -- never use smaller

### Sizing
- Sidebar: `w-64` (256px), nav items `px-5 py-3`
- Main content: `ml-64 p-8`
- Default icon: `w-5 h-5`, nav icon: `w-[18px] h-[18px]`

### Notifications
- Ephemeral: `showToast(message, type)` -- info, warning, error, success
- Persistent: `_addNotification(message, type)` -- also fires browser notification when unfocused

### Overflow
- Never set `overflow: hidden|auto|scroll` on containers with absolutely-positioned children (tooltips, popovers) -- CSS computes the other axis to `auto`, clipping popouts
- Use `max-width` and opacity transitions on individual children instead

## Checklist (verify before finishing UI work)
- All colors reference CSS variables
- Buttons use `.sova-btn-*` variants
- Cards use `.sova-card` or equivalent
- Typography follows the 6-role scale
- Icons use the `_icons.html` macro
- No text smaller than `text-xs`
- Responsive layout uses grid breakpoints (md, lg)
