# Architecture overview

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
└── data_manager/
    ├── models.py              # FeedSubmission, StaticFeedEntry, RealtimeSubmission, ...
    ├── tasks.py               # Celery tasks: feed validation, fetching, dispatchers run by Celery beat
    ├── scheduler.py           # low-level fetch functions called by the tasks (no scheduling)
    ├── validators.py          # in-process feed content checks (incl. GTFS-RT protobuf parsing)
    └── api/
        ├── urls.py
        ├── views.py
        └── serializers.py
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
```

Other `data_manager` routes (plain views, not routers):

```text
/api/data_manager/submissions/
/api/data_manager/proxy-feeds/
/api/data_manager/fetch-errors/
```
