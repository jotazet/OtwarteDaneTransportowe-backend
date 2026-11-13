# ODT API

This documentation describes the REST API of the OtwarteDaneTransportowe (Open Transport Data) backend: its endpoints, the most important response fields and the required permissions. It is written as context for building client (frontend) applications.

## Contents

- [Quick start](#quick-start)
- [Roles and permissions](#roles-and-permissions)
- [JWT authentication](#jwt-authentication)
- [Endpoint map](#endpoint-map)
- [Notes for client developers](#notes-for-client-developers)
- [Error codes](#error-codes)
- [Blog API](blog.md)
- [Organizations and cases API](cases.md)
- [Feeds API and public file downloads](feeds.md)
- [Journey planner API (public)](planner.md)
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
POST /api/auth/logout/
```

The website uses [cookie sessions](#cookie-sessions-for-the-website) instead, which keep the refresh token in an HttpOnly cookie:

```http
POST /api/auth/session/
POST /api/auth/session/refresh/
POST /api/auth/session/logout/
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
| `Helper` | Feed queue: **only** confirming/rejecting (`stage`, `rejection_cause`); cases (data providers, organizations, statuses), except deleting an organization. **No** editing of the URL/file or the submission content. |
| Anonymous | Public reads, blog reactions, downloading published feeds. |

### Access matrix

| Area | Public read | Write |
|--------|------------------|-------|
| Blog posts | Yes | create: `Blogger`/`Editor`/`Admin`; edit other users' posts: `Editor`/`Admin` |
| Blog reactions | Yes, `POST` without login | Anyone, limited per IP |
| Data providers | Yes | `Helper` or `Admin` |
| Case statuses | Yes | `Helper` or `Admin` |
| Transport organizations | Yes | `Helper` or `Admin`; delete: `Admin` |
| Static feed submissions | No | `DataProvider` creates; `Helper`/`Admin` confirms |
| Realtime submissions | No | `DataProvider` creates; `Helper`/`Admin` confirms |
| Published feeds catalog | Yes | No writes through the public API |
| Public feed files | Yes, published only | No writes |
| Journey planner API (`/api/planner/v1/`) | Yes, any origin (CORS), optional API key | No writes |
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

### `POST /api/auth/logout/`

Ends a session: blacklists the given refresh token, so it can no longer be refreshed. Other sessions of the same user are not affected.

**Permissions:** public, requires a valid `refresh` (no access token needed, so logout works after the access token has expired).

**Request:**

```json
{
  "refresh": "eyJ..."
}
```

**Response 200:** `{}`

A refresh token that is invalid, expired or already blacklisted returns `401` with `"code": "token_not_valid"`; the session is over either way, so the client clears its tokens in both cases.

The access token of that session is not revoked: access tokens are stateless and stay valid until they expire (`JWT_ACCESS_MINUTES`, 15 minutes by default). The client discards it.

### Cookie sessions for the website

Browser sessions in which the refresh token never reaches JavaScript ([ADR 0017](../architecture/decisions/0017-refresh-token-in-an-httponly-cookie-for-the-website.md)). The API keeps the refresh token in an HttpOnly cookie and answers with the access token only; the client keeps the access token in memory, never in `localStorage`. The endpoints above (`/api/auth/token/…`, `/api/auth/logout/`) are unchanged for other clients.

| Method | Path | Body | Success |
|---|---|---|---|
| `POST` | `/api/auth/session/` | `{"username": "...", "password": "..."}` (JSON) | `200` `{"access": "eyJ..."}`, sets the cookie |
| `POST` | `/api/auth/session/refresh/` | none | `200` `{"access": "eyJ..."}`, sets a new cookie |
| `POST` | `/api/auth/session/logout/` | none | `200` `{}`, clears the cookie |

**Permissions:** public; no `Authorization` header is needed or read.

**Every request must:**

- be sent with `fetch(url, {method: "POST", credentials: "include", ...})`, so the browser sends and stores the cookie;
- carry the header `X-ODT-CSRF: 1` (exactly `1`);
- come from an origin in the API's CORS allowlist (`CORS_ALLOWED_ORIGINS` or `CORS_ALLOWED_ORIGIN_REGEXES`, matched exactly as django-cors-headers does). Browsers send the `Origin` header themselves on every `POST`; a request without it, with `Origin: null` or with another origin is refused. For local development, put the website's origin (e.g. `http://localhost:3000`) into `CORS_ALLOWED_ORIGINS` of the backend's `.env`.

Otherwise the answer is `403` `{"code": "auth.csrf_failed", "details": {}}`, checked before anything else: nothing is rotated or blacklisted, and the request does not count against a rate limit. These endpoints are meant for browsers; other clients use `/api/auth/token/`.

**The cookie:**

```http
Set-Cookie: odt_refresh=eyJ...; expires=...; HttpOnly; Max-Age=86400; Path=/api/auth/session/; SameSite=Strict; Secure
```

| Attribute | Value |
|---|---|
| Name | `odt_refresh` |
| Value | the refresh JWT (the same token `/api/auth/token/` returns as `refresh`, with the `roles`, `first_name`, `last_name` claims) |
| `HttpOnly` | always |
| `Secure` | always in production; a local http setup may turn it off with `AUTH_REFRESH_COOKIE_SECURE=false` |
| `SameSite` | `Strict` (the website and the API are the same site, `odt.org.pl`) |
| `Path` | `/api/auth/session/`: the browser sends it to these three endpoints only |
| `Domain` | none: a host-only cookie of the API's host |
| `Max-Age` | the refresh token lifetime, `JWT_REFRESH_DAYS` × 86400 s (86400 by default), counted again from every refresh |

The cookie is cleared with the same name, `Path` and flags and `Max-Age=0`.

**`POST /api/auth/session/`** logs in. On success it sets the cookie and returns `{"access": "..."}`; the access token is the same as from `/api/auth/token/`. Errors:

- `401` `{"code": "auth.invalid_credentials", "details": {}}`: wrong username or password, or an inactive account;
- `400` with DRF's field dictionary when `username` or `password` is missing (DRF prose, e.g. `{"password": ["This field is required."]}`);
- `429`: the `login` throttle.

**`POST /api/auth/session/refresh/`** reads the cookie, blacklists that refresh token and sets a new one (rotation, as `/api/auth/token/refresh/` does), and returns a new `{"access": "..."}`. The claims of the new access token come from the refresh token, as with the body-based refresh. Errors:

- `401` `{"code": "auth.refresh_cookie_missing", "details": {}}`: no cookie was sent (never logged in, logged out, or the cookie expired);
- `401` `{"code": "auth.refresh_cookie_invalid", "details": {}}`: the token in the cookie is invalid, expired or blacklisted, or its user is inactive or deleted. Two refreshes with the same token never both succeed: the second waits for the first and gets this code. The cookie is **not** cleared (another tab may just have replaced it);
- `429`: the `session_refresh` throttle (`THROTTLE_SESSION_REFRESH`, 60/min per client IP by default), separate from the login budget.

**`POST /api/auth/session/logout/`** blacklists the cookie's refresh token, if it is still valid, and clears the cookie. It answers `200` `{}` also without a cookie or with an invalid one, so the client always treats it as done. As with `/api/auth/logout/`, the access token stays valid until it expires; the client drops it.

**CORS:** responses of these endpoints, and their preflights, carry `Access-Control-Allow-Origin: <the origin>` and `Access-Control-Allow-Credentials: true` for allowlisted origins only; `X-ODT-CSRF` and `Content-Type` are allowed request headers. No other endpoint allows credentials, so the client sends `credentials: "include"` only to these three.

**Client flow:**

1. Login: `POST /api/auth/session/`; keep `access` in memory.
2. Page load, a new tab, or an access token that expired or got a `401`: `POST /api/auth/session/refresh/`. `auth.refresh_cookie_missing` or `auth.refresh_cookie_invalid` means the visitor is logged out.
3. Refreshes from several tabs race: each rotates the cookie, and a request that still carried the previous token gets `auth.refresh_cookie_invalid`. Run refreshes one at a time across tabs (e.g. `navigator.locks.request("odt-refresh", …)`), and retry once after `auth.refresh_cookie_invalid` before treating the visitor as logged out.
4. Logout: `POST /api/auth/session/logout/`, then drop `access` whatever the answer.
5. After `POST /api/users/me/change-password/` the cookie's token is blacklisted with all others of the user. Ignore the `refresh` in that response and log in again with `POST /api/auth/session/` and the new password.

### Ending all sessions of a user

A password change (`POST /api/users/me/change-password/`) and an admin password reset (`POST /api/users/{id}/reset-password/`) blacklist **all** refresh tokens of that user, so every session must log in again once its access token expires. A self-service change returns a fresh pair for the session that made it (see the [endpoint map](#users-djangocontribauthmodelsuser)). Expired tokens are deleted daily by the beat task `accounts.flush_expired_tokens` (03:15 UTC).

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
| `POST` | `/api/auth/logout/` | public | Blacklist the given `refresh` (end the session) |
| `POST` | `/api/auth/session/` | public, [CSRF header and origin](#cookie-sessions-for-the-website) | Website login: `access` in the body, `refresh` in an HttpOnly cookie |
| `POST` | `/api/auth/session/refresh/` | public, CSRF header and origin | Rotate the refresh cookie; returns a new `access` |
| `POST` | `/api/auth/session/logout/` | public, CSRF header and origin | Blacklist the refresh cookie and clear it |
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
| `PUT` | `/api/users/{id}/` | `Admin` (group)¹ | Same fields and rules as `PATCH`; none is required, and with a JSON body omitted fields keep their values |
| `PATCH` | `/api/users/{id}/` | `Admin` (group)¹ | Edit `email`, `first_name`, `last_name`, `is_active`, `roles` |
| `DELETE` | `/api/users/{id}/` | `Admin` (group)¹ | Permanent deletion; not yourself (`users.cannot_delete_self`) nor the last `Admin` (`users.last_admin`) |
| `POST` | `/api/users/{id}/reset-password/` | `Admin` (group)¹ | New random password in `generated_password`; ends all sessions of that user |
| `GET` | `/api/users/me/` | logged in | Current user's profile |
| `POST` | `/api/users/me/change-email/` | logged in | `current_password`, `new_email`; rate limit `account_change` |
| `POST` | `/api/users/me/change-password/` | logged in | `current_password`, `new_password`; ends all sessions of the user and returns a fresh `{"access", "refresh"}` pair for this one; rate limit `account_change` |

¹ A **protected account** (`is_superuser` or `is_staff` true; both are read-only fields of every user object) can be edited, deleted or have its password reset only by a caller who is a superuser as well as in the `Admin` group. Anyone else gets `403` with `{"code": "users.protected_account", "details": {}}`. Reading such an account (`GET`) is not restricted. The flags themselves are set only through Django (`createsuperuser`, the admin site), never through this API.

Allowed `roles` values: `Admin`, `Blogger`, `Editor`, `DataProvider`, `Helper` (`PUT`/`PATCH` fully replace the list of groups); any other value returns `400` with `{"roles": ["users.unknown_role"]}`. Removing the `Admin` role from, or deactivating, the last active admin returns `400` with `{"code": "users.last_admin", "details": {}}`. `change-email` and `change-password` return `{"current_password": ["users.wrong_password"]}` for a wrong current password, and `change-password` returns `users.password_*` codes on `new_password` for a password Django's validators refuse ([error codes](#error-codes)).

For `POST /api/users/` the body does **not** contain a password — example `201` response:

```json
{
  "id": 12,
  "username": "jan.kowalski",
  "email": "jan@example.com",
  "first_name": "Jan",
  "last_name": "Kowalski",
  "is_active": true,
  "is_staff": false,
  "is_superuser": false,
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
| `GET` | `/api/data_manager/feed-submissions/{id}/download/realtime/{endpoint_pk}/` | owner or `Helper`/`Admin` | Download the private cache of an RT endpoint linked to the static submission (owner: only of its own realtime submissions) |
| `GET` | `/api/data_manager/static-feed-entries/` | owner or `Helper`/`Admin` | List of static feeds with their fetch status |
| `GET` | `/api/data_manager/static-feed-entries/{id}/` | owner or `Helper`/`Admin` | Details of a single static feed with its fetch status |
| `POST` | `/api/data_manager/static-feed-entries/{id}/pause-fetch/` | `Helper`/`Admin` | Manually pause fetching of a static feed |
| `POST` | `/api/data_manager/static-feed-entries/{id}/resume-fetch/` | `Helper`/`Admin` | Resume fetching of a static feed and cancel the retry block; proxied feeds only (`400` otherwise, as `pause-fetch`) |
| `GET` | `/api/data_manager/proxy-feeds/` | logged in | A single, paginated list of proxied feeds (static + realtime), sorted by region, organization and feed name ([order](../domain/feed-workflow.md#fetch-errors-and-pauses-of-proxied-feeds)), filtered by `fetch_status`, `source`, `organization` (id) and `region`: a provider sees their own, helper/admin see all. Pausing/resuming (`pause-fetch`/`resume-fetch`) is `Helper`/`Admin` only |
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
| `GET` | `/api/planner/admin/rental-candidates/` | `Admin` | Published GBFS feeds that can be added to the planner |
| `GET/POST` | `/api/planner/admin/rental-sources/` | `Admin` | Bike and scooter sharing feeds the planner routes with |
| `GET/PATCH/DELETE` | `/api/planner/admin/rental-sources/{id}/` | `Admin` | One rental source (`network` cannot change) |
| `GET/PATCH/DELETE` | `/api/planner/admin/feeds/{id}/` | `Admin` | One planner feed (`feed_id` cannot change) |
| `GET/POST` | `/api/planner/admin/osm/` | `Admin` | OSM extract downloads; `POST` replaces the map from a URL (`202`, `409` while one runs) |
| `GET/POST` | `/api/planner/admin/builds/` | `Admin` | Graph builds; `POST` builds now (`202`, `409` while one runs) |
| `GET` | `/api/planner/admin/builds/{id}/` | `Admin` | Build with per-feed outcomes and log excerpt |
| `POST` | `/api/planner/admin/builds/{id}/activate/` | `Admin` | Serve an earlier build's graph again (rollback) |
| `GET/PUT` | `/api/planner/admin/schedule/` | `Admin` | Build schedule: weekdays and time in UTC |
| `GET` | `/api/planner/admin/status/` | `Admin` | Served build, running build, maintenance window, OTP health |
| `GET/PATCH` | `/api/planner/admin/settings/` | `Admin` | Kill switch and rate limits of the public planner API |
| `GET/POST` | `/api/planner/admin/api-keys/` | `Admin` | API keys; `POST` returns the plain key once |
| `GET/PATCH` | `/api/planner/admin/api-keys/{id}/` | `Admin` | One key (revoke with `is_active: false`) |
| `GET/POST` | `/api/planner/admin/ip-bans/` | `Admin` | Banned addresses and networks |
| `PATCH/DELETE` | `/api/planner/admin/ip-bans/{id}/` | `Admin` | One ban |
| `GET` | `/api/planner/admin/requests/` | `Admin` | Request log of the public planner API (30 days, no query strings) |
| `GET` | `/api/planner/admin/stats/daily/` | `Admin` | Requests per day by status class |
| `GET` | `/api/planner/admin/stats/summary/` | `Admin` | Dashboard totals: requests, unique IPs and keys, errors, latency |
| `GET` | `/api/planner/admin/stats/timeseries/` | `Admin` | Chart points per minute, hour or day, by status class or endpoint |
| `GET` | `/api/planner/admin/stats/endpoints/` | `Admin` | Requests, errors and latency per endpoint |
| `GET` | `/api/planner/admin/stats/top-clients/` | `Admin` | Busiest IPs or API keys, with user agent and ban status |
| `GET` | `/api/planner/admin/stats/user-agents/` | `Admin` | User-Agents by requests |

### Journey planner (public)

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/api/planner/v1/status/` | public | Availability, served graph, feeds with licences, attribution |
| `GET` | `/api/planner/v1/plan/` | public | Trip planning with realtime delays |
| `GET` | `/api/planner/v1/stops/search/` | public | Stop search by name (places with their poles) |
| `GET` | `/api/planner/v1/stops/nearby/` | public | Places with stops near a point |
| `GET` | `/api/planner/v1/stops/` | public | Every stop in a map area (`bbox`) |
| `GET` | `/api/planner/v1/stop/` | public | One stop with its lines (`?id=`) |
| `GET` | `/api/planner/v1/routes/` | public | Line search and lists, also the lines of given stops |
| `GET` | `/api/planner/v1/agencies/` | public | Operators with their ODT organization |
| `GET` | `/api/planner/v1/stop/departures/` | public | Next departures with realtime predictions |
| `GET` | `/api/planner/v1/stop/timetable/` | public | A stop's timetable for one day |
| `GET` | `/api/planner/v1/route/` | public | A line with its variants (`?id=`) |
| `GET` | `/api/planner/v1/trip/` | public | A trip on its service date with realtime (`?id=&date=`) |
| `GET` | `/api/planner/v1/trip/block/` | public | Every trip of a trip's block (brigade) on a day |
| `GET` | `/api/planner/v1/vehicles/` | public | Vehicles reporting their position now |
| `GET` | `/api/planner/v1/vehicles/coverage/` | public | Share of chosen operators' trips under way that report a position |
| `GET` | `/api/planner/v1/alerts/` | public | Operators' service alerts, by line, stop or operator |
| `GET` | `/api/planner/v1/rentals/` | public | Bike and scooter sharing stations and vehicles in an area |
| `GET` | `/api/planner/v1/schema/` | public | OpenAPI document of the public planner API |
| `GET` | `/api/planner/v1/docs/` | public | ReDoc for the public planner API |

Details: [Journey planner API](planner.md).

Details: [Journey planner: admin API](planner-admin.md).

### Public feed files

| Method | Endpoint | Permissions | Description |
|--------|----------|-------------|------|
| `GET` | `/feed/` | public | Returns `400` with `{"code": "feeds.feed_id_required", "details": {}}`; a feed ID is required |
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
- On logout, send the stored `refresh` to `POST /api/auth/logout/`, then clear the tokens whatever the response.
- After `POST /api/users/me/change-password/`, store the `access` and `refresh` from the response: the old `refresh` no longer works.
- Public endpoints do not need a token.
- A browser client should use [cookie sessions](#cookie-sessions-for-the-website) instead: the refresh token stays in an HttpOnly cookie and the access token in memory.

### Role-based UI

Roles and profile data: from the JWT claims (`roles`, `first_name`, `last_name`) or `GET /api/users/me/` (full profile with `username`, `email`, `roles`).

Recommended UI logic:

- `Blogger`/`Editor`/`Admin` - show the blog panel (`Blogger` edits only their own posts).
- `DataProvider`/`Admin` - show the feed submission forms.
- `Helper`/`Admin` - show the queue of feeds awaiting confirmation and the case/organization panel.

In edit forms for `DataProvider`, **from stage 2 onwards** (static and realtime, without a rejection): lock the **name**, the feed source (URL/file or endpoint URL), **is_original**, **hide_original**, **auth_type** and **auth_value**. Static: `note`, `license` and fetch times (`download_time_1/2`, UTC) are allowed. Realtime: `note`, `license`, `interval` (at least 5 s) are allowed. After a **rejection**, restore the full edit form.

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
| `409` | Conflict with the current state, e.g. a feed submission's stage changed while the request ran (`feeds.stage_changed`, see [Feeds API](feeds.md)) or a planner job already runs |
| `429` | Rate limit: DRF throttling of login (`/api/auth/token/`, `/api/auth/token/refresh/`, `/api/auth/session/` and the Django admin login form share one budget), feed downloads and blog reaction changes (`login`, `feed_download`, `reaction`; rates set by the `THROTTLE_*` settings; counted per client IP), of the website's cookie refresh (`session_refresh`, `POST /api/auth/session/refresh/`), of password and email changes (`account_change`, counted per user), the per-IP blog reaction limit, or the planner API limits ([planner.md](planner.md)). Other endpoints have no rate limit |

### Error codes

Errors that the backend itself decides carry a stable code instead of prose ([ADR 0006](../architecture/decisions/0006-api-error-codes.md)). Codes are `<area>.<snake_case_reason>` and never change once released; the frontend translates them and never shows backend text. They come in two shapes:

- **Request errors** (the whole request is refused): `{"code": "users.last_admin", "details": {}}`. `details` is an object, empty unless stated.
- **Field errors**: DRF's field dictionary, with the code as the message, e.g. `{"stage": ["feeds.invalid_stage"]}` or `{"static_entry": {"url": ["feeds.url_blocked"]}}`. A field error of a nested object that is not tied to one of its fields is under `__all__`, e.g. `{"static_entry": {"__all__": ["feeds.static_source_url_and_file"]}}`.

Errors produced by DRF or Django themselves (`This field is required.`, an invalid choice, `401`, a permission class's default `403`, `404`, throttling's `429`) are still English prose; match on the status and the field name for those.

Request errors:

| Status | Code | Where | Meaning |
|---|---|---|---|
| `403` | `auth.csrf_failed` | `/api/auth/session/…` | The `X-ODT-CSRF: 1` header is missing or the `Origin` is not allowed ([cookie sessions](#cookie-sessions-for-the-website)). |
| `401` | `auth.invalid_credentials` | `POST /api/auth/session/` | Wrong username or password, or an inactive account. |
| `401` | `auth.refresh_cookie_missing` | `POST /api/auth/session/refresh/` | No refresh cookie was sent. |
| `401` | `auth.refresh_cookie_invalid` | `POST /api/auth/session/refresh/` | The refresh cookie holds an invalid, expired or blacklisted token. |
| `400` | `feeds.feed_id_required` | `GET /feed/` | The download URL has no feed ID. |
| `400` | `feeds.not_proxy_managed` | `pause-fetch`, `resume-fetch` | The feed is not proxied, so its fetching cannot be paused. |
| `403` | `feeds.submissions_forbidden` | `GET /api/data_manager/submissions/` | The user has none of the `DataProvider`, `Helper`, `Admin` roles. |
| `403` | `feeds.review_forbidden` | `PUT`/`PATCH` of a static or realtime submission | Only `Helper`/`Admin` may send `stage` or `rejection_cause`. |
| `403` | `feeds.helper_content_edit_forbidden` | `PUT`/`PATCH` of a static or realtime submission | A `Helper` may send only `stage` and `rejection_cause`. |
| `403` | `feeds.delete_forbidden` | `DELETE` of a static or realtime submission | Only `Admin` deletes submissions. |
| `403` | `feeds.fetch_control_forbidden` | `pause-fetch`, `resume-fetch` | Only `Helper`/`Admin` pause or resume fetching. |
| `409` | `feeds.stage_changed` | `PUT`/`PATCH` of a static or realtime submission | The stage changed while the request ran; reload and retry. |
| `400` | `users.cannot_delete_self` | `DELETE /api/users/{id}/` | An admin cannot delete their own account. |
| `400` | `users.last_admin` | `DELETE`, `PUT`/`PATCH /api/users/{id}/` | The change would delete, deactivate or remove the `Admin` role from the last active admin. |
| `403` | `users.protected_account` | `PUT`/`PATCH`/`DELETE /api/users/{id}/`, `reset-password` | The account is a superuser or staff account. |
| `400` | `blog.client_ip_unknown` | `POST /api/blog/reactions/{post_id}/` | The server could not determine the client's IP address. |
| `400` | `blog.post_id_required` | `POST /api/blog/reactions/{post_id}/` | The URL has no post ID. |
| `429` | `blog.reaction_daily_limit` | `POST /api/blog/reactions/{post_id}/` | The IP has added 10 reactions in the last 24 hours. |

Field errors:

| Code | Field | Meaning |
|---|---|---|
| `feeds.invalid_integer` | list query params, e.g. `transport_organization` | The value is not an integer. |
| `feeds.invalid_choice` | list query params `fetch_status`, `source` of `proxy-feeds` | The value is not one of the allowed values. |
| `feeds.invalid_stage` | `stage` | The stage is not an integer from 1 to 4. |
| `feeds.field_locked` | `name`, `transport_organization`, `data_type`, `static_entry.<field>`, `protocol`, `static_submission`, `endpoints` | The owner cannot change this field while the submission is in review or published (stages 2–4 without a rejection). |
| `feeds.url_blocked` | `static_entry.url`, an endpoint's `url` | The URL is refused by ODT's outbound security rules: not http(s), no host name, the host does not resolve, or it resolves to a non-public address. |
| `feeds.static_source_missing` | `static_entry`, `static_entry.__all__` | Neither a URL nor a file was given. |
| `feeds.static_source_url_and_file` | `static_entry.__all__` | Both a URL and a file were given. |
| `feeds.download_time_required` | `static_entry.download_time_1` | A URL source needs `download_time_1`. |
| `feeds.download_time_with_file` | `static_entry.download_time_1`, `static_entry.download_time_2` | An uploaded file has no download schedule. |
| `feeds.hide_original_with_file` | `static_entry.hide_original` | `hide_original` (or `auth_type`) applies only to a URL source. |
| `feeds.cached_file_with_upload` | `static_entry.__all__` | A server-downloaded copy exists for an uploaded file; not expected from clients. |
| `feeds.gtfs_not_zip` | `static_entry.file` | The uploaded GTFS file is not a ZIP archive. |
| `feeds.file_too_large` | `static_entry.file` | The uploaded file exceeds `MAX_FEED_FILE_SIZE_BYTES`. |
| `realtime.endpoints_required` | `endpoints` | A realtime submission needs at least one endpoint. |
| `realtime.endpoints_locked` | `endpoints` | Endpoints cannot be added or removed in review or after publication. |
| `realtime.endpoint_type_unknown` | `endpoints` | In review or after publication, an endpoint type the submission does not have. |
| `realtime.endpoint_type_duplicate` | `endpoints` | Two endpoints of the same type. |
| `realtime.endpoints_invalid_for_protocol` | `endpoints`, `endpoints.endpoint_type`, `protocol` | An endpoint type does not fit the protocol. |
| `realtime.interval_too_short` | an endpoint's `interval` | The interval is under 5 seconds. |
| `realtime.static_submission_required` | `static_submission` | GTFS-RT and SIRI need a static submission. |
| `realtime.static_submission_not_allowed` | `static_submission` | GBFS cannot link a static submission. |
| `realtime.static_submission_other_organization` | `static_submission` | The static submission belongs to another organization. |
| `realtime.static_submission_type_mismatch` | `static_submission` | The static feed type does not fit the protocol (GTFS-RT needs GTFS, SIRI needs NeTEx). |
| `realtime.static_submission_not_published` | `static_submission` | The static submission is not published yet. |
| `realtime.static_submission_not_owned` | `static_submission` | A `DataProvider` may link only a static feed it submitted. |
| `users.unknown_role` | `roles` | A role is not one of `Admin`, `Blogger`, `Editor`, `DataProvider`, `Helper`. |
| `users.wrong_password` | `current_password` | The current password is wrong. |
| `users.password_too_short` | `new_password` | Shorter than 8 characters. |
| `users.password_too_similar` | `new_password` | Too similar to the username, name or email. |
| `users.password_too_common` | `new_password` | On the list of common passwords. |
| `users.password_entirely_numeric` | `new_password` | Only digits. |
| `blog.too_many_tags` | `tags` | More than 5 tags (DRF's own `max_length` message comes with it). |
| `blog.image_too_large` | `image` | The image exceeds `MAX_IMAGE_FILE_SIZE_BYTES`. |
| `cases.data_provider_name_taken` | `name` | A data provider with this name already exists. |
| `cases.organization_exists` | `non_field_errors` | A transport organization with this `region` and `transport_organization` already exists. |

The planner has its own codes ([planner.md](planner.md), [planner-admin.md](planner-admin.md)), and stored causes have theirs: [static](feeds.md#static-rejection-reasons) and [realtime](feeds.md#realtime-rejection-reasons) rejection reasons, and GTFS validation codes in [static feeds](feeds.md#static-feeds-feedsubmission).

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

Text fields (`name`, `note`, `license`, `url`, `description`, `website`, `contact_email`, `phone_number`, `auth_type`, `cause`, `reaction`, ...) have one empty value: `""`. Responses never contain `null` for them, and a request that sends `null` gets `400` with DRF's default field error, e.g. `{"name": ["This field may not be null."]}`; send `""` or omit the field. This is DRF's own message, not a code (see [error codes](#error-codes)), so match on the status and the field name, not on the message. Computed fields and fields that are not text keep `null` for "none", e.g. `rejection_cause`, `published_at`, `download_time_2`, `file`, `download_url`.

### Important constraints

- `auth_value` is never returned by the API.
- Public feeds are visible only when `current_stage=4`.
- The public endpoint `/api/data_manager/feeds/` returns organizations, not a raw list of feeds.
- `Helper` can confirm feeds but does not create feeds as a provider.
- `DataProvider` can create feeds but cannot confirm them.
- `Admin` has access to everything.
- Logged-in user's profile: `GET /api/users/me/`; email/password change: `POST /api/users/me/change-email/`, `POST /api/users/me/change-password/`.
