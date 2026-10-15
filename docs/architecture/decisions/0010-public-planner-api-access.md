# 0010. Open the planner API to everyone, with optional keys, per-minute limits, bans and a 30-day request log

- **Status:** Proposed
- **Date:** 2026-10-13

## Context

The journey planner API ([ADR 0008](0008-journey-planner-on-private-opentripplanner.md)) is for ODT's own website and for outside developers. The website calls it from browsers, so it cannot hold a secret. A single OpenTripPlanner on an 11 GB VPS must not be overloaded by any one client or by all of them together. The admins want to see who calls the API and to block abusers.

Client IP addresses are personal data under the GDPR (Recital 30; CJEU C-582/14 *Breyer*), and trip coordinates can reveal where people live and work.

## Decision

- **Access.** Anonymous by default, with an optional API key in the `X-API-Key` header that raises the limits.
  - Keys are issued by admins; only their SHA-256 is stored, and the plain key is shown once.
  - The API ships switched off (`PlannerSettings.public_api_enabled`) and is turned on from the panel. `GET /api/planner/v1/status/` always answers.
- **Limits.** Requests per minute in three groups: `plan` (trip planning), `realtime` (OTP-backed lookups) and `lookup` (index lookups).
  - Each group is limited per client (an IP address, or a whole IPv6 `/64`), per key, and for all clients together.
  - Limits are edited in the panel. Counters are minute-stamped Redis keys.
  - An exceeded limit returns `429 planner.rate_limited` with `details.scope` (`client` or `global`) and `Retry-After`.
- **Client IP.** Taken from `get_client_ip` (trusted proxies only, rightmost untrusted `X-Forwarded-For` hop), never from DRF's throttle identity, which trusts the raw header.
- **Bans.** IPv4/IPv6 networks with an optional expiry, edited in the panel, returning `403 planner.client_banned`.
- **Build window.** Endpoints that need OTP answer `503 planner.unavailable` with `Retry-After` while a build has OTP stopped; index lookups keep working.
- **Request log.** One row per request with time, endpoint name, status, latency, client IP, key and User-Agent, and **never the query string**.
  - Rejected requests (403, 429) are logged at most once a minute per client.
  - Rows older than 30 days (`PLANNER_REQUEST_LOG_RETENTION_DAYS`) are deleted daily after each finished day is summarised into `PlannerDailyStat`, which holds no IP addresses.
  - Only the `Admin` group can read the log.
- **CORS.** Any origin may call `/api/planner/v1/`, without credentials. The rest of the API keeps its allowlist.
- **Errors.** Public planner errors are `{code, details}` ([ADR 0006](0006-api-error-codes.md)); invalid parameters return `planner.invalid_request` with the field codes in `details.fields`. The handler is set per view (`get_exception_handler`), so no other endpoint changes shape.
- **Documentation.** A planner-only OpenAPI schema (`/api/planner/v1/schema/`) and ReDoc (`/api/planner/v1/docs/`) are public. The full API schema stays restricted to admins.

## Consequences

- **What developers get.** They can start without registering; heavy users ask for a key, and the panel shows who uses how much.
- **Shared IPs.** Clients behind one carrier-grade NAT share an anonymous limit, which is why the per-IP defaults are generous; the global limits protect OTP.
- **What the stats can show.** Without coordinates in the log, demand cannot be mapped from it.
- **Privacy notice.** It must describe the log (purpose, retention, legal basis); this is a legal and frontend task. The legal basis has to be confirmed with the data protection officer: if the operator is a public body, Art. 6(1)(e) GDPR rather than legitimate interest.
- **Not covered yet.** DRF's default throttles elsewhere in the API still trust the raw `X-Forwarded-For` header; that is a separate fix.

## Alternatives considered

- **Mandatory keys (Digitransit)** — every browser user of ODT's website would need a server-side proxy, and casual developers would face a registration step.
- **A required client-name header (Entur `ET-Client-Name`)** — identifies apps without registration, but the name is self-declared, so limits would still be per IP.
- **Logging full parameters for a short time** — easier to debug a bad route, but stores precise locations linked to IP addresses.
