# 0004. Run fetching and validation in Celery with Redis

- **Status:** Accepted (recorded retroactively)
- **Date:** 2026-10-12

## Context

ODT downloads proxied static feeds on a daily schedule, polls realtime endpoints every few seconds, and validates GTFS files with a validator that can run for minutes. None of this may block an HTTP request, and periodic work must survive restarts.

## Decision

- **Celery** with **Redis** as broker and result backend. Redis also backs Django's cache, so locks and throttling are shared between web and worker processes.
- **Beat** with `django-celery-beat`'s database scheduler: `refresh_static_feeds` every minute and `bootstrap_realtime_tasks` every 5 minutes, both on the `default` queue.
- **Queues:** `default` for dispatching and realtime fetching; `feeds` for `fetch_static_entry` and `validate_gtfs_feed`, served by a dedicated validator worker.
- Realtime fetching is a self-rescheduling task per endpoint, guarded by a Redis lock and a liveness marker. The marker holds a random **loop token**: each run passes a fresh token to the one message it queues, and a message whose token no longer matches the marker exits without fetching or rescheduling. A second seed after the marker expired (lagging queue), a Redis redelivery or a message left over from before a pause/resume therefore never forks the loop (issue #38).
- **No long ETAs.** A realtime message waits at most the endpoint's interval. A delayed endpoint (back-off) has no queued message; `bootstrap_realtime_tasks` resumes it once `next_fetch_after` passes. This keeps every ETA far below the Redis broker's visibility timeout (1 h by default), past which Redis redelivers a waiting message. We chose this over raising `visibility_timeout` above 6 h, which would also delay the redelivery of messages lost by a crashed worker by as much and keep hours-long ETA messages in worker memory. The cost: a back-off ends up to one bootstrap period (5 min) late.
- **Requests never fail because the broker is down:** every enqueue during a request goes through `data_manager.tasks.enqueue()`, which logs and returns `False` instead of raising, and is called from `transaction.on_commit` so a worker never sees uncommitted rows (`enqueue_on_commit()` does both). Lost jobs are recovered by the periodic dispatchers and the `retry_gtfs_validation` / `retry_realtime_validation` commands.
- A failing source backs off (5 min, 1 h, 6 h) and is then auto-paused; people can pause and resume fetching by hand.

## Consequences

- Tasks must be idempotent: the same job may run twice after a retry or a re-seed.
- Local development runs Redis, a worker, beat and the validator worker through Docker Compose.
- Without `REDIS_CACHE_URL` (tests) the cache falls back to per-process memory, so tests never need Redis.

## Alternatives considered

- **Cron + management commands** — no per-endpoint polling, no retries, no queue isolation for slow validation.
