# ODT documentation

All documentation is in English, and every change updates the relevant pages on the same branch. Keep pages short and current. A wrong document is worse than none.

This repository holds the documentation shared by the backend and [the frontend](https://github.com/jotazet/otwarte-dane-transportowe-frontend). The frontend keeps only its own design system and UI decisions in its `docs/`.

| Area | Contents |
| --- | --- |
| [api/](api/README.md) | REST API reference: [auth and roles](api/README.md), [blog](api/blog.md), [organizations and cases](api/cases.md), [feeds](api/feeds.md), [models and enums](api/models.md), [changelog](api/changelog.md) |
| [domain/](domain/) | [glossary](domain/glossary.md), [feed workflow](domain/feed-workflow.md), [Polish and EU context](domain/poland.md) |
| [architecture/](architecture/) | [overview](architecture/overview.md) and [decisions (ADRs)](architecture/decisions/) |
| [development/](development/) | [git workflow](development/git-workflow.md) |
| [deployment.md](deployment.md) | production deployment, configuration and operations |

Agent and team rules are in [CLAUDE.md](../CLAUDE.md).

## What goes where

- **A decision that is hard to reverse, or that someone will later ask "why?" about** → a new ADR (copy [0000-template.md](architecture/decisions/0000-template.md)).
- **A change to the API contract** → update `api/` and add an entry to [api/changelog.md](api/changelog.md).
- **A domain fact** (format rule, legal requirement, industry practice) → `domain/`, **with a source link**. Facts nobody has verified are marked as such.
- **A new concept or term** → [glossary](domain/glossary.md), with its Polish UI term.
- **A new env var, service or operational procedure** → [deployment.md](deployment.md) and `.env.example`.
