---
name: design-taste
description: Generic UI design review checklist and best practices, adaptable to any project's design system. Auto-activates when reviewing or validating UI changes for visual quality.
allowed_tools: Read, Grep, Glob
---

# Design Taste

Use this checklist when creating, modifying, or reviewing UI elements on a
project whose own design system isn't already covered by a more specific
skill. It is generic by design: before applying it, read the project's own
design system documentation (for example `docs/design-system.md` or a style
guide), identify its component library (Bootstrap, Material, DaisyUI, custom,
etc.) and how colors are defined, named, and applied, then map each category
below onto that project's actual tokens, components, and conventions rather
than applying the generic names literally.

## Before You Start

1. Read the project's design system documentation.
2. Identify the component library in use.
3. Understand how the color system is defined, named, and applied.
4. Check existing components before building anything new.

## Areas to Review

### Colors
- [ ] Use only colors from the project's defined palette
- [ ] Use CSS variables / tokens, not hardcoded values
- [ ] Test in both light and dark modes, if applicable
- [ ] Avoid arbitrary color choices; ask "why this shade?"
- [ ] No semantic color abuse (red/green for non-semantic data)

### Buttons
- [ ] Follow the project's button variants (primary, secondary, tertiary, etc.)
- [ ] Clear, action-oriented labels
- [ ] Don't create new button variants without a design review
- [ ] Hover/active/disabled states are visible
- [ ] One primary CTA per form or section, when applicable

### Icons
- [ ] Check the existing icon library before creating new icons
- [ ] Consistent sizing for similar use cases
- [ ] Use the component system if available; don't inline raw SVGs
- [ ] Decorative icons are hidden from screen readers
- [ ] Action icons are clearly labeled

### Typography
- [ ] Follow the project's type scale (headings, body, small, etc.)
- [ ] Consistent font weights for hierarchy
- [ ] Adequate line height for readability
- [ ] Responsive sizing on mobile
- [ ] Monetary/data values use appropriate styling

### Forms
- [ ] All inputs have associated labels
- [ ] Focus states are clearly visible and accessible
- [ ] Error states are clearly indicated
- [ ] Consistent spacing and padding
- [ ] Validation messages are clear and helpful

### Spacing and Layout
- [ ] Consistent margins and padding
- [ ] Follows the project's spacing scale, if one is defined
- [ ] White space is used intentionally
- [ ] Mobile responsive (reflow, not hidden elements)

### Components
- [ ] Reuse existing components instead of rebuilding them
- [ ] Component props are used correctly
- [ ] No broken references to removed components
- [ ] Composition supports the intended layout

### Accessibility
- [ ] Sufficient color contrast (WCAG AA minimum)
- [ ] Semantic HTML (proper heading hierarchy, etc.)
- [ ] Keyboard navigation works
- [ ] ARIA labels where needed
- [ ] Alt text for images

## Workflow

1. Identify the element: button, card, form, icon, etc.
2. Check the design system: how is this element already defined?
3. Check existing code: is this already implemented somewhere?
4. Build or modify the element, following project conventions.
5. Validate against the checklist above.
6. Test in context: light/dark mode, responsive layout, interactive states.
7. Get a design review if unsure about visual direction.

## Common Pitfalls

- Color drift: adding colors outside the palette
- Variant sprawl: creating new button/badge variants without design guidance
- Hardcoded values: colors, sizes, or spacing hardcoded instead of using design tokens
- Theme blindness: testing only light mode, or only dark mode
- Component duplication: building new components that already exist
- Missing accessibility: insufficient contrast, no labels, no keyboard support
- Inconsistent spacing: arbitrary padding/margins instead of a defined scale

## How to Adapt This Checklist

Replace the generic category names with the project's real equivalents before
using this checklist. For example, a project on Material Design with custom
colors maps "palette" onto its Material color variable names, "button
variants" onto Material's filled/outlined/text variants, and "spacing scale"
onto Material's 4px/8px/12px/16px increments; a project on Tailwind maps
"palette" onto `tailwind.config.js`'s color definitions and "spacing scale"
onto Tailwind's own spacing scale. The categories above stay the same;
only the vocabulary and the concrete values change.

Fold this checklist into the project's own review and development workflow
rather than running it as a one-off: a UI change is most likely to regress
design consistency during development, not after it ships.
