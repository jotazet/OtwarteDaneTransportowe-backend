# OtwarteDaneTransportowe

Django-based backend for managing and publishing open transport datasets (static feeds and realtime endpoints), with an operator workflow (review/approval), validation, and background fetching/proxying where applicable.

This README intentionally stays **high-level**. Detailed API docs and deployment instructions live elsewhere.

## What's in this repo

- **Backend**: Django + Django REST Framework
- **Runtime**: Docker Compose (web + PostgreSQL) for local development

## Documentation

- **Product / API documentation**: `Documentation.md`
- **Deployment**: `DEPLOYMENT.md` (production: `./scripts/deploy.sh up -d --build`)

## Quick start (local development)

### Requirements

- Docker + Docker Compose (`docker compose`)

### Run

```bash
docker compose up --build
```

Then open:

- App: `http://localhost:8000/`
- Django admin: `http://localhost:8000/admin/`

## Common tasks

### Create an admin user

```bash
docker compose exec web python manage.py createsuperuser
```

### Run tests

```bash
docker compose exec -T web python -m pytest
```

## Configuration notes

- Environment variables are managed via `.env` (see `.env.example` for a baseline).
- Service/port mapping for local development is defined in `docker-compose.yml`.

