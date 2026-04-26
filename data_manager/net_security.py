from __future__ import annotations

import socket
from ipaddress import ip_address
from urllib.parse import urlsplit


class OutboundURLBlocked(ValueError):
    """Raised when an outbound URL is considered unsafe (SSRF protection)."""


def _is_public_ip(ip: str) -> bool:
    addr = ip_address(ip)
    if addr.is_private:
        return False
    if addr.is_loopback:
        return False
    if addr.is_link_local:
        return False
    if addr.is_multicast:
        return False
    if addr.is_reserved:
        return False
    if addr.is_unspecified:
        return False
    return True


def assert_safe_outbound_url(url: str) -> None:
    """
    Basic SSRF guard:
    - scheme must be http/https
    - must have hostname
    - disallow userinfo (user:pass@host)
    - resolve DNS and block any private/loopback/link-local/etc IPs
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"}:
        raise OutboundURLBlocked("Only http/https URLs are allowed.")

    if not parts.hostname:
        raise OutboundURLBlocked("URL must include a hostname.")

    if parts.username or parts.password:
        raise OutboundURLBlocked("Userinfo in URL is not allowed.")

    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise OutboundURLBlocked(f"DNS resolution failed for host '{parts.hostname}'.") from exc

    resolved_ips: set[str] = set()
    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            resolved_ips.add(sockaddr[0])
        elif family == socket.AF_INET6:
            resolved_ips.add(sockaddr[0])

    if not resolved_ips:
        raise OutboundURLBlocked(f"Could not resolve host '{parts.hostname}'.")

    for ip in resolved_ips:
        if not _is_public_ip(ip):
            raise OutboundURLBlocked("URL resolves to a non-public IP address.")

