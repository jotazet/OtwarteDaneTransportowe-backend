# API changelog

Breaking and notable changes to the API contract, newest first. Every change that a client must react to gets an entry here in the same pull request.

## 2026-11-12: coded errors for a source credential ODT cannot decrypt

Not breaking. When ODT cannot decrypt a stored `auth_value` (a key configuration problem on ODT's side), the download is not attempted and the failure is reported as a code:

- a fetch of a proxied feed fails with a `FeedFetchError` of `error_type` `auth_error` (defined before, written now) and `message` `fetch.credential_undecryptable`; `fetch_pause_reason` and `last_fetch_error_message` are that code too, and the usual backoff and auto-pause apply;
- GTFS validation of a non-proxied URL ends with `validation_status` `error` and the new code `gtfs_validation.credential_undecryptable` (no rejection);
- realtime verification stays at stage 2 instead of rejecting, and is retried.

Input that starts with `fernet$` is now encrypted like any other value. See [Static feeds](feeds.md#static-feeds-feedsubmission) and [Fetch errors and pauses](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds).

**Frontend:** translate `gtfs_validation.credential_undecryptable` for `validation_status: "error"`, and `fetch.credential_undecryptable` where fetch errors are shown.

---

## 2026-11-12: faster realtime retries, daily retry of auto-paused feeds

Not breaking. A proxied realtime endpoint that fails with a transient error (timeout, connection error, truncated body, HTTP 5xx or 429) is now retried after 30 s, 2 min, 10 min and 1 h before it is auto-paused (`fetch_status=delayed` with `next_fetch_after` set as before). Static feeds and permanent errors keep 5 min, 1 h, 6 h. An `auto_paused` feed now has `next_fetch_after` set 24 hours ahead (it used to be `null`) and is tried again then; a success returns it to `active`, a failure moves `next_fetch_after` another day ahead. Feeds auto-paused before this change are retried once right after the deploy. `manual_paused` feeds keep `next_fetch_after: null` and are never retried automatically. See [Fetch errors and pauses](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds).

---

## 2026-11-12: `vehicles/coverage/` answers `planner.busy` while an operator's first count runs

Not breaking for clients that handle `planner.busy`, which every planner endpoint can answer. `GET /api/planner/v1/vehicles/coverage/` recounts an operator's trips under way in one request at a time. A request that cannot recount an operator now (another request is recounting it, or the server is busy recounting others) gets its previous count (at most 10 minutes old), as before; when there is no previous count yet (after new timetables, or 10 minutes without requests for that operator) it now answers `503` with `{"code": "planner.busy", "details": {}}` and `Retry-After`, instead of recounting in parallel. The limit of 20 operators per request is unchanged. See [Planner API](planner.md#get-apiplannerv1vehiclescoverage).

---

## 2026-11-12: `409 feeds.stage_changed` when a submission's stage changes mid-request

Not breaking, but clients should handle the new status. `PUT`/`PATCH /api/data_manager/feed-submissions/{id}/` and `realtime-submissions/{id}/` now lock the submission and return `409` with `{"code": "feeds.stage_changed", "details": {}}` when its stage or rejection changed after the request read it (a validation run or another reviewer wrote history in between). Nothing is saved; reload and retry. A reviewer's publish no longer races a validation rejection, so a just-published feed is never rejected automatically, and history rows always record the real `stage_before`. See [Static feeds](feeds.md#static-feeds-feedsubmission).

---

## 2026-11-12: source URLs in fetch error messages are redacted

Not breaking. URLs inside `fetch_pause_reason`, `last_fetch_error_message` and the cause of an automatic rejection after a failed validation download keep only scheme, host, port and path; userinfo, the query string and the fragment are removed. See [Fetch errors and pauses](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds).

---

## 2026-11-12: unchanged static feeds are not downloaded and validated again

Not breaking. A scheduled fetch of a proxied static feed sends `If-None-Match` / `If-Modified-Since` and compares the SHA-256 of the body with the cached copy. When the upstream file has not changed, `last_fetch_success_at` is updated but `cached_at`, the copy and the validation report are not, and no GTFS validation runs. `cached_at` therefore moves only when a new copy is stored; `last_fetch_success_at` tells when the upstream was last checked. See [Fetch errors and pauses](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds).

---

## 2026-11-12: non-numeric ids and filters no longer return `500`

Not breaking. The `transport_organization` query filter of `GET /api/data_manager/submissions/`, `feed-submissions/` and `realtime-submissions/` returns `400` with `{"transport_organization": ["feeds.invalid_integer"]}` for a value that is not an integer (it used to be a `500`). A non-numeric id in `realtime-submissions/eligible-static-submissions/{id}/` and `feed-submissions/{pk}/download/static|realtime/{endpoint_pk}/` returns `404`. See [Feeds API](feeds.md).

---

## 2026-11-12: simultaneous first reactions no longer return 500

Not breaking. Two simultaneous first `POST /api/blog/reactions/{post_id}/` requests from one IP (a double click) used to make one of them fail with `500`. The request that loses the race now updates the row the other created and answers `200`. See [Blog API](blog.md).

---

## 2026-11-12: realtime refresh interval of at least 5 seconds

Not breaking for clients that send sensible intervals. A realtime endpoint's `interval` in `POST`/`PUT`/`PATCH /api/data_manager/realtime-submissions/` must now be at least **5** seconds; `0` to `4` return `400` with `["realtime.interval_too_short"]` as the messages of `endpoints.{index}.interval` (it used to be accepted, and `0` made ODT fetch the provider in a tight loop). Endpoints stored with an interval below 5 were raised to 5 by a migration, so the published `interval` in the catalogue is never below 5 either. See [Realtime validation rules](feeds.md).

---

## 2026-11-12: realtime verification time budget

Not breaking. All downloads of one realtime verification now share a 4-minute budget, so `realtime.endpoint_timeout` is also given to endpoints the verification did not reach in time. A verification that runs out of time for another reason no longer ends in a rejection or hangs: the submission stays at stage 2 (`current_stage: 2`) and is verified again automatically within about 30 minutes. See [Realtime rejection reasons](feeds.md#realtime-rejection-reasons).

---

## 2026-11-12: truncated and trickling downloads

Not breaking. A feed body cut off mid-transfer (shorter than its `Content-Length`, or a broken chunked encoding) was recorded as fetch error `invalid_content`; it is now `connection_error`, and in realtime validation `realtime.endpoint_unreachable` instead of `realtime.endpoint_fetch_failed`. A server that sends bytes slowly enough to stay inside the per-read timeout is now cut off at the total time budget as `timeout`. See [Fetch errors](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds) and [Realtime rejection reasons](feeds.md#realtime-rejection-reasons).

---

## 2026-11-12: switching a proxied static feed to another source

Not breaking. A `PATCH` that switches a proxied static submission (`hide_original=true`) to an uploaded file used to answer `400` because of the server's cached copy, also for an Admin; it now succeeds. Any source change of a proxied feed (to a file, to another URL, `hide_original` to `false`) sets `cached_file` and `cached_at` to `null` and deletes the copy, so validation and `/feed/{id}/` no longer use the old URL's file. See [Feeds API](feeds.md).

---

## 2026-11-12: GTFS validation that could not run no longer rejects

Not breaking: no field is added or removed.

- When GTFS validation cannot run (Docker unreachable, the validator container fails or runs out of memory, no usable `report.json`, the time limit, the feed file missing on ODT's storage), the submission is **no longer rejected** to stage 1 with English exception text as `rejection_cause`. It keeps its stage, `static_entry.validation_status` is `error` and `static_entry.validation_message` is a code: `gtfs_validation.file_missing`, `gtfs_validation.validator_unavailable`, `gtfs_validation.validator_failed` or `gtfs_validation.timeout`. The ODT team re-runs it (`manage.py retry_gtfs_validation`); a proxied feed is also re-validated at its next fetch. See [Static feeds](feeds.md#static-feeds-feedsubmission).
- `validation_message` is empty while `validation_status` is `pending` (it was `"Validation in progress"`).
- Errors in the feed itself are unchanged: rejection to stage 1, `validation_status` `invalid`, the validator codes in `rejection_cause` and `validation_message`. A failed download of the provider's non-proxied URL also still rejects, now with `validation_status` `invalid` instead of `error`.

**Frontend:** translate the four codes when showing `validation_message` for `validation_status: "error"`; never show `validation_message` as text in that case.

---

## 2026-11-12: proxy-feeds pages without duplicates

Not breaking. `GET /api/data_manager/proxy-feeds/` repeated some feeds and left out others across pages once there were more than `page_size` of them: the static and realtime rows were cut to a page in the database and merged in Python, and the two order Polish letters and upper/lower case differently. The whole list is now ordered and paged in the database, so every feed is on exactly one page. The visible order follows the database collation: `Łódzkie` and `śląskie` sit next to `L` and `S` instead of after `z`, and case no longer puts `Mazowieckie` before `lubelskie`. Response shape and envelope are unchanged. See [Fetch errors and pauses of proxied feeds](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds).

---

## 2026-11-12: admin login shares the login limit

Not breaking. `POST /admin/login/` (the Django admin login form) now counts against the `login` limit (`THROTTLE_LOGIN`, 10/min per client IP by default) and answers `429` with `Retry-After` when it is used up. The budget is shared with `POST /api/auth/token/` and `POST /api/auth/token/refresh/`, so attempts on either path count together. Opening the form (`GET`) is not limited.

---

## 2026-11-12: logout and session revocation

Not breaking for the ODT frontend, which ignores the body of `me/change-password/`; a client that reads its `detail` must stop. The frontend should adopt the first two points.

- **`POST /api/auth/logout/`** (new, public) takes `{"refresh": "..."}`, blacklists that refresh token and returns `200 {}`. An invalid, expired or already blacklisted token returns `401` (`token_not_valid`). Access tokens stay valid until they expire. See [Logout](README.md#post-apiauthlogout).
- **`POST /api/users/me/change-password/`** blacklists all refresh tokens of the user and now returns a fresh pair `{"access", "refresh"}` for the calling session instead of `{"detail": "Password updated."}`. A client that does not store the new pair is logged out at its next refresh.
- **`POST /api/users/{id}/reset-password/`** blacklists all refresh tokens of that user. The response is unchanged.
- `me/change-password/` and `me/change-email/` are rate-limited (`account_change`, 5/min per user by default, `THROTTLE_ACCOUNT_CHANGE`) and answer `429` beyond it.

See [Ending all sessions of a user](README.md#ending-all-sessions-of-a-user).

---

## 2026-11-12: realtime submissions link only to the caller's own static feed

Not breaking for the website, which already offers only `eligible-static-submissions`. `POST /api/data_manager/realtime-submissions/` and `PUT`/`PATCH .../{id}/` by a `DataProvider` with a `static_submission` the caller did not submit now answer `400` on `static_submission` (code `realtime.static_submission_not_owned`; until the ADR 0006 exception handler lands, the body carries an English message, so match on status and field); `Admin` may still link any feed. In `GET /api/data_manager/feed-submissions/{id}/` a `DataProvider` sees in `realtime_submissions` only the feed owner's realtime submissions, and `download/realtime/{endpoint_pk}/` returns `404` for other users' endpoints. Helper/Admin responses are unchanged. See [Realtime validation rules](feeds.md#realtime-feeds-realtimesubmission).

---

## 2026-11-12: protected accounts in user management

Not breaking for regular accounts.

- User objects (`/api/users/`, `/api/users/{id}/`, `/api/users/me/`) gain read-only **`is_staff`** and **`is_superuser`**.
- An account with either flag is a protected account: `PUT`, `PATCH`, `DELETE` `/api/users/{id}/` and `POST /api/users/{id}/reset-password/` on it need a caller who is a superuser as well as in the `Admin` group. Anyone else gets `403` with `{"code": "users.protected_account", "details": {}}` and nothing changes. The frontend can hide those actions when the target has either flag and the logged-in user (`/api/users/me/`) has `is_superuser: false`.

See [Users](README.md#users-djangocontribauthmodelsuser).

---

## 2026-11-12: planner skips provider URLs with `${`

Not breaking. A realtime or GBFS endpoint polled from the provider's URL whose URL contains `${` is left out of the planner's OTP configuration, because OTP would read it as a placeholder for an environment variable. It is reported with the new error code **`planner.rt_url_unsupported`** in `feed_results[].realtime_skipped` of a build and in a rental source's `active_result`. Proxied endpoints are unaffected. See [Graph builds](planner-admin.md#graph-builds).

---

## 2026-11-12: only Admin deletes transport organizations

**Breaking** for `Helper` accounts. `DELETE /api/cases/transport-organizations/{id}/` now needs the `Admin` role; a `Helper` gets `403`. Creating and updating organizations stays open to `Helper` and `Admin`. Deleting an organization also deletes all its feed submissions, and deleting a submission was already `Admin` only. Clients should offer the delete action to `Admin` only. See [TransportOrganization](cases.md#transportorganization).

---

## 2026-10-17: entity types of a realtime endpoint

Not breaking. Endpoints in the public catalogue (`GET /api/data_manager/feeds/{organization_id}/`, `realtime_feed.endpoints[]`) gain **`entity_types`**: the GTFS-RT entity types ODT has seen in the endpoint's bodies (`trip_update`, `vehicle_position`, `service_alert`), or `[]`. A GTFS-RT feed may mix them, and in October 2026 seven published submissions had vehicle positions (one also alerts) inside an endpoint submitted as `trip_update`; the catalogue can now show that. The journey planner polls these types too, from the next build. See [Public feed catalogue](feeds.md#public-feed-catalogue).

---

## 2026-10-17: planner feed with another feed's realtime

Not breaking. Planner feeds in `/api/planner/admin/feeds/` gain **`realtime_from`**: the id of a `FeedSubmission` of the same organization whose published GTFS-RT OTP polls instead of the feed's own, or `null` (default, unchanged behaviour). It lets a copy of a provider's feed with the same trip IDs, such as ODT's version with shapes added, use the provider's realtime. A feed of another organization is refused with `400` and field code `planner.feed_other_organization` on `realtime_from`. Takes effect at the next build. See [Planner feeds](planner-admin.md#planner-feeds).

---

## 2026-10-17: times at the stops of a planned leg

Not breaking. With `stops=true`, every stop in `legs[].stops` of `GET /api/planner/v1/plan/` now has `arrival` and `departure` (`scheduled`, `estimated`, `delay_s`, as in `trip/`), so a trip's details can show when the vehicle calls at the stops along the way, not only at boarding and alighting. `scheduled` is `null` at the stops of flexible (GTFS-Flex) trips. Stops get estimates only on legs that have one (`realtime`), so never with `realtime=false` (fixed on the day of release: they had live estimates there). See [plan/](planner.md#get-apiplannerv1plan).

---

## 2026-10-16: no rate limit on blog, catalogue and cases

Not breaking. The global `anon` (60/min per client IP) and `user` (240/min) limits are gone, so endpoints without their own limit no longer answer `429`. Login (`login`), feed downloads (`feed_download`) and the planner API keep their limits. `POST /api/blog/reactions/{post_id}/` gets its own limit (`reaction`, 30/min per client IP), because the daily limit of 10 counts only new reactions, not changes to an existing one. See [ADR 0012](../architecture/decisions/0012-rate-limit-only-scoped-views-and-the-planner.md).

---

## 2026-10-16: vehicle coverage despite engine errors on some trips

Not breaking. `GET /api/planner/v1/vehicles/coverage/` answered `503 planner.unavailable` for a whole request when OpenTripPlanner failed on the start or end of a few trips of one operator: on 2026-10-16 every request that included Warsaw (`warszawa:0`). The trips of the line variants with such a trip are now left out of `trips_under_way` and `trips_with_position`, and the other trips and operators are counted. See [vehicles/coverage/](planner.md#get-apiplannerv1vehiclescoverage).

---

## 2026-10-15: planner stop search without lines, lines of a place

**Breaking** for clients of `/api/planner/v1/stops/search/` and `stops/nearby/`.

- Places no longer carry `routes`. Most of the answer was lines (66 of 74 KB for `q=dworzec`), and feeds that publish every line variant as a separate route listed one line many times. Ask `routes/?stops=` for the lines of the place the user picks.
- `routes/` takes `stops` (stop IDs, repeat, up to 50): only lines calling at any of them, e.g. every pole of a place. They come in the order of `routes/` (by number), not by mode first as `routes` in places did.
- `modes`, `agencies` and `organizations` now apply per pole in places: a place is returned when one of its poles matches, and its `stops` and `modes` list only the poles that match (before, a place with one matching pole came with every pole, including other operators'). A place whose poles each match only part of the filters is no longer returned.

See [Stops, lines and operators](planner.md#stops-lines-and-operators).

---

## 2026-10-13: anonymous rate limits count per client IP

Not breaking. The `anon`, `login` and `feed_download` throttles (and `user` for anonymous requests) used to identify an anonymous client by the raw `X-Forwarded-For` header, so a client could send a new value with every request and never get `429`. They now use the same client IP as the blog reaction limit: the rightmost `X-Forwarded-For` address that is not one of ODT's proxies. Clients behind one shared proxy or NAT now share its limit. Rates are unchanged.

---

## 2026-10-13: journey planner (administration, public API access, trip planning, search, departures and vehicles)

Not breaking: new routes only. Admin routes require the `Admin` group.

- `/api/planner/admin/` adds what the team needs to run the journey planner: choosing published GTFS feeds for the OpenTripPlanner graph, building it on a schedule or on demand, per-feed build outcomes (`fresh`, `stale`, `excluded` with error codes), rollback to the previous graph and a status endpoint. See [Journey planner: admin API](planner-admin.md).
- A public journey planner API starts under `/api/planner/v1/` (switched off until an admin enables it): `status/`, an OpenAPI document (`schema/`) and ReDoc (`docs/`). It is open to any origin (CORS), works without a key, limits requests per minute and returns errors as `{code, details}`. See [Journey planner API](planner.md).
- `GET /api/planner/v1/plan/` plans trips between coordinates or stop IDs, with modes, wheelchair access, walking speed, transfers, via stops, operator and line filters, realtime on or off and cursor paging. Times are UTC instants; a `time` parameter without a UTC offset is rejected. Parameter errors carry codes such as `planner.invalid_location` and `planner.stop_not_found`; routing errors (`planner.no_transit_connection`, …) come in a `200` answer. New error code `planner.busy` (`503`). Operators can be filtered by ODT organization (`organizations`, `exclude_organizations`), and leg paths come only with `geometry=true`. See [Journey planner API](planner.md#get-apiplannerv1plan).
- Stop, line and operator search under `/api/planner/v1/`: `stops/search/`, `stops/nearby/`, `stops/` (map area), `stop/`, `routes/` and `agencies/`. Search folds Polish letters and returns places (poles with the same name close together). These keep answering while timetables are rebuilt. See [Stops, lines and operators](planner.md#stops-lines-and-operators).
- Departures and timetables from the routing engine: `stop/departures/` (realtime predictions and cancellations), `stop/timetable/` (one day by line), `route/` (variants, paths on request) and `trip/` (stops with realtime, block ID). New codes `planner.route_not_found`, `planner.trip_not_found`, `planner.trip_not_on_date` and `planner.date_outside_service_period` (`404`, with the first and last covered date).
- `trip/block/` (a vehicle's whole day, the trip under way marked; new code `planner.block_not_available`) and `vehicles/` (live positions matched to trips, filtered by area, line, mode, operator or organization). See [Journey planner API](planner.md#get-apiplannerv1vehicles).
- Bike and scooter sharing (GBFS): `plan/?rental=bicycle|scooter` may use shared vehicles (legs with `rented`, places with `rental_station_id` / `rental_vehicle_id`); `rentals/` shows stations and free vehicles in an area. Admin routes `rental-sources/` and `rental-candidates/`; builds carry `rental_results`; `status/` lists `rental_networks` with their licences.
- Stops carry `heading` (the direction vehicles leave the pole, from the lines' paths) for direction arrows on maps; `vehicles/coverage/` gives, for chosen operators, how many trips under way report a vehicle position (`null`, not 0, for operators whose positions the planner does not receive). Build results list the realtime updaters polled per feed (`realtime_polled`).
- `alerts/`: operators' GTFS-RT service alerts with what they affect and whether they are in force, filtered by line, stop, operator or organization. See [Departures, timetables, lines and trips](planner.md#departures-timetables-lines-and-trips).
- Admin routes for its access: `settings/` (kill switch, limits), `api-keys/`, `ip-bans/`, `requests/` (30-day log without query strings), `stats/daily/` and `stats/top-clients/`. See [planner-admin](planner-admin.md).
- Admin dashboard statistics: `stats/summary/`, `stats/timeseries/` (minute, hour or day; by status class or endpoint), `stats/endpoints/` and `stats/user-agents/`. `stats/top-clients/` takes `since`/`until` and `by=ip|api_key` instead of `days`, and adds shares, status classes, first and last seen, the usual user agent and ban status. All can be narrowed to one endpoint, key or IP.
- `/api/planner/admin/osm/` replaces the OSM extract from a URL (downloaded and checked by the planner worker); default lookup limits are 150 a minute without a key and 500 with one.
- Builds in `/api/planner/admin/builds/` carry `indexed_at`: when search was refreshed from that graph.
- `GET /feed/rt/{realtime_submission_id}/{endpoint_type}/` is not throttled for requests carrying the planner's internal token (`X-ODT-Internal-Token`), which OTP sends when it polls proxied realtime files over the internal network. Nothing changes for other clients.

---

## 2026-10-12: catalogue refresh times, fetch error links and contract corrections

Not breaking for API clients: fields are only added, the schema access change (3) affects only operator accounts, and the corrected docs (4) describe behaviour that was already there.

### 1. Refresh times in the public catalogue

`GET /api/data_manager/feeds/{organization_id}/`: each `feeds[].static_feed` gains **`download_time_1`** and **`download_time_2`**, the daily refresh times of a proxied feed in **UTC** (`"03:00:00"`). Both are `null` when the feed is not proxied (ODT does not fetch it on a schedule). See [Public feed catalogue](feeds.md#public-feed-catalogue).

### 2. Fetch errors link to their submission

Every record of `GET /api/data_manager/fetch-errors/`, `/feed-submissions/{id}/fetch-errors/` and `/realtime-submissions/{id}/fetch-errors/` gains **`submission_id`**: the `FeedSubmission` id when `source` is `static`, the `RealtimeSubmission` id when it is `realtime` (the `#<id>` in `feed_name`). Use it to link an error to its feed. See [Fetch errors](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds).

### 3. API schema for the `Admin` role only

Outside `DEBUG`, `/api/schema/`, `/api/schema/swagger-ui/` and `/api/schema/redoc/` now require the `Admin` group, like user management. They used to require `is_staff`, which grants nothing anywhere else in the API ([ADR 0003](../architecture/decisions/0003-jwt-and-group-based-roles.md)). A staff account or a superuser without the `Admin` group now gets `403`; add the account to the group to keep access. Clients of the public API are not affected.

### 4. Corrected documentation (behaviour unchanged)

- `GET /api/data_manager/feeds/` lists **all** transport organizations, also those without a published feed (empty `static_types` and `dynamic_types`), not only those with published feeds.
- Helper/Admin `PATCH {"stage": N}` may set any stage 1–4 from any state, skipping stages or moving back; this reviewer override is recorded in `history` as `stage_advanced` (or `completed` for stage 4). See [Static feeds](feeds.md#static-feeds-feedsubmission).
- A Helper's `PUT`/`PATCH` with any field other than `stage` and `rejection_cause` returns `403`, not a `400` validation error.
- `POST /api/auth/token/refresh/` returns `{access, refresh}`, not only `access`: refresh tokens rotate and the old one is blacklisted, so a client that keeps the old `refresh` gets `401` on its next refresh. See [JWT authentication](README.md#post-apiauthtokenrefresh).
- Sending `null` for a text field returns DRF's field error, e.g. `{"name": ["This field may not be null."]}`. The 2026-10 entry below and the API overview promised an error code `null` in the body; there is none until the exception handler of [ADR 0006](../architecture/decisions/0006-api-error-codes.md) exists.
- Realtime `PATCH` in restricted mode with an `endpoint_type` the submission does not have returns `{"endpoints": ["Unknown endpoint type '...'."]}` (or a field error, see the corrected table in the 2026-07 entry below), not the body documented there before.
- Two existing routes are now documented: `PUT /api/users/{id}/` (same fields as `PATCH`) and `GET /internal-media/blog/{path}` (public blog images, the URL in a post's `image`).

---

## 2026-10: `resume-fetch` returns `400` for non-proxied feeds; error code for both

`POST /api/data_manager/static-feed-entries/{id}/resume-fetch/` and `POST /api/data_manager/realtime-endpoints/{id}/resume-fetch/` on a feed without proxying (`is_proxy_managed=false`) used to return `200` and reset fetch fields that such a feed does not use. They now return `400`, as the [feed workflow](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds) already documented.

The `400` of `pause-fetch` and `resume-fetch` for a non-proxied feed now carries an error code ([ADR 0006](../architecture/decisions/0006-api-error-codes.md)) instead of English prose: `{"code": "feeds.not_proxy_managed", "details": {}}` (it used to be `{"detail": "Pause/resume applies only to proxy-managed ..."}`). Not breaking for clients that offer pause/resume only for proxied feeds (the `proxy-feeds` list), as the ODT frontend does.

---

## 2026-10: a realtime edit keeps stored endpoint secrets

Not breaking: the request and response shapes are unchanged.

A full edit of a realtime submission (`PUT/PATCH /api/data_manager/realtime-submissions/{id}/` with `endpoints`, by the owner at stage 1 or after a rejection, or by an Admin) replaces the endpoint rows. `auth_value` is write-only, so the frontend sends it only when the user retypes it, and the stored secret used to be lost while `auth_type` stayed set: the server then fetched the source without credentials. Now, when an endpoint is sent with `auth_type` but without `auth_value` (or with `""`), the previous endpoint of the same `endpoint_type` had the same `auth_type`, and the URL keeps its origin (scheme and host), the stored secret is kept. A URL moved to another origin gets no stored secret, so it is never sent to a host it was not given for. The owner's restricted edit (from stage 2) follows the same rule: an endpoint sent with `"auth_value": ""` used to return `400` as a change of credentials and now keeps the stored secret; a different `auth_value` is still refused. See [Feeds API](feeds.md#realtime-feeds-realtimesubmission).

---

## 2026-10: a failed re-validation no longer unpublishes a realtime feed

Not breaking: the response shape is unchanged.

When an Admin replaced the endpoints of a published realtime submission (stage 4) and the background re-validation failed, the submission was rejected back to stage 1 and disappeared from the public catalogue and `/feed/rt/{id}/`. It now stays at stage 4, as static feeds already did: no `rejected` history entry is written, `rejection_reasons` stays `null`, and the failure codes go to the server log only. Submissions at stage 2 or 3 are still rejected with coded `rejection_reasons`. This replaces the note in section 6 of the 2026-07 entry ("only its failure rejects the submission").

---

## 2026-10: empty text fields are `""`, never `null`

⚠️ **Breaking for writes.** Text fields have one empty value, `""` (Django convention, ruff `DJ001`). Existing `NULL`s are converted to `""` by migrations `blog.0003`, `cases.0005` and `data_manager.0012`.

**Deploy order:** first the `auth_type` fix below, then the frontend that sends `""`, then this backend version. The backend before this one already accepts `""` for all these fields.

**Deploy and rollback:** the previous backend writes `NULL` into these columns, which fails once the migrations have run. Stop the services that write before deploying (`./scripts/deploy.sh stop web celery_worker celery_validator_worker celery_beat`; a short downtime) so no old container writes between the migration and its own restart. To roll back, reverse the migrations **before** starting the old image: `migrate data_manager 0011`, `migrate cases 0004`, `migrate blog 0002`.

Affected fields:

| Resource | Fields |
|----------|--------|
| Blog reactions | `reaction` |
| Case statuses | `description` |
| Data providers (also nested in organizations) | `website`, `contact_email` |
| Transport organizations (also in the public feed catalogue) | `website`, `contact_email`, `phone_number` |
| Static feed submissions | `name`, `note`, `history[].cause` |
| `static_entry` | `license`, `url`, `validation_message`, `auth_type` |
| Realtime submissions | `name`, `note`, `license`, `history[].cause` |
| Realtime `endpoints[]` | `auth_type` |

- **Responses:** these fields return `""` where they used to return `null`. Truthiness checks (`if (x)`, `x || fallback`) keep working; `x ?? fallback` and `x === null` do not. This also applies to the public catalogue (`license`, organization contacts), so external consumers see the change.
- **Requests:** sending `null` for any of these fields returns `400` with DRF's field error, e.g. `{"name": ["This field may not be null."]}` (no error code in the body). Send `""` or omit the field. To remove a blog reaction, send `{"reaction": ""}`.
- **`auth_type`:** "no authorization" is `""` (was `null`), see `docs/api/models.md`.
- **Unchanged:** computed and non-text fields keep `null` for "none": `rejection_cause`, `published_at`, `your_reaction`, `download_time_2`, `file`, `download_url`, `feed_url`, and the nullable fields of `proxy-feeds`.

---

## 2026-10: an empty `auth_type` no longer forces `hide_original`

In `static_entry`, `"auth_type": ""` used to set `hide_original=true` as if a credential had been given, while `null` did not. Both now mean "no authorization" and keep the `hide_original` the client sent. Realtime endpoints already behaved this way. Not breaking: clients may send `""` for "no authorization" from now on.

---

## 2026-10: PUT/PATCH responses show the saved state

Not breaking: the response shape is unchanged.

`PUT/PATCH /api/data_manager/feed-submissions/{id}/` and
`/api/data_manager/realtime-submissions/{id}/` that changed the stage — a
confirmation or rejection (`stage`, `rejection_cause`) or an owner resubmitting a
rejected feed (new source or endpoints) — used to return the submission as it was
**before** the change:
`current_stage`, `current_stage_label`, `is_rejected`, `rejection_cause` and
`published_at` were stale, although the transition was saved. The response now
reflects the new state, the same as a following GET. Clients that re-fetch after the PATCH (as the
ODT frontend does) see no difference.

---

## 2026-10: coded reasons for automatic realtime rejections

Not breaking: fields are only added.

- `GET /api/data_manager/realtime-submissions/{id}/` (and the write responses of the same resource) gain **`rejection_reasons`**: a list of `{code, details}` or `null`. Each `history` entry gains **`reasons`** with the same shape.
- Automatic realtime validation no longer writes Polish text to `rejection_cause`; it stays `null` for these rejections. `rejection_cause` now carries only a reviewer's free text.
- Existing rejections whose text was fully recognised got codes by a data migration and keep their old text in `rejection_cause`; when `rejection_reasons` is not `null`, show it and ignore `rejection_cause`.
- `has_rejection_cause` in `GET /api/data_manager/submissions/` is also `true` when only `rejection_reasons` is set.
- The codes and their `details` are listed in [Realtime rejection reasons](feeds.md#realtime-rejection-reasons). The frontend translates them and shows a generic message for unknown codes.

**Frontend:** until the frontend reads `rejection_reasons`, automatic realtime rejections show no reason text.

**Not changed yet:** static (GTFS) rejection causes are still English text in `rejection_cause`; they move to codes in a separate change ([ADR 0006](../architecture/decisions/0006-api-error-codes.md)).

---

## 2026-07: paginated lists, canonical realtime file routes and other breaking changes

Applies to the backend deployment of July 2026 (hardening + DRF unification).
**Breaking** changes are marked with ⚠️. After applying the fixes, the frontend should
pass a full smoke test: login → lists → details → submission forms →
feed downloads.

---

### 1. ⚠️ Pagination: EVERY list now returns the DRF envelope

All list endpoints return a uniform object instead of a bare array:

```json
{
  "count": 123,
  "next": "https://api.example.org/api/...?page=2",
  "previous": null,
  "results": [ /* items of the current page */ ]
}
```

**Code migration:**

```js
// BEFORE:
const items = await (await fetch(url)).json();
items.map(...)

// AFTER:
const data = await (await fetch(url)).json();
data.results.map(...)          // page items
// data.count  – total count
// data.next   – URL of the next page or null
```

Helper for fetching all pages (use sparingly — lists can be long):

```js
async function fetchAllPages(url, init) {
  const results = [];
  let next = url;
  while (next) {
    const data = await (await fetch(next, init)).json();
    results.push(...data.results);
    next = data.next;
  }
  return results;
}
```

**Parameters:** `?page=N` (page number, starting from 1), `?page_size=M`.

#### Endpoints affected by the change (previously returned a bare array)

| Endpoint | Default page_size | max |
|---|---|---|
| `GET /api/blog/posts/` | 4 | 50 |
| `GET /api/data_manager/feeds/` (public organization catalog) | 50 | 200 |
| `GET /api/data_manager/feed-submissions/` | 50 | 200 |
| `GET /api/data_manager/realtime-submissions/` | 50 | 200 |
| `GET /api/data_manager/realtime-submissions/eligible-static-submissions/{org_id}/` (and the `/{data_type}/` variant) | 50 | 200 |
| `GET /api/data_manager/static-feed-entries/` | 50 | 200 |
| `GET /api/data_manager/realtime-endpoints/` | 50 | 200 |
| `GET /api/cases/transport-organizations/` | 50 | 200 |
| `GET /api/cases/case-statuses/` | 50 | 200 |
| `GET /api/cases/data-providers/` | 50 | 200 |
| `GET /api/users/` | 50 | 200 |

#### Unchanged (already paginated before — same shape as today)

- `GET /api/data_manager/fetch-errors/` (25 / max 100)
- `GET /api/data_manager/proxy-feeds/` (25 / max 100)
- `GET /api/data_manager/feed-submissions/{id}/fetch-errors/`
- `GET /api/data_manager/realtime-submissions/{id}/fetch-errors/`

#### Still WITHOUT pagination (intentionally — shape unchanged)

- `GET /api/data_manager/submissions/` → `{ "user": <id>, "static": [...], "realtime": [...] }`
- `GET /api/users/me/`, details (`/{id}/`), `/feed/...` payloads

#### Removed hybrid in the blog ⚠️

`GET /api/blog/posts/` **without** `?page` used to return a bare array of all
posts, and **with** `?page` — the envelope. Now it **always** returns the envelope
(4 posts/page). If the frontend fetched "everything" without `?page`, it must switch
to pagination (or `?page_size=50`).

---

### 2. ⚠️ Realtime file downloads: new canonical route by `endpoint_type`

The old route by file name was non-deterministic (two endpoints could have
the same base file name). New route:

```
GET /feed/rt/{realtime_submission_id}/{endpoint_type}/
```

`endpoint_type` is e.g. `trip_update`, `vehicle_position`, `service_alert`,
`gbfs`, `station_status`… (unique within a submission — the download always
hits the right file). **Mind the trailing slash** — it is part of the route.

#### What the informational endpoints return now

`GET /feed/rt/{id}/`:

```json
{
  "dynamic": {
    "trip_update": "https://api.example.org/feed/rt/12/trip_update/",
    "vehicle_position": "https://api.example.org/feed/rt/12/vehicle_position/"
  }
}
```

Public catalog `GET /api/data_manager/feeds/{org_id}/` — the `feed_url` field
of proxied RT endpoints now has the canonical route format:

```json
{ "endpoint_type": "trip_update", "feed_url": "https://api.example.org/feed/rt/12/trip_update/", ... }
```

**Migration:** if the frontend built file URLs itself or parsed the
file name from `feed_url` — stop; always use `feed_url` / `dynamic[...]`
from the API response. The old route `/feed/rt/{id}/{filename}` still works
(compatibility), but it is legacy and file names may change.

Static downloads are **unchanged**: `GET /feed/{id}/` → `{"static": "...url..."}`,
`GET /feed/{id}/{filename}`.

---

### 3. ⚠️ Deleting submissions: Admin only

`DELETE /api/data_manager/feed-submissions/{id}/` and
`DELETE /api/data_manager/realtime-submissions/{id}/`:

- role `Admin` → `204 No Content`,
- anyone else (including the submission owner) → `403` with body
  `{"detail": "Only Admin can delete submissions."}`.

Previously the owner could delete a realtime submission at stage 1 — not anymore.
**Migration:** hide the "Delete" button on submissions for users without the
`Admin` role (the `roles` claim in the JWT).

---

### 4. POST/PATCH responses have the same shape as GET

Until now an operator (`Helper`/`Admin`) received a different
(poorer) object in the response to POST/PATCH than from GET. Now the shape is consistent per role — for
operators, write responses additionally include `submitted_by_username`,
just like GET. Nothing needs to change, unless the frontend assumed the field was **absent**
after a write.

---

### 5. New/changed error responses (previously partly 500)

| Situation | Status | Body |
|---|---|---|
| Submission `PATCH` with a non-numeric `stage` | **400** (was 500) | `{"stage": ["Stage must be an integer."]}` |
| `PATCH` with `stage` outside 1–4 | **400** (was 500) | `{"stage": ["Stage must be between 1 and 4."]}` |
| Realtime `PATCH` with an unknown `endpoint_type` (restricted mode — only `interval` can change) | **400** (was 500) | `{"endpoints": ["Unknown endpoint type '...'."]}`; a value that is not an endpoint type at all: `{"endpoints": [{"endpoint_type": ["\"...\" is not a valid choice."]}]}`; a different number of endpoints: `{"endpoints": ["Cannot add or remove endpoints after validation."]}` |
| Upload of a GTFS file that is not a ZIP | **400** | `{"static_entry": {"file": ["Uploaded GTFS file is not a ZIP archive."]}}` |
| Deactivating (`is_active:false`) the last active Admin | **400** | `["Cannot remove the Admin role from, or deactivate, the last admin user."]` |

Additionally: an invalid `stage` is now validated **before** the content is saved —
a 400 response means that **nothing** was saved (previously the content could
have been saved despite the error).

---

### 6. Behavior changes worth knowing about (no frontend code changes)

- **Admin edits of RT endpoints no longer revert publication** — a published
  feed (stage 4) stays at stage 4 after its endpoints are replaced; validation
  runs in the background and only its failure rejects the submission (with an entry in the history).
- **Resubmitting a rejected submission always restarts verification**
  — even when the URL/file are identical. The frontend can show
  "Step 2: Data verification" right after the PATCH.
- **`your_reaction` in the blog** is computed from the same IP as the reaction write
  (the `X-Forwarded-For` header is honored only from a trusted
  reverse proxy). Deployment note: in production behind nginx/Caddy the backend must
  have `TRUSTED_PROXY_CIDRS` set, otherwise all users behind the proxy
  share one IP (the reaction limit and `your_reaction` will be shared).
- **`Editor` role**: creates/edits/deletes any blog posts. `Blogger`
  edits only their own posts. If the UI shows edit buttons — for
  `Blogger` only on their own posts (`author` vs the logged-in user's id),
  for `Editor`/`Admin` on all of them.

---

### 7. ⚠️ Blog reactions: `your_reaction` must NOT come from SSR

**New endpoint:**

```
GET /api/blog/reactions/mine/?post_ids=1,2,3
→ { "1": "like", "3": "angry" }        // missing key = no reaction
```

Returns **only the caller's reactions** (identified by their IP address);
other people's reactions or addresses are never disclosed.

**Why this is necessary:** the `your_reaction` field in the `/api/blog/posts/` response
describes whoever made the request. When the page is rendered by the server (Next.js SSR),
the request comes from the **frontend server** — so `your_reaction` described that server,
identically for all visitors. Symptom: everyone saw someone else's reaction
as their own, and the interface allowed them to overwrite it.

**Migration:**

- ❌ do not use `post.your_reaction` from data fetched in a server component,
- ✅ in a client component, fetch your own state from `/api/blog/reactions/mine/`
  (in the frontend repository done in `ReactionPanel`; helper: `fetchMyReactions()` in
  `lib/api/blog.ts`),
- `reactions_summary` (global counters) remains correct from SSR — it is public
  data, independent of identity.

The `your_reaction` field stays in the API (it is correct for requests sent directly
from the browser), but it must be ignored with SSR.

**Deployment requirement:** a backend behind a reverse proxy must have
`TRUSTED_PROXY_CIDRS` set, and the proxy must forward `X-Forwarded-For` — otherwise all
visitors share one identity (one reaction per post, a shared daily
limit). `manage.py check` reports the missing setting as `blog.W001`.

---

### 8. Frontend migration checklist

- [ ] All list fetches switched to `data.results` (+ handling of `next`/`count`).
- [ ] Blog: posts fetched with pagination (there is no longer a bare array without `?page`).
- [ ] RT file URLs taken directly from `feed_url` / `dynamic[...]` (not built from file names).
- [ ] "Delete submission" button visible only for the `Admin` role.
- [ ] Handling of the new 400 messages (stage, endpoint_type, non-ZIP GTFS, last admin).
- [ ] Blog edit buttons by role: `Blogger` = own, `Editor`/`Admin` = all.
- [ ] Reactions: the "my reaction" state read from `/api/blog/reactions/mine/` in
      the browser (not from `your_reaction` from SSR).
- [ ] (Ops) `TRUSTED_PROXY_CIDRS` set in production behind a reverse proxy +
      the proxy forwards `X-Forwarded-For` (`manage.py check` without `blog.W001`).
