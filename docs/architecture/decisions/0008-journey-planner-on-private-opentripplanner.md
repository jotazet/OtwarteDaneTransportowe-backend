# 0008. Run the journey planner on a private OpenTripPlanner behind ODT's own API

- **Status:** Proposed
- **Date:** 2026-10-13

## Context

ODT wants a journey planner over the feeds it publishes: trip planning, realtime departures, vehicle positions, stop and line search, and a public, documented API for developers, plus an admin panel to run it. Building a router is out of the question; OpenTripPlanner (OTP) is the open-source standard that Entur (Norway) and Digitransit (Finland) run in production.

OTP's own APIs are GraphQL schemas tied to OTP versions, with no paging on some top-level queries and no built-in access control, rate limits or logs. Exposing them would tie ODT's public contract to OTP releases and let any client send arbitrarily expensive queries.

A spike on 2026-10-12 (all 44 published GTFS feeds, OSM for Poland, OTP 2.10.0) measured what it costs. The production VPS has 11 GB RAM and 15 GB swap.

## Decision

- OTP runs as a **private** container (`otp`, compose profile `planner`). It has no published port and is reachable only on the internal `planner` network.
- The pinned image is `opentripplanner/opentripplanner:2.10.0`; a graph file only loads in the OTP version that built it. Only the GTFS GraphQL API and the actuator (health, metrics) are enabled. Transmodel, the debug UI and costly assertions are off.
- ODT exposes its **own REST API** (`/api/planner/v1/`, later PRs) with fixed parameters. Each endpoint translates its parameters into one of a set of fixed GraphQL documents; client input travels only in GraphQL variables. GraphQL is never passed through.
- The admin API lives under `/api/planner/admin/` and is restricted to the `Admin` group (`RequiresAdminGroup`).
- Search (stops, lines, operators) is served from a Postgres index copied from OTP after each graph build, with names folded to ASCII and matched with `pg_trgm`. OTP's name search is prefix-only and does not fold Polish diacritics, and a Postgres copy keeps search up while OTP is stopped for a build.
- OTP serves trip planning, departures, timetables, vehicle positions and blocks (brigades); its GTFS-RT updaters feed it the realtime data.
- Memory follows the spike: the graph is built from OSM cropped to about 3 km around the stops of the included feeds, and served with a 6 GB heap (~7 GB container).

## Consequences

- ODT owns its contract: OTP upgrades change internal queries, not the public API. Each upgrade still needs every feed re-tested and a new street graph, because graphs are version-bound.
- The planner covers walking and transit near stops; a trip starting more than ~3 km from any included stop returns an error instead of a route.
- One more long-running JVM on the VPS: ~7 GB while serving. Builds need ~13–15 GB and run in a maintenance window ([ADR 0009](0009-planner-graph-build-pipeline.md)).

## Alternatives considered

- **Expose OTP's GraphQL directly (Entur, Digitransit)** — fastest, but the public contract would follow OTP's schema and there would be no per-client limits or logs without a gateway that understands GraphQL.
- **MOTIS (Transitous)** — a strong REST router, but smaller community, and ODT's team and the spike are on OTP.
- **Whole-Poland OSM without cropping** — needs ~8 GB to serve and leaves ~0.5 GB headroom on the VPS; the cropped graph serves in ~7 GB with better latency under load.
