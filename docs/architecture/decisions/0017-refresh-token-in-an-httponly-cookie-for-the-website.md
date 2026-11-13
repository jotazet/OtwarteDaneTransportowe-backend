# 0017. Keep the website's refresh token in an HttpOnly cookie

- **Status:** Proposed
- **Date:** 2026-11-13

## Context

The website (`https://odt.org.pl`) calls the API (`https://api.odt.org.pl`) from the browser with `fetch` and kept both JWTs from `/api/auth/token/` in `localStorage` ([ADR 0003](0003-jwt-and-group-based-roles.md)). Any script that ever runs on the website's origin can read them. Refresh tokens rotate, so a copied refresh token stays alive for as long as the attacker keeps refreshing it: one XSS is a session takeover for up to `JWT_REFRESH_DAYS` per rotation, indefinitely. The access token alone is worth far less: it lives 15 minutes and cannot be renewed without the refresh token. Issue #137.

Other clients (scripts, tools) use the body-based endpoints and must keep working, and the old frontend must keep working while the backend is deployed first.

The website and the API are different origins but the **same site**: `org.pl` is a public suffix, so both are under the registrable domain `odt.org.pl`. A cookie of `api.odt.org.pl` is a first-party cookie for the website, so third-party cookie blocking does not apply, and `SameSite=Strict` cookies are sent on the website's `fetch` calls.

## Decision

- **New endpoints, old ones unchanged.** `POST /api/auth/session/` (log in), `POST /api/auth/session/refresh/` and `POST /api/auth/session/logout/` are added. `/api/auth/token/`, `/api/auth/token/refresh/` and `/api/auth/logout/` keep their contract. The website moves to the new endpoints; nothing forces other clients to.
- **The refresh token lives only in a cookie.** The session endpoints set it in `odt_refresh` and answer with `{"access": ...}` only. The cookie is:
  - `HttpOnly`, so scripts cannot read it;
  - `Secure`, which `AUTH_REFRESH_COOKIE_SECURE=false` turns off for a local http setup; `settings_prod` always forces it on;
  - `SameSite=Strict`: the website and the API are same-site, so `Strict` costs nothing; nothing needs the cookie on a navigation from another site, which `Lax` would allow;
  - `Path=/api/auth/session/`, so it is sent to the three session endpoints and to no other request;
  - host-only (no `Domain`), so other subdomains of `odt.org.pl` never receive it;
  - `Max-Age` = `JWT_REFRESH_DAYS`. Every refresh rotates it and blacklists the old token, as the body-based refresh does. Logout blacklists it and clears the cookie.
- **The access token lives in the website's memory only.** A new tab or a reload calls the refresh endpoint to get one.
- **CSRF.** The cookie is sent automatically, so the session endpoints check two things before they read the body or the cookie, and answer `403 auth.csrf_failed` otherwise:
  - the header `X-ODT-CSRF: 1`. A custom header makes every cross-origin request non-simple, so the browser sends a CORS preflight first, and only allowlisted origins pass it;
  - an `Origin` header that is one of the CORS allowlist (`CORS_ALLOWED_ORIGINS`, `CORS_ALLOWED_ORIGIN_REGEXES`). Browsers send `Origin` on every `POST`, so the server refuses a forged request even if the CORS configuration were wrong, and a request from another subdomain of the same site (which `SameSite` lets through).

  The views use no authentication class, so a Django admin session cookie sent along does not trigger DRF's session CSRF check.
- **CORS with credentials only where needed.** `CORS_ALLOW_CREDENTIALS` stays off. A thin subclass of django-cors-headers' middleware adds `Access-Control-Allow-Credentials: true` on `/api/auth/session/` when the origin passed the allowlist. Every other endpoint is still called without cookies, and the public planner API, which answers any origin, never allows credentials.
- **Errors are codes** ([ADR 0006](0006-api-error-codes.md)): `auth.csrf_failed` (403), `auth.invalid_credentials` (401), `auth.refresh_cookie_missing` (401) and `auth.refresh_cookie_invalid` (401). A failed refresh does not clear the cookie: another tab may have just rotated it, and clearing it would log out every tab.
- Refresh and login share the existing `login` throttle, as the body-based endpoints do.

## Consequences

- An XSS on the website can still act as the user while it runs, but can no longer take the session away: it gets at most a 15-minute access token.
- The website refreshes on every page load and in every tab. Two tabs refreshing at once race on the rotation: one gets `auth.refresh_cookie_invalid` although the session is fine. The website serializes refreshes across tabs (Web Locks API) and retries once after that error.
- A password change or an admin reset blacklists the cookie's token as well ([`set_password_and_end_sessions`](../../../accounts/services.py)). The `refresh` that `change-password` returns cannot be put into an HttpOnly cookie by the website, so after a self-service change it logs in again through `POST /api/auth/session/` with the new password.
- The CORS allowlist is now also the CSRF allowlist of the session endpoints; adding an origin to `CORS_ALLOWED_ORIGINS` lets it log users in with credentials.
- Server-side rendering of the website never sees the cookie (it is a cookie of the API's host), so pages rendered on the server stay anonymous, as before.
- The body-based endpoints remain; deprecating them for browsers can be decided later.

## Alternatives considered

- **Opt-in on the existing endpoints** (a header that switches `/api/auth/token/` to the cookie) — one URL with two contracts, and the cookie's `Path` would have to cover `/api/auth/token/`, which other clients use.
- **`SameSite=Lax`** — no case needs the cookie on a top-level navigation from another site; `Strict` is stricter at no cost.
- **Django's CSRF token (double-submit cookie)** — needs a readable CSRF cookie on the API's host and a round trip to get it; the custom header plus the `Origin` check gives the same protection for `fetch` without either.
- **`CORS_ALLOW_CREDENTIALS = True` for all paths** — would also allow credentials on the public planner API, which answers any origin, and on every other endpoint, which never needs cookies.
- **Access token in a cookie too** — every authenticated endpoint would need CSRF protection and credentials in CORS; the access token in memory is short-lived and out of `localStorage`.
- **A backend-for-frontend (Next.js proxies the API)** — moves the whole API traffic through the website's server; much larger change for the same gain.
