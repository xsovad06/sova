# PatternFly Persona

> Auto-detected when: `@patternfly/react-core` found in package.json dependencies

## PatternFly MCP Server

When working on this project, the PatternFly MCP server is available. Use it to look up
component documentation, prop schemas, and design guidelines before writing UI code.

Available MCP tools:
- `searchPatternFlyDocs`: search component documentation by keyword
- `usePatternFlyDocs`: get detailed docs for a specific component
- `searchPatternFly`: general PatternFly search

Always query the MCP server for component props before creating or modifying PatternFly components.

## Components

- Import components from `@patternfly/react-core`, `@patternfly/react-table`, `@patternfly/react-icons`
- Use PatternFly components instead of raw HTML elements for UI
- Check component prop types via MCP before passing props
- Prefer composition over configuration: use child components instead of complex prop objects

## Layout

- Use `PageSection`, `Gallery`, `Grid`, `Flex`, `Stack`, `Split` for layout
- Never use raw CSS grid/flexbox when a PatternFly layout component exists
- Use PatternFly spacing tokens (`--pf-v5-global--spacer--*`) instead of arbitrary values
- Responsive behavior is built into layout components via `breakpoint` props

## Forms

- Use `FormGroup` to wrap form controls with labels and validation
- Use `helperText` and `validated` props for inline validation feedback
- Use `ActionGroup` for form action buttons (Submit, Cancel)

## Tables

- Use `@patternfly/react-table` composable table pattern
- Use `Thead`, `Tbody`, `Tr`, `Th`, `Td` components
- Add sortable columns via `sort` prop on `Th`
- Use `ActionsColumn` for row-level action menus

## Accessibility

- All interactive elements must have accessible labels
- Use `aria-label` or `aria-labelledby` on components that lack visible labels
- Icons used as buttons need `aria-label`
- Modals must trap focus and return focus on close (PatternFly handles this)
- Color alone must not convey meaning: pair with text or icons

## Common Pitfalls

- Do not override PatternFly CSS classes with custom styles: use PatternFly tokens and variants
- Do not mix PatternFly v4 and v5 components in the same view
- Do not use `style` props for spacing: use PatternFly spacing utilities or layout components
- Check the PatternFly changelog when upgrading: component APIs change between minor versions
