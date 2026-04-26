"""
Backward-compatible settings module.

Default to development settings. For production set:
  DJANGO_SETTINGS_MODULE=OtwarteDaneTransportowe.settings_prod
"""

from .settings_dev import *  # noqa: F403,F401