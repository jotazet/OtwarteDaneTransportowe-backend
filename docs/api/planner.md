# Journey planner API (public)

The public API of ODT's journey planner, for ODT's website and for anyone building on Polish public transport data. Base URL: `/api/planner/v1/`. Interactive reference: `GET /api/planner/v1/docs/` (ReDoc); OpenAPI document: `GET /api/planner/v1/schema/`.

The planner routes over the feeds ODT publishes with [OpenTripPlanner](https://www.opentripplanner.org/); how it works and its limits are described in [journey planner](../domain/journey-planner.md). Admin routes: [planner-admin](planner-admin.md).

> Status: being built. Available now: `status/`. Trip planning, stops, lines, departures, timetables, vehicles and blocks follow.

## Access

- **No registration needed.** Every endpoint works anonymously, with limits per IP address (per `/64` for IPv6).
- **API key.** For higher limits, ask ODT for a key and send it in the `X-API-Key` header. Keys are personal: do not embed one in a public website, where anyone can read it; browser apps should call the API anonymously.
- **CORS.** Any website may call `/api/planner/v1/` from the browser (no cookies or credentials are used). `X-API-Key` is an allowed header and `Retry-After` is readable.

## Limits

Requests per minute, counted separately for three groups of endpoints:

| Group | Endpoints |
| --- | --- |
| `plan` | trip planning |
| `realtime` | answers computed by the routing engine: departures, timetables, trips, vehicles, alerts |
| `lookup` | stop, line and operator search, status |

Each group has a limit per client (an IP address, an IPv6 `/64`, or an API key) and a limit for all clients together. The numbers are set by ODT and may change; a client over a limit gets:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 23

{"code": "planner.rate_limited", "details": {"scope": "client"}}
```

`scope` is `client` (your limit) or `global` (the planner is busy for everyone). Wait `Retry-After` seconds before retrying.

## Errors

Every error is a stable code with details; codes never change once published, and texts for users are the client's job.

```json
{"code": "planner.invalid_request", "details": {"fields": {"time": ["invalid"]}}}
```

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `planner.invalid_request` | A parameter is invalid; `details.fields` maps each to its error codes. |
| 401 | `planner.invalid_api_key` | `X-API-Key` is unknown or revoked. Send no key to use anonymous access. After 10 wrong keys in a minute, further key attempts get `429`. |
| 403 | `planner.client_banned` | Your address is blocked. Contact ODT. |
| 404 | `planner.not_found` | No such endpoint or object. |
| 405 | `planner.method_not_allowed` | Only `GET` is supported. |
| 429 | `planner.rate_limited` | Over a limit; see [Limits](#limits). |
| 503 | `planner.disabled` | The planner API is switched off. |
| 503 | `planner.unavailable` | The routing engine is not answering; `details.reason` is `maintenance` while the timetable data is being rebuilt (usually at night, see `Retry-After`). Stop and line search still answer. |

## Identifiers

Stops, lines, trips and blocks have IDs of the form `<feed_id>:<id in the feed>`, e.g. `krk:2047691`. They stay the same as long as the transport organization keeps its own IDs. IDs can contain `/` or `:`, so they are always passed as query parameters, never in the path.

## Attribution and licences

Routes are computed on [OpenStreetMap](https://www.openstreetmap.org/copyright) data (ODbL): applications that show them must credit OpenStreetMap. Each feed's licence is listed in `status/`; follow the licence of the data you display.

## `GET /api/planner/v1/status/`

Whether the planner answers now, which graph it serves and from which feeds. It also answers while the API is switched off or the data is being rebuilt. Limit group: `lookup`.

```json
{
  "available": true,
  "reason": null,
  "graph": { "built_at": "2026-10-14T01:56:12Z", "otp_version": "2.10.0" },
  "feeds": [
    {
      "feed_id": "krk",
      "organization": { "id": 5, "name": "Zarząd Transportu Publicznego", "region": "Kraków" },
      "license": "CC-BY-4.0",
      "outcome": "fresh",
      "version_at": "2026-10-13T03:00:12Z"
    }
  ],
  "attribution": [
    { "source": "openstreetmap", "license": "ODbL-1.0", "url": "https://www.openstreetmap.org/copyright" }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `available` | `false` when the API is off, the data is being rebuilt, or no graph exists yet. |
| `reason` | `disabled`, `maintenance`, `no_graph` or `null`. |
| `feeds[].outcome` | `fresh`: the newest published version. `stale`: an older version, because the newest could not be used; realtime data is not applied to it. |
| `feeds[].version_at` | When that version of the feed was obtained. |

## Versioning

Breaking changes get a new path (`/api/planner/v2/`); `v1` keeps working for a transition period announced in the [changelog](changelog.md). Fields may be added to responses at any time.
