## What

<!-- One or two sentences: what changes for the user or the system. -->

## Why

<!-- The problem this solves and who benefits (feed consumer, data provider, ODT team). Link the issue if there is one. -->

## How it was verified

<!-- Tests added or updated, manual checks against the running stack, validator results. -->

## Checklist

- [ ] Checks pass (`ruff check`, `ruff format --check`, `pytest`)
- [ ] Migrations included and `makemigrations --check` is clean, or not needed
- [ ] API contract unchanged, or `docs/api/` and `docs/api/changelog.md` updated and the frontend PR linked
- [ ] Docs updated (ADR, glossary, deployment) or not needed
- [ ] `/simplify` and `/code-review` run; remaining findings explained below

## Notes for reviewers

<!-- Trade-offs, deployment steps (new env vars, migrations), follow-ups. -->
