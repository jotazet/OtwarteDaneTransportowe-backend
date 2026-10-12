# ODT backend

Backend of **Otwarte Dane Transportowe (ODT)**, a Polish portal for open public transport data: a catalogue of static and realtime feeds (GTFS, NeTEx, GTFS-RT, SIRI, GBFS), a review workflow for publishing them, a transparency record of requests for data (cases) and a blog.

- **Users:**
  - **developers and enthusiasts** who consume feeds (apps, journey planners, research);
  - **organizers and operators** (gminy, transport authorities, carriers) who submit and maintain their feeds;
  - **the ODT team**, who verify submissions, run proxied feeds and track cases.
- **Goal:** as much correct, stable and openly licensed Polish transport data as possible, easy to find and to use.
- The website is a separate repository, [otwarte-dane-transportowe-frontend](https://github.com/jotazet/otwarte-dane-transportowe-frontend) (Next.js + MUI), usually checked out next to this one at `../otwarte-dane-transportowe-frontend` ([ADR 0001](docs/architecture/decisions/0001-separate-backend-and-frontend-repositories.md)).

## How to work here

- **Be a skeptical partner, not an order-taker.** Check every request against the goal and the transit domain. If an idea is wrong, over-engineered, or a simpler or better option exists, say so before implementing. Be brief and state the trade-off.
- **Ask instead of guessing.** Ask the user about domain rules, legal requirements, product decisions and unclear scope, as multiple-choice questions when the options are clear.
- **No invented facts.** Mark unverified claims as unverified. Never make up GTFS/NeTEx/SIRI/GBFS fields, regulations or Polish legal terms. Check `docs/domain/` and the official specs first.
- **Keep it simple and surgical.** Follow the `karpathy-guidelines` skill: write the minimum code that solves the problem, add no speculative abstractions, and make every changed line traceable to the request.

## Hard rules

1. **Language.** Code identifiers, comments, docstrings, log messages, commit messages, PRs and everything in `docs/` are in **English**. Some older comments are in Polish; translate them when you change that code, not in bulk. Conversations with the team may be in Polish.
2. **No user-facing text from the backend.** The UI is translated into pl, en, de and bg by the frontend. New errors are **codes**, not prose ([ADR 0006](docs/architecture/decisions/0006-api-error-codes.md)); the frontend translates them.
3. **Domain.** Use the **`transit-domain`** skill whenever you touch feeds, submissions, the workflow, validation, proxying or terminology. Name things after [`docs/domain/glossary.md`](docs/domain/glossary.md).
4. **Django.** Follow the **`django-drf`** skill and the conventions below.
5. **The API contract is shared with the frontend.** A change to a response shape, a status code or a route updates `docs/api/` and gets an entry in [`docs/api/changelog.md`](docs/api/changelog.md) in the same PR. Breaking changes need a coordinated frontend PR.
6. **Docs are part of the change.** Update `docs/` on the same branch. A new decision gets an ADR in `docs/architecture/decisions/` (copy `0000-template.md`). New domain knowledge goes in `docs/domain/`. Changed setup, env vars or commands go in `README.md` and [`docs/deployment.md`](docs/deployment.md).
7. **Git.** Follow the workflow below. It is enforced by `.claude/hooks/protect-main.sh`.

## Git workflow

- **Never commit to or push to `main`.** Every change goes through its own branch and a pull request.
- Branch names follow `<type>/<short-kebab-summary>`. Types are `feature`, `fix`, `perf`, `refactor`, `docs`, `chore` and `test`. Example: `fix/realtime-proxy-timeout`.
- Commit messages use Conventional Commits: `type(scope): imperative summary`, at most 72 characters, with a body that explains *why* when it isn't obvious.
- **No AI attribution:** no `Co-Authored-By` trailers and no "Generated with" lines in commits or PRs.
- Commit freely on your branch. Push and open a PR only when the user asks.
- Full details: [`docs/development/git-workflow.md`](docs/development/git-workflow.md).

## Architecture at a glance

- **Django + DRF + PostgreSQL** ([ADR 0002](docs/architecture/decisions/0002-django-drf-and-postgresql.md)). Apps: `accounts` (users), `blog`, `cases` (organizations, data providers, case statuses), `data_manager` (feed submissions, proxying, validation). The project package `OtwarteDaneTransportowe/` holds settings, URLs, Celery, roles, JWT views and shared helpers.
- **JWT auth and five roles as Django groups**, all checks in `OtwarteDaneTransportowe/auth_roles.py` ([ADR 0003](docs/architecture/decisions/0003-jwt-and-group-based-roles.md)).
- **Celery + Redis** for fetching and validation, queues `default` and `feeds` ([ADR 0004](docs/architecture/decisions/0004-celery-and-redis-for-background-work.md)).
- **Validation:** the MobilityData GTFS validator in a container through `docker-socket-proxy`; realtime bodies parsed in process ([ADR 0005](docs/architecture/decisions/0005-feed-validation.md)).
- **Workflow:** submissions move through stages 1–4, derived from their history ([feed workflow](docs/domain/feed-workflow.md)).

See [`docs/architecture/overview.md`](docs/architecture/overview.md). Decisions marked *Proposed* still need team confirmation, so treat them as defaults you may challenge.

## Django conventions

- **Settings and secrets** come from environment variables read in `OtwarteDaneTransportowe/settings_base.py` (or the dev/prod layer), never through `os.environ` elsewhere in the code. Document every new variable in `.env.example` and `docs/deployment.md`. Never hardcode secrets.
- **Time:** `USE_TZ = True`, stored in UTC. Instants are timezone-aware `datetime`; times of day such as `download_time_1/2` are UTC and named as such in the API.
- **Permissions** only through `auth_roles.py` helpers and permission classes, never ad-hoc role checks in views. Public views declare `AllowAny` explicitly.
- **Outbound HTTP** to user-supplied URLs only through `data_manager/net_security.py` (SSRF protection, DNS pinning, size caps). Credentials for sources are stored in encrypted fields (`data_manager/fields.py`).
- **Background work** is queued with `data_manager.tasks.enqueue()` inside `transaction.on_commit`, never with a bare `.delay()` in a request.
- **Lists** return the page envelope `{count, next, previous, results}`.
- **Migrations:** every model change ships its migration; never edit a migration that has been deployed.
- Use the `logging` module, never `print`. Never log personal data or secrets.

## Repository map

```
accounts/                 users and role assignment (api/, services.py)
blog/                     posts and reactions
cases/                    transport organizations, data providers, case statuses
data_manager/             feed submissions, proxying, validation, Celery tasks
OtwarteDaneTransportowe/  settings, urls, celery, auth_roles, jwt_views, pagination
<app>/tests/              pytest tests per app
scripts/                  deploy.sh and container helpers
docs/api/                 API reference and changelog (shared contract with the frontend)
docs/domain/              glossary, feed workflow, Polish and EU context
docs/architecture/        overview and ADRs
docs/development/         git workflow
docs/deployment.md        production deployment and operations
.claude/                  team settings, hooks and project skills
```

## Commands

```sh
docker compose up --build                                  # http://localhost:8000 (/admin, /api/schema/swagger-ui/ in DEBUG)
docker compose exec -T web python -m pytest                # tests
docker compose exec web python manage.py makemigrations    # after model changes
```

## Definition of done

- The tests pass for everything you touched.
- New logic has tests. Workflow and permission logic has tests for every role and stage it affects; outbound fetches and Docker are mocked.
- No user-facing prose from the backend; new errors use codes.
- `docs/` is updated: API reference and changelog, ADR, glossary, deployment.
- Before opening a PR, run `/simplify`, then `/code-review`. Fix what is real and mention what you skipped.
