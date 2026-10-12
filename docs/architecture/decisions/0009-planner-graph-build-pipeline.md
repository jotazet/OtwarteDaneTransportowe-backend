# 0009. Build the planner graph in a maintenance window, test each feed alone and keep its last good version

- **Status:** Proposed
- **Date:** 2026-10-13

## Context

The planner graph (OTP) is built from the published GTFS feeds an admin selects, and has to be rebuilt as providers publish new schedules. Several feeds in the catalogue publish only 6–13 days ahead. OTP 2 cannot swap a graph in a running process, and one broken feed aborts the whole build, since OTP has no per-feed error isolation. The spike measured ~13–15 GB of memory to build all of Poland (cropped OSM) against ~7 GB to serve it, on a VPS with 11 GB RAM.

## Decision

- **Schedule.** Builds run on a schedule set in the admin panel: a django-celery-beat `PeriodicTask`, in UTC, default Wednesday and Sunday 01:40. Admins can also build on demand. One build at a time, serialised with a Postgres advisory lock.
- **Maintenance window.** A build first collects every feed's newest file (copied, or downloaded through `net_security` when ODT holds no copy). Then it stops the serving OTP to free memory, and the OTP-backed public endpoints answer `503 planner.unavailable`. After that it builds and starts OTP on the new graph.
- **Per-feed isolation.** Every feed whose file changed since its last good version, or every feed when the OTP image changed, is first built **alone** (transit only, no street map) in a network-less container.
  - If it fails, the feed's failure policy applies: `keep_last_good` (default) uses the snapshot of the last version that went into a served graph and marks the feed `stale`; `exclude` (or no snapshot) leaves it out.
  - The outcome, an error code and an excerpt of OTP's log are stored per feed in `GraphBuild.feed_results`.
- **Street graph.** OSM is filtered to routable ways and cropped to ~3 km cells around the included stops (osmium). The street graph is rebuilt only when the OSM file, the OTP image or the set of cells changes.
- **Activation.**
  - `planner_data/serving` is a symlink to `graphs/<build>`, mounted read-only into `otp`; Docker resolves it on each start.
  - Activation repoints the symlink, starts OTP, waits for `/otp/actuators/health` and checks that every included feed is served.
  - If anything fails, the previous graph is restored and started. The previous graph is kept on disk for a rollback from the panel.
- **Recovery.** A watchdog (every 5 minutes) fails builds whose heartbeat stopped (worker killed by a deploy or reboot) and restarts the active graph.
- **Realtime sources.** OTP polls the same links the public catalogue shows, at the endpoint's ODT interval.
  - Proxied endpoints are read from ODT's cached copy over the internal network. The requests carry a shared token (`PLANNER_INTERNAL_TOKEN`) that exempts them from public throttling.
  - Non-proxied endpoints are polled from the provider's URL directly. **This is a deliberate exception to "outbound HTTP to user-supplied URLs only through `net_security`"**, mitigated as follows:
    - every such URL passes `assert_safe_outbound_url` when the configuration is generated, and blocked ones are skipped (`planner.rt_url_blocked`);
    - OTP's network does not reach Postgres, Redis or the Docker proxy;
    - sources with credentials are always proxied, so OTP never holds secrets.
  - Remaining risks: DNS rebinding after the check, and no response size cap beyond OTP's memory limit.
  - Feeds served from a `stale` snapshot get no realtime updaters, because live data refers to the newest schedule.

## Consequences

- A broken new version of one feed no longer stops the planner from updating; the panel shows which feed, why, and that it is not the newest.
- The planner is down during builds. On the spike machine (swap in RAM) building all of Poland took about 13 minutes, plus 8 when the street graph is rebuilt and about 1 minute to start; disk swap on the VPS is slower and must be measured after deployment.
- Snapshots of the last good version are kept under `planner_data/snapshots/`, and they cannot be recreated, so back them up.
- The planner worker can create containers through `docker-socket-proxy`, the same accepted risk as the GTFS validator ([ADR 0005](0005-feed-validation.md)).

## Alternatives considered

- **Blue/green with two serving instances** — no downtime, but needs ~14 GB for two serving JVMs alone.
- **Build the whole graph on another machine and upload it** — no build load on the VPS, but every build would depend on a second machine.
- **Detect the culprit from OTP's log and retry** — OTP logs the feed it was reading, but a single-feed build gives a deterministic answer in seconds to minutes, as Digitransit does.
- **ODT relays realtime for non-proxied endpoints** — keeps OTP off the internet, but needs a second fetch loop for 29 of the 37 published endpoints.
