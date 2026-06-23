#!/bin/sh
# Run as root (default Alpine). Fixes bind-mounted uploaded_data for app UID:GID.
set -eu
mkdir -p /app/uploaded_data
exec chown -R "${TARGET_UID:-1000}:${TARGET_GID:-1000}" /app/uploaded_data
