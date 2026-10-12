# ODT API

This documentation describes the REST API of the OtwarteDaneTransportowe (Open Transport Data) backend: its endpoints, the most important response fields and the required permissions. It is written as context for building client (frontend) applications.

## Contents

- [Quick start](#quick-start)
- [Roles and permissions](#roles-and-permissions)
- [JWT authentication](#jwt-authentication)
- [Endpoint map](#endpoint-map)
- [Notes for client developers](#notes-for-client-developers)
- [Blog API](blog.md)
- [Organizations and cases API](cases.md)
- [Feeds API and public file downloads](feeds.md)
- [Journey planner: admin API](planner-admin.md)
- [Models and enums](models.md)
- [Changelog for client developers](changelog.md)
- [Feed workflow](../domain/feed-workflow.md)
- [Architecture overview (backend structure)](../architecture/overview.md)

---

## Quick start

The backend is Django 6 + Django REST Framework. The API is divided into four main areas:

- `/api/blog/` - blog posts and reactions.
- `/api/cases/` - data providers, transport organizations and case statuses.
- `/api/data_manager/` - submitting static and realtime feeds, and the public catalog of published feeds.
- `/api/users/` - account management (`Admin` group) plus profile and password/email changes for the logged-in user.

API authentication uses JWT:

```http
POST /api/auth/token/
POST /api/auth/token/refresh/
```

For requests that require login, the client sends:

```http
Authorization: Bearer <access_token>
```

Blog reads, reads of cases (data providers, case statuses, transport organizations), the published feed catalogue (`/api/data_manager/feeds/`), blog reactions and downloads of published feeds do not require a token. All other `/api/data_manager/` lists (submissions, static feed entries, realtime endpoints, `proxy-feeds`, fetch errors) and `/api/users/` require login.

---

## Roles and permissions

Roles are plain Django groups (`auth.Group`). A migration creates the groups:

- `Admin`
- `Blogger`
- `Editor`
- `DataProvider`
- `Helper`

`Admin` permissions also apply to Django superusers (`is_superuser=True`); `is_staff` alone grants nothing in the API. User management (`/api/users/`), editing a feed source at stages 2–4 and, outside `DEBUG`, the API schema (`/api/schema/...`) require membership in the `Admin` group itself — a superuser without the group gets `403` there.

### Role meanings

| Role | Capabilities |
|------|------------|
| `Admin` | Full administrative access. Inherits all permissions of the domain roles. |
| `Blogger` | Creating blog posts; editing and deleting **only their own** posts. |
| `Editor` | Editing and deleting **any** blog posts (including other users' posts). |
| `DataProvider` | Adding static and realtime feeds and managing their own submissions before review. |
| `Helper` | Feed queue: **only** confirming/rejecting (`stage`, `rejection_cause`); cases (data providers, organizations, statuses). **No** editing of the URL/file or the submission content. |
| Anonymous | Public reads, blog reactions, downloading published feeds. |

### Access matrix

| Area | Public read | Write |
|--------|------------------|-------|
| Blog posts | Yes | create: `Blogger`/`Editor`/`Admin`; edit other users' posts: `Editor`/`Admin` |
| Blog reactions | Yes, `POST` without login | Anyone, limited per IP |
| Data providers | Yes | `Helper` or `Admin` |
| Case statuses | Yes | `Helper` or `Admin` |
| Transport organizations | Yes | `Helper` or `Admin` |
| Static feed submissions | No | `DataProvider` creates; `Helper`/`Admin` confirms |
| Realtime submissions | No | `DataProvider` creates; `Helper`/`Admin` confirms |
| Published feeds catalog | Yes | No writes through the public API |
| Public feed files | Yes, published only | No writes |
| Journey planner administration | No | `Admin` only |

---

## JWT authentication

### `POST /api/auth/token/`

Returns a pair of JWT tokens.

**Permissions:** public.

**Request:**

```json
{
  "username": "user",
  "password": "password"
}
```

**Response 200:**

```json
{
  "refresh": "eyJ...",
  "access": "eyJ..."
}
```

**Access token contents (decoded payload):** in addition to the standard simplejwt fields (`token_type`, `exp`, `iat`, `jti`, `user_id`), the backend adds:

| Field | Type | Description |
|------|-----|------|
| `roles` | array of strings | Names of the user's Django groups (e.g. `Admin`, `DataProvider`), sorted alphabetically |
| `first_name` | string | `User.first_name` (empty if not set) |
| `last_name` | string | `User.last_name` (empty if not set) |

The same three fields (`roles`, `first_name`, `last_name`) are included in the **refresh** token at login and are carried into the new **access** token after a successful `POST /api/auth/token/refresh/` (the client can read them from the new access JWT without a separate profile endpoint).

### `POST /api/auth/token/refresh/`

Refreshes the access token using the refresh token.

**Permissions:** public, requires a valid `refresh`.

**Request:**

```json
{
  "refresh": "eyJ..."
}
```

**Response 200:**

```json
{
  "access": "eyJ...",
  "refresh": "eyJ..."
}
```

Refresh tokens rotate (`ROTATE_REFRESH_TOKENS`, `BLACKLIST_AFTER_ROTATION`): every successful refresh returns a **new** `refresh` and blacklists the one that was sent. The client must store the new `refresh` and use it next time; sending the old one again returns `401` (`{"detail": "Token is blacklisted", "code": "token_not_valid"}`).

### `/api-auth/`

DRF browsable API session login/logout. Useful for development; a production client should use JWT.

---

## Endpoint map

### List contract (pagination)

Every list endpoint returns the standard DRF envelope (the only exception is `GET /api/data_manager/submissions/`, which is intentionally not paginated and returns `{user, static, realtime}`):

```json
{ "count": 123, "next": "...?page=2", "previous": null, "results": [ ... ] }
```

By default 50 items per page (`page_size` up to 200). Exceptions: blog posts 4 per
page (max 50); fetch errors and `proxy-feeds` 25 per page (max 100).

### System

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/admin/` | Django staff/admin | Django admin panel |
| `POST` | `/api/auth/token/` | public | Obtain JWT `access` and `refresh` |
| `POST` | `/api/auth/token/refresh/` | public | Refresh JWT `access`; returns a new `refresh` as well |
| `GET` | `/api/schema/` | public when `DEBUG`, otherwise `Admin` (group) only | OpenAPI schema |
| `GET` | `/api/schema/swagger-ui/` | public when `DEBUG`, otherwise `Admin` (group) only | Swagger UI |
| `GET` | `/api/schema/redoc/` | public when `DEBUG`, otherwise `Admin` (group) only | ReDoc |
| `GET` | `/api-auth/` | public/dev | Browsable API login/logout |

### Users (`django.contrib.auth.models.User`)

Requires the Django group **`Admin`** (`has_admin_role`) — `is_staff` / `is_superuser` without this group get `403`.

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/api/users/` | `Admin` (group) | List of all users |
| `POST` | `/api/users/` | `Admin` (group) | Create an account; the server generates the password — returned in `generated_password` |
| `GET` | `/api/users/{id}/` | `Admin` (group) | User details |
| `PUT` | `/api/users/{id}/` | `Admin` (group) | Same fields and rules as `PATCH`; none is required, and with a JSON body omitted fields keep their values |
| `PATCH` | `/api/users/{id}/` | `Admin` (group) | Edit `email`, `first_name`, `last_name`, `is_active`, `roles` |
| `DELETE` | `/api/users/{id}/` | `Admin` (group) | Permanent deletion (you cannot delete yourself or the last `Admin`) |
| `POST` | `/api/users/{id}/reset-password/` | `Admin` (group) | New random password in `generated_password` |
| `GET` | `/api/users/me/` | logged in | Current user's profile |
| `POST` | `/api/users/me/change-email/` | logged in | `current_password`, `new_email` |
| `POST` | `/api/users/me/change-password/` | logged in | `current_password`, `new_password` |

Allowed `roles` values: `Admin`, `Blogger`, `Editor`, `DataProvider`, `Helper` (`PUT`/`PATCH` fully replace the list of groups).

For `POST /api/users/` the body does **not** contain a password — example `201` response:

```json
{
  "id": 12,
  "username": "jan.kowalski",
  "email": "jan@example.com",
  "first_name": "Jan",
  "last_name": "Kowalski",
  "is_active": true,
  "roles": ["DataProvider"],
  "generated_password": "xK9mP2vL8nQwR4sT1uY0zA"
}
```

**Note:** `DELETE` of a user also deletes their blog posts (`author` CASCADE). Feed submissions remain, with `submitted_by=null`.

### Blog

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/api/blog/posts/` | public | List of posts |
| `POST` | `/api/blog/posts/` | `Blogger`/`Editor`/`Admin` | Create a post |
| `GET` | `/api/blog/posts/{id}/` | public | Post details |
| `PUT` | `/api/blog/posts/{id}/` | own: `Blogger`; any: `Editor`/`Admin` | Full post edit |
| `PATCH` | `/api/blog/posts/{id}/` | own: `Blogger`; any: `Editor`/`Admin` | Partial post edit |
| `DELETE` | `/api/blog/posts/{id}/` | own: `Blogger`; any: `Editor`/`Admin` | Delete a post |
| `POST` | `/api/blog/reactions/{post_id}/` | public | Add/change/remove a reaction per IP |
| `GET` | `/api/blog/reactions/mine/?post_ids=1,2,3` | public | The caller's own reactions, resolved from their IP; read it from the browser, not during SSR |
| `GET` | `/internal-media/blog/{path}` | public | Blog post images; a post's `image` field is the URL of this route |

### Cases

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/api/cases/data-providers/` | public | List of data providers |
| `POST` | `/api/cases/data-providers/` | `Helper`/`Admin` | Add a data provider |
| `GET` | `/api/cases/data-providers/{id}/` | public | Data provider details |
| `PUT/PATCH` | `/api/cases/data-providers/{id}/` | `Helper`/`Admin` | Edit a data provider |
| `DELETE` | `/api/cases/data-providers/{id}/` | `Helper`/`Admin` | Delete a data provider |
| `GET` | `/api/cases/case-statuses/` | public | List of case statuses |
| `POST` | `/api/cases/case-statuses/` | `Helper`/`Admin` | Add a case status |
| `GET` | `/api/cases/case-statuses/{id}/` | public | Status details |
| `PUT/PATCH` | `/api/cases/case-statuses/{id}/` | `Helper`/`Admin` | Edit a status |
| `DELETE` | `/api/cases/case-statuses/{id}/` | `Helper`/`Admin` | Delete a status |
| `GET` | `/api/cases/transport-organizations/` | public | List of transport organizations |
| `POST` | `/api/cases/transport-organizations/` | `Helper`/`Admin` | Add an organization |
| `GET` | `/api/cases/transport-organizations/{id}/` | public | Organization details with status history |
| `PUT/PATCH` | `/api/cases/transport-organizations/{id}/` | `Helper`/`Admin` | Edit an organization |
| `DELETE` | `/api/cases/transport-organizations/{id}/` | `Helper`/`Admin` | Delete an organization |

### Data Manager

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/api/data_manager/feed-submissions/` | `DataProvider`/`Helper`/`Admin` | List of static submissions: a provider sees their own, helper/admin see all |
| `POST` | `/api/data_manager/feed-submissions/` | `DataProvider`/`Admin` | Add a static feed |
| `GET` | `/api/data_manager/feed-submissions/{id}/` | owner or `Helper`/`Admin` | Static submission details |
| `PUT/PATCH` | `/api/data_manager/feed-submissions/{id}/` | owner (edit rules in [Feeds API](feeds.md)) or `Helper`/`Admin` | Edit, or confirm/reject |
| `DELETE` | `/api/data_manager/feed-submissions/{id}/` | `Admin` only (submissions are part of the audit trail) | Delete a submission |
| `GET` | `/api/data_manager/feed-submissions/{id}/fetch-errors/?days=7` | owner or `Helper`/`Admin` | Fetch error history of a static feed for the last N days |
| `GET` | `/api/data_manager/feed-submissions/{id}/download/static/{endpoint_pk}/` | owner or `Helper`/`Admin` | Download the private static file of the given submission |
| `GET` | `/api/data_manager/feed-submissions/{id}/download/realtime/{endpoint_pk}/` | owner or `Helper`/`Admin` | Download the private cache of an RT endpoint linked to the static submission |
| `GET` | `/api/data_manager/static-feed-entries/` | owner or `Helper`/`Admin` | List of static feeds with their fetch status |
| `GET` | `/api/data_manager/static-feed-entries/{id}/` | owner or `Helper`/`Admin` | Details of a single static feed with its fetch status |
| `POST` | `/api/data_manager/static-feed-entries/{id}/pause-fetch/` | `Helper`/`Admin` | Manually pause fetching of a static feed |
| `POST` | `/api/data_manager/static-feed-entries/{id}/resume-fetch/` | `Helper`/`Admin` | Resume fetching of a static feed and cancel the retry block; proxied feeds only (`400` otherwise, as `pause-fetch`) |
| `GET` | `/api/data_manager/proxy-feeds/` | logged in | A single, paginated list of proxied feeds (static + realtime): a provider sees their own, helper/admin see all. Pausing/resuming (`pause-fetch`/`resume-fetch`) is `Helper`/`Admin` only |
| `GET` | `/api/data_manager/fetch-errors/?days=7` | logged in | Global, paginated list of fetch errors: a provider sees their own feeds, helper/admin see all |
| `GET` | `/api/data_manager/submissions/` | `DataProvider`/`Helper`/`Admin` | Combined list of the logged-in user's own static and realtime submissions (not paginated) |
| `GET` | `/api/data_manager/realtime-submissions/` | `DataProvider`/`Helper`/`Admin` | List of realtime submissions: a provider sees their own, helper/admin see all |
| `GET` | `/api/data_manager/realtime-submissions/eligible-static-submissions/{id-organizacji}/` | `DataProvider`/`Helper`/`Admin` | Published static submissions of the given organization to which a realtime feed can be added |
| `GET` | `/api/data_manager/realtime-submissions/eligible-static-submissions/{id-organizacji}/{typ}/` | `DataProvider`/`Helper`/`Admin` | Same, filtered by `gtfs`, `netex` or `other` |
| `POST` | `/api/data_manager/realtime-submissions/` | `DataProvider`/`Admin` | Add a realtime feed |
| `GET` | `/api/data_manager/realtime-submissions/{id}/` | owner or `Helper`/`Admin` | Realtime submission details |
| `PUT/PATCH` | `/api/data_manager/realtime-submissions/{id}/` | owner (edit rules in [Feeds API](feeds.md)) or `Helper`/`Admin` | Edit, or confirm/reject |
| `DELETE` | `/api/data_manager/realtime-submissions/{id}/` | `Admin` only (submissions are part of the audit trail) | Delete a realtime submission |
| `GET` | `/api/data_manager/realtime-submissions/{id}/fetch-errors/?days=7` | owner or `Helper`/`Admin` | Fetch error history of realtime endpoints for the last N days |
| `GET` | `/api/data_manager/realtime-endpoints/` | owner or `Helper`/`Admin` | List of realtime endpoints with their fetch status |
| `GET` | `/api/data_manager/realtime-endpoints/{id}/` | owner or `Helper`/`Admin` | Details of a single realtime endpoint with its fetch status |
| `POST` | `/api/data_manager/realtime-endpoints/{id}/pause-fetch/` | `Helper`/`Admin` | Manually pause fetching of a realtime endpoint |
| `POST` | `/api/data_manager/realtime-endpoints/{id}/resume-fetch/` | `Helper`/`Admin` | Resume fetching of a realtime endpoint and cancel the retry block; proxied endpoints only (`400` otherwise, as `pause-fetch`) |
| `GET` | `/api/data_manager/feeds/` | public | Public catalog of all transport organizations and their published feeds (organizations without one are listed too) |
| `GET` | `/api/data_manager/feeds/{id}/` | public | Details of an organization and its published feeds |

In the paths above, `{id-organizacji}` is the organization ID and `{typ}` is the feed type.

### Journey planner (admin)

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/api/planner/admin/feed-candidates/` | `Admin` | Published GTFS feeds that can be added to the planner |
| `GET/POST` | `/api/planner/admin/feeds/` | `Admin` | Feeds included in the planner graph |
| `GET/PATCH/DELETE` | `/api/planner/admin/feeds/{id}/` | `Admin` | One planner feed (`feed_id` cannot change) |
| `GET/POST` | `/api/planner/admin/builds/` | `Admin` | Graph builds; `POST` builds now (`202`, `409` while one runs) |
| `GET` | `/api/planner/admin/builds/{id}/` | `Admin` | Build with per-feed outcomes and log excerpt |
| `POST` | `/api/planner/admin/builds/{id}/activate/` | `Admin` | Serve an earlier build's graph again (rollback) |
| `GET/PUT` | `/api/planner/admin/schedule/` | `Admin` | Build schedule: weekdays and time in UTC |
| `GET` | `/api/planner/admin/status/` | `Admin` | Served build, running build, maintenance window, OTP health |

Details: [Journey planner: admin API](planner-admin.md).

### Public feed files

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/feed/` | public | Returns a 400 error; a feed ID is required |
| `GET` | `/feed/{feed_submission_id}/` | public | Returns a link to the published static file |
| `GET` | `/feed/{feed_submission_id}/{filename}` | public | Downloads the published static file |
| `GET` | `/feed/rt/{realtime_submission_id}/` | public | Returns links to the published realtime files (per `endpoint_type`) |
| `GET` | `/feed/rt/{realtime_submission_id}/{endpoint_type}/` | public | Downloads the published realtime file (canonical route) |
| `GET` | `/feed/rt/{realtime_submission_id}/{filename}` | public | Legacy: downloads the file by name (kept for compatibility) |

---

## Notes for client developers

### Token handling

- Keep `access` short-lived and refresh it via `/api/auth/token/refresh/`. Replace the stored `refresh` with the one from each refresh response; the old one stops working.
- After a `401`, try a refresh first and only then log the user out.
- Public endpoints do not need a token.

### Role-based UI

Roles and profile data: from the JWT claims (`roles`, `first_name`, `last_name`) or `GET /api/users/me/` (full profile with `username`, `email`, `roles`).

Recommended UI logic:

- `Blogger`/`Editor`/`Admin` - show the blog panel (`Blogger` edits only their own posts).
- `DataProvider`/`Admin` - show the feed submission forms.
- `Helper`/`Admin` - show the queue of feeds awaiting confirmation and the case/organization panel.

In edit forms for `DataProvider`, **from stage 2 onwards** (static and realtime, without a rejection): lock the **name**, the feed source (URL/file or endpoint URL), **is_original**, **hide_original**, **auth_type** and **auth_value**. Static: `note`, `license` and fetch times (`download_time_1/2`, UTC) are allowed. Realtime: `note`, `license`, `interval` are allowed. After a **rejection**, restore the full edit form.

### HTTP status codes

Most common responses:

| Status | Meaning |
|--------|-----------|
| `200` | OK |
| `201` | Created |
| `204` | No content, e.g. reaction removal |
| `400` | Invalid request data |
| `401` | Missing or invalid token |
| `403` | Valid token, but the required role/permission is missing |
| `404` | Object does not exist or is not publicly available |
| `429` | Rate limit: DRF throttling (`anon`, `user`, `login`, `feed_download`; rates set by the `THROTTLE_*` settings) or the per-IP blog reaction limit |

### Multipart nested fields

For a static file upload, the backend accepts the nested file field as:

```text
static_entry.file
```

Similarly, you can send:

```text
static_entry.is_original
static_entry.license
```

### Empty text fields

Text fields (`name`, `note`, `license`, `url`, `description`, `website`, `contact_email`, `phone_number`, `auth_type`, `cause`, `reaction`, ...) have one empty value: `""`. Responses never contain `null` for them, and a request that sends `null` gets `400` with DRF's default field error, e.g. `{"name": ["This field may not be null."]}`; send `""` or omit the field. The body carries no machine-readable error code: codes will come with the custom exception handler proposed in [ADR 0006](../architecture/decisions/0006-api-error-codes.md), so match on the status and the field name, not on the message. Computed fields and fields that are not text keep `null` for "none", e.g. `rejection_cause`, `published_at`, `download_time_2`, `file`, `download_url`.

### Important constraints

- `auth_value` is never returned by the API.
- Public feeds are visible only when `current_stage=4`.
- The public endpoint `/api/data_manager/feeds/` returns organizations, not a raw list of feeds.
- `Helper` can confirm feeds but does not create feeds as a provider.
- `DataProvider` can create feeds but cannot confirm them.
- `Admin` has access to everything.
- Logged-in user's profile: `GET /api/users/me/`; email/password change: `POST /api/users/me/change-email/`, `POST /api/users/me/change-password/`.
