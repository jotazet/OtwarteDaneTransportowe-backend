FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
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

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app

RUN groupadd --system app \
 && useradd --system --gid app --home-dir /app app \
 && mkdir -p /app/uploaded_data /app/staticfiles \
 && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["gunicorn", "OtwarteDaneTransportowe.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--no-control-socket"]

