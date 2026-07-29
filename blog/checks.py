"""Deployment checks for the IP-based blog reaction limits.

Anonymous reactions are keyed on the client IP. Behind a reverse proxy the
socket peer is the proxy itself, so the real address only survives in
``X-Forwarded-For`` — which is honoured exclusively for peers listed in
``TRUSTED_PROXY_CIDRS``. When that setting is missing, every visitor collapses
into a single identity: one shared reaction per post (each visitor overwriting
the previous one) and one shared daily limit. That failure is invisible at
runtime, hence this check.
"""
from django.conf import settings
from django.core.checks import Warning as CheckWarning, register


W_NO_TRUSTED_PROXIES = 'blog.W001'


@register('blog')
def check_trusted_proxy_cidrs(app_configs, **kwargs):
    if settings.DEBUG or getattr(settings, 'TRUSTED_PROXY_CIDRS', None):
        return []
    return [
        CheckWarning(
            'TRUSTED_PROXY_CIDRS is empty while running with DEBUG=False.',
            hint=(
                'If this deployment sits behind a reverse proxy, every visitor '
                'is seen with the proxy address: blog reactions collapse to one '
                'shared reaction per post and one shared daily limit. Set '
                'TRUSTED_PROXY_CIDRS to the proxy network(s) (e.g. '
                '172.16.0.0/12,127.0.0.1/32 for nginx in front of Docker) and '
                'make the proxy forward X-Forwarded-For. Ignore this only when '
                'clients connect to the app directly.'
            ),
            id=W_NO_TRUSTED_PROXIES,
        )
    ]
