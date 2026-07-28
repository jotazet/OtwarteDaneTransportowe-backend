# Ensure Celery app is loaded when Django starts so @shared_task decorators work.
from .celery import app as celery_app  # noqa: F401

# NOTE: file-cleanup signal receivers (cleanup_files.py) are registered in
# data_manager.apps.DataManagerConfig.ready(). Importing them here would raise
# AppRegistryNotReady (models cannot be imported before the app registry is
# populated) — the previous try/except silently swallowed exactly that, so the
# receivers were never registered at all.

