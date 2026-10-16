# 0012. Rate limit only login, feed downloads and the planner API

- **Status:** Accepted
- **Date:** 2026-10-16

## Context

Every API view had DRF's `anon` (60/min per client IP) and `user` (240/min) throttles. The website renders public pages (blog, data catalogue, cases) on the server and calls the API for its visitors, so these calls only get separate per-visitor buckets when the whole client-IP chain of [ADR 0011](0011-client-ip-behind-cloudflare-and-ssr.md) is in place: Caddy trusting Cloudflare, and the frontend forwarding the visitor's IP. If any part of that chain is missing, all visitors share one bucket, and public pages show a load error once the whole site passes 60 API calls a minute.

On 2026-10-16, after `main` (with per-client-IP throttles, PR #18) was deployed, public pages showed "Nie udało się wczytać tej strony" while the planner API worked. That API has its own limits ([ADR 0010](0010-public-planner-api-access.md)). Which part of the chain was missing was not checked on the server.

The blog, catalogue and cases endpoints are cheap, paginated reads. The limits that protect something are the ones on login (password guessing), feed downloads (bandwidth) and the planner (OpenTripPlanner capacity).

## Decision

Remove the global `anon` and `user` throttles. Only views with a `throttle_scope` are limited (`login`, `feed_download`), still per client IP, and the public planner API keeps its own per-key, per-IP and global limits.

## Consequences

- Public pages no longer fail because of a shared bucket, whatever the client-IP setup.
- Blog, catalogue and cases reads have no rate limit in Django; abusive traffic must be stopped at Cloudflare or Caddy. The `THROTTLE_ANON` and `THROTTLE_USER` variables are ignored.
- ADR 0011 is still needed: without it the login, feed download and planner limits, the planner's bans and the blog reaction limits still see one shared client.
- A new expensive endpoint outside the planner needs its own `throttle_scope`.

## Alternatives considered

- **Raise `THROTTLE_ANON`**: keeps a shared bucket that a single client can still exhaust for everyone.
- **Only complete ADR 0011 on the server**: per-visitor buckets work, but any break in the IP chain takes the public website down again, and a crawler rendering many pages from one address still gets `429`.
