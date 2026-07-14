---
name: visual-audit
description: Audit dashboard templates for visual hierarchy violations, spacing inconsistency, anti-slop violations, design system drift, and common AI-generated UI defects. Use when asked to audit, review, or check templates for visual quality. Reports findings without auto-fixing.
---

# Visual Audit Protocol

Audit existing templates for design quality issues. Reports findings in a structured format for human review -- does not auto-fix.

## When to Use

Invoke this skill when:
- Asked to audit templates for visual quality
- Before a major UI PR (run alongside `/review`)
- After bulk template changes to catch regressions
- When a page "looks off" but the issue is not obvious

## Audit Procedure

### Step 1: Scope

Determine which templates to audit:
- **Full audit**: All templates in `sova/dashboard/templates/` and `sova/dashboard/static/`
- **Targeted audit**: Specific templates (e.g., `agents.html`, `dashboard.html`)
- **Diff audit**: Only templates changed in the current branch (`git diff --name-only main -- sova/dashboard/templates/ sova/dashboard/static/`)

### Step 2: Read References

Before auditing, read these files to understand the baseline:
1. `docs/design-system.md` -- palette, typography, components
2. `sova/dashboard/static/style.css` -- CSS variables, component classes
3. `.claude/skills/dashboard-design/SKILL.md` -- quality rules and anti-slop bans

### Step 3: Run Automated Checks

```bash
# Color violations: hardcoded hex/rgb/hsl outside Catppuccin palette
grep -rn --include='*.html' --include='*.js' --include='*.css' -E '(#[0-9a-fA-F]{3,8}|rgb\(|rgba\(|hsl\(|hsla\()' sova/dashboard/templates/ sova/dashboard/static/ | grep -v '{#' | grep -v 'style.css' | grep -v '_head.html'

# Inline event handlers in templates (except onclick with stopPropagation in JS-rendered HTML)
grep -rn --include='*.html' -E 'onchange=|onsubmit=|onkeydown=|onload=' sova/dashboard/templates/

# Native title attribute instead of data-tooltip
grep -rn --include='*.html' --include='*.js' 'title="' sova/dashboard/templates/ sova/dashboard/static/app.js | grep -v '<title>' | grep -v 'data-tooltip'

# transition: all (should specify properties)
grep -rn --include='*.html' --include='*.css' --include='*.js' 'transition:\s*all\b' sova/dashboard/templates/ sova/dashboard/static/

# Arbitrary spacing values
grep -rn --include='*.html' -E '(p|m|gap)-\[' sova/dashboard/templates/

# Font sizes below minimum
grep -rn --include='*.html' --include='*.css' -E 'text-\[(9|10|11)px\]|font-size:\s*(9|10|11)px' sova/dashboard/templates/ sova/dashboard/static/

# Banned shadow classes (SOVA uses border-based depth)
grep -rn --include='*.html' -E 'shadow-(md|lg|xl|2xl)' sova/dashboard/templates/ | grep -v '{#'

# Overflow hidden/auto/scroll on containers (may clip tooltips/popovers)
grep -rn --include='*.html' --include='*.css' -E 'overflow-(hidden|auto|scroll)|overflow:\s*(hidden|auto|scroll)' sova/dashboard/templates/ sova/dashboard/static/ | grep -v 'overflow-x-auto'

# Missing escapeHtml on dynamic content in innerHTML
grep -rn --include='*.html' --include='*.js' 'innerHTML.*+.*\b(item\.|agent\.|data\.|pr\.)' sova/dashboard/templates/ sova/dashboard/static/ | grep -v 'escapeHtml'

# Cost values without parseFloat guard
grep -rn --include='*.html' --include='*.js' '\.toFixed(' sova/dashboard/templates/ sova/dashboard/static/ | grep -v 'parseFloat'

# ASCII characters used as icons (arrows, checks, dots)
grep -rn --include='*.html' -E "'&#x2[0-9a-fA-F]{3};|&rarr;|&larr;|&check;|\\\\u2" sova/dashboard/templates/
```

