# Journey planner

ODT's journey planner routes over the published feeds with [OpenTripPlanner](https://docs.opentripplanner.org/en/v2.10.0/) (OTP) 2.10.0. This page collects the domain rules that the code depends on. Architecture: [ADR 0008](../architecture/decisions/0008-journey-planner-on-private-opentripplanner.md) and [ADR 0009](../architecture/decisions/0009-planner-graph-build-pipeline.md). Admin API: [planner-admin](../api/planner-admin.md).

## Feeds in the graph

- An admin includes a **published GTFS** feed (`PlannerFeed`). NeTEx and SIRI are not used yet: OTP is only thoroughly tested with the Nordic NeTEx profile, and the Polish profile has not been checked ([OTP: NeTEx and SIRI compatibility](https://docs.opentripplanner.org/en/v2.10.0/features-explained/Netex-Siri-Compatibility/)).
- **Feed ID.** Every feed gets an OTP `feedId` (`feed_id`, lowercase letters, digits, hyphens), and OTP prefixes every entity ID with it: `krk:2047691`. The planner API exposes those IDs, so the `feed_id` never changes once created.
  - When a provider re-submits a feed instead of updating it, the admin re-points the existing `PlannerFeed` to the new submission of the same organization, and IDs stay stable.
- **Duplicates.** The catalogue contains several versions of the same service: Warsaw in three feeds, Polregio and Koleje Małopolskie in two each. OTP namespaces each feed separately and does **not** deduplicate, so including two such feeds doubles every trip in results. The admin chooses one.
- **Service window.** The graph holds service from yesterday to two months ahead (`transitServiceStart -P1D`, `transitServiceEnd P2M`). On 2026-10-12, 7 of 44 published feeds ended within 14 days (Wrocław within 6), which is why builds run at least twice a week. For comparison, Google asks for at least 4 weeks of service from the day it receives a feed ([Google Transit Partners](https://support.google.com/transitpartners/answer/6394315)). OTP does not extend expiring feeds.

## Build outcomes per feed

| Outcome | Meaning |
| --- | --- |
| `fresh` | The newest file passed its single-feed test and is in the graph. |
| `stale` | The newest file failed (or could not be fetched); the last good version is in the graph instead (`failure_policy = keep_last_good`). It gets no realtime updaters, because live data refers to the newest schedule. |
| `excluded` | Not in the graph: unpublished, failed with `failure_policy = exclude`, or failed without any good version. |

Error codes stored per feed: `planner.feed_not_published`, `planner.feed_source_unavailable`, `planner.feed_invalid`, `planner.feed_no_service_in_period` (OTP found no trips in the service window). Realtime endpoints skipped by the configuration carry `planner.rt_url_blocked`.

## Realtime

- **Updaters.** OTP's GTFS-RT updaters poll every endpoint of the feed's published GTFS-RT submission at the endpoint's ODT `interval`: `trip_update` with `stop-time-updater`, `vehicle_position` with `vehicle-positions`, `service_alert` with `real-time-alerts`. OTP matches realtime to trips by `feedId` + `trip_id`.
- **Success rates.** Kraków applied 95.5% of trip updates in the spike; OTP rejects updates with errors such as `NEGATIVE_DWELL_TIME`, and its Prometheus endpoint reports them per feed (`batch_trip_updates_successful` / `_failed`).
- **Vehicle positions.** OTP drops positions whose trip it cannot match, so vehicles without a valid `trip_id` are not shown.
- **Blocks (brigades).** A block is the GTFS `block_id`: the sequence of trips one vehicle runs in a day. 21 of the 44 published feeds set it (e.g. Kraków `T/72-04`). Block IDs can contain `/`, which is why planner IDs are passed as query parameters, never path segments.

## Trip planning

- **Times are instants.** `plan/` takes `time` only with a UTC offset and answers in UTC. On 2026-10-25 the hour 02:00–03:00 happens twice in Poland, so a local time without an offset could mean two moments.
- **Service day.** GTFS stop times count from "noon minus 12 hours" of the service day, which is midnight except on the nights the clocks change ([GTFS reference, field types: Time](https://gtfs.org/documentation/schedule/reference/#field-types)). OTP gives departures as that service day plus seconds, and the API adds them into UTC instants, so a 02:30 departure on 2026-10-25 lands on the right one of the two 02:30s. The service date is read at the day's noon, which is on the right date even when its start is not.
- **Service date.** A leg's `service_date` is its GTFS service day, which for trips after midnight (stop times past `24:00:00`) is the previous calendar day. Together with `trip_id` it identifies the run.
- **Walking speed presets.** `slow`, `normal` and `fast` (1.0, 1.33 and 1.7 m/s) rather than any number: OTP caches the transfers between stops per set of street preferences, so a few fixed values keep that cache bounded.
- **Wheelchair access.** With `wheelchair=true`, OTP uses only stops and trips explicitly marked accessible (`wheelchair_boarding`, `wheelchair_accessible` = 1; OTP 2.10 defaults `onlyConsiderAccessible: true` for stops and trips). In the 2026-10-12 audit, only 2 of 44 feeds (Warsaw) marked most stops, and 11 marked most trips.
- **Realtime in plans.** By default plans use OTP's realtime timetable (delays, cancellations); `realtime=false` plans on the published timetable. Only `fresh` feeds have realtime.
- **No walking directions.** OTP's step-by-step instructions name unnamed paths in English ("path", "sidewalk"), which would be backend prose; legs carry only the geometry. OTP's labels "Origin" and "Destination" for requested points are dropped for the same reason.
- **Routing errors** are OTP's `RoutingErrorCode` values, lowercased and prefixed (`planner.location_not_found`). See [the API reference](../api/planner.md#routing-errors).

## Search

- **Source.** After a graph goes live, the planner worker copies its operators, lines and stops from OTP into Postgres (`planner/search_index.py`), replacing the previous copy in one transaction. Search therefore shows exactly what the planner routes on and keeps working while OTP is stopped for a build. If OTP does not answer, the previous copy stays and the watchdog retries every 5 minutes. On the Małopolska test graph (7,773 stops) the copy took 1 s.
- **Names.** Compared after lowercasing, removing diacritics (`ł` by hand, as Unicode does not decompose it) and punctuation, with PostgreSQL trigram word similarity (`pg_trgm`).
- **Places.** Feeds list every pole separately: in the 2026-10-12 audit only 13 of 44 feeds use GTFS `parent_station`, mostly railways. Search groups poles with the same name within 400 m of each other into one place (single linkage, so a place can be longer than 400 m). Places with the same name farther apart (`Kościół` in many villages) stay separate; `near` ranks the closest first.
- **Unserved stops** (no line calls there in the graph) are not indexed.
- **Search has no town.** GTFS stops have no locality field, so two places with the same name are told apart only by position (`lat`, `lon`, `distance_m`) and their lines.
- **Organizations.** Operators (GTFS agencies) are linked to the ODT organization that publishes their feed. OTP can filter trips only by agency or route, so a plan filter on an organization becomes a filter on all its operators.
- **Lines.** Many feeds leave `route_long_name` empty (Kraków), so such lines are found by number only.

## Street map

- **Source and cropping.** The street graph comes from an OSM extract (Geofabrik). Before building, it is filtered to the ways OTP routes on (osmium tag filter from [OTP's docs](https://docs.opentripplanner.org/en/v2.10.0/Preparing-OSM/)) and cropped to ~3 km grid cells around the included stops, plus one cell of margin. In the spike the stops covered about 40% of Poland.
- **What the crop does not change.** In the spike, filtering did not change how many stops OTP could link to streets.
- **What it does change.** A trip that starts or ends more than ~3 km from any included stop finds no street to start on.
