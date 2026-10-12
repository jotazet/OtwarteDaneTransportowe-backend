# Journey planner: admin API

Operate the planner: choose which published feeds go into the OpenTripPlanner graph, schedule and run graph builds, roll back, and check what is being served. Background: [journey planner](../domain/journey-planner.md), [ADR 0009](../architecture/decisions/0009-planner-graph-build-pipeline.md).

**Permissions:** every route requires the `Admin` group (`RequiresAdminGroup`). Anonymous requests get `401`; other roles and superusers without the group get `403`.

Base path: `/api/planner/admin/`. Error responses of planner operations use the `{code, details}` shape ([ADR 0006](../architecture/decisions/0006-api-error-codes.md)); field validation errors keep DRF's field dictionary, with the codes listed below.

## Feed candidates

```http
GET /api/planner/admin/feed-candidates/
```

Published GTFS submissions, with `planner_feed` set to the planner feed's id when the feed is already included (otherwise `null`). Paginated.

```json
{
  "count": 2, "next": null, "previous": null,
  "results": [
    { "id": 11, "name": "GTFS KRK", "organization": { "id": 5, "name": "Zarząd Transportu Publicznego", "region": "Kraków" }, "planner_feed": 1 },
    { "id": 22, "name": "Kielce", "organization": { "id": 7, "name": "ZTM Kielce", "region": "Kielce" }, "planner_feed": null }
  ]
}
```

## Planner feeds

```http
GET    /api/planner/admin/feeds/
POST   /api/planner/admin/feeds/
GET    /api/planner/admin/feeds/{id}/
PATCH  /api/planner/admin/feeds/{id}/
DELETE /api/planner/admin/feeds/{id}/
```

```json
{
  "id": 1,
  "submission": 11,
  "submission_name": "GTFS KRK",
  "organization": { "id": 5, "name": "Zarząd Transportu Publicznego", "region": "Kraków" },
  "feed_id": "krk",
  "is_enabled": true,
  "include_realtime": true,
  "failure_policy": "keep_last_good",
  "last_good_at": "2026-10-14T01:52:10Z",
  "active_result": {
    "feed_id": "krk", "outcome": "fresh", "sha256": "…", "version_at": "2026-10-13T03:00:12Z",
    "error_code": "", "log_excerpt": "", "realtime_skipped": []
  },
  "created_at": "2026-10-13T10:00:00Z",
  "updated_at": "2026-10-13T10:00:00Z"
}
```

| Field | Notes |
| --- | --- |
| `submission` | A published GTFS `FeedSubmission`. On `PATCH` it may only move to another submission of the **same organization** (a provider re-submitted the feed); IDs in the planner stay the same. |
| `feed_id` | OTP feed ID, the prefix of every stop, route and trip ID in the planner (`krk:2047691`). Lowercase letters, digits and hyphens, 2–32 characters. **Cannot change** after creation. |
| `is_enabled` | Disabled feeds are left out of the next build. |
| `include_realtime` | Whether OTP polls the feed's published GTFS-RT endpoints. |
| `failure_policy` | `keep_last_good` (default): if the newest file fails, the last version that went into a served graph is used and the feed is marked `stale`. `exclude`: the feed is left out. |
| `last_good_at` | When the feed's last good version went into a served graph. Read-only. |
| `active_result` | This feed's entry in the build being served, or `null`. Read-only. |

Field error codes: `planner.feed_not_gtfs`, `planner.feed_not_published`, `planner.feed_other_organization` (on `submission`); `planner.invalid_feed_id`, `planner.feed_id_immutable`, `unique` (on `feed_id`).

Changes take effect at the next build.

## Graph builds

```http
GET  /api/planner/admin/builds/
POST /api/planner/admin/builds/
GET  /api/planner/admin/builds/{id}/
POST /api/planner/admin/builds/{id}/activate/
```

- `POST /builds/` builds now. It returns `202` with the queued build, or `409 {"code": "planner.build_in_progress"}` when a build is already queued or running.
- `POST /builds/{id}/activate/` serves that earlier build's graph again (a rollback). It returns `202`, or `409` with `planner.build_in_progress` or `planner.build_not_activatable` (the build did not succeed, is already served, was built with another OTP image, or its graph was removed). Only the served graph and the newest other one are kept.
- Lists are newest first; the detail adds `feed_results` and `log_excerpt`.

```json
{
  "id": 42,
  "status": "succeeded",
  "trigger": "schedule",
  "requested_by": null,
  "rollback_of": null,
  "otp_image": "opentripplanner/opentripplanner:2.10.0",
  "created_at": "2026-10-14T01:40:00Z",
  "started_at": "2026-10-14T01:40:01Z",
  "otp_stopped_at": "2026-10-14T01:40:30Z",
  "finished_at": "2026-10-14T01:56:12Z",
  "is_active": true,
  "error_code": "",
  "feed_results": [
    { "planner_feed": 3, "feed_id": "kml", "submission": 38, "outcome": "stale", "sha256": "…",
      "version_at": "2026-10-13T02:00:00Z", "error_code": "planner.feed_invalid",
      "log_excerpt": "… CsvEntityIOException …", "realtime_skipped": [] }
  ],
  "log_excerpt": ""
}
```

| Field | Values |
| --- | --- |
| `status` | `queued`, `running`, `succeeded`, `failed` |
| `trigger` | `schedule`, `manual`, `rollback` |
| `otp_stopped_at` | Start of the maintenance window: the serving OTP is stopped from then until the build ends. |
| `feed_results[].outcome` | `fresh`, `stale`, `excluded` (see [journey planner](../domain/journey-planner.md#build-outcomes-per-feed)) |
| `feed_results[].error_code` | `planner.feed_not_published`, `planner.feed_source_unavailable`, `planner.feed_invalid`, `planner.feed_no_service_in_period` |
| `feed_results[].realtime_skipped` | `[{feed_id, endpoint_type, error_code: "planner.rt_url_blocked"}]` |
| `error_code` (build) | `planner.no_feeds`, `planner.osm_missing`, `planner.osm_preparation_failed`, `planner.street_build_failed`, `planner.graph_build_failed`, `planner.build_out_of_memory`, `planner.graph_missing`, `planner.activation_failed`, `planner.docker_unavailable`, `planner.build_interrupted`, `planner.build_failed` |

`log_excerpt` texts are OTP's own log output for operators and are not translated.

## Build schedule

```http
GET /api/planner/admin/schedule/
PUT /api/planner/admin/schedule/
```

```json
{ "enabled": true, "weekdays": [3, 7], "time_utc": "01:40" }
```

`weekdays` are ISO days (1 = Monday … 7 = Sunday). `time_utc` is in **UTC**: 01:40 UTC is 03:40 in Polish summer time and 02:40 in winter. Until the schedule is saved, `GET` returns this default with `enabled: false`. Saving never starts a build immediately.

## Status

```http
GET /api/planner/admin/status/
```

```json
{
  "active_build": { "id": 42, "status": "succeeded", "…": "…" },
  "running_build": null,
  "maintenance": false,
  "otp_healthy": true
}
```

`maintenance` is `true` while a build has the serving OTP stopped. `otp_healthy` comes from OTP's health check (graph loaded, realtime polled once) and is cached for 15 seconds.
