# 0018. Hold back feed versions that have not started yet

- **Status:** Proposed
- **Date:** 2026-11-27

## Context

A planner build takes the newest file of every feed ([ADR 0009](0009-planner-graph-build-pipeline.md)). Some providers publish the next version of their schedule days before it starts, at the same URL. ZTM Poznań's `getGTFSFile` returned `20261128_20261213.zip` from 2026-11-26 (`feed_info` and `calendar.txt`: 2026-11-28 to 2026-12-13). The build of 2026-11-27 02:19 UTC put only that file in the graph, so on 2026-11-27 Poznań had no departures, and `plan/` offered only walking. A user of the public API reported it. The version running that day, `20261124_20261205.zip`, is on ZTM's list of files (`/otwarte-dane/gtfsfiles/`), and ZTM's versions overlap: each new one starts before the previous one ends.

The feed passed its test and was reported `fresh` in `status/`, so nothing showed the gap.

ZTM's trip updates matched the running version, not the next one: 413 of 424 trip IDs on 2026-11-27 were in `20261124_20261205`, 292 in `20261128_20261213`.

## Decision

When the newest file's service starts after today (the date in Poland at the start of the build) and the feed's last good version runs today, the build serves the last good version and marks the feed **`pending`**, with the code `planner.feed_not_started`. Otherwise it serves the newest file, also when there is no last good version or that version no longer runs today.

- The service period comes from `calendar.txt` (rows running on some weekday) and the dates `calendar_dates.txt` adds, not from the optional `feed_info.txt`.
- This is not a failure, so it applies with either `failure_policy`.
- A `pending` feed gets realtime updaters, unlike `stale`, because its realtime describes the schedule running today.
- The newest file is not tested and not kept as a snapshot. It is fetched again, and tested, by the first build after it starts.

## Consequences

- Feeds published ahead keep their service in the planner until the new version starts. It applies to every planner feed.
- The new version reaches the graph only with the first build on or after its start date. With builds every other day, a feed whose last good version ends before the new one starts can lose a day of service; Poznań's versions overlap, so it does not. Daily builds close that gap without more code.
- On the first day of a new version, night trips of the previous service day (stop times past `24:00:00`) from the old version are gone.
- `status/` and build results have a new outcome, `pending`, and a new code; the website needs a label and translations.
- A feed without a last good version and without service today still shows `fresh`.

## Alternatives considered

- **Merge both versions** (the old one until the day before the new one starts, the new one from then) — always right, but trip IDs repeat across versions and would have to be renamed, which breaks realtime matching for one of them, and clipping calendars is error-prone.
- **Pick the running file from ZTM's list** — fixes only Poznań, and depends on the format of their page.
- **Treat it as `stale`** — no new outcome, but `stale` gets no realtime, and Poznań's realtime matches the held-back version.
