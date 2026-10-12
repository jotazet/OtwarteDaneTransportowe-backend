# Glossary

One concept, one name. Code uses the **English term**; the Polish UI uses the **Polish term** (source of truth for UI wording: the frontend's `i18n/lang/pl.ts`). When you add a concept, add it here in the same change. Terms marked *confirm* still need agreement with the team.

## Feeds and submissions

| English (code) | Polish (UI) | Meaning |
| --- | --- | --- |
| Feed | Feed | A published dataset in a standard format, static or realtime. |
| Static feed | Feed statyczny | A schedule dataset: GTFS, NeTEx or other (`FeedSubmission.data_type`). |
| Realtime feed | Feed realtime | Live data from one or more endpoints: GTFS-RT, SIRI or GBFS (`RealtimeSubmission.protocol`). |
| Submission (`FeedSubmission`, `RealtimeSubmission`) | Zgłoszenie (feedu statycznego / realtime) | A data provider's request to publish a feed; carries the workflow stage and history. |
| Static feed entry (`StaticFeedEntry`) | Plik statyczny | The source of a static submission: a URL or an uploaded file, never both. |
| Endpoint (`RealtimeEndpointRT`) | Endpoint | One URL of a realtime submission, typed by `endpoint_type` (e.g. `trip_update`, `sx`, `station_status`). |
| Stage | Etap | Workflow step 1–4: upload/endpoints → data verification → admin confirmation → published. Derived from history, never stored. See [feed workflow](feed-workflow.md). |
| History event (`*History`) | Historia zgłoszenia | `uploaded`, `stage_advanced`, `rejected`, `completed`; see [History](../api/models.md#history). |
| Rejection cause | Przyczyna odrzucenia | Why a submission went back to stage 1: a reviewer's or the validator's text in `rejection_cause`, or, for automatic realtime rejections, codes in `rejection_reasons` that the frontend translates. See [History](../api/models.md#history). |
| Published | Opublikowane | Stage 4: visible in the public catalogue. |
| Validation report (`FeedValidationReport`) | Raport walidacji *confirm* | Output of the GTFS validator for a static entry. |
| Hide original URL (`hide_original`), proxy feed | Ukryj oryginalny adres URL; feed proxy | ODT fetches the source itself and serves the file from its own URL. Forced when the source needs authorization. |
| Source authorization (`auth_type`) | Autoryzacja źródła | Credentials ODT uses to fetch a source (API key, bearer token, basic auth); stored encrypted. |
| Download time (`download_time_1/2`) | Godzina pobierania (UTC) | Daily fetch times of a proxied static feed, in UTC. |
| Interval | Interwał / Aktualizacja źródeł | Seconds between fetches of a proxied realtime endpoint. |
| Fetch status | Status pobierania | `active`, `delayed`, `auto_paused`, `manual_paused`. |
| Fetch error (`FeedFetchError`) | Błąd pobierania | `timeout`, `connection_error`, `http_error`, `invalid_content`, `auth_error`. `auth_error` is defined but not written yet: a rejected credential (HTTP 401/403) is recorded as `http_error` with `http_status_code`. |
| Pause | Wstrzymanie / Auto-pauza | Fetching stopped by a person (`manual_paused`) or after repeated failures (`auto_paused`). |
| Feed catalogue | Katalog feedów | The public list of published feeds by organization. |

## Journey planner

| English (code) | Polish (UI) | Meaning |
| --- | --- | --- |
| Journey planner | Planer podróży | ODT's trip planning service over the published feeds, run on OpenTripPlanner. See [journey planner](journey-planner.md). |
| OTP (OpenTripPlanner) | OTP | The open-source routing engine the planner runs privately. |
| Planner feed (`PlannerFeed`) | Feed w planerze *confirm* | A published GTFS feed the admin included in the planner graph. |
| Feed ID (`feed_id`) | Identyfikator feedu w planerze *confirm* | OTP's prefix for every ID of a feed (`krk:2047691`); never changes. |
| Graph | Graf | OTP's routing data: streets plus timetables, built from OSM and the planner feeds. |
| Street graph | Graf ulic *confirm* | The street part of the graph, built from the cropped OSM extract and reused until the map, OTP version or feed area changes. |
| Graph build (`GraphBuild`) | Budowanie grafu | One run of the build pipeline, scheduled or manual; also a rollback. |
| Maintenance window | Przerwa techniczna planera *confirm* | While a build has the serving OTP stopped; OTP-backed endpoints answer `503`. |
| Failure policy (`failure_policy`) | Postępowanie przy błędzie feedu *confirm* | `keep_last_good` or `exclude`: what a build does with a feed whose newest version fails. |
| Last good version | Ostatnia działająca wersja | The file of a feed that last went into a served graph (snapshot). |
| Fresh / stale / excluded | Aktualny / nieaktualny / wykluczony *confirm* | A feed's outcome in a build: newest version, last good version instead, or left out. |
| Block (`block_id`) | Brygada | The sequence of trips one vehicle runs in a day (GTFS `block_id`). |
| Rollback | Przywrócenie poprzedniego grafu *confirm* | Serving an earlier build's graph again. |

## Organizations and cases

| English (code) | Polish (UI) | Meaning |
| --- | --- | --- |
| Transport organization (`TransportOrganization`) | Organizacja transportowa | A public or private body whose transport data ODT tracks or publishes. |
| Data provider (`DataProvider`) | Dostawca danych | A person or institution data is obtained from; linked to organizations. |
| Case | Sprawa | ODT's effort to obtain data from an organization; its history is a list of case statuses. |
| Case status (`CaseStatus`) | Status sprawy | One entry in a case's history, e.g. `requested` (Wniosek o dane), `denial` (Odmowa), `received` (Otrzymano dane), `reminder` (Ponaglenie). |

## Roles

| Code | Polish (UI) | Can |
| --- | --- | --- |
| `Admin` | Administrator | Everything, including user management, deleting submissions and changing a feed source at stages 2–4. |
| `Helper` | Weryfikator | Confirm or reject submissions. |
| `DataProvider` | Dostawca danych | Submit feeds and edit their own submissions within the workflow rules. |
| `Blogger` | Blogger | Write and edit their own blog posts. |
| `Editor` | Redaktor | Edit any blog post. |

Roles are Django groups; see [ADR 0003](../architecture/decisions/0003-jwt-and-group-based-roles.md).

## Standards

| Name | What it is | Reference |
| --- | --- | --- |
| GTFS | Static schedule format (ZIP of CSV files). | <https://gtfs.org/documentation/schedule/reference/> |
| GTFS-RT | Realtime extension of GTFS (protobuf): trip updates, vehicle positions, service alerts. | <https://gtfs.org/documentation/realtime/reference/> |
| NeTEx | European XML standard for static transport data (CEN/TS 16614); the EU MMTIS format. | <https://netex-cen.eu> |
| SIRI | European realtime standard (CEN/TS 15531); ODT supports SX, SM, VM, ET, GM. | <https://transmodel-cen.eu/index.php/siri/> |
| GBFS | Shared mobility (bikes, scooters) realtime format. | <https://gbfs.org> |
| KPD MMTIS | Polish national access point for travel information. | [poland.md](poland.md) |
