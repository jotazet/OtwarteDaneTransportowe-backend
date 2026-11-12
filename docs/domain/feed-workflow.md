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
     the URL submission or the proxy fetch; no errors -> stage 3, errors -> rejection (stage 1);
     validation that could not run (Docker, validator, time limit) -> no rejection, the stage stays,
     validation_status=error with a code, re-run by manage.py retry_gtfs_validation
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
     (see "Realtime rejection reasons" in the Feeds API); a published submission
     (stage 4, e.g. after an Admin edits its endpoints) stays published and the
     failure is only logged
     all downloads of one verification share a 4-minute budget; endpoints that do not fit
     it are rejected with `realtime.endpoint_timeout`
     a verification that still runs out of time (task soft time limit) is an infrastructure
     error, not a rejection: the submission stays at stage 2, and a periodic sweep
     (`retry_stuck_realtime_verifications`, every 15 minutes) re-queues verifications that
     have been at stage 2 for more than 16 minutes; a stage 2 set by a reviewer is left alone
  -> from stage 2 the owner may PATCH only the note, license and interval on endpoints (not the name, URL, auth, is_original, hide_original); an interval is never below 5 s
  -> Helper/Admin PATCH { "stage": 4 }
  -> the realtime feed appears publicly in the feed catalogue and /feed/rt/{id}/
