---
name: transit-domain
description: Public transport data domain guide for ODT - GTFS, GTFS-RT, NeTEx, SIRI, GBFS, the feed submission workflow, proxying and validation, Polish and EU open-data law. Use whenever designing or changing feed submissions, stages, validation, fetching/proxying, the public catalogue, cases, or domain terminology - and whenever a request makes a claim about how transit data formats, Polish law or industry practice work.
---

# Transit data domain

ODT collects and publishes Polish public transport data: static schedules (GTFS, NeTEx) and realtime feeds (GTFS-RT, SIRI, GBFS), after a review workflow. Domain mistakes are the most expensive bugs here: a broken feed URL silently removes a city from journey planners, a wrong claim about the law misleads public bodies, and an unvalidated feed spreads errors to every consumer.

## Sources of truth (read before acting)

| Need | Read |
| --- | --- |
| Names for concepts (code and Polish UI) | `docs/domain/glossary.md` |
| Submission stages, roles per stage, proxying | `docs/domain/feed-workflow.md`, `OtwarteDaneTransportowe/auth_roles.py` |
| API contract consumers depend on | `docs/api/` |
| Polish and EU legal duties, national access point (KPD) | `docs/domain/poland.md` |
| Format rules | official specs: [GTFS](https://gtfs.org/documentation/schedule/reference/), [GTFS-RT](https://gtfs.org/documentation/realtime/reference/), [NeTEx](https://netex-cen.eu), [SIRI](https://transmodel-cen.eu/index.php/siri/), [GBFS](https://gbfs.org) |
| Decisions | `docs/architecture/decisions/` (especially 0004, 0005) |

## Workflow

1. **Name it.** Find the concept in the glossary. If it's missing, use the name from the relevant standard and a Polish UI term, and add both in the same change.
2. **Check the standard.** Read the official spec for any field, file or endpoint type you touch. If a format cannot express something, say so and decide explicitly what ODT does.
3. **Check the invariants below.** A change that breaks one needs an ADR, not a quiet exception.
4. **Think about consumers.** Who downloads this feed or calls this endpoint, and what breaks for them if a URL, an id or a shape changes?
5. **Test with real-world cases** (see below).
6. **Ask when unsure.** Ask the user about anything not covered by the sources above. Never invent fields, legal duties or industry terms; mark unverified claims as unverified.

## Invariants

- **Public feed URLs are stable.** `/feed/<id>/…` and `/feed/rt/<id>/<endpoint_type>/` must keep working across re-uploads, re-fetches and re-validation; consumers (journey planners, apps) poll them for years. Expired or moved feeds silently drop services from journey planners ([Google](https://support.google.com/transitpartners/answer/6394315), [GTFS best practices](https://gtfs.org/documentation/schedule/schedule-best-practices/)).
- **The stage is derived from history**, never stored: every transition appends a history event (`uploaded`, `stage_advanced`, `rejected`, `completed`). History is the audit trail, which is why only Admin may delete submissions.
- **Publication follows the workflow**: verification (stage 2) and admin confirmation (stage 3) are the normal path. The ODT team (Helper/Admin) can override it explicitly by setting any stage, and every override is recorded in history with the reviewer as actor.
- **A failed validation of a published feed does not unpublish it**; it is reported instead.
- **Realtime depends on static:** GTFS-RT needs a published GTFS submission and SIRI a published NeTEx one of the same organization; GBFS stands alone.
- **ODT serves providers' data, it does not edit it.** Proxying (`hide_original`) changes where a file is served from, never its content.
- **Source credentials stay secret:** encrypted at rest, write-only in the API, never logged.
- **Legal statements need a source** in `docs/domain/poland.md`; never put an obligation or deadline in docs or UI copy without one.

## Real-world test cases to cover

- A source that times out, returns HTTP errors, redirects to a private address (SSRF), or exceeds the size cap.
- A GTFS ZIP that fails validation; a non-ZIP upload; a URL and a file sent together.
- A realtime body that is not valid protobuf / XML / JSON for its protocol.
- Each role at each stage, including the forbidden actions (owner after stage 1, Helper editing a source, non-Admin deleting).
- Broker (Redis) unreachable during a write: the write must still succeed.
- Fetch times around midnight UTC and across the Polish DST changes (download times are UTC).
