---
name: django-drf
description: Django 6 + Django REST Framework + Celery conventions for the ODT backend - where business logic goes, views, serializers, permissions, validation errors, querysets, background tasks, migrations and tests. Use whenever writing or reviewing Python code in this repository - models, serializers, views, URLs, permissions, Celery tasks, management commands, migrations or tests.
---

# Django and DRF in ODT

The idioms below follow Django and DRF practice and are taken from this codebase. `CLAUDE.md` has the hard rules; this skill shows how to write the code. When the existing code in an area does something differently, don't rewrite it on the way: follow these rules for new code and move old code only when the task is about it.

## Where code goes

DRF has no service layer of its own; its documentation puts validation in serializers and persistence in `serializer.save()`. ODT follows that and adds services only where a write spans more than that:

| Kind of logic | Place | Example |
| --- | --- | --- |
| Shape and validation of one request | serializer `validate_<field>()` / `validate()` | `FeedSubmissionWriteSerializer.validate` |
| Rule about one model's own fields | model `clean()` (+ `full_clean()` before saving) | `StaticFeedEntry.clean` |
| Create/update of one aggregate | serializer `create()` / `update()` in `transaction.atomic` | `FeedSubmissionWriteSerializer.create` |
| Operation across several models, or with side effects (history rows, tasks, e-mails), or used from more than one entry point (view, task, command, admin) | function in `<app>/services.py`, no DRF imports | `accounts/services.py`: `set_user_roles`, `would_remove_last_admin` |
| Who may do it | `OtwarteDaneTransportowe/auth_roles.py` (`can_*` helpers, permission classes) | `can_edit_static_feed_source` |
| HTTP: pick serializer, check permission, call the above, return a response | view / viewset | `FeedSubmissionViewSet` |

- **Views stay thin.** No queries beyond `get_queryset()`, no business rules, no inline role checks (`if 'Admin' in groups`); use or add an `auth_roles` helper.
- A service takes plain arguments (model instances, ids, values), returns plain results and raises domain exceptions. It never takes `request`, never returns a `Response`.
- Don't create `services.py` for one-line pass-throughs. Create it when the first real case arrives.

## Serializers

- Separate read and write serializers where the shapes differ (`FeedSubmissionSerializer` / `FeedSubmissionWriteSerializer`). After a write, **respond with the read serializer**, so a write returns the same shape as `GET` (see the 2026-07 entry in `docs/api/changelog.md`).
- Nested input stays under its parent key, and model-level `ValidationError` is re-raised with `raise_as_drf_validation_error(exc, field=...)` so the client gets a 400 for the right field, never a 500.
- New validation errors carry a code: `serializers.ValidationError("…", code="static_source_url_and_file")`. The code is the contract; the message is for developers only ([ADR 0006](../../../docs/architecture/decisions/0006-api-error-codes.md)).
- Secrets are write-only (`write_only=True`) and never come back in a response.

## Views and URLs

- Prefer `ModelViewSet` / `ReadOnlyModelViewSet` and routers; add `@action` for workflow steps (pause, resume, stage transitions) instead of overloading `update`.
- Set `permission_classes` on every view. The default is `IsAuthenticated`; public endpoints declare `AllowAny` explicitly.
- Lists use the default paginator (`OtwarteDaneTransportowe/pagination.py`, envelope `{count, next, previous, results}`); override `page_size` per view only with a reason, and document it in `docs/api/`.
- Throttling: public write endpoints get a scoped throttle (`throttle_scope`) with a rate in settings.

## Querysets

- Every list view uses `select_related` / `prefetch_related` for what its serializer touches, and computed per-row values as annotations (`annotate_fetch_health`), not per-row queries.
- Guard list endpoints with a query-count test: `django_assert_max_num_queries` (see `data_manager/tests/test_list_query_counts.py`).
- Filter by permissions in `get_queryset()`, so objects a user may not see return 404, not 403.

## Transactions and background work

- Writes that touch several rows run in `transaction.atomic`.
- Queue Celery tasks only after commit, and only through `enqueue()`, so a broker outage never turns a saved write into a 500:

  ```python
  transaction.on_commit(lambda: enqueue(validate_gtfs_feed_task, entry_id))
  ```

- Tasks take ids, not model instances, re-read the rows they need and are **idempotent**: a task may run twice after a retry or a re-seed. Route slow work (downloads, validation) to the `feeds` queue in `CELERY_TASK_ROUTES`.
- Every outbound request to a user-supplied URL goes through `data_manager/net_security.py`.

## Models and migrations

- Choices as module/class constants plus a `*_CHOICES` list, as the existing models do; the stored values are API contract.
- Time: `models.DateTimeField` with aware datetimes (`django.utils.timezone.now()`); never naive `datetime.now()`.
- Every model change ships its migration (`python manage.py makemigrations`); `makemigrations --check` must be clean. Never edit a deployed migration; data migrations define a reverse function or say why they can't.
- Role groups are created by migrations, never by hand in production.

## Tests

- pytest + pytest-django on a real PostgreSQL; `pytestmark = pytest.mark.django_db`.
- Test through the HTTP API (`APIClient.force_authenticate`) or a service function, not private helpers.
- Name tests after the behaviour: `test_owner_cannot_change_url_at_stage_2`. Cover every role and stage a rule depends on, including the forbidden ones.
- Mock the network and Docker (see `data_manager/tests/test_flow.py`); never call real feeds.
- A bug fix starts with a test that reproduces it.