### Step 4: Manual Inspection Checklist

For each template in scope, check:

#### A. Visual Hierarchy (most impactful)

| Check | What to look for | Severity |
|-------|-----------------|----------|
| **3-level rule** | Does every content section have primary/secondary/tertiary weight? | High |
| **Stat card structure** | Value (primary) > Label (secondary) > Detail (tertiary)? | High |
| **Table hierarchy** | Headers uppercase/medium, data normal, actions small/muted? | Medium |
| **Heading consistency** | Same heading level uses same size/weight across pages? | Medium |
| **Empty states** | Meaningful empty-state message, not just blank space? | Low |

#### B. Spacing Consistency

| Check | What to look for | Severity |
|-------|-----------------|----------|
| **Standard increments** | Only Tailwind scale values (gap-2, gap-4, gap-6, p-4, p-6)? | High |
| **Card padding** | All cards use consistent `p-4` to `p-6` padding? | Medium |
| **Section spacing** | Consistent `mb-6`/`mb-8` between major sections? | Medium |
| **Grid gap consistency** | Same gap value for same-type grids across pages? | Low |

#### C. Color & Theme

| Check | What to look for | Severity |
|-------|-----------------|----------|
| **Palette compliance** | All UI colors from Catppuccin Mocha CSS vars? | Critical |
| **Semantic color abuse** | `text-accent-green`/`text-accent-red` used only for data, not UI chrome? | Medium |
| **Opacity consistency** | Using `/20` for fills, `/30` for borders, `text-gray-500` for muted? | Low |

#### D. Component Usage

| Check | What to look for | Severity |
|-------|-----------------|----------|
| **Icon system** | Using `{{ icon("name") }}` not inline SVG or ASCII? | High |
| **Button variants** | Only `.sova-btn-primary|success|danger|warning` used? | High |
| **Badge shapes** | `rounded-full` for indicators, `rounded` for buttons? | Medium |
| **Tooltip system** | `data-tooltip` not native `title` attribute? | Medium |

#### E. Interaction Quality

| Check | What to look for | Severity |
|-------|-----------------|----------|
| **Hover states** | Every clickable element has visible hover feedback? | High |
| **stopPropagation** | Nested buttons inside clickable containers call it? | High |
| **External links** | GitHub links have `target="_blank" rel="noopener"`? | Medium |
| **Transition specificity** | Transitions name properties (not `transition: all`)? | Low |

#### F. AI-Tell Detection

| Tell | Description | Severity |
|------|------------|----------|
| **Side-stripe border** | `border-l-4 border-accent` on cards | Medium |
| **Gradient text** | `bg-clip-text text-transparent bg-gradient-to-r` | High |
| **Ghost-card** | `border-dashed` placeholder as "add new" | Medium |
| **Identical triplet cards** | Three cards, same structure, no hierarchy variation | High |
| **Rainbow tags** | 4+ distinct hues in the same badge row | Medium |
| **Over-rounded** | `rounded-3xl` or larger on content cards | Low |
| **Gratuitous dividers** | `<hr>` between every section instead of spacing | Low |

### Step 5: Report Format

Report findings as a structured list, grouped by severity:

```markdown
## Visual Audit Report

**Scope**: [templates audited]
**Date**: [date]

### Critical (must fix before merge)
1. **[file:line]** [issue description] -- [fix suggestion]

### High (fix in this PR if touched, otherwise create issue)
1. **[file:line]** [issue description] -- [fix suggestion]

### Medium (fix when next touching this template)
1. **[file:line]** [issue description] -- [fix suggestion]

### Low (nice to have)
1. **[file:line]** [issue description] -- [fix suggestion]

### Summary
- Total findings: N
- Critical: N | High: N | Medium: N | Low: N
- Templates audited: N
- Templates clean: N
```

Do NOT auto-fix findings. Report them for human review. The user decides which to address.
