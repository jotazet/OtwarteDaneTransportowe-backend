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

## Street map

- **Source and cropping.** The street graph comes from an OSM extract (Geofabrik). Before building, it is filtered to the ways OTP routes on (osmium tag filter from [OTP's docs](https://docs.opentripplanner.org/en/v2.10.0/Preparing-OSM/)) and cropped to ~3 km grid cells around the included stops, plus one cell of margin. In the spike the stops covered about 40% of Poland.
- **What the crop does not change.** In the spike, filtering did not change how many stops OTP could link to streets.
- **What it does change.** A trip that starts or ends more than ~3 km from any included stop finds no street to start on.
