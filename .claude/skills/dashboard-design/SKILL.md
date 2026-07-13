---
name: dashboard-design
description: SOVA dashboard UI design system and quality guardrails -- Catppuccin Mocha dark theme, Tailwind CSS, Jinja2 templates, component macros. Anti-slop rules, visual hierarchy discipline, spacing/motion quality. Auto-activates when creating or modifying dashboard templates, CSS, or JavaScript.
allowed_tools: Read, Grep, Glob, Bash, Edit, Write
---

# SOVA Dashboard Design System + Quality Protocol

Read this skill BEFORE writing any template HTML, CSS, or frontend JavaScript under `sova/dashboard/`. It combines the design system reference with quality-of-execution rules that prevent generic AI output.

## 0. Reference Files

Before making changes, read these:

1. `sova/dashboard/static/style.css` -- CSS variables (Catppuccin Mocha), component classes
2. `sova/dashboard/templates/_head.html` -- shared Tailwind config (centralized)
3. `sova/dashboard/templates/_icons.html` -- SVG icon macro (Heroicons v2 outline)
4. `sova/dashboard/templates/_components.html` -- Jinja2 component macros (stat_card, card_header)
5. `docs/design-system.md` -- full design system reference

## 1. Design Dials

Three dials control output. SOVA defaults (do not change without explicit user instruction):

| Dial | Value | Effect |
|------|-------|--------|
| **DESIGN_VARIANCE** | 3 (Predictable) | Symmetrical grids, equal paddings. Developers need trust, not surprise. |
| **MOTION_INTENSITY** | 2 (Static) | CSS `:hover`/`:focus` only. No scroll-triggered reveals, no auto-animations. Agent status data should feel stable. |
| **VISUAL_DENSITY** | 6 (Dense App) | Tighter spacing (`p-4` to `p-8`). Dashboard is information-dense by nature. |

These dials mean: no masonry layouts, no parallax, no staggered scroll reveals, no ambient background motion.

## 2. Anti-Slop Rules (Banned Patterns)

If your generated code includes ANY of these, the output fails quality checks:

### Layout
- Generic 3-equal-column grids with identical cards (the "SaaS landing page" look)
- Cards with no visual hierarchy -- all same padding, same font size, same weight
- Full-width sections with no constraint (content stays within the `ml-64 p-8` main area)
- Centered text blocks wider than `max-w-2xl`

### Color
- Any color outside Catppuccin Mocha palette (no hardcoded hex, rgb, hsl)
- Gradient backgrounds on containers or sections (SOVA is flat, not glassmorphic)
- Random opacity values -- use the established scale: `text-gray-500` for muted, `/30` for borders, `/20` for subtle fills, `/10` for faint backgrounds
- `rgba(0,0,0,...)` or similar generic shadows -- use Catppuccin surface colors

### Typography
- More than the 6 established type roles (page heading, section heading, label, body, caption, data)
- Font sizes below 12px (`text-xs` is the minimum)
- `font-bold` on body text or captions (bold is for headings and data values only)
- ALL-CAPS text except for badge labels and `text-xs uppercase tracking-wider` eyebrow tags
- Mixing font weights within the same visual role

### Components
- Inventing new button variants beyond `.sova-btn-primary|success|danger|warning`
- Using Tailwind shadow classes (`shadow-md`, `shadow-lg`) -- use border-based depth
- Inline SVGs for icons that exist in `_icons.html` (use `{{ icon("name", "w-5 h-5") }}`)
- Native browser elements without `.sova-input`, `.sova-table` classes
- ASCII characters as icons (arrows, dots, checks) -- use Heroicon SVGs

### Motion
- `transition: all` (always specify properties: `transition-colors`, `transition-opacity`)
- `linear` or `ease-in-out` on interactive elements (use `ease` or `transition-colors`)
- Scroll-triggered animations or `IntersectionObserver` reveals
- `animation` on content elements (only for loaders and `animate-pulse` status indicators)
- Transition durations longer than 300ms on interactive elements

### Content
- Placeholder text ("Lorem ipsum", "John Doe")
- AI copywriting cliches ("Seamless", "Elevate", "Unleash", "Next-Gen")
- Emojis anywhere in templates, code, or UI text

### Jinja2/JS
- `{{ variable }}` without `escapeHtml()` in JS string contexts
- Inline `<script>` without being in the page's main script block
- `innerHTML` with user-controlled data without escaping

## 3. Visual Hierarchy Rules

Every page element must have a clear place in the visual hierarchy.

### The 3-Level Rule

Every content section needs exactly 3 levels of visual weight:

1. **Primary** -- the thing the user came to see (stat value, agent status, PR state). Largest, boldest, highest contrast.
2. **Secondary** -- context and labels (stat titles, column headers, issue titles). Medium size, medium weight.
3. **Tertiary** -- metadata and actions (timestamps, badges, action buttons). Smallest, lightest, most muted.

If everything is the same size and weight, nothing stands out. If there are more than 3 levels, the hierarchy is noisy.

### Applying to SOVA Components

| Component | Primary | Secondary | Tertiary |
|-----------|---------|-----------|----------|
| Stat card | `.sova-stat-value` (text-2xl, font-semibold) | `.sova-stat-label` (text-xs, uppercase) | `.sova-stat-detail` (text-xs, text-gray-500) |
| Task row | Issue title (text-sm, text-white) | Issue number (text-xs, font-bold, text-accent) | Badges, timestamps (text-xs, muted) |
| Agent card | Role + issue (font-semibold) | Status + step (text-sm) | Cost, duration (text-xs, text-gray-500) |
| Table | Data cells (text-sm) | Column headers (text-xs, font-medium, uppercase) | Action icons (text-xs, muted) |

