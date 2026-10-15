# OtwarteDaneTransportowe

Django-based backend for managing and publishing open transport datasets (static feeds and realtime endpoints), with an operator workflow (review/approval), validation, and background fetching/proxying where applicable.

This README intentionally stays **high-level**. Detailed API docs and deployment instructions live elsewhere.

## What's in this repo

- **Backend**: Django + Django REST Framework
- **Runtime**: Docker Compose (web + PostgreSQL) for local development

## Documentation

- **API reference**: [`docs/api/`](docs/api/README.md), with breaking changes in [`docs/api/changelog.md`](docs/api/changelog.md)
- **Deployment**: [`docs/deployment.md`](docs/deployment.md) (production: `./scripts/deploy.sh up -d --build`)
- **Domain, architecture and decisions**: [`docs/`](docs/README.md)
- **Conventions for contributors and AI agents**: [`CLAUDE.md`](CLAUDE.md)

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

The journey planner (OpenTripPlanner, ~7 GB of RAM) is opt-in:

```bash
COMPOSE_PROFILES=planner docker compose up --build
```

It needs an OSM extract in `planner_data/osm/region.osm.pbf` (a region such as Małopolska is enough locally); see [Journey planner](docs/deployment.md#19-journey-planner-opentripplanner).

## Common tasks

### Create an admin user

```bash
docker compose exec web python manage.py createsuperuser
```

### Run checks and tests

```bash
uv sync                                         # local virtualenv with dev tools
uv run ruff check . && uv run ruff format --check .
docker compose exec -T web python -m pytest     # tests in the container
```

Dependencies are managed with [uv](https://docs.astral.sh/uv/): `uv add <package>` (or `uv add --dev`), then rebuild the image.

## Configuration notes

- Environment variables are managed via `.env` (see `.env.example` for a baseline).
- Service/port mapping for local development is defined in `docker-compose.yml`.

