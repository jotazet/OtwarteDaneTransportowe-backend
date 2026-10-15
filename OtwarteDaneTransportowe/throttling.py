"""DRF throttles that identify clients by the trusted-proxy client IP.

DRF's BaseThrottle.get_ident() uses the raw X-Forwarded-For header, which the
client controls: behind nginx a new spoofed value per request gives a fresh
bucket each time. These subclasses use get_client_ip() instead, so throttling
sees the same client as the blog reaction limits.
"""

from rest_framework import throttling

from OtwarteDaneTransportowe.request_ip import get_client_ip


class _ClientIPMixin:
    def get_ident(self, request):
        return get_client_ip(request)


class AnonRateThrottle(_ClientIPMixin, throttling.AnonRateThrottle):
    pass


class UserRateThrottle(_ClientIPMixin, throttling.UserRateThrottle):
    pass


class ScopedRateThrottle(_ClientIPMixin, throttling.ScopedRateThrottle):
    pass
