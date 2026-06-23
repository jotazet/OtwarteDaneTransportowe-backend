#!/usr/bin/env sh
# Production Docker Compose helper — always applies docker-compose.prod.yml.
#
# Usage (from repo root):
#   ./scripts/deploy.sh up -d --build
#   ./scripts/deploy.sh ps
#   ./scripts/deploy.sh logs -f web
set -e
cd "$(dirname "$0")/.."
export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
exec docker compose "$@"
