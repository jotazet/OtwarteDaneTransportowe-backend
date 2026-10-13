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

## Bike and scooter sharing (rental sources)

Published GBFS feeds the planner routes with: OTP polls them at the endpoint's ODT interval, and `plan/` can use their bikes and scooters.

```http
GET    /api/planner/admin/rental-candidates/
GET    /api/planner/admin/rental-sources/
POST   /api/planner/admin/rental-sources/
GET    /api/planner/admin/rental-sources/{id}/
PATCH  /api/planner/admin/rental-sources/{id}/
DELETE /api/planner/admin/rental-sources/{id}/
```

`rental-candidates/` lists published GBFS submissions (`id`, `name`, `organization`, `rental_source`: the source's ID if already added). A source:

```json
{
  "id": 1,
  "submission": 41,
  "submission_name": "Dott Oświęcim",
  "organization": { "id": 9, "name": "Dott", "region": "Oświęcim" },
  "network": "dott-oswiecim",
  "is_enabled": true,
  "active_result": { "rental_source": 1, "network": "dott-oswiecim", "submission": 41, "outcome": "included", "error_code": "" },
  "created_at": "2026-10-13T10:00:00Z",
  "updated_at": "2026-10-13T10:00:00Z"
}
```

| Field | Notes |
| --- | --- |
| `submission` | A published `RealtimeSubmission` with protocol `gbfs` and a `gbfs` (main file) endpoint. |
| `network` | OTP's rental network, the prefix of every station and vehicle ID (`dott-oswiecim:…`). Same format as `feed_id`. **Cannot change** after creation. |
| `active_result` | This source's entry in the build being served: `outcome` `included` or `skipped` with `error_code` `planner.rental_source_not_published`, `planner.rental_source_without_gbfs`, `planner.rental_source_requires_auth` or `planner.rt_url_blocked`. |

Field error codes: `planner.feed_not_gbfs`, `planner.feed_not_published`, `planner.rental_source_without_gbfs`, `planner.rental_source_requires_auth` (on `submission`); `planner.invalid_network`, `planner.network_immutable`, `unique` (on `network`). Changes take effect at the next build. Sources that need credentials cannot be used: OTP follows the provider's links inside `gbfs.json` itself.

## OSM extract

The street map comes from an OpenStreetMap extract (`.osm.pbf`). Admins replace it with a file from a URL; the planner worker downloads it, checks it, and the next build uses it (rebuilding the street graph).

```http
GET  /api/planner/admin/osm/
POST /api/planner/admin/osm/   {"url": "https://download.geofabrik.de/europe/poland-latest.osm.pbf"}
```

`POST` answers `202` with the queued download, or `409` while another download (`planner.osm_download_in_progress`) or a graph build (`planner.build_in_progress`) is queued or running: a build reads the file a download replaces. For the same reason `POST builds/` answers `409 planner.osm_download_in_progress` during a download, and a scheduled build that falls on one is skipped. `GET` lists downloads, newest first; the newest `succeeded` one describes the file in use.

```json
{
  "id": 3,
  "url": "https://download.geofabrik.de/europe/poland-latest.osm.pbf",
  "status": "succeeded",
  "error_code": "",
  "size_bytes": 2147483648,
  "data_timestamp": "2026-10-12T20:21:35Z",
  "requested_by": "admin",
  "created_at": "2026-10-13T08:00:00Z",
  "started_at": "2026-10-13T08:00:01Z",
  "finished_at": "2026-10-13T08:01:40Z"
}
```

| Field | Values |
| --- | --- |
| `status` | `queued`, `running`, `succeeded`, `failed` |
| `error_code` | `planner.osm_download_failed` (HTTP error or connection), `planner.osm_too_large` (over `PLANNER_OSM_MAX_BYTES`), `planner.osm_url_blocked` (private address), `planner.osm_invalid` (not an OSM PBF extract), `planner.osm_download_interrupted` (the worker stopped; the watchdog notices within about 15 minutes, or an hour and a quarter for a running one) |
| `data_timestamp` | When the OSM data was exported, from the file's header. |

A failed download leaves the current file in place.

## Graph builds

```http
GET  /api/planner/admin/builds/
POST /api/planner/admin/builds/
GET  /api/planner/admin/builds/{id}/
POST /api/planner/admin/builds/{id}/activate/
```

- `POST /builds/` builds now. It returns `202` with the queued build, or `409 {"code": "planner.build_in_progress"}` when a build is already queued or running (`planner.osm_download_in_progress` during an OSM download).
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
  "indexed_at": "2026-10-14T01:56:25Z",
  "error_code": "",
  "rental_results": [],
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
| `indexed_at` | When stop and line search was refreshed from this build's graph; `null` until then (retried every 5 minutes while the build is active). |
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

## Public API settings

```http
GET   /api/planner/admin/settings/
PATCH /api/planner/admin/settings/
```

```json
{
  "public_api_enabled": false,
  "anonymous_plan_per_minute": 20, "anonymous_realtime_per_minute": 120, "anonymous_lookup_per_minute": 150,
  "key_plan_per_minute": 120, "key_realtime_per_minute": 600, "key_lookup_per_minute": 500,
  "global_plan_per_minute": 300, "global_realtime_per_minute": 3000, "global_lookup_per_minute": 6000,
  "updated_at": "2026-10-13T10:00:00Z"
}
```

- `public_api_enabled` is the kill switch of `/api/planner/v1/`: when `false`, every public endpoint except `status/` answers `503 planner.disabled`. It is `false` until an admin turns it on.
- Limits are requests per minute: `anonymous_*` per IP address (per `/64` for IPv6), `key_*` per API key, and `global_*` for all clients together. Groups are explained in [Journey planner API](planner.md#limits).
- Changes apply immediately.

## API keys

```http
GET   /api/planner/admin/api-keys/
POST  /api/planner/admin/api-keys/
GET   /api/planner/admin/api-keys/{id}/
PATCH /api/planner/admin/api-keys/{id}/
```

```json
{
  "id": 3, "name": "Gmina Wieliczka — tablica odjazdów", "contact_email": "it@example.org", "note": "",
  "prefix": "odt_Q2mS8xk", "is_active": true,
  "plan_per_minute": null, "realtime_per_minute": 1200, "lookup_per_minute": null,
  "created_by": "admin", "created_at": "2026-10-13T10:00:00Z", "last_used_at": "2026-10-13T11:25:00Z"
}
```

- `POST` returns the plain key **once**, in `key` (`odt_…`); only its SHA-256 is stored, so it cannot be shown again. Give it to the developer over a private channel.
- `*_per_minute` override the `key_*` limits for this key; `null` uses the defaults.
- Revoke a key with `PATCH {"is_active": false}`; keys are not deleted, because the request log refers to them.
- `last_used_at` is updated at most every 5 minutes.
- `contact_email` is personal data: show it to admins only.

## IP bans

```http
GET    /api/planner/admin/ip-bans/
POST   /api/planner/admin/ip-bans/
PATCH  /api/planner/admin/ip-bans/{id}/
DELETE /api/planner/admin/ip-bans/{id}/
```

```json
{ "id": 1, "network": "203.0.113.0/24", "reason": "scraping", "expires_at": null, "created_by": "admin", "created_at": "2026-10-13T10:00:00Z" }
```

`network` takes an address or a network (IPv4 or IPv6) and is stored normalised (`203.0.113.7` becomes `203.0.113.7/32`); an invalid one returns the field code `planner.invalid_network`. Bans apply within seconds; banned clients get `403 planner.client_banned`. An IPv6 client rotates addresses within its `/64`, so ban the `/64`.

## Request log

```http
GET /api/planner/admin/requests/?since=&until=&endpoint=&status=&ip=&api_key=
```

Requests to `/api/planner/v1/` of the last 30 days, newest first (default: the last 24 hours). Paginated.

```json
{ "id": 9, "created_at": "2026-10-13T11:25:00Z", "endpoint": "planner-v1-status", "status_code": 200, "latency_ms": 12,
  "client_ip": "198.51.100.7", "api_key": null, "api_key_name": null, "user_agent": "Mozilla/5.0 …" }
```

- No query parameters are stored, so trip origins and destinations never reach the log.
- Refused requests (`401`, `403`, `429`, `503`) are logged at most once a minute per client and status.
- Rows are deleted after `PLANNER_REQUEST_LOG_RETENTION_DAYS` (30) days, once each day has been summarised (see below). The log contains IP addresses, which are personal data; it is for the `Admin` group only.

## Statistics (dashboard)

For the admin dashboard: charts of API use, the most used endpoints and who sends the most requests.

```http
GET /api/planner/admin/stats/summary/?since=&until=&endpoint=&api_key=&ip=
GET /api/planner/admin/stats/timeseries/?interval=hour&split=status&since=&until=&endpoint=&api_key=&ip=
GET /api/planner/admin/stats/endpoints/?since=&until=&api_key=&ip=
GET /api/planner/admin/stats/top-clients/?by=ip&since=&until=&endpoint=&api_key=
GET /api/planner/admin/stats/user-agents/?since=&until=&endpoint=&api_key=&ip=
GET /api/planner/admin/stats/daily/?date_from=&date_to=&endpoint=&api_key=
```

The first five read the raw request log, so they cover the last 30 days (`PLANNER_REQUEST_LOG_RETENTION_DAYS`) and can be narrowed to one IP or key. They share the filters `since` and `until` (ISO 8601, default: the last 24 hours), `endpoint` (the URL name, e.g. `planner-v1-plan`), `api_key` (ID) and `ip`. A dashboard drills down by adding them: the whole API, then one endpoint, then one address. Longer history comes from `stats/daily/`.

Counts are split into status classes: `ok` (below 400), `client_errors` (400–499 except 429), `rate_limited` (429) and `server_errors` (500 and above, which includes `503` while the planner is switched off or rebuilding). Rejections (401, 403, 429, 503) are logged at most once a minute per client, so their counts are lower bounds. Latencies are in milliseconds; `p95_latency_ms` is the 95th percentile.

**`stats/summary/`**: totals for the dashboard tiles.

```json
{ "since": "2026-10-12T10:00:00Z", "until": "2026-10-13T10:00:00Z",
  "requests": 15230, "unique_ips": 412, "unique_api_keys": 3,
  "ok": 14980, "client_errors": 120, "rate_limited": 85, "server_errors": 45,
  "avg_latency_ms": 64, "p95_latency_ms": 410, "max_latency_ms": 12000 }
```

**`stats/timeseries/`**: points for a chart, every bucket present (zero when empty), in UTC.

| Parameter | Values |
| --- | --- |
| `interval` | `minute`, `hour` (default) or `day`; at most 1000 points (`planner.too_many_buckets`), e.g. 16 hours by the minute or 41 days by the hour. |
| `split` | `status` (default; one series per status class), `endpoint` (the 8 busiest endpoints and `other`) or `none` (one series `all`). |

```json
{ "since": "…", "until": "…", "interval": "hour", "split": "status",
  "buckets": ["2026-10-13T08:00:00Z", "2026-10-13T09:00:00Z"],
  "series": [ { "name": "ok", "requests": [620, 710] }, { "name": "rate_limited", "requests": [3, 0] } ],
  "latency_ms": { "avg": [58, 61], "p95": [380, 402] } }
```

**`stats/endpoints/`**: one row per endpoint, busiest first, paginated: `endpoint`, `requests`, `share` (of all requests in the range), the status classes and `avg_latency_ms`, `p95_latency_ms`, `max_latency_ms`.

**`stats/top-clients/`**: the busiest clients, paginated. With `by=ip` (default), one row per IP address:

```json
{ "client_ip": "203.0.113.9", "requests": 5310, "share": 0.3486,
  "ok": 5100, "client_errors": 10, "rate_limited": 200, "server_errors": 0,
  "endpoints": 2, "api_keys": 0, "user_agent": "python-requests/2.32",
  "first_seen": "2026-10-12T10:02:11Z", "last_seen": "2026-10-13T09:59:58Z", "banned": false }
```

`user_agent` is the most frequent one; `banned` is whether a current ban covers the address (ban it with `ip-bans/`). With `by=api_key`, one row per key with `api_key: {id, name, prefix}` and `ips` (how many addresses used it) instead of `client_ip`, `api_keys` and `banned`. IPv6 clients are counted per address here, while limits and bans apply to their `/64`.

**`stats/user-agents/`**: User-Agents by requests, paginated, with `ips` (how many addresses sent each): telling an app from a scraper behind one address.

**`stats/daily/`** (default: the last 30 days) counts requests per day, paginated like every list. It reads the daily summaries, which keep no IP addresses and are kept indefinitely; days not summarised yet (today, and yesterday until the 00:30 UTC rollup) come from the raw log:

```json
{ "count": 30, "next": null, "previous": null,
  "results": [ { "date": "2026-10-12", "requests": 1520, "ok": 1490, "client_errors": 12, "rate_limited": 3,
                 "server_errors": 15, "avg_latency_ms": 41, "max_latency_ms": 2310 } ] }
```

Error codes on these routes: field codes `planner.invalid_range` (`since` not before `until`, or `date_from` after `date_to`), `planner.too_many_buckets`, and DRF's defaults.
