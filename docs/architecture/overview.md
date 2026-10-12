# Architecture overview

## Backend structure

The most important files:

```text
OtwarteDaneTransportowe/
├── OtwarteDaneTransportowe/
│   ├── settings_base.py       # DRF, JWT, apps, database
│   ├── urls.py                # main project URLs
│   └── auth_roles.py          # roles and permission classes
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
    ├── tasks.py               # feed validation and fetching
    ├── scheduler.py           # refresh schedule
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
/api/data_manager/feeds/
```
