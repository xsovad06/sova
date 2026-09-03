# Design Taste Skill

This skill provides UI design validation and standards guidance for any project that maintains a design system.

## Files

- `design-standards.md` — Generic design review checklist and best practices

## Usage

**All projects**: Use `design-standards.md` as a reference template. Adapt the specific rules (color palettes, component names, spacing systems) to match your project's design system documentation.

## Integration with Commands

Design validation can be integrated into:
- `/review` command — add design checks to code review
- `/develop` command — validate UI changes during development
- Custom commands — create project-specific design review commands

## How to Adapt for Your Project

1. Read `design-standards.md` to understand the validation categories
2. Identify your project's design system documentation (design-system.md, style guide, etc.)
3. Map generic concepts to your specific implementation:
   - Replace "brand palette" with your project's actual colors
   - Replace "button variants" with your framework (DaisyUI, Material, Bootstrap, etc.)
   - Replace component names with your project's real components
4. Integrate into your `/review` or `/develop` workflows

## Notes

Generic design review checklist extracted during multi-project command sync audit (Sept 2026). Applicable to any project with a design system.
