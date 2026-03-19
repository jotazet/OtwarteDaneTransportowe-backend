# Ensure Celery app is loaded when Django starts so @shared_task decorators work.
from .celery import app as celery_app  # noqa: F401

# Ensure signal handlers for cleaning up files are registered
try:
    from . import cleanup_files  # noqa: F401
except Exception:
    # Avoid hard failures at import time; Django will surface real issues.
    pass

