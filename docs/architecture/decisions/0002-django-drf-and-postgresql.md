# 0002. Build the backend with Django, Django REST Framework and PostgreSQL

- **Status:** Accepted (recorded retroactively)
- **Date:** 2026-10-12

## Context

ODT needs a REST API for the website, an admin panel for the team, user accounts with roles, file uploads and a relational model of organizations, cases and feed submissions with their history. The team is small, so batteries-included tooling matters more than raw performance.

## Decision

- **Django 6** with **Django REST Framework** for the API, `django-filter` for list filtering and **drf-spectacular** for the OpenAPI schema (public only with `DEBUG`, otherwise for admin users).
- **PostgreSQL 16** as the only database. The code uses PostgreSQL features (`django.contrib.postgres`, `ArrayField`, partial indexes), so there is no SQLite fallback, also not in tests.
- The project is split into Django apps by area: `accounts`, `blog`, `cases`, `data_manager`, with the project package `OtwarteDaneTransportowe/` holding settings, URLs, Celery, roles and shared helpers.
- Settings are layered: `settings_base.py`, then `settings_dev.py` / `settings_prod.py`; configuration and secrets come from environment variables. Production refuses to start with weak or default secrets.

## Consequences

- The Django admin covers back-office work without custom UI.
- Every list endpoint returns DRF's page envelope `{count, next, previous, results}` (see [API](../../api/README.md)).
- Tests need a real PostgreSQL (Docker Compose locally, a service container in CI).

## Alternatives considered

- **FastAPI** (used in the sister project Solitable) — leaner, but ODT would have to build accounts, admin and permissions that Django gives for free.
