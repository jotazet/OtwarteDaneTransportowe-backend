# Models and enums

## Static feed data types

| Value | Meaning |
|-------|---------|
| `gtfs` | GTFS |
| `netex` | NeTEx |
| `other` | Other static format |

## Realtime protocols

| Value | Meaning |
|-------|---------|
| `gtfs_rt` | GTFS Realtime |
| `siri` | SIRI |
| `gbfs` | General Bikeshare Feed Specification |

## Realtime endpoint types

| Protocol | Allowed `endpoint_type` |
|----------|-------------------------|
| `gtfs_rt` | `trip_update`, `vehicle_position`, `service_alert` |
| `siri` | `sx`, `sm`, `vm`, `et`, `gm` |
| `gbfs` | `gbfs`, `gbfs_versions`, `system_information`, `vehicle_types`, `station_information`, `station_status`, `free_bike_status`, `system_hours`, `system_alerts` |

## Auth types for feed sources

| Value | Meaning |
|-------|---------|
| `null` | No authorization |
| `api_key` | API key |
| `bearer_token` | Bearer token |
| `basic_auth` | Basic auth, `auth_value` in the format `username:password` |

## Static feed stages

| Stage | Label | Meaning |
|-------|-------|---------|
| `1` | `Step 1: Upload data` | Data needs correction, or there is no history |
| `2` | `Step 2: Data verification` | Data submitted, validation in progress/finished; owner: restricted editing from stage 2 (see [Static feeds (FeedSubmission)](feeds.md)) |
| `3` | `Step 3: Admin confirmation` | Data ready for confirmation; same restrictions as stage 2 |
| `4` | `Step 4: Complete` | Feed published; same restrictions as stage 2 |

## Realtime feed stages

| Stage | Label | Meaning |
|-------|-------|---------|
| `1` | `Step 1: Endpoints` | Endpoints added / need correction |
| `2` | `Step 2: Data verification` | Validation in progress (link-check, in-process check of the response format, first cache fetch) |
| `3` | `Step 3: Admin confirmation` | Verification finished; ready for confirmation |
| `4` | `Step 4: Published` | Realtime feed published |

## History

A static feed has `FeedSubmissionHistory`, a realtime feed has `RealtimeSubmissionHistory`.

| `event_type` | Meaning |
|--------------|---------|
| `uploaded` | Creation or re-upload of data |
| `stage_advanced` | Transition to the next stage |
| `rejected` | Rejection; `cause` contains a reviewer's text, and for realtime feeds `reasons` holds coded automatic reasons (see [Realtime rejection reasons](feeds.md#realtime-rejection-reasons)) |
| `completed` | Publication, stage 4 |
