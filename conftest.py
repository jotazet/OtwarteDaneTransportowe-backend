"""Global pytest fixtures.

Throttling and the shared cache are production concerns; during tests we disable
DRF throttling and clear the cache between tests so rate limits never cause
flaky failures.
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_throttling(settings):
    rest = dict(getattr(settings, 'REST_FRAMEWORK', {}))
    rest['DEFAULT_THROTTLE_CLASSES'] = []
    rest['DEFAULT_THROTTLE_RATES'] = {}
    # Reassigning REST_FRAMEWORK fires setting_changed so DRF reloads api_settings.
    settings.REST_FRAMEWORK = rest
    yield


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()
