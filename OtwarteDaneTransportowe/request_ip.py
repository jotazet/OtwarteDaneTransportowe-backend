"""Shared client-IP resolution honouring the trusted-proxy allowlist.

``X-Forwarded-For`` is attacker-controlled unless the request actually came
from a reverse proxy we operate. The header is therefore only trusted when
``REMOTE_ADDR`` falls inside one of the ``TRUSTED_PROXY_CIDRS`` networks;
otherwise the socket peer address is used. Every consumer of a client IP
(blog reaction limits, ``your_reaction`` lookups) must go through this helper
so reads and writes agree on the same identity.
"""
from ipaddress import ip_address, ip_network

from django.conf import settings


def get_client_ip(request) -> str | None:
    remote = request.META.get('REMOTE_ADDR')
    xff = request.META.get('HTTP_X_FORWARDED_FOR')

    trusted = getattr(settings, 'TRUSTED_PROXY_CIDRS', []) or []
    is_trusted_proxy = False
    if remote and trusted:
        try:
            r_ip = ip_address(remote)
            for cidr in trusted:
                try:
                    if r_ip in ip_network(str(cidr), strict=False):
                        is_trusted_proxy = True
                        break
                except ValueError:
                    continue
        except ValueError:
            is_trusted_proxy = False

    if is_trusted_proxy and xff:
        # X-Forwarded-For can contain multiple IPs: client, proxies...
        candidate = xff.split(',')[0].strip()
        return candidate or remote

    return remote
