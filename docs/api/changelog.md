# API changelog

Breaking and notable changes to the API contract, newest first. Every change that a client must react to gets an entry here in the same pull request.

## 2026-10-12: catalogue refresh times, fetch error links and contract corrections

Not breaking: fields are only added, and the corrected docs describe behaviour that was already there.

### 1. Refresh times in the public catalogue

`GET /api/data_manager/feeds/{organization_id}/`: each `feeds[].static_feed` gains **`download_time_1`** and **`download_time_2`**, the daily refresh times of a proxied feed in **UTC** (`"03:00:00"`). Both are `null` when the feed is not proxied (ODT does not fetch it on a schedule). See [Public feed catalogue](feeds.md#public-feed-catalogue).

### 2. Fetch errors link to their submission

Every record of `GET /api/data_manager/fetch-errors/`, `/feed-submissions/{id}/fetch-errors/` and `/realtime-submissions/{id}/fetch-errors/` gains **`submission_id`**: the `FeedSubmission` id when `source` is `static`, the `RealtimeSubmission` id when it is `realtime` (the `#<id>` in `feed_name`). Use it to link an error to its feed. See [Fetch errors](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds).

### 3. Corrected documentation (behaviour unchanged)

- `POST /api/auth/token/refresh/` returns `{access, refresh}`, not only `access`: refresh tokens rotate and the old one is blacklisted, so a client that keeps the old `refresh` gets `401` on its next refresh. See [JWT authentication](README.md#post-apiauthtokenrefresh).
- Sending `null` for a text field returns DRF's field error, e.g. `{"name": ["This field may not be null."]}`. The 2026-10 entry below and the API overview promised an error code `null` in the body; there is none until the exception handler of [ADR 0006](../architecture/decisions/0006-api-error-codes.md) exists.

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
| Realtime `PATCH` with an unknown `endpoint_type` (restricted mode — only `interval` can change) | **400** (was 500) | `{"endpoints": ["Unknown endpoint_type '...' ..."]}` |
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
