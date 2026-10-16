"""DRF throttle that identifies clients by the trusted-proxy client IP.

DRF's BaseThrottle.get_ident() uses the raw X-Forwarded-For header, which the
client controls: behind nginx a new spoofed value per request gives a fresh
bucket each time. This subclass uses get_client_ip() instead, so throttling
sees the same client as the blog reaction limits.
"""

from rest_framework import throttling

from OtwarteDaneTransportowe.request_ip import get_client_ip


class ScopedRateThrottle(throttling.ScopedRateThrottle):
    def get_ident(self, request):
        return get_client_ip(request)
