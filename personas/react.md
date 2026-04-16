# React Persona

> Auto-detected when: `package.json` contains `react` dependency

## Architecture

- Organize by feature, not by type
- Feature folders: `components/`, `hooks/`, `utils/`, `types/`
- Shared components in `src/components/common/` or `src/components/ui/`
- Keep components small and focused (< 200 lines)

## Components

- Prefer function components with hooks
- Use TypeScript with explicit prop types (interface, not type alias for props)
- Export named components, not default exports
- Colocate component, styles, tests, and types

```tsx
// Preferred structure
export function UserCard({ user, onEdit }: UserCardProps) {
  // ...
}
```

## Hooks

- Extract logic into custom hooks when reused or complex
- Name hooks `use<Thing>` (e.g., `useAuth`, `usePagination`)
- Keep hooks focused on one concern
- Return stable references (useMemo, useCallback) for objects/functions

## State Management

- Use local state (`useState`) when state is component-scoped
- Use context for shared state within a feature
- Use a state library (Zustand, Redux Toolkit) for global state
- Avoid prop drilling beyond 2 levels — use context or composition

## Performance

- Memoize expensive computations with `useMemo`
- Memoize callback props with `useCallback`
- Use `React.memo` for components that re-render with same props
- Use `React.lazy` + `Suspense` for code splitting
- Virtualize long lists (react-window, @tanstack/virtual)

## Data Fetching

- Use TanStack Query (React Query) or SWR for server state
- Separate server state from UI state
- Handle loading, error, and empty states explicitly
- Use optimistic updates for better UX

## Forms

- Use a form library (React Hook Form, Formik) for complex forms
- Validate with Zod or Yup schemas
- Show validation errors inline, not in alerts

## Testing

- Use React Testing Library (not Enzyme)
- Test behavior, not implementation details
- Query by role, label, or text — not by test IDs unless necessary
- Test user interactions (click, type, submit)
- Mock API calls, not components

## Styling

- Use CSS Modules, Tailwind, or styled-components (match project convention)
- Avoid inline styles except for dynamic values
- Use design tokens for colors, spacing, typography

## Common Pitfalls

- Don't mutate state directly — always create new objects/arrays
- Don't use array index as `key` for dynamic lists
- Don't put side effects in render — use `useEffect`
- Don't fetch data in `useEffect` without cleanup/cancellation
- Don't ignore ESLint exhaustive-deps warnings
