# 0003. Authenticate with JWT and authorize with roles stored as Django groups

- **Status:** Accepted (recorded retroactively)
- **Date:** 2026-10-12

## Context

The website is a separate Next.js application that calls the API from the browser, so session cookies across two origins would need CSRF and cookie configuration on both sides. Users have different duties: publishing feeds, verifying them, curating cases, writing the blog, administering users.

## Decision

- **Authentication:** `djangorestframework-simplejwt`. Access tokens live 15 minutes and refresh tokens 1 day by default (`JWT_ACCESS_MINUTES`, `JWT_REFRESH_DAYS`); refresh tokens rotate and are blacklisted after rotation. Tokens carry `roles`, `first_name` and `last_name` claims (`OtwarteDaneTransportowe/jwt_views.py`). Login is throttled. Session authentication stays enabled for the browsable API and admin.
- **Authorization:** five roles as Django groups — `Admin`, `Helper`, `DataProvider`, `Blogger`, `Editor` — created by a data migration (`cases/migrations/0004_create_auth_role_groups.py`). All checks go through `OtwarteDaneTransportowe/auth_roles.py` (`has_role`, `can_*` helpers and permission classes).
  - `is_admin` means the Admin group or a superuser; `has_admin_role` means the Admin group only, and is required for user management and for changing a feed source at stages 2–4. `is_staff` grants nothing.
- The default permission is `IsAuthenticated`; public views declare `AllowAny` explicitly.

## Consequences

- New permission logic goes into `auth_roles.py`, never inline in views.
- Role names are part of the API contract (JWT claims, user endpoints) and of the frontend's labels.
- The frontend stores tokens in `localStorage`, which exposes them to any XSS. Moving the refresh token to an HttpOnly cookie would need a backend change and an ADR.

## Alternatives considered

- **Session cookies across origins** — CSRF and SameSite handling on two deployments for no gain over short-lived JWTs.
- **Django model permissions per object** — finer than ODT needs; roles map directly to people's duties.
