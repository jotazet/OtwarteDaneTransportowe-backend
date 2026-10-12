# Feeds API

Feeds are split into two separate flows:

- `feed-submissions` - static feeds: GTFS, NeTEx, other.
- `realtime-submissions` - realtime feeds: GTFS-RT, SIRI, GBFS.

Both flows have a stage history (`history`) and are published only after reaching stage 4.

The full submission lifecycle (stages, rejections, fetch errors and pauses) is described in the [feed workflow](../domain/feed-workflow.md). Values of enums and stage labels are listed in [Models and enums](models.md).

## Contents

- [Combined list of a user's submissions](#combined-list-of-a-users-submissions)
- [Static feeds (FeedSubmission)](#static-feeds-feedsubmission)
- [Realtime feeds (RealtimeSubmission)](#realtime-feeds-realtimesubmission)
- [Public feed catalogue](#public-feed-catalogue)
- [Public file downloads](#public-file-downloads)

## Combined list of a user's submissions

```http
GET /api/data_manager/submissions/
```

The endpoint requires JWT and returns, in a single response (not paginated), the logged-in user's own submissions: static (`feed-submissions`) and realtime (`realtime-submissions`). The optional query param `transport_organization` filters both types by organization.

```json
{
  "user": 12,
  "static": [
    {
      "id": 37,
      "transport_organization": "ZTM Warszawa",
      "data_type": "gtfs",
      "current_stage": 2,
      "current_stage_label": "Step 2: Data verification",
      "is_rejected": false,
      "published_at": null,
      "created_at": "2026-04-27T10:00:00Z",
      "updated_at": "2026-04-27T10:00:00Z",
      "has_rejection_cause": false
    }
  ],
  "realtime": [
    {
      "id": 12,
      "transport_organization": "ZTM Warszawa",
      "protocol": "gtfs_rt",
      "current_stage": 1,
      "current_stage_label": "Step 1: Endpoints",
      "is_rejected": false,
      "published_at": null,
      "created_at": "2026-04-27T10:00:00Z",
      "updated_at": "2026-04-27T10:00:00Z",
      "has_rejection_cause": false
    }
  ]
}
```

## Static feeds (FeedSubmission)

Base endpoint:

```http
/api/data_manager/feed-submissions/
```

**Permissions:**

- `GET list` - `DataProvider` sees its own submissions; `Helper`/`Admin` sees all.
- `POST` - `DataProvider` or `Admin`.
- `GET detail` - owner or `Helper`/`Admin`.
- `PUT/PATCH` - owner (`DataProvider`) subject to the **editing restrictions** below; **Helper** may only **confirm/reject** (`stage`, `rejection_cause`); **Admin** (the `Admin` group) may edit the submission content as well as confirm/reject.
- `DELETE` - `Admin` only (the `Admin` group or a superuser); anyone else, including the owner, gets `403` (submissions are part of the review audit trail).

**Editing restrictions for the owner (static feed):**

> **URL with explicit credentials:** an address of the form `https://user:haslo@host/...`
> is allowed and treated as **intentionally public** (e.g. demo feeds with a
> documented login) — it does not force proxying and is shown in the
> catalogue in its original form. When downloading/validating, the server converts
> it into an `Authorization: Basic` header (with percent-encoding decoded).
> **Secret** credentials must be provided via `auth_type`/`auth_value`
> (encrypted, they force proxying and are never disclosed).

- **Full editing** — only when the submission is **rejected** (`is_rejected=true`, usually `current_stage=1`) or when the history contains only stage **1** (a rare case with no transition to stage 2). In that case all fields can be changed, including `name`, the source (`url` / file), `is_original`, `hide_original`, `auth_type`, `auth_value`.
- **Editing from stage 2 to 4** — when there is **no** rejection and `current_stage` is **2, 3 or 4**, the owner (`DataProvider`) may change only:
  - on the submission: **`note`** (the **`name` field is locked**);
  - in `static_entry`: only **`license`**, **`download_time_1`**, **`download_time_2`** (including setting `download_time_2` to `null`).
- **Forbidden from stage 2 to 4 for the owner and Helper** (an attempt to change them via `PATCH` returns a validation error): **`name`**, **`transport_organization`**, **`data_type`**, link / file (`url`, `file`), **`is_original`**, **`hide_original`**, **`auth_type`**, **`auth_value`** (password / secret). **Only a user in the Django `Admin` group** may change the URL/file at stages 2–4 (`is_superuser` / `is_staff` alone without that group is **not** enough).
- **Published feed (stage 4):** re-running GTFS validation **does not move** the stage back to rejection — the error is logged and the `FeedSubmission` stays at stage 4.
- **Helper** does not edit submission content (only `stage` / `rejection_cause`). **Admin** (the `Admin` group) may change the URL/file at stages 2–4; other fields according to role.

**List query params:**

- `data_type` - e.g. `gtfs`.
- `transport_organization` - organization ID.

**List:**

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 37,
      "transport_organization": 5,
      "data_type": "gtfs",
      "current_stage": 3,
      "current_stage_label": "Step 3: Admin confirmation",
      "is_rejected": false,
      "published_at": null,
      "created_at": "2026-04-27T10:00:00Z",
      "updated_at": "2026-04-27T10:00:00Z",
      "has_rejection_cause": false
    }
  ]
}
```

**Detail:**

```json
{
  "id": 37,
  "transport_organization": 5,
  "submitted_by": 12,
  "data_type": "gtfs",
  "name": "Rozklad ZTM",
  "note": "Aktualizacja kwiecien",
  "created_at": "2026-04-27T10:00:00Z",
  "updated_at": "2026-04-27T10:00:00Z",
  "current_stage": 3,
  "current_stage_label": "Step 3: Admin confirmation",
  "is_rejected": false,
  "rejection_cause": null,
  "published_at": null,
  "static_entry": {
    "id": 9,
    "url": "https://example.com/gtfs.zip",
    "file": null,
    "is_original": true,
    "hide_original": false,
    "auth_type": "",
    "download_time_1": "03:00:00",
    "download_time_2": null,
    "license": "CC BY 4.0",
    "cached_at": null,
    "uploaded_at": "2026-04-27T10:00:00Z"
  },
  "realtime_submissions": [],
  "history": [
    {
      "id": 100,
      "event_type": "uploaded",
      "stage_before": 1,
      "stage_after": 2,
      "actor": "provider",
      "cause": "",
      "created_at": "2026-04-27T10:00:00Z"
    }
  ]
}
```

`auth_value` is write-only and is never returned in a response.

**Create - URL:**

```json
{
  "transport_organization": 5,
  "data_type": "gtfs",
  "name": "Rozklad ZTM",
  "note": "Zrodlo oficjalne",
  "static_entry": {
    "url": "https://example.com/gtfs.zip",
    "is_original": true,
    "hide_original": false,
    "auth_type": "",
    "download_time_1": "03:00:00",
    "download_time_2": null,
    "license": "CC BY 4.0"
  }
}
```

**Create - URL with authorization:**

```json
{
  "transport_organization": 5,
  "data_type": "gtfs",
  "name": "Rozklad prywatny",
  "static_entry": {
    "url": "https://example.com/private/gtfs.zip",
    "is_original": true,
    "auth_type": "bearer_token",
    "auth_value": "secret-token",
    "download_time_1": "03:00:00",
    "license": "CC BY 4.0"
  }
}
```

If `auth_type` is non-empty (not `null` or `""`), the backend automatically sets `hide_original=True`.

For a URL source, `hide_original=true` **does not require** authorization to be set — the server can download a public URL and serve a copy without disclosing the original link.

**Create - file upload:**

```http
POST /api/data_manager/feed-submissions/
Content-Type: multipart/form-data
Authorization: Bearer <token>

transport_organization=5
data_type=gtfs
name=Rozklad ZTM
static_entry.file=<zip file>
static_entry.is_original=true
static_entry.license=CC BY 4.0
```

**Confirmation by Helper/Admin:**

```json
{
  "stage": 4
}
```

`stage` must be a number from 1 to 4. If `stage=4`, the backend creates a `completed` history entry and the feed becomes public.

**Rejection by Helper/Admin:**

```json
{
  "rejection_cause": "Brakuje wymaganych plikow GTFS."
}
```

## Realtime feeds (RealtimeSubmission)

Base endpoint:

```http
/api/data_manager/realtime-submissions/
```

**Permissions:**

- `GET list` - `DataProvider` sees its own; `Helper`/`Admin` sees all.
- `POST` - `DataProvider` or `Admin`.
- `GET detail` - owner or `Helper`/`Admin`.
- `PUT/PATCH` - owner (`DataProvider`) subject to the **editing restrictions** below; **Helper** may only **confirm/reject** (`stage`, `rejection_cause`); **Admin** (the `Admin` group) may edit the submission content at any stage as well as confirm/reject.
- `DELETE` - `Admin` only (the `Admin` group or a superuser); anyone else, including the owner, gets `403`.

**Editing restrictions for the owner (realtime feed):**

- **Full editing** — at stage **1** or after a **rejection** (`is_rejected`): the owner may change, among others, **`name`**, endpoint URLs, authorization (`auth_type`, `auth_value`), `is_original`, `hide_original`, and replace the entire `endpoints` list; the backend may re-run realtime validation after such a change.
- **Stored secrets** (any edit, owner or Admin): `auth_value` is never returned, so it may be omitted (or sent as `""`) when the user does not retype it. If the endpoint of the same `endpoint_type` had the same `auth_type` before the edit, its stored `auth_value` is kept. On a full edit, to replace the secret, send the new `auth_value`; when changing `auth_type`, send the new `auth_value` too; to remove authorization, send `"auth_type": ""`.
- **Editing from stage 2 upwards** — when there is **no** rejection and `current_stage >= 2`, `DataProvider` may change only:
  - on the submission: **`note`**, **`license`** (the **`name` field is locked**);
  - in each **existing** endpoint: only **`interval`**.
- **Forbidden from stage 2:** **`name`**, the feed **`url`**, **`auth_type`**, **`auth_value`**, **`is_original`**, **`hide_original`**, adding/removing endpoints, changing `transport_organization` / `static_submission` / `protocol`.
- With restricted editing, a shortened endpoint object can be sent, e.g. `{"endpoint_type": "gbfs", "interval": 120}` — missing fields are filled in from the database; an attempt to change a locked field returns a validation error.
- Changing only **`interval`** does **not** re-run the link validation task and does not delete endpoint records.
- **Helper** does not edit submission content (only `stage` / `rejection_cause`). **Admin** (the `Admin` group) may change endpoints (URL, authorization, adding/removing) at stages 2–4; the submission keeps its current stage and realtime validation re-runs in the background. A failure rejects a submission at stage 2 or 3, but **does not unpublish** one at stage 4: no rejection is recorded and the codes go to the server log only.

**List query params:**

- `transport_organization` - organization ID.

**Static submissions to which realtime can be added:**

```http
GET /api/data_manager/realtime-submissions/eligible-static-submissions/5/
```

Optionally, a data type can be added:

```http
GET /api/data_manager/realtime-submissions/eligible-static-submissions/5/gtfs/
GET /api/data_manager/realtime-submissions/eligible-static-submissions/5/netex/
GET /api/data_manager/realtime-submissions/eligible-static-submissions/5/other/
```

No type means all types (`gtfs`, `netex`, `other`). The endpoint requires JWT. `DataProvider` sees only its own successfully completed submissions, while `Helper`/`Admin` sees all of them for the organization. Only static feeds that still have some allowed realtime protocol left are returned.

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 37,
      "transport_organization": 5,
      "submitted_by": 12,
      "data_type": "gtfs",
      "name": "GTFS ZTM",
      "current_stage": 4,
      "current_stage_label": "Step 4: Complete",
      "published_at": "2026-04-27T10:00:00Z",
      "allowed_realtime_protocols": ["gtfs_rt"]
    },
    {
      "id": 38,
      "transport_organization": 5,
      "submitted_by": 12,
      "data_type": "netex",
      "name": "NeTEx ZTM",
      "current_stage": 4,
      "current_stage_label": "Step 4: Complete",
      "published_at": "2026-04-27T10:00:00Z",
      "allowed_realtime_protocols": ["siri"]
    }
  ]
}
```

**Create GTFS-RT:**

```json
{
  "transport_organization": 5,
  "static_submission": 37,
  "protocol": "gtfs_rt",
  "name": "Realtime ZTM",
  "note": "GTFS-RT do statycznego GTFS",
  "license": "CC BY 4.0",
  "endpoints": [
    {
      "endpoint_type": "trip_update",
      "url": "https://example.com/trip-updates.pb",
      "is_original": true,
      "auth_type": "",
      "interval": 30
    },
    {
      "endpoint_type": "vehicle_position",
      "url": "https://example.com/vehicle-positions.pb",
      "is_original": true,
      "auth_type": "api_key",
      "auth_value": "secret",
      "interval": 15
    }
  ]
}
```

**Create GBFS:**

```json
{
  "transport_organization": 5,
  "protocol": "gbfs",
  "name": "Rowery miejskie",
  "license": "ODbL",
  "endpoints": [
    {
      "endpoint_type": "gbfs",
      "url": "https://example.com/gbfs.json",
      "is_original": true,
      "auth_type": "",
      "interval": 60
    }
  ]
}
```

**Realtime validation rules:**

- `protocol=gtfs_rt` and `protocol=siri` require `static_submission`.
- `gtfs_rt` can only be attached to a static `gtfs` feed.
- `siri` can only be attached to a static `netex` feed.
- `static_submission` must be a published static feed (`current_stage=4`).
- `static_submission` must belong to the same `transport_organization`.
- `protocol=gbfs` cannot have a `static_submission`, i.e. GBFS cannot be attached to `gtfs` or `netex`.
- `endpoints` are required and cannot be empty.
- Two endpoints of the same `endpoint_type` cannot be added to one submission.
- The endpoint type must match the protocol.
- `auth_value` is write-only.
- If `auth_type` is non-empty (not `null` or `""`), the backend automatically sets `hide_original=True`.
- `hide_original=true` **does not require** authorization — the endpoint can be fetched by the server from a public URL without `auth_type` / `auth_value`.

**Realtime detail:**

```json
{
  "id": 12,
  "transport_organization": 5,
  "submitted_by": 12,
  "static_submission": 37,
  "protocol": "gtfs_rt",
  "name": "Realtime ZTM",
  "note": "GTFS-RT",
  "license": "CC BY 4.0",
  "created_at": "2026-04-27T10:00:00Z",
  "updated_at": "2026-04-27T10:00:00Z",
  "current_stage": 3,
  "current_stage_label": "Step 3: Admin confirmation",
  "is_rejected": false,
  "rejection_cause": null,
  "rejection_reasons": null,
  "published_at": null,
  "endpoints": [
    {
      "id": 20,
      "endpoint_type": "trip_update",
      "url": "https://example.com/trip-updates.pb",
      "is_original": true,
      "hide_original": false,
      "auth_type": "",
      "interval": 30,
      "cached_at": null
    }
  ],
  "history": []
}
```

**Realtime confirmation/rejection:**

Same as for static feeds:

```json
{ "stage": 4 }
```

or:

```json
{ "rejection_cause": "Endpoint nie odpowiada." }
```

### Realtime rejection reasons

A realtime submission can be rejected in two ways, and the detail carries one field for each:

- **Automatic validation** (`validate_realtime_submission_task`, stage 2): `rejection_reasons` is a list of `{code, details}` objects, one per problem; `rejection_cause` is `null`. The frontend translates each code ([ADR 0006](../architecture/decisions/0006-api-error-codes.md)). A published submission (stage 4) is never rejected automatically.
- **A reviewer's rejection** (Helper/Admin `PATCH {"rejection_cause": "..."}`): `rejection_cause` holds their free text; `rejection_reasons` is `null`.

Both are `null` unless the submission is currently rejected. History entries carry the same pair as `cause` and `reasons`.

```json
{
  "is_rejected": true,
  "rejection_cause": null,
  "rejection_reasons": [
    { "code": "realtime.endpoint_http_error", "details": { "endpoint_type": "trip_update", "status": 404 } },
    { "code": "realtime.endpoint_content_empty", "details": { "endpoint_type": "vehicle_position" } }
  ]
}
```

Rejections recorded before codes existed were converted when every part of their text was recognised; those rows keep the old text in `rejection_cause` as well. **When `rejection_reasons` is not `null`, show it and ignore `rejection_cause`.** Older texts that could not be recognised stay in `rejection_cause` only.

| Code | `details` | Meaning |
|------|-----------|---------|
| `realtime.static_submission_missing` | – | A GTFS-RT or SIRI submission is not linked to a static submission. |
| `realtime.static_submission_not_published` | – | The linked static submission is not published (stage 4). |
| `realtime.no_endpoints` | – | The submission has no endpoints. |
| `realtime.endpoint_http_error` | `endpoint_type`, `status` | The endpoint answered with HTTP `status` (400 or higher). |
| `realtime.endpoint_content_empty` | `endpoint_type` | The endpoint answered with an empty body. |
| `realtime.endpoint_content_invalid` | `endpoint_type`, `protocol` | The body is not a valid feed of `protocol` (`gtfs_rt`: not a parsable protobuf or no `header.gtfs_realtime_version`; `gbfs`: not JSON; `siri`: an HTML page). |
| `realtime.endpoint_too_large` | `endpoint_type`, `limit_bytes` | The response exceeded the size limit for realtime feeds (`MAX_REALTIME_FEED_BYTES`, 32 MiB by default). |
| `realtime.endpoint_url_blocked` | `endpoint_type` | The URL was refused by the outbound security rules: its host does not resolve or resolves to a non-public address, or a redirect was refused. |
| `realtime.endpoint_timeout` | `endpoint_type` | The endpoint did not answer in time. |
| `realtime.endpoint_unreachable` | `endpoint_type` | No connection could be made (refused, reset, TLS error). |
| `realtime.endpoint_fetch_failed` | `endpoint_type` | Any other error while fetching; details are in the server log. |
| `realtime.endpoint_cache_failed` | `endpoint_type` | Verification passed, but the first fetch into the server cache (`hide_original`) stored nothing. |

`endpoint_type` and `protocol` take the values listed in [Models and enums](models.md). Codes never change once released; new ones may be added, so clients fall back to a generic message for an unknown code.

## Public feed catalogue

Base endpoint:

```http
/api/data_manager/feeds/
```

**Permissions:** public.

This is not a list of individual feeds, but a list of transport organizations that have published feeds.

**List (`GET /api/data_manager/feeds/`):**

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 5,
      "region": "Mazowieckie",
      "transport_organization": "ZTM Warszawa",
      "website": "https://www.wtp.waw.pl/",
      "contact_email": "kontakt@example.com",
      "phone_number": "+48123456789",
      "is_public": true,
      "static_types": ["gtfs"],
      "dynamic_types": ["gbfs", "gtfs-rt"]
    }
  ]
}
```

`dynamic_types` uses the values `gbfs`, `gtfs-rt` and `siri` — note the hyphen in `gtfs-rt`, unlike the `gtfs_rt` protocol value.

**Detail (`GET /api/data_manager/feeds/{organization_id}/`):**

```json
{
  "id": 5,
  "region": "Mazowieckie",
  "transport_organization": "ZTM Warszawa",
  "website": "https://www.wtp.waw.pl/",
  "contact_email": "kontakt@example.com",
  "phone_number": "+48123456789",
  "is_public": true,
  "feeds": [
    {
      "id": 37,
      "name": "Rozklad ZTM",
      "data_type": "gtfs",
      "submitted_by": "provider",
      "created_at": "2026-04-27T10:00:00Z",
      "updated_at": "2026-04-27T10:00:00Z",
      "static_feed": {
        "download_url": "https://example.com/feed/37/gtfs.zip",
        "license": "CC BY 4.0",
        "cached_at": "2026-04-27T10:00:00Z",
        "is_original": true
      },
      "realtime_feed": {
        "protocol": "gtfs_rt",
        "license": "CC BY 4.0",
        "published_at": "2026-04-27T11:00:00Z",
        "endpoints": [
          {
            "endpoint_type": "trip_update",
            "interval": 30,
            "feed_url": "https://example.com/feed/rt/12/trip_update/",
            "cached_at": "2026-04-27T11:00:00Z",
            "is_original": true
          }
        ]
      }
    },
    {
      "id": 14,
      "name": "Rowery miejskie",
      "data_type": "gbfs",
      "submitted_by": "provider",
      "created_at": "2026-04-27T10:00:00Z",
      "updated_at": "2026-04-27T10:00:00Z",
      "static_feed": null,
      "realtime_feed": {
        "protocol": "gbfs",
        "license": "ODbL",
        "published_at": "2026-04-27T11:00:00Z",
        "endpoints": [
          {
            "endpoint_type": "gbfs",
            "interval": 60,
            "feed_url": "https://example.com/feed/rt/14/gbfs/",
            "cached_at": "2026-04-27T11:00:00Z",
            "is_original": true
          }
        ]
      }
    }
  ]
}
```

The public catalogue shows only feeds whose latest history entry has `stage_after=4`.

## Public file downloads

These endpoints are outside `/api/` because they serve as stable public links to feeds.

### `GET /feed/{feed_submission_id}/`

Returns a link to the static feed file if the feed is published.

**Permissions:** public.

**Response 200:**

```json
{
  "static": "https://example.com/feed/37/gtfs.zip"
}
```

If the feed does not exist or is not published, the backend returns `404`.

### `GET /feed/{feed_submission_id}/{filename}`

Returns the static file as a `FileResponse`.

**Permissions:** public, published feeds only.

### `GET /feed/rt/{realtime_submission_id}/`

Returns links to the realtime files of the given published submission.

**Permissions:** public.

**Response 200:**

```json
{
  "dynamic": {
    "trip_update": "https://example.com/feed/rt/12/trip_update/",
    "vehicle_position": "https://example.com/feed/rt/12/vehicle_position/"
  }
}
```

### `GET /feed/rt/{realtime_submission_id}/{endpoint_type}/`

Canonical route: returns the cached file of the given endpoint as a `FileResponse`.
`endpoint_type` is unique within a submission, so the download is
deterministic.

**Permissions:** public, published realtime feeds only.

### `GET /feed/rt/{realtime_submission_id}/{filename}` (legacy)

Kept for compatibility: returns the first file with a matching name.
New consumers should use the route with `endpoint_type`.

**Permissions:** public, published realtime feeds only.
