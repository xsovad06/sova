---
name: design-system
description: Frontend design conventions for {{ project_name }} -- layout hierarchy, spacing, typography, anti-slop rules. Auto-activates when creating or modifying templates, CSS, or frontend components.
allowed_tools: Read, Grep, Glob, Bash, Edit, Write
---

# Design System Conventions

When creating or modifying frontend templates, CSS, or UI components, follow these conventions.

## Visual Hierarchy

- Establish clear heading hierarchy (h1 > h2 > h3) with distinct sizes
- Use whitespace to separate logical sections -- generous padding over cramped layouts
- Primary actions must be visually distinct from secondary actions (size, color, weight)
- Data tables: header row visually distinct, alternating row backgrounds for readability

## Typography

- Limit to 2 font families maximum (one for headings, one for body)
- Body text: 14-16px minimum, 1.5-1.6 line height
- Do not use font weights below 400 for body text
- Headings: use weight contrast (600-700) rather than excessive size

## Spacing

- Use a consistent spacing scale (4px base: 4, 8, 12, 16, 24, 32, 48)
- Containers: minimum 16px padding on all sides
- Between sections: minimum 24px gap
- Between related items: 8-12px gap

## Color

- Maintain WCAG AA contrast ratios (4.5:1 for text, 3:1 for large text)
- Use semantic color names in CSS (--color-primary, --color-danger) not raw values
- Status indicators: green=success, yellow=warning, red=error, blue=info
- Dark mode: never use pure black (#000) backgrounds -- use dark grays

## Anti-Slop Rules

- No centered-everything layouts -- use left-aligned text with intentional centering
- No generic card grids without hierarchy -- vary card sizes to show importance
- No walls of same-sized buttons -- establish visual priority
- No unstyled form elements -- every input needs focus states and consistent borders
- Animations: subtle and purposeful (150-300ms transitions), never decorative

## Responsive

- Mobile-first: design for smallest viewport, enhance for larger
- Touch targets: minimum 44x44px on mobile
- Do not hide critical information behind hover states on touch devices
