# Persona: Frontend

## Project Context
React-based frontend applications using PatternFly design system.
TypeScript-first with strict type checking.

## Tech Stack
- React 18+ with TypeScript
- PatternFly 5 component library
- Redux Toolkit for state management
- Vite or Webpack bundler

## Testing Patterns
- Jest + React Testing Library
- Test user behavior, not implementation details
- Snapshot tests for layout-critical components
- E2E tests with Cypress or Playwright for critical flows

## Code Style
- ESLint + Prettier
- Functional components with hooks (no class components)
- Named exports preferred over default exports
- Props interfaces defined adjacent to component

## Common Pitfalls
- Always use PatternFly components — never raw HTML for UI elements
- Memoize expensive computations (useMemo) and callbacks (useCallback)
- Handle loading, error, and empty states for all async operations
- Accessibility: all interactive elements need aria labels
- Never store secrets or API keys in frontend code

## MCP Tools
- (none specific — standard web tools)
