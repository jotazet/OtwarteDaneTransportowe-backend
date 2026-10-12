# 0004. Run fetching and validation in Celery with Redis

- **Status:** Accepted (recorded retroactively)
- **Date:** 2026-10-12

## Context

ODT downloads proxied static feeds on a daily schedule, polls realtime endpoints every few seconds, and validates GTFS files with a validator that can run for minutes. None of this may block an HTTP request, and periodic work must survive restarts.

## Decision

- **Celery** with **Redis** as broker and result backend. Redis also backs Django's cache, so locks and throttling are shared between web and worker processes.
- **Beat** with `django-celery-beat`'s database scheduler: `refresh_static_feeds` every minute and `bootstrap_realtime_tasks` every 5 minutes, both on the `default` queue.
- **Queues:** `default` for dispatching and realtime fetching; `feeds` for `fetch_static_entry` and `validate_gtfs_feed`, served by a dedicated validator worker.
- Realtime fetching is a self-rescheduling task per endpoint, guarded by a Redis lock and a liveness marker.
- **Requests never fail because the broker is down:** every enqueue during a request goes through `data_manager.tasks.enqueue()`, which logs and returns `False` instead of raising, and is called from `transaction.on_commit` so a worker never sees uncommitted rows. Lost jobs are recovered by the periodic dispatchers and the `retry_gtfs_validation` / `retry_realtime_validation` commands.
- A failing source backs off (5 min, 1 h, 6 h) and is then auto-paused; people can pause and resume fetching by hand.

## Consequences

- Tasks must be idempotent: the same job may run twice after a retry or a re-seed.
- Local development runs Redis, a worker, beat and the validator worker through Docker Compose.
- Without `REDIS_CACHE_URL` (tests) the cache falls back to per-process memory, so tests never need Redis.

## Alternatives considered

- **Cron + management commands** — no per-endpoint polling, no retries, no queue isolation for slow validation.
