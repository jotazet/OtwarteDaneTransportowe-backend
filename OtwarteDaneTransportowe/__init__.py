# ...existing imports and package markers...
# Ensure signal handlers for cleaning up files are registered
try:
    from . import cleanup_files  # noqa: F401
except Exception:
    # Avoid hard failures at import time; Django will surface real issues.
    pass

