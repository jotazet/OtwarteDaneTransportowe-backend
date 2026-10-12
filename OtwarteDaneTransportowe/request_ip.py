"""Shared client-IP resolution honouring the trusted-proxy allowlist.

``X-Forwarded-For`` is attacker-controlled unless the request actually came
from a reverse proxy we operate. The header is therefore only trusted when
``REMOTE_ADDR`` falls inside one of the ``TRUSTED_PROXY_CIDRS`` networks;
otherwise the socket peer address is used. Every consumer of a client IP
(blog reaction limits, ``your_reaction`` lookups) must go through this helper
so reads and writes agree on the same identity.

Proxies append the address they received the request from, so only the right
end of the header is trustworthy: the client is the rightmost entry that is not
one of our proxies. The leftmost entry is whatever the client chose to send.
"""

from ipaddress import ip_address, ip_network

from django.conf import settings


def _trusted_networks():
    networks = []
    for cidr in getattr(settings, 'TRUSTED_PROXY_CIDRS', []) or []:
        try:
            networks.append(ip_network(str(cidr), strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted(address: str, networks) -> bool:
    try:
        ip = ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def get_client_ip(request) -> str | None:
    remote = request.META.get('REMOTE_ADDR')
    xff = request.META.get('HTTP_X_FORWARDED_FOR')

    networks = _trusted_networks()
    if not remote or not xff or not _is_trusted(remote, networks):
        return remote

    # Walk from the right, skipping our own proxies. An unparseable hop means
    # the chain can't be followed further, so the last trusted hop is used.
    client = remote
    for hop in reversed([part.strip() for part in xff.split(',')]):
        try:
            ip_address(hop)
        except ValueError:
            return client
        client = hop
        if not _is_trusted(hop, networks):
            return hop
    return client
