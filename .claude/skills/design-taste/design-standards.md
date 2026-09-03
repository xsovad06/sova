---
name: design-standards
description: UI design system validation checklist for visual consistency
category: ui
---

# UI Design Standards Checklist

Use this checklist when creating or modifying UI elements to ensure consistency with your project's design system.

**Context**: $ARGUMENTS

## Before You Start

1. **Read your design system documentation** — Usually in `docs/design-system.md` or similar
2. **Know your component library** — Bootstrap, Material, DaisyUI, custom, etc.
3. **Understand your color system** — How colors are defined, named, and applied
4. **Check existing components** — Don't duplicate what already exists

## Areas to Review

### Colors
- [ ] Use only colors from the project's defined palette
- [ ] Use CSS variables / tokens (not hardcoded values)
- [ ] If applicable, test in both light and dark modes
- [ ] Avoid arbitrary color choices (ask: "why this shade?")
- [ ] No semantic color abuse (red/green for non-semantic data)

### Buttons
- [ ] Follow project's button variants (primary, secondary, tertiary, etc.)
- [ ] Clear, action-oriented labels
- [ ] Don't create new button variants without design review
- [ ] Hover/active/disabled states visible
- [ ] One primary CTA per form/section when applicable

### Icons
- [ ] Check existing icon library before creating new icons
- [ ] Consistent sizing for similar use cases
- [ ] Use component system if available (don't inline raw SVGs)
- [ ] Decorative icons hidden from screen readers
- [ ] Action icons clearly labeled

### Typography
- [ ] Follow project's type scale (headings, body, small, etc.)
- [ ] Consistent font weights for hierarchy
- [ ] Adequate line height for readability
- [ ] Responsive sizing on mobile
- [ ] Monetary/data values use appropriate styling

### Forms
- [ ] All inputs have associated labels
- [ ] Focus states clearly visible and accessible
- [ ] Error states clearly indicated
- [ ] Consistent spacing and padding
- [ ] Validation messages clear and helpful

### Spacing & Layout
- [ ] Consistent margins and padding
- [ ] Follows project's spacing scale (if defined)
- [ ] White space used intentionally
- [ ] Mobile responsive (reflow, not hidden elements)

### Components
- [ ] Reuse existing components instead of rebuilding
- [ ] Component props used correctly
- [ ] No broken references to removed components
- [ ] Composition supports the intended layout

### Accessibility
- [ ] Sufficient color contrast (WCAG AA minimum)
- [ ] Semantic HTML (proper heading hierarchy, etc.)
- [ ] Keyboard navigation works
- [ ] ARIA labels where needed
- [ ] Alt text for images

## Workflow

1. **Identify the element** — Button, card, form, icon, etc.
2. **Check design system** — How is this element defined?
3. **Check existing code** — Is this already implemented?
4. **Build/modify the element** — Following project conventions
5. **Validate against checklist** — Did I miss anything?
6. **Test in context** — Light/dark mode, responsive, interactive states
7. **Get design review** — If unsure about visual direction

## Common Pitfalls

- **Color drift** — Adding colors outside the palette
- **Variant sprawl** — Creating new button/badge variants without design guidance
- **Hardcoded values** — Colors, sizes, spacing hardcoded instead of using design tokens
- **Theme blindness** — Testing only light mode (or only dark mode)
- **Component duplication** — Building new components that already exist
- **Missing accessibility** — Insufficient contrast, no labels, no keyboard support
- **Inconsistent spacing** — Using arbitrary padding/margins instead of a scale

## Example: How to Adapt This Checklist

Your project uses **Material Design with custom colors**:
- Colors section: Check your Material color variable names instead of "palette"
- Buttons section: Reference Material button variants (filled, outlined, text)
- Icons section: Reference your icon font/library
- Typography section: Material's type scale (headline, title, body, label)
- Spacing section: Material's 4px/8px/12px/16px increments

Your project uses **Tailwind + custom design system**:
- Colors section: Check `tailwind.config.js` for color definitions
- Buttons section: Reference your custom button classes
- Icons section: SVG files in your project, or Heroicons
- Typography section: Tailwind's text size scale
- Spacing section: Tailwind's spacing scale (4, 8, 12, 16, 20, 24, etc.)

## Integration Points

- Add to `/review` — Include design validation in code reviews
- Add to `/develop` — Validate UI during development
- Reference in project docs — Link to this as a standard

## Cross-References

- **Code review?** Pair this with your project's `/review` command
- **Need more guidance?** Check your project's design documentation
