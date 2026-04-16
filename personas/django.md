# Django Persona

> Auto-detected when: `manage.py` exists AND `django` found in requirements

## Architecture

- Follow Django's MTV pattern (Model-Template-View)
- Business logic belongs in **services**, not views or models
- Views should be thin — delegate to services
- Use class-based views for CRUD, function views for custom logic

## Models

- Always add `__str__` to models
- Use `Meta.ordering` for default sort
- Use `related_name` on all ForeignKey/M2M fields
- Monetary fields: use `DecimalField(max_digits=12, decimal_places=2)`, never FloatField
- Add `db_index=True` on fields used in filters/lookups
- Write data migrations for data changes, not RunPython in schema migrations

## QuerySets

- Always scope querysets by user/tenant in views and services
- Use `.select_related()` / `.prefetch_related()` to avoid N+1
- Use `.only()` / `.defer()` for large models
- Never use `.all()` in views without filtering
- Use `F()` and `Q()` objects for complex queries
- Prefer `.update()` / `.bulk_create()` over loops for batch operations

## Views & URLs

- URL patterns: use `path()`, not `re_path()` unless regex needed
- Return proper HTTP status codes (201 for create, 204 for delete)
- Use `get_object_or_404()` for single-object views
- Permission checks: use `LoginRequiredMixin` / `@login_required` + object-level perms

## Forms & Validation

- Validate at the form/serializer level, not in views
- Use `clean_<field>()` for field-specific validation
- Use `clean()` for cross-field validation

## Testing

- Use `TestCase` for DB tests, `SimpleTestCase` for non-DB tests
- Use `TransactionTestCase` only when testing transaction behavior
- Factories (factory_boy) over fixtures
- Test views via the test client, not by calling view functions directly
- Always test both success and error paths

## Migrations

- One migration per logical change
- Never edit existing migrations (create new ones)
- Name migrations descriptively: `0042_add_user_email_verified.py`
- Check migration conflicts before push: `python manage.py makemigrations --check`

## Settings

- Use environment variables for secrets (never hardcode)
- Split settings: `base.py`, `local.py`, `production.py`
- Use `django-environ` or `python-decouple` for env parsing

## Common Pitfalls

- Don't import models at module level in apps that have circular dependencies — use lazy imports
- Don't use `datetime.now()` — use `django.utils.timezone.now()`
- Don't access `request.user` without checking `request.user.is_authenticated`
- Don't use `Model.objects.create()` in a loop — use `bulk_create()`