```

## Reviewer override

The flows above are the normal path. Helper/Admin can override it: `PATCH {"stage": N}` sets any stage 1–4 from any state, so a reviewer can skip a stage (e.g. publish from stage 2) or move a submission back (e.g. 4 → 2, which unpublishes it). Every override is a history entry with the reviewer as `actor` (`completed` for stage 4, otherwise `stage_advanced`, also when moving back), so the audit trail shows who bypassed the workflow and when. See the reviewer override in the [Feeds API](../api/feeds.md#static-feeds-feedsubmission).

The Django admin offers the same kind of change as the bulk action "Advance selected submissions to next stage" on the FeedSubmission list. It is available only to accounts that hold Django's `change` permission on FeedSubmission **and** a reviewer role (Helper or Admin group, or superuser), so `is_staff` and model permissions alone are not enough. The action locks each submission and re-reads its stage, and it skips rejected submissions and those already at stage 4.

A stage transition through the API (a reviewer, an owner's resubmission) or a validation task locks the submission row and reads the stage from committed history before writing its history entry (`lock_submission()` in `data_manager/models.py`), so transitions of one submission happen one at a time. A request whose stage changed after it was read returns `409` `feeds.stage_changed` instead of acting on the old stage.

## Fetch errors and pauses of proxied feeds

The pause, retry and background cache mechanism applies **only to proxied feeds** (`hide_original=true`). A feed without proxying (`hide_original=false`) or an uploaded file is **not** periodically fetched or served from the server cache — the original URL (or the uploaded file) is what is publicly visible. For such feeds:

- `pause-fetch` / `resume-fetch` return `400 Bad Request` with `{"code": "feeds.not_proxy_managed", "details": {}}`
- the fields `fetch_status`, `fetch_failure_count`, `next_fetch_after` etc. are **not returned** by the API (`is_proxy_managed=false`)
- the Celery schedule and the realtime loop **skip** these feeds

When a proxied static feed stops being proxied, or its URL changes, the cache of the old URL is dropped with the change, so it is never validated or served for the new source.

A scheduled fetch of a proxied static feed skips a file that has not changed upstream. With the cached copy the server stores the upstream `ETag` and `Last-Modified` and the SHA-256 of the body, and sends `If-None-Match` / `If-Modified-Since` on the next fetch. A `304 Not Modified`, or a `200` whose body has the same SHA-256 (an upstream that ignores conditional requests), is a successful fetch: `last_fetch_success_at` is updated, while the copy, `cached_at` and the validation report stay as they are and no GTFS validation is queued. The exception is a validation that never finished (`validation_status` `pending` or `error`): it is queued again. The conditional headers follow the rule for credentials and are not sent to a redirect target on another origin, so such feeds rely on the hash. A fetch queued by a new or resubmitted source always downloads and validates the file, and a source change clears the stored `ETag`, `Last-Modified` and hash with the cache.

Exception: **validation stage 2** — for a static GTFS feed with a non-proxied URL, the server may download the file **once** into a temporary directory solely for the validator (without saving to `cached_file` and without starting the proxy mechanism). For realtime, stage 2 validation performs a link-check on the URL, checks the response format in process (`data_manager/validators.py`) and creates a cache only for `hide_original=true`.

Proxy fetching applies to static feeds and realtime endpoints with `hide_original=true`. Each fetch has a total time budget (`FEED_FETCH_TOTAL_TIMEOUT_SECONDS` / `REALTIME_FETCH_TOTAL_TIMEOUT_SECONDS`) besides the per-read timeout, so a server that trickles bytes is cut off as a `timeout`. A body cut off mid-transfer (shorter than its `Content-Length`, or a broken chunked encoding) is a `connection_error`, not `invalid_content`: a transfer problem, not a problem with the data. Every fetch error is stored as a `FeedFetchError` and can be read from these endpoints:

```http
GET /api/data_manager/fetch-errors/?days=7
GET /api/data_manager/feed-submissions/{id}/fetch-errors/?days=7
GET /api/data_manager/realtime-submissions/{id}/fetch-errors/?days=7
```

`GET /api/data_manager/fetch-errors/` is a global list of errors from all feeds: `DataProvider` sees errors of its own feeds, `Helper`/`Admin` see all. The starting point for diagnosis is this list first, and then the per-feed details.

Available filters:

- `days` - number of days back, default `7`, maximum `365`
- `error_type` - e.g. `timeout`, `connection_error`, `http_error`, `invalid_content`, `auth_error` (ODT cannot decrypt the stored credential, `message` `fetch.credential_undecryptable`; nothing is sent to the provider)
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

Error records and `proxy-feeds` items also return that id as `submission_id`, so the client can link to the feed: `source=static` → `/api/data_manager/feed-submissions/{submission_id}/`, `source=realtime` → `/api/data_manager/realtime-submissions/{submission_id}/`.

Example record from `GET /api/data_manager/fetch-errors/`:

```json
{
  "id": 91,
  "source": "static",
  "static_entry": 12,
  "endpoint_rt": null,
  "endpoint_type": null,
  "submission_id": 23,
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

`fetch_pause_reason`, `last_fetch_error_message` and the server logs show the source URL without userinfo (`user:pass@`), query string and fragment, where providers put credentials (`data_manager.net_security.redact_url`).

After consecutive failed fetches the system automatically postpones the next attempts. A realtime endpoint that fails with a **transient** error (timeout, connection error, a body cut off mid-transfer, HTTP 5xx or 429) retries sooner, because realtime data is worthless after an hour; static feeds and **permanent** errors (HTTP 401/403/404 and other 4xx, a blocked URL, content that is not a valid feed) use the long ladder:

| Consecutive error | Static feed, or realtime permanent error | Realtime transient error |
|-------------------|------------------------------------------|--------------------------|
| 1 | `delayed`, next attempt after 5 minutes | `delayed`, after 30 seconds |
| 2 | `delayed`, after 1 hour | `delayed`, after 2 minutes |
| 3 | `delayed`, after 6 hours | `delayed`, after 10 minutes |
| 4 | `auto_paused`, retried once a day | `delayed`, after 1 hour |
| 5 and more | `auto_paused`, retried once a day | `auto_paused`, retried once a day |

The error counter spans both ladders and the latest error picks one: a realtime endpoint that timed out twice and then returns 404 waits 6 hours (step 3 of the long ladder).

An auto-paused feed is not given up on: `next_fetch_after` is set 24 hours ahead and the feed is tried once more when it passes. A failed daily retry keeps it `auto_paused` until the next day (the counter keeps growing); a static feed is not fetched at its `download_time_1/2` in the meantime. The first successful fetch, a daily retry included, resets the error counter to zero, sets `fetch_status=active` and clears `next_fetch_after` and `fetch_pause_reason`. A `manual_paused` feed is never retried automatically.

A realtime back-off step shorter than the bootstrap period (5 minutes, so the 30-second and 2-minute steps) is queued as the endpoint's next fetch. A longer step and the daily retry have no fetch queued: the periodic realtime bootstrap (every 5 minutes) resumes the endpoint once `next_fetch_after` has passed, so the actual retry comes up to 5 minutes after the time in the table. Static feeds are retried by the per-minute dispatcher.

A manual pause is never undone by a fetch: a download that was already running when `pause-fetch` was called still records `last_fetch_success_at` or `last_fetch_error_at` (and its `FeedFetchError`), but `fetch_status`, the error counter and `fetch_pause_reason` stay as the pause set them, and the realtime loop stops. Only `resume-fetch` lifts it.

`Helper` or `Admin` can manually pause or resume a single **proxied** feed (`is_proxy_managed=true`). The `proxy-feeds` list is available to any logged-in user (a `DataProvider` sees only its own feeds), but `pause-fetch` / `resume-fetch` return `403` for anyone other than `Helper`/`Admin`:

```http
GET /api/data_manager/proxy-feeds/
POST /api/data_manager/static-feed-entries/{id}/pause-fetch/
POST /api/data_manager/static-feed-entries/{id}/resume-fetch/
POST /api/data_manager/realtime-endpoints/{id}/pause-fetch/
POST /api/data_manager/realtime-endpoints/{id}/resume-fetch/
```

`GET /api/data_manager/proxy-feeds/` returns a single, paginated list of all proxied feeds (static and realtime). Each item has a `source` (`static` or `realtime`) and the entry `id` — these values indicate which `pause-fetch` / `resume-fetch` endpoint to call. The list is sorted by `region`, `organization`, `feed_name`, then `source` and `id`, in the database with its collation (`en_US.utf8` in the `postgres:16` image): case does not decide the order and Polish letters sort next to their base letters (`łódzkie` next to `lubelskie`, not after `z`). Pages are cut from that one order, so every feed appears on exactly one page.

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

The static scheduler and the realtime loop do not fetch feeds that are `manual_paused`, or `delayed` or `auto_paused` before `next_fetch_after`. `resume-fetch` also cancels the temporary retry block.

## Who sees what

| User | `feed-submissions` list | `realtime-submissions` list |
|------|-------------------------|-----------------------------|
| `DataProvider` | Own only | Own only |
| `Helper` | All | All |
| `Admin` | All | All |
| Anonymous | No access | No access |
