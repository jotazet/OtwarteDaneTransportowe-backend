# 0007. Manage dependencies with uv and check code with ruff

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Dependencies were installed with `pip install -r requirements.txt`. Half of the requirements had only lower bounds (`celery>=5.4.0`, `gunicorn>=23.0.0`), so two builds a week apart could run different code, and `pytest` was installed into the production image. The code had no linter or formatter, so style drifted and diffs mixed formatting with logic.

## Decision

- **uv** manages dependencies: `pyproject.toml` declares them, `uv.lock` pins every version, and `uv add` / `uv add --dev` change them. Never edit `uv.lock` by hand, never use pip.
- The image installs from the lock with `uv sync --locked` into `/opt/venv` (compose bind-mounts the project over `/app`). Dev dependencies (`pytest`, `pytest-django`, `ruff`) are installed only with the build arg `INSTALL_DEV=true`, which `docker-compose.yml` sets and `docker-compose.prod.yml` turns off.
- **ruff** lints and formats, configured in `pyproject.toml`: pycodestyle, pyflakes, isort, bugbear, pyupgrade, `DTZ` (naive datetimes) and `DJ` (Django); line length 100; quote style preserved. Migrations are excluded.
- CI runs `ruff check`, `ruff format --check`, a migrations check and the tests on every pull request.
- No type checker for now: Django's ORM is dynamic, `ty` has no Django support, and `mypy` with `django-stubs` would need annotations across the codebase.

## Consequences

- Builds are reproducible; upgrading is an explicit `uv lock --upgrade-package <name>` in a pull request.
- The production image no longer contains test tooling.
- The first format pass touched about half the files. Its commits are listed in `.git-blame-ignore-revs`, so the pull request that introduced them is merged with a merge commit (squashing would change the hashes).
- `DJ001` (`null=True` on string fields) was ignored at first because fixing it alters the API contract; the fields have since been migrated to `""` and the rule is enforced.

## Alternatives considered

- **pip-tools** — locks too, but slower and one more tool next to pip.
- **Black + isort + flake8** — three tools and three configs for what ruff does alone.
