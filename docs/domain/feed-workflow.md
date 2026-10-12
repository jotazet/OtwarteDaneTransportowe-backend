# Feed workflow

Troubleshooting a GTFS validation that hangs at stage 2 is covered in the [deployment guide](../deployment.md).

The endpoints used below are documented in the [Feeds API](../api/feeds.md); stage labels and enum values are listed in [Models and enums](../api/models.md).

## Static feed

```text
DataProvider
  -> POST /api/data_manager/feed-submissions/
  -> backend saves FeedSubmission + StaticFeedEntry
  -> backend creates an uploaded history entry (stage 1 -> 2), so a new submission starts at stage 2
  -> data_type=gtfs: GTFS validation (Celery feeds queue, worker with Docker) runs after the file upload,
     the URL submission or the proxy fetch; no errors -> stage 3, errors -> rejection (stage 1)
  -> netex / other: no automatic validation; the submission stays at stage 2 until Helper/Admin sets the stage
  -> StaticFeedEntry.validation_status: pending / valid / invalid / error
  -> from stage 2 the owner may PATCH only the note, license and download schedule (not the name, URL/file, auth, is_original, hide_original)
  -> Helper/Admin PATCH { "stage": 4 }
  -> the feed appears publicly in /api/data_manager/feeds/ and /feed/{id}/
```

If the feed is rejected:

```text
Helper/Admin PATCH { "rejection_cause": "..." }
  -> is_rejected=true
  -> current_stage_label="Rejected"
  -> DataProvider sees the reason in the detail view
  -> DataProvider may PATCH the full static_entry (url / file, auth, etc.)
  -> after a PATCH that changes the source (url, file, auth, is_original, hide_original)
     backend creates an uploaded history entry (stage 1 -> 2)
     -> is_rejected=false, rejection_cause=null
     -> the source is locked for the owner again (as after the first POST)
  -> GTFS validation re-runs after the file changes / the cache is fetched
```

## Realtime feed

```text
DataProvider
  -> POST /api/data_manager/realtime-submissions/
  -> backend saves RealtimeSubmission + RealtimeEndpointRT[]
  -> realtime validation task (stage 1 -> 2, on success 2 -> 3; with hide_original also the first cache fetch)
     errors -> rejection (stage 1) with coded rejection_reasons, one per problem
     (see "Realtime rejection reasons" in the Feeds API)
  -> from stage 2 the owner may PATCH only the note, license and interval on endpoints (not the name, URL, auth, is_original, hide_original)
  -> Helper/Admin PATCH { "stage": 4 }
  -> the realtime feed appears publicly in the feed catalogue and /feed/rt/{id}/
```

## Fetch errors and pauses of proxied feeds

The pause, retry and background cache mechanism applies **only to proxied feeds** (`hide_original=true`). A feed without proxying (`hide_original=false`) or an uploaded file is **not** periodically fetched or served from the server cache — the original URL (or the uploaded file) is what is publicly visible. For such feeds:

- `pause-fetch` / `resume-fetch` return `400 Bad Request`
- the fields `fetch_status`, `fetch_failure_count`, `next_fetch_after` etc. are **not returned** by the API (`is_proxy_managed=false`)
- the Celery schedule and the realtime loop **skip** these feeds

Exception: **validation stage 2** — for a static GTFS feed with a non-proxied URL, the server may download the file **once** into a temporary directory solely for the validator (without saving to `cached_file` and without starting the proxy mechanism). For realtime, stage 2 validation performs a link-check on the URL, checks the response format in process (`data_manager/validators.py`) and creates a cache only for `hide_original=true`.

Proxy fetching applies to static feeds and realtime endpoints with `hide_original=true`. Every fetch error is stored as a `FeedFetchError` and can be read from these endpoints:

```http
GET /api/data_manager/fetch-errors/?days=7
GET /api/data_manager/feed-submissions/{id}/fetch-errors/?days=7
GET /api/data_manager/realtime-submissions/{id}/fetch-errors/?days=7
```

`GET /api/data_manager/fetch-errors/` is a global list of errors from all feeds: `DataProvider` sees errors of its own feeds, `Helper`/`Admin` see all. The starting point for diagnosis is this list first, and then the per-feed details.

Available filters:

- `days` - number of days back, default `7`, maximum `365`
- `error_type` - e.g. `timeout`, `connection_error`, `http_error`, `invalid_content`
- `source` - `static` or `realtime`
- `endpoint_type` - realtime only, e.g. `trip_update`, `gbfs`

All error lists are paginated (`PageNumberPagination`) so that everything is not loaded on a single page:

- `page` - page number
- `page_size` - page size, default `25`, maximum `100`