### Spacing Hierarchy

Spacing must follow a consistent scale:

| Context | Gap | Usage |
|---------|-----|-------|
| Between page sections | `mb-8` | Major sections on a page |
| Between cards | `gap-4` to `gap-6` | Card grids, stat rows |
| Inside cards | `p-4` to `p-6` | Card content padding |
| Between list items | `gap-2` to `gap-3` | Task rows, agent cards |
| Between related items | `gap-1` to `gap-2` | Badge groups, icon+text |

Never use arbitrary spacing values (`p-[13px]`, `gap-[7px]`). Round to the nearest standard value.

## 4. Interaction Quality

With MOTION_INTENSITY at 2 (Static), interactions should be:
- Hover: subtle background shift (`hover:bg-surface-hover`), `transition-colors` 150ms
- Active/pressed: slightly darker background
- Focus: accent ring for keyboard navigation
- Disabled: `opacity-50`, `cursor-not-allowed`, `pointer-events-none`

### Clickable Element Rules
- Every clickable element must have a visible hover state change
- `cursor-pointer` on all non-link clickable elements
- All nested interactive elements inside clickable containers must call `event.stopPropagation()`
- Links to external sites (GitHub) must use `target="_blank" rel="noopener"`
- Use `data-tooltip="text"` for hover context (not native `title`)

### Polling & Dynamic Content
- Polling `innerHTML` refresh kills open dropdowns -- track interactive state flags, skip re-renders while menus are open
- Clear stale UI on negative polling path (state gone = clear innerHTML or add `hidden`)
- `parseFloat(value || 0).toFixed(N)` for all API cost fields (Decimal serializes as string)

## 5. Color System Quick Reference

### Catppuccin Mocha Palette

| Token | Usage |
|-------|-------|
| `bg-surface` / `--ctp-mantle` | Page background |
| `bg-surface-card` / `--ctp-base` | Card background |
| `bg-surface-hover` / `--ctp-surface0` | Hover states |
| `text-accent` / `--ctp-blue` | Links, primary actions |
| `text-accent-green` / `--ctp-green` | Success, done |
| `text-accent-red` / `--ctp-red` | Error, failed |
| `text-accent-yellow` / `--ctp-yellow` | Warning, running |
| `text-accent-purple` / `--ctp-mauve` | Metadata, researcher |
| `text-gray-500` / `--ctp-overlay1` | Muted text |

### Semantic Rules
- Blue = primary accent, links, developer role
- Green = success, done states, money
- Red = error, failed, danger actions
- Yellow/peach = warning, running, action needed
- Purple/mauve = metadata, researcher role
- Never use semantic colors on UI chrome (only on data indicators)

## 6. Component Quick Reference

### Buttons
`.sova-btn .sova-btn-primary|success|danger|warning` -- never invent variants

### Cards
`.sova-card` container, `.sova-card-header` or `card_header()` macro

### Stats
`.sova-stat` + `.sova-stat-label` + `.sova-stat-value` or `stat_card()` macro

### Icons
`{{ icon("name", "w-5 h-5") }}` -- never use ASCII characters for icons

### Badges
`rounded-full` for read-only state indicators (pills), `rounded` for clickable buttons

### Tooltips
`data-tooltip="text"` with optional `data-tooltip-pos="top|bottom|left|right"`

### Notifications
- Ephemeral: `showToast(message, type)` -- info, warning, error, success
- Persistent: `_addNotification(message, type)`

### Confirmation Dialogs
`sovaConfirm(message, opts)` -- returns Promise. Use `confirmClass: 'danger'` for destructive actions.

## 7. AI-Tell Detection

Common patterns that reveal AI-generated code:

| Tell | Fix |
|------|-----|
| Side-stripe `border-l-4 border-accent` on cards | Remove; use heading weight for hierarchy |
| Gradient text `bg-clip-text text-transparent` | Use solid `text-accent` |
| Ghost-card `border-dashed` as "add new" placeholder | Use a proper `.sova-btn` |
| Identical triplet cards in a row | Vary sizes, use stat cards, or merge into a list |
| Rainbow tags (4+ hues in one row) | Use Catppuccin semantic colors only |
| Over-rounded `rounded-3xl` on cards | `rounded-xl` is the SOVA standard |
| Gratuitous `<hr>` dividers everywhere | Use spacing (margin/padding) for separation |
| Generic `transition: all` on everything | Specify: `transition-colors`, `transition-opacity` |

## 8. Overflow Rule

Never set `overflow: hidden|auto|scroll` on containers with absolutely-positioned children (tooltips, popovers, notification panel). CSS computes the other axis to `auto`, clipping popouts. Use `max-width` and opacity transitions on individual children instead.

## 9. Pre-Flight Checklist

Before delivering UI work:

- [ ] All colors reference Catppuccin CSS variables (no hardcoded hex)
- [ ] Buttons use `.sova-btn-*` variants only
- [ ] Cards use `.sova-card` or equivalent
- [ ] Typography follows the 6-role scale
- [ ] Icons use the `_icons.html` macro
- [ ] No text smaller than `text-xs` (12px)
- [ ] Every content section has 3 levels of visual weight
- [ ] Spacing uses standard Tailwind increments
- [ ] Every clickable element has visible hover state
- [ ] Transitions specify properties (not `transition: all`)
- [ ] No scroll-triggered animations
- [ ] Tooltips use `data-tooltip`, not native `title`
- [ ] No AI-tell patterns (side-stripes, gradient text, identical triplets)
- [ ] No emojis, no placeholder text, no AI cliches
