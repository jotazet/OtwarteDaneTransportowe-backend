# API changelog

Breaking and notable changes to the API contract, newest first. Every change that a client must react to gets an entry here in the same pull request.

## 2026-10: an empty `auth_type` no longer forces `hide_original`

In `static_entry`, `"auth_type": ""` used to set `hide_original=true` as if a credential had been given, while `null` did not. Both now mean "no authorization" and keep the `hide_original` the client sent. Realtime endpoints already behaved this way. Not breaking: clients may send `""` for "no authorization" from now on.

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
