# Journey planner API (public)

The public API of ODT's journey planner, for ODT's website and for anyone building on Polish public transport data. Base URL: `/api/planner/v1/`. Interactive reference: `GET /api/planner/v1/docs/` (ReDoc); OpenAPI document: `GET /api/planner/v1/schema/`.

The planner routes over the feeds ODT publishes with [OpenTripPlanner](https://www.opentripplanner.org/); how it works and its limits are described in [journey planner](../domain/journey-planner.md). Admin routes: [planner-admin](planner-admin.md).

> Status: being built. Available now: `status/`, `plan/`, stop, line and operator search. Departures, timetables, vehicles and blocks follow.

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
| 503 | `planner.unavailable` | The routing engine is not answering. `details.reason` is `maintenance` while the timetable data is being rebuilt (usually at night, see `Retry-After`; stop and line search still answer) or `engine` when it failed to answer. |
| 503 | `planner.busy` | Too many requests are being routed at this moment. Retry after `Retry-After` seconds. |

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

## `GET /api/planner/v1/plan/`

Trips by public transport and walking between two places, with realtime delays where operators publish them. Limit group: `plan`.

```http
GET /api/planner/v1/plan/?from=50.0617,19.9373&to=krk:2047513&time=2026-10-14T08:00:00%2B02:00&modes=tram&modes=bus
```

### Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `from`, `to` | required | `lat,lon` (WGS 84, e.g. `50.0617,19.9373`) or a stop or station ID (`krk:2047513`). Addresses are not searched. |
| `time` | now | ISO 8601 **with a UTC offset** (`2026-10-14T08:00:00+02:00` or `…Z`); a time without one is rejected, because a local time is ambiguous on the night the clocks change. Encode `+` as `%2B`. |
| `arrive_by` | `false` | `true`: `time` is the latest arrival instead of the earliest departure; needs `time`. |
| `modes` | all | Transit modes to use; repeat the parameter (`modes=tram&modes=bus`). `bus`, `coach`, `tram`, `trolleybus`, `subway`, `rail`, `ferry`, `funicular`, `cable_car`, `gondola`, `monorail`. |
| `wheelchair` | `false` | Only stops and trips the feed marks as wheelchair-accessible; those without the information are not used. Few feeds publish it (in October 2026, for most stops, only Warsaw's), so elsewhere the answer is usually `planner.no_stops_in_range`. |
| `walk_speed` | `normal` | `slow` (1.0 m/s), `normal` (1.33 m/s) or `fast` (1.7 m/s). |
| `max_transfers` | engine default | 0–8. |
| `min_transfer_minutes` | `2` | 0–30: extra time added to every transfer. |
| `via` | — | Up to 2 stop IDs the trip must pass through, in order; repeat the parameter. |
| `agencies`, `routes` | — | Use only these operators or lines (IDs, up to 20 each, repeated). Several include filters add up: a trip qualifies if it matches any of them. |
| `organizations` | — | Use only the operators whose data these ODT organizations publish (organization IDs from `agencies/` or `status/`, up to 20). |
| `exclude_agencies`, `exclude_routes` | — | Never use these (IDs, up to 20 each); they win over the include filters. |
| `exclude_organizations` | — | Never use the operators of these ODT organizations. |
| `realtime` | `true` | `false`: plan on the timetable only, ignoring delays and cancellations. |
| `num_itineraries` | `5` | 1–10 itineraries per page. |
| `cursor` | — | `next_cursor` or `previous_cursor` of an earlier answer, with the same other parameters: later or earlier departures. |
| `stops` | `false` | `true`: list the stops of every transit leg. |
| `geometry` | `false` | `true`: return every leg's path in `geometry`; otherwise it is `null`. Paths are most of the answer's size. |

Operator, line and stop IDs come from the other planner endpoints or from the feeds themselves (`<feed_id>:<agency_id>`, `<feed_id>:<route_id>`, `<feed_id>:<stop_id>`).

Invalid parameters return `400 planner.invalid_request` with the codes in `details.fields` (plus DRF's generic codes such as `required`, `invalid`, `invalid_choice`, `max_value`, `max_length`):

| Code | Parameter |
| --- | --- |
| `planner.invalid_location` | `from`, `to`: neither coordinates in range nor a `<feed_id>:<id>` ID. |
| `planner.invalid_id` | `via`, `agencies`, `routes`, `exclude_*`: not a `<feed_id>:<id>` ID (reported per position). |
| `planner.organization_not_found` | `organizations`, `exclude_organizations`: an organization with no operator in the planner. |
| `planner.stop_not_found` | `via`: no such stop or station. |
| `planner.time_without_offset` | `time` has no UTC offset. |
| `planner.arrive_by_without_time` | `arrive_by=true` without `time`. |
| `planner.invalid_cursor` | `cursor` is not one this API returned. |

An unknown `from` or `to` stop is not a parameter error: the answer is `200` with the routing error `planner.location_not_found`.

### Response

```json
{
  "itineraries": [
    {
      "start": "2026-10-14T06:03:30Z",
      "end": "2026-10-14T06:40:24Z",
      "duration_s": 2214,
      "walk_distance_m": 1178,
      "transfers": 0,
      "legs": [
        {
          "mode": "walk",
          "from": { "name": null, "lat": 50.0617, "lon": 19.9373, "stop_id": null, "platform": null },
          "to": { "name": "Starowiślna", "lat": 50.057227, "lon": 19.946086, "stop_id": "krk:2047470", "platform": null },
          "start": { "scheduled": "2026-10-14T06:03:30Z", "estimated": null, "delay_s": null },
          "end": { "scheduled": "2026-10-14T06:15:00Z", "estimated": null, "delay_s": null },
          "duration_s": 690,
          "distance_m": 874,
          "route": null,
          "agency": null,
          "trip_id": null,
          "service_date": null,
          "headsign": null,
          "realtime": false,
          "geometry": null,
          "alerts": [],
          "stops": null
        },
        {
          "mode": "tram",
          "from": { "name": "Starowiślna", "lat": 50.057227, "lon": 19.946086, "stop_id": "krk:2047470", "platform": null },
          "to": { "name": "Os. Kolorowe", "lat": 50.072198, "lon": 20.0298, "stop_id": "krk:2047513", "platform": null },
          "start": { "scheduled": "2026-10-14T06:15:00Z", "estimated": "2026-10-14T06:16:30Z", "delay_s": 90 },
          "end": { "scheduled": "2026-10-14T06:36:00Z", "estimated": "2026-10-14T06:37:00Z", "delay_s": 60 },
          "duration_s": 1260,
          "distance_m": 7300,
          "route": { "id": "krk:352529", "short_name": "70", "long_name": null, "color": null, "text_color": null },
          "agency": { "id": "krk:302", "name": "Kraków" },
          "trip_id": "krk:53620411_256159",
          "service_date": "2026-10-14",
          "headsign": "Pleszów",
          "realtime": true,
          "geometry": null,
          "alerts": [],
          "stops": null
        }
      ]
    }
  ],
  "next_cursor": "n:MnxORVhUX1BBR0V8…",
  "previous_cursor": "p:MnxQUkVWSU9VU19QQUdF…",
  "routing_errors": []
}
```

| Field | Meaning |
| --- | --- |
| `start`, `end`, `*.scheduled`, `*.estimated` | UTC instants; convert to local time for display. |
| `legs[].mode` | `walk` or a transit mode from `modes`. |
| `legs[].from.name`, `legs[].to.name` | Stop name; `null` for the coordinates you sent. |
| `legs[].start.estimated`, `delay_s` | The realtime prediction and its difference from the timetable in seconds (negative: early); `null` without realtime data. `realtime` is `true` when the leg has a prediction. |
| `legs[].route.color`, `text_color` | Hex colour without `#`, as in the feed; often `null`. |
| `legs[].trip_id`, `service_date` | Identify the vehicle's trip on that day (`service_date` is the timetable day, which for trips after midnight can be the day before). |
| `legs[].geometry` | With `geometry=true`: the leg's path as an [encoded polyline](https://developers.google.com/maps/documentation/utilities/polylinealgorithm) (precision 5); otherwise `null`. |
| `legs[].alerts` | Service alerts published by the operator for that leg. `header` and `description` are the operator's own texts, usually in Polish; `effect` and `severity` are the lowercase GTFS-RT values (`detour`, `no_service`, …; `info`, `warning`, `severe`). |
| `legs[].stops` | With `stops=true`: the stops of a transit leg from boarding to alighting (walking legs: `[]`); otherwise `null`. |
| `next_cursor`, `previous_cursor` | Pass as `cursor` for the next or previous page. |
| `routing_errors` | Why no (or no better) trip was found; see below. The answer is still `200`. |

There are no turn-by-turn walking directions: the engine names unnamed paths in English, and the API returns no prose.

### Routing errors

| Code | Meaning |
| --- | --- |
| `planner.location_not_found` | `from` or `to` is an unknown stop ID, or a point with no street or stop near it (the planner's map covers only the surroundings of its stops). |
| `planner.outside_bounds` | A point is outside the area the planner covers. |
| `planner.no_stops_in_range` | No stop can be reached on foot from `from` or `to`. |
| `planner.no_transit_connection` | No public transport connects the places on that day or the next. |
| `planner.no_transit_connection_in_search_window` | A connection exists, but outside the time searched; follow `next_cursor` or `previous_cursor`. |
| `planner.outside_service_period` | `time` is outside the dates the timetables cover (the planner holds about two months ahead). |
| `planner.walking_better_than_transit` | Walking all the way is easier than any transit trip, so transit trips were left out; a walking itinerary may be returned. |
| `planner.no_direct_mode_connection` | No walking route was found and no transit was searched. |

New codes may be added; show a generic message for unknown ones.

## Stops, lines and operators

These answer from ODT's own index of the served data, so they keep working while the timetables are being rebuilt. Limit group: `lookup`. Lists use the standard page envelope `{count, next, previous, results}` with `page` and `page_size`.

**Names.** Search ignores case, Polish letters and punctuation: `lodz` finds `Łódź Kaliska`, `rondo mog` finds `Rondo Mogilskie`. Close misspellings match too, ranked below exact ones.

**Stops** here are those at least one line calls at; others cannot start or end a trip and are left out.

**Places.** Most feeds publish every pole (each direction, each platform) as a separate stop. Stop search and `stops/nearby/` therefore return **places**: poles with the same name less than 400 m apart, with every pole in `stops`. To plan a trip from a place, use its `lat,lon` or one of its stop IDs.

**Filters** (all list endpoints except `agencies/`): `modes` (repeat; values as in `plan/`), `agencies` (operator IDs) and `organizations` (ODT organization IDs); operators and organizations add up.

### `GET /api/planner/v1/stops/search/`

| Parameter | Meaning |
| --- | --- |
| `q` | Required, at most 100 characters with at least 3 letters or digits (shorter fragments match too much to rank). |
| `near` | `lat,lon`: places near it rank higher (and get `distance_m`). |
| filters | See above. |

Best matches first: name similarity, then names starting with `q`, places with more lines and, with `near`, nearer places. Default page size 10, at most 50 (each result carries its poles and lines).

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "name": "Rondo Mogilskie",
      "lat": 50.0651,
      "lon": 19.9585,
      "modes": ["bus", "tram"],
      "distance_m": null,
      "routes": [
        { "id": "krk:352529", "short_name": "70", "long_name": null, "mode": "tram", "color": null, "text_color": null, "agency": { "id": "krk:302", "name": "Kraków" } }
      ],
      "stops": [
        { "id": "krk:2047520", "name": "Rondo Mogilskie", "code": "121-01", "platform": null, "lat": 50.065, "lon": 19.958, "mode": "tram", "station_id": null }
      ]
    }
  ]
}
```

`routes` are sorted by mode, then number. `stops[].code` is the code shown to passengers when the feed has one; `station_id` is the GTFS parent station (mostly railway).

### `GET /api/planner/v1/stops/nearby/`

Places within `radius` metres (default 500, at most 2000) of `near` (`lat,lon`, required), nearest first, with `distance_m`. Same result shape and paging as search; filters apply.

### `GET /api/planner/v1/stops/`

Every single pole inside `bbox`, for drawing on a map.

| Parameter | Meaning |
| --- | --- |
| `bbox` | Required: `min_lon,min_lat,max_lon,max_lat` (GeoJSON order, as map libraries give bounds), at most 0.5° on each side; larger areas get `planner.bbox_too_large`, show stops only when zoomed in. |
| filters | See above. |

Results are `stops[]` objects as above, by ID. Operator filters apply per pole: only poles where a line of those operators calls. Default page size 500, at most 2000, so that one request usually covers the view.

### `GET /api/planner/v1/stop/?id=krk:2047470`

One stop: the fields above plus `routes` (lines calling there) and `group` (`name`, `lat`, `lon` and every pole of the same place). An unknown ID returns `404 planner.stop_not_found`.

### `GET /api/planner/v1/routes/`

Lines (GTFS routes) by number or name.

| Parameter | Meaning |
| --- | --- |
| `q` | Optional, at least one letter or digit. An exact number comes first (`70`), then numbers starting with it (`704`), then names. Without `q`, every line matching the filters. |
| `near` | `lat,lon`: lines whose stops are centred nearer come first (useful when many towns have a line `1`). |
| filters | See above. |

Results: `id`, `short_name`, `long_name`, `mode`, `color`, `text_color`, `agency` `{id, name}`. Lines without `q` are in timetable order: numbers numerically (`2`, `10`, `52`), then letters. Many feeds leave `long_name` empty; then a line is found by its number only.

### `GET /api/planner/v1/agencies/`

Every operator in the planner, by name: `id`, `name`, `url`, `feed_id`, `organization` (the ODT organization that publishes the feed: `id`, `name`, `region`; `null` if unknown) and `route_count`. Filter with `organizations` (an organization without operators in the planner is `planner.organization_not_found`). Use these IDs for the `agencies` and `organizations` filters.

### Lookup errors

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `planner.invalid_request` | With field codes: `planner.invalid_location` (`near`), `planner.invalid_bbox`, `planner.bbox_too_large`, `planner.invalid_id`, `planner.organization_not_found`, or DRF's generic ones. |
| 404 | `planner.stop_not_found` | `stop/`: no stop with that ID in the planner. |

## Versioning

Breaking changes get a new path (`/api/planner/v2/`); `v1` keeps working for a transition period announced in the [changelog](changelog.md). Fields may be added to responses at any time.
