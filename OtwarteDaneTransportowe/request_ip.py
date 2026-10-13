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

from functools import lru_cache
from ipaddress import ip_address, ip_network

from django.conf import settings


@lru_cache(maxsize=8)
def _trusted_networks(cidrs: tuple[str, ...]):
    # Keyed on the setting's value, so a changed setting is parsed afresh.
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ip_network(cidr, strict=False))
        except ValueError:
            continue
    return networks


def _parse_hop(hop: str):
    """Parse one X-Forwarded-For entry; some load balancers append a port."""
    hop = hop.strip()
    if hop.startswith('[') and ']' in hop:
        hop = hop[1 : hop.index(']')]
    elif hop.count(':') == 1:
        hop = hop.split(':', 1)[0]
    return ip_address(hop)


def _is_trusted(ip, networks) -> bool:
    return any(ip in network for network in networks)


def get_client_ip(request) -> str | None:
    remote = request.META.get('REMOTE_ADDR')
    xff = request.META.get('HTTP_X_FORWARDED_FOR')

    if not remote or not xff:
        return remote
    try:
        client = ip_address(remote)
    except ValueError:
        return remote
    networks = _trusted_networks(tuple(settings.TRUSTED_PROXY_CIDRS))
    if not _is_trusted(client, networks):
        return remote

    # Walk from the right, skipping our own proxies. An unparseable hop means
    # the chain can't be followed further, so the last trusted hop is used.
    for hop in reversed(xff.split(',')):
        try:
            client = _parse_hop(hop)
        except ValueError:
            break
        if not _is_trusted(client, networks):
            break
    return str(client)
