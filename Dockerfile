FROM python:3.12-slim

# The virtualenv lives outside /app: compose bind-mounts the project over /app,
# which would hide a /app/.venv.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/app

ARG DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Runtime libraries used by psycopg/Pillow without keeping build tooling in the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    libpq5 \
    zlib1g \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

# Dev dependencies (pytest, ruff) only when asked: docker-compose.yml builds
# with INSTALL_DEV=true so tests run in the container, production does not.
ARG INSTALL_DEV=false
COPY pyproject.toml uv.lock /app/
RUN if [ "$INSTALL_DEV" = "true" ]; then uv sync --locked; else uv sync --locked --no-dev; fi

COPY . /app

# Collect admin/DRF/Swagger static assets for WhiteNoise (no DB required).
ENV DJANGO_SETTINGS_MODULE=OtwarteDaneTransportowe.settings_dev
RUN python manage.py collectstatic --noinput

RUN groupadd --system app \
 && useradd --system --gid app --home-dir /app app \
 && mkdir -p /app/uploaded_data /app/staticfiles \
 && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["gunicorn", "OtwarteDaneTransportowe.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--no-control-socket"]

