# 0005. Validate static GTFS with the MobilityData validator and realtime feeds in process

- **Status:** Accepted (recorded retroactively)
- **Date:** 2026-10-12

## Context

A feed is verified before an admin confirms it (stage 2 of the [workflow](../../domain/feed-workflow.md)). Static GTFS needs a full validation of the schedule; the reference tool is the MobilityData GTFS validator, which is a Java application. Realtime bodies must at least parse before ODT caches and serves them. Until commit `4f4014d` a separate GTFS-RT validator service did that check; it was removed in favour of in-process validation.

## Decision

- **Static GTFS:** `validate_gtfs_feed` (queue `feeds`) runs the `ghcr.io/mobilitydata/gtfs-validator` container through the Docker API. Only the `docker-socket-proxy` service mounts the Docker socket; the validator worker reaches it over TCP with everything except containers, images and POST denied. The report is stored as a `FeedValidationReport`, and rejection causes list validator codes as `code: count`.
- **Realtime:** fetched GTFS-RT bodies are parsed with `gtfs-realtime-bindings` in the worker (`data_manager/validators.py`) before they are cached; SIRI and GBFS bodies get content sniffing.
- A failed validation of an already published feed does not unpublish it.

## Consequences

- The validator worker can still create containers, so a compromised worker stays dangerous; the proxy shrinks the surface but does not remove it (see the [deployment guide](../../deployment.md)). Rootless Docker or a separate validator service would close it.
- The validator image is pulled as `:latest`, so results can change without a code change. Pinning a version is a follow-up.
- NeTEx has no automated validation yet.

## Alternatives considered

- **Hosted validation APIs** — sends providers' data to a third party and adds an external dependency to publishing.
- **A separate GTFS-RT validator service** — removed: one more service to run for a check the worker can do itself.