The response has the format `{"count", "next", "previous", "results": [...]}`.

Every error record, and the responses of the `static-feed-entries`, `realtime-endpoints` and `proxy-feeds` endpoints, contain readable fields identifying the feed (instead of bare ids):

- `organization` - name of the transport organization (not the id)
- `region` - region of the transport organization (only in `static-feed-entries`, `realtime-endpoints` and `proxy-feeds`; error records do not have it)
- `feed_name` - feed name with its id in the format `#<id> name`, e.g. `#23 ZTM Warszawa` (when the feed has no name, just `#23`)

`#<id>` is the id of the feed submission (`FeedSubmission` for static, `RealtimeSubmission` for realtime) - the same id under which the feed is published at `/feed/{id}/` and `/feed/rt/{id}/`.

Example record from `GET /api/data_manager/fetch-errors/`:

```json
{
  "id": 91,
  "source": "static",
  "static_entry": 12,
  "endpoint_rt": null,
  "endpoint_type": null,
  "organization": "ZTM Warszawa",
  "feed_name": "#23 GTFS ZTM",
  "error_type": "timeout",
  "http_status_code": null,
  "message": "HTTPSConnectionPool... Read timed out.",
  "url_attempted": "https://example.org/gtfs.zip",
  "occurred_at": "2026-06-22T15:04:11Z"
}
```

Static and realtime feeds also return fetch health fields in their serializers (only when `is_proxy_managed=true`):

- `is_proxy_managed` — whether the feed is handled by the background proxy/cache
- `fetch_status`: `active`, `delayed`, `auto_paused`, `manual_paused`
- `fetch_failure_count`
- `next_fetch_after`
- `fetch_paused_at`
- `fetch_pause_reason`
- `last_fetch_success_at`
- `last_fetch_error_at`
- `last_fetch_error_message`
- `fetch_error_count_7d`

After consecutive failed fetches the system automatically postpones the next attempts:

| Consecutive error | Behaviour |
|-------------------|-----------|
| 1 | `fetch_status=delayed`, next attempt after 5 minutes |
| 2 | `fetch_status=delayed`, next attempt after 1 hour |
| 3 | `fetch_status=delayed`, next attempt after 6 hours |
| 4 | `fetch_status=auto_paused`, no further automatic attempts |

The first successful fetch resets the error counter to zero and sets `fetch_status=active`.

`Helper` or `Admin` can manually pause or resume a single **proxied** feed (`is_proxy_managed=true`). The `proxy-feeds` list is available to any logged-in user (a `DataProvider` sees only its own feeds), but `pause-fetch` / `resume-fetch` return `403` for anyone other than `Helper`/`Admin`:

```http
GET /api/data_manager/proxy-feeds/
POST /api/data_manager/static-feed-entries/{id}/pause-fetch/
POST /api/data_manager/static-feed-entries/{id}/resume-fetch/
POST /api/data_manager/realtime-endpoints/{id}/pause-fetch/
POST /api/data_manager/realtime-endpoints/{id}/resume-fetch/
```

`GET /api/data_manager/proxy-feeds/` returns a single, paginated list of all proxied feeds (static and realtime). Each item has a `source` (`static` or `realtime`) and the entry `id` — these values indicate which `pause-fetch` / `resume-fetch` endpoint to call. The list is sorted by `region`, `organization`, `feed_name`.

Example item from `GET /api/data_manager/proxy-feeds/`:

```json
{
  "source": "static",
  "id": 12,
  "submission_id": 23,
  "organization": "ZTM Warszawa",
  "region": "mazowieckie",
  "feed_name": "#23 GTFS ZTM",
  "data_type": "gtfs",
  "protocol": null,
  "endpoint_type": null,
  "fetch_status": "active",
  "fetch_failure_count": 0,
  "next_fetch_after": null,
  "fetch_paused_at": null,
  "fetch_pause_reason": "",
  "last_fetch_success_at": "2026-06-22T14:00:00Z",
  "last_fetch_error_at": null,
  "last_fetch_error_message": null,
  "fetch_error_count_7d": 0
}
```

`pause-fetch` accepts an optional JSON body:

```json
{
  "reason": "Awaria po stronie dostawcy"
}
```

The static scheduler and the realtime loop do not fetch feeds that are `manual_paused`, `auto_paused`, or `delayed` before `next_fetch_after`. `resume-fetch` also cancels the temporary retry block.

## Who sees what

| User | `feed-submissions` list | `realtime-submissions` list |
|------|-------------------------|-----------------------------|
| `DataProvider` | Own only | Own only |
| `Helper` | All | All |
| `Admin` | All | All |
| Anonymous | No access | No access |
