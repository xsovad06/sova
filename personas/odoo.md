# Odoo Persona

> Auto-detected when: `__manifest__.py` with Odoo-style keys exists

## Architecture

- One module per feature/business domain
- Module structure: `models/`, `views/`, `security/`, `data/`, `wizards/`, `reports/`
- `__manifest__.py` must list all data files and dependencies
- Use inheritance (`_inherit`) over new models when extending existing functionality

## Models

- Use `_name` with dot notation: `module.model_name`
- Use `_description` for human-readable name
- Field order: special fields, relational, computed, stored
- Use `_sql_constraints` for database-level constraints
- Use `_rec_name` for display name if not `name`
- Always set `string=` on fields for proper label display

## ORM

- Use recordsets, not raw SQL (unless performance-critical)
- Use `search()` with domains, not manual filtering
- Use `write()` on recordsets for batch updates
- Use `create()` with list of dicts for batch creation
- Use `sudo()` sparingly — document why elevated privileges are needed
- Check access rights: `check_access_rights()` / `check_access_rule()`

## Views (XML)

- Form views: use `<group>` for layout, `<notebook>` for tabs
- List views: show key fields, make important ones sortable
- Search views: add useful filters and group-by options
- Use `attrs` for conditional visibility (now `column_invisible`, `invisible`, `readonly`, `required`)
- Use `context` and `domain` on relational fields for filtering

## Security

- Always create `ir.model.access.csv` for new models
- Use record rules (`ir.rule`) for multi-company / user-level access
- Group hierarchy: user -> manager -> admin
- Test access with non-admin users

## Testing

- Use `TransactionCase` for standard tests
- Use `SavepointCase` for tests that need rollback within the test
- Use `HttpCase` for testing controllers/web
- Use `tagged()` for test categorization
- Test with demo data when possible
- Test multi-company scenarios

## Wizards

- Use `TransientModel` for wizards
- Clean up transient records (auto-vacuum handles most)
- Use `active_ids` from context for batch operations

## Reports

- Use QWeb templates for PDF reports
- Test report generation in CI
- Use `t-foreach` carefully — watch for N+1 queries

## Common Pitfalls

- Don't modify `__manifest__.py` data file list without checking all entries exist
- Don't use `env['model'].search([])` without a domain — always filter
- Don't forget to add fields to views after creating them
- Don't use `api.model` when `api.multi` (Odoo < 13) is needed
- Don't skip XML ID namespacing: always use `module.xml_id`
- Don't forget `noupdate="1"` for data that users might customize
