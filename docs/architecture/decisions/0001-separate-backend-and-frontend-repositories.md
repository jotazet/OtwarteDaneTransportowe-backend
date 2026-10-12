# 0001. Keep the backend and the frontend in separate repositories

- **Status:** Accepted (recorded retroactively)
- **Date:** 2026-10-12

## Context

ODT has a Django REST API and a Next.js website. They were started as two repositories, `OtwarteDaneTransportowe-backend` and `otwarte-dane-transportowe-frontend`, and are deployed as separate Docker Compose stacks: the frontend calls the API from the browser and from its server, with the API base URL baked in at build time.

## Decision

Keep two repositories. The API contract between them is HTTP + JSON described in `docs/api/` of this repository. Documentation that both sides need (domain, glossary, system-level ADRs, git workflow) lives here, in the backend repository; the frontend keeps its own `docs/` for the design system and UI decisions and links here.

## Consequences

- Each side deploys on its own schedule. A change that needs both is two linked pull requests, and the backend must stay compatible with the deployed frontend until the frontend catches up.
- Every change a client must react to gets an entry in [`docs/api/changelog.md`](../../api/changelog.md).
- Agent instructions and skills are duplicated where both repositories need them (git workflow summary, `karpathy-guidelines`). Keep the copies in sync.
- No shared types: the frontend writes its API types by hand. Generating them from the OpenAPI schema (drf-spectacular) is possible later.

## Alternatives considered

- **Monorepo** (as in Solitable) — one PR per change and shared tooling, but it means rewriting two deployment setups and histories for no current pain.
