# 0011. Resolve the client IP at the reverse proxy and forward it from server-side rendering

- **Status:** Accepted (the `anon` throttle it protected was removed in [0012](0012-rate-limit-only-scoped-views-and-the-planner.md); the client-IP chain still applies)
- **Date:** 2026-10-13

## Context

Anonymous clients are told apart by IP: DRF's `anon` throttle (`THROTTLE_ANON`, 60/min) and the blog reaction limits (one reaction per post and IP, a daily cap per IP). `get_client_ip()` trusts `X-Forwarded-For` only from `TRUSTED_PROXY_CIDRS`.

Production, as checked on 2026-10-13: visitors → Cloudflare → Caddy on the VPS → the `web` and `odt-frontend` containers. Two things hid the visitor's IP:

- **Cloudflare.** Caddy had no `trusted_proxies`, so it ignored the incoming `X-Forwarded-For` and sent the address of the Cloudflare edge server instead. All 12 stored blog reactions (7 distinct addresses) carried Cloudflare addresses: everyone routed through the same edge server shared one throttle bucket and one reaction per post.
- **Server-side rendering.** The frontend renders public pages on the server and fetched the API anonymously through its public URL, without the visitor's IP. Measured on a production build: 0–2 API calls per page, plus 3 per desktop page view from the full prefetch of the top menu. Once Cloudflare is trusted, all of this traffic would arrive from the VPS's own address and share one `anon` bucket: about 12–20 desktop page views per minute for the whole site, or one client sending about a request per second, and every visitor gets error pages (lists render an error with HTTP 200, detail pages HTTP 500).

## Decision

1. **Caddy resolves the client IP once.** It trusts Cloudflare's published ranges and the Docker networks (`trusted_proxies`), reads `CF-Connecting-IP` from Cloudflare and `X-Forwarded-For` from the frontend (`client_ip_headers`), and sends `X-Forwarded-For: {client_ip}`, a single value, to both the API and the frontend. `TRUSTED_PROXY_CIDRS` is unchanged: only Caddy connects to gunicorn.
2. **Server-side rendering reaches Caddy directly.** The frontend container resolves the public API host to this machine (`API_HOSTNAME` → `extra_hosts: host-gateway`), so its calls skip Cloudflare but keep the public URL, `Host` and TLS. They forward the visitor's IP: the rightmost `X-Forwarded-For` entry the frontend received from Caddy. Cached (ISR) requests such as the sitemap forward none, because their response is shared.
3. **The frontend's top menu uses the default prefetch**, so a page view no longer renders the data catalogue, the cases list and the blog list in advance.

## Consequences

- Every visitor has their own `anon` bucket, whether they call the API from the browser or through a page rendered on the server; a misbehaving client throttles only itself. A crawler rendering many pages from one address can still hit `429`.
- Blog reaction limits apply per visitor again. Reactions stored before the change are tied to Cloudflare addresses and stay as they are.
- Deploy the frontend, then reload Caddy right after it: in between, all server-side rendering shares one bucket (under the container's address before the Caddy change, under the VPS's address through Cloudflare after it).
- The Cloudflare ranges in the Caddyfile must be updated when Cloudflare changes them (<https://www.cloudflare.com/ips/>).
- Any container on the machine can claim a client IP to Caddy (`172.16.0.0/12` is trusted). They are all ours; the worst case is evading per-IP limits.
- The frontend trusts `X-Forwarded-For` from whoever connects to it, so it must be reachable only through Caddy (`FRONTEND_BIND`, now `127.0.0.1` by default).
- Identical GETs within one server render share a request (`cache()` around `getJson`), because the timeout's `AbortSignal` opts out of Next's fetch deduplication.

## Alternatives considered

- **ISR caching of public pages** — fewer calls for popular URLs, but requests for many distinct URLs (other ids, `?page=`) still miss the cache and share one bucket, and public data goes stale after edits.
- **A separate, higher limit for the frontend server** (shared secret header) — still one shared bucket, only larger; protection per visitor would have to move to the reverse proxy.
- **Raising `THROTTLE_ANON`** — raises the limit for every API client and keeps the shared bucket.
- **Server-side rendering over the backend's Docker network** (`http://odt-web:8000`) — the API builds absolute URLs from the request's host, so feed download links in the public catalogue would point at `http://odt-web:8000`; and the internet-facing frontend container would share a network with `docker-socket-proxy`, Redis and PostgreSQL.
- **Server-side rendering through Cloudflare with the visitor's IP** — Caddy would also have to trust the VPS's address and parse `X-Forwarded-For` from the right instead of using `CF-Connecting-IP`; every render takes a round trip through the CDN, and Cloudflare's bot protection may limit it.
- **Leave it** — limits stay per Cloudflare edge server and reactions keep overwriting each other.
