# Architecture overview

## System context

```
 feed consumers, data providers, ODT team
            │  browser
            ▼
   ┌──────────────────────┐  HTTP/JSON (JWT)  ┌──────────────────────────────┐
   │ frontend (separate   │ ────────────────▶ │ web: Django + DRF (gunicorn) │
   │ repo): Next.js + MUI │                   └───────┬──────────────┬───────┘
   └──────────────────────┘                           │              │ enqueue (on commit)
                                                      ▼              ▼
                                               PostgreSQL 16      Redis: broker, results, cache
                                                      ▲              │
                                                      │              ▼
                       ┌──────────────────────────────┴──────────────────────────────┐
                       │ celery_beat (DB scheduler)  → dispatches every 1 / 5 min      │
                       │ celery_worker (queue default) → realtime fetching, dispatch   │
                       │ celery_validator_worker (queue feeds) → static downloads,      │
                       │   GTFS validation via docker-socket-proxy → MobilityData image │
                       └──────────────────────────────────────────────────────────────┘
                                                      │
                                                      ▼
                                 providers' source URLs (SSRF-guarded, net_security.py)
```

- Consumers find published feeds in the catalogue (`/api/data_manager/feeds/`). Uploaded files and copies of proxied sources (`hide_original`) are served by the API host under `/feed/...`; sources that are not proxied are listed with their original URL.
- Decisions: [0001 two repositories](decisions/0001-separate-backend-and-frontend-repositories.md), [0002 Django + DRF + PostgreSQL](decisions/0002-django-drf-and-postgresql.md), [0003 JWT and roles](decisions/0003-jwt-and-group-based-roles.md), [0004 Celery + Redis](decisions/0004-celery-and-redis-for-background-work.md), [0005 feed validation](decisions/0005-feed-validation.md), [0006 error codes](decisions/0006-api-error-codes.md) (proposed), [0007 uv and ruff](decisions/0007-uv-and-ruff.md), [0008 journey planner on private OTP](decisions/0008-journey-planner-on-private-opentripplanner.md) (proposed), [0009 planner graph builds](decisions/0009-planner-graph-build-pipeline.md) (proposed).
- Workflow of a submission: [feed workflow](../domain/feed-workflow.md). Operations: [deployment](../deployment.md).
- **Journey planner** (compose profile `planner`): a private OpenTripPlanner container (`otp`) with no published port, on a network shared only with `web` and the planner worker, built and switched by `celery_planner_worker` (queue `planner`, Docker via `docker-socket-proxy`). Admin API under `/api/planner/admin/`. See [journey planner](../domain/journey-planner.md), [ADR 0008](decisions/0008-journey-planner-on-private-opentripplanner.md) and [ADR 0009](decisions/0009-planner-graph-build-pipeline.md).

## Backend structure

The most important files:

```text
OtwarteDaneTransportowe/
├── OtwarteDaneTransportowe/
│   ├── settings_base.py       # DRF, JWT, apps, database, Celery beat schedule
│   ├── settings_dev.py        # development overrides
│   ├── settings_prod.py       # production hardening and startup checks
│   ├── settings.py            # default module, imports settings_dev
│   ├── urls.py                # main project URLs
│   ├── auth_roles.py          # roles and permission classes
│   ├── pagination.py          # default list pagination (50 / max 200)
│   ├── jwt_views.py           # JWT views (role claims, login throttling)
│   ├── request_ip.py          # client IP resolution (TRUSTED_PROXY_CIDRS)
│   ├── cleanup_files.py       # deletes replaced/removed files from storage
│   └── celery.py              # Celery app
├── accounts/
│   ├── services.py            # user/role helpers
│   └── api/                   # /api/users/ (user management, /me/)
│       ├── urls.py
│       ├── views.py
│       └── serializers.py
├── blog/
│   ├── models.py              # Post, Reaction
│   └── api/
│       ├── urls.py
│       ├── views.py
│       └── serializers.py
├── cases/
│   ├── models.py              # DataProvider, TransportOrganization, CaseStatus
│   └── api/
│       ├── urls.py
│       ├── views.py
│       └── serializers.py
├── data_manager/
│   ├── models.py              # FeedSubmission, StaticFeedEntry, RealtimeSubmission, ...
│   ├── tasks.py               # Celery tasks: feed validation, fetching, dispatchers run by Celery beat
│   ├── scheduler.py           # low-level fetch functions called by the tasks (no scheduling)
│   ├── validators.py          # in-process feed content checks (incl. GTFS-RT protobuf parsing)
│   └── api/
│       ├── urls.py
│       ├── views.py
│       └── serializers.py
└── planner/
    ├── models.py              # PlannerFeed, GraphBuild
    ├── services.py            # build requests, rollback, build schedule (django-celery-beat)
    ├── pipeline.py            # graph build steps, activation, recovery (Celery only)
    ├── otp_runtime.py         # Docker (build containers, serving OTP) and OTP HTTP
    ├── otp_config.py          # generated build-config/otp-config/router-config
    ├── osm.py                 # OSM filtering and cropping to the feeds' area (osmium)
    ├── tasks.py               # Celery tasks: builds, scheduled builds, watchdog
    └── api/                   # /api/planner/admin/
```

Main DRF routers:

```text
/api/blog/posts/
/api/cases/data-providers/
/api/cases/case-statuses/
/api/cases/transport-organizations/
/api/data_manager/feed-submissions/
/api/data_manager/realtime-submissions/
/api/data_manager/static-feed-entries/
/api/data_manager/realtime-endpoints/
/api/data_manager/feeds/
/api/users/
/api/planner/admin/feeds/
/api/planner/admin/builds/
```

Other `data_manager` routes (plain views, not routers):

```text
/api/data_manager/submissions/
/api/data_manager/proxy-feeds/
/api/data_manager/fetch-errors/
```
