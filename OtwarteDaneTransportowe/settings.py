"""
The development settings under their old name, used by pytest (pytest.ini).

The entry points (wsgi.py, asgi.py, celery.py) and the image default to settings_prod.
"""

from .settings_dev import *  # noqa: F403,F401
