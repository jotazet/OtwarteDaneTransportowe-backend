from __future__ import annotations

import socket
from contextlib import contextmanager
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import requests

DEFAULT_MAX_REDIRECTS = 5


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


def split_userinfo(url: str) -> tuple[str, dict]:
    """Split ``user:pass@`` credentials out of a URL.

    Returns ``(clean_url_without_userinfo, headers)`` where headers carries
    the equivalent ``Authorization: Basic`` header (percent-encoding in the
    credentials is decoded first — the same thing a browser does). URLs
    without userinfo return ``(url, {})``.

    Product decision: URLs with embedded credentials are ALLOWED and treated
    as intentionally public (e.g. demo feeds with documented sample logins).
    Secret credentials belong in auth_type/auth_value (encrypted, proxied).
    The SSRF host checks still apply to the real hostname — urlsplit parses
    the host after the ``@``, so `https://trusted.com@evil.com/` is validated
    against evil.com.
    """
    parts = urlsplit((url or "").strip())
    if not parts.username and not parts.password:
        return url, {}
    import base64
    from urllib.parse import unquote, urlunsplit

    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    clean = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return clean, {"Authorization": f"Basic {token}"}


def resolve_public_ips(url: str) -> tuple[str, int, set[str]]:
    """
    Validate an outbound URL and return ``(hostname, port, resolved_public_ips)``.

    SSRF guard:
    - scheme must be http/https
    - must have hostname
    - resolve DNS and block if ANY resolved IP is private/loopback/link-local/etc.

    Userinfo (``user:pass@host``) is permitted — see :func:`split_userinfo`;
    validation always runs against the real hostname after the ``@``.

    Raises :class:`OutboundURLBlocked` on any violation.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"}:
        raise OutboundURLBlocked("Only http/https URLs are allowed.")

    if not parts.hostname:
        raise OutboundURLBlocked("URL must include a hostname.")

    port = parts.port or (443 if parts.scheme == "https" else 80)

    try:
        infos = socket.getaddrinfo(parts.hostname, port)
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

    return parts.hostname, port, resolved_ips


def assert_safe_outbound_url(url: str) -> None:
    """Backwards-compatible guard: validate the URL or raise OutboundURLBlocked."""
    resolve_public_ips(url)


@contextmanager
def _pinned_dns(hostname: str, allowed_ips: set[str]):
    """
    Temporarily force ``hostname`` to resolve ONLY to ``allowed_ips`` (already
    validated as public). This closes the TOCTOU/DNS-rebinding gap between the
    validation lookup and the actual connection, while leaving TLS SNI and
    certificate verification intact (the request still carries the real hostname).

    Used only inside single-threaded Celery worker tasks.
    """
    real_getaddrinfo = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        if host == hostname:
            results: list = []
            for ip in allowed_ips:
                # Resolving a literal IP performs no network DNS lookup.
                results.extend(real_getaddrinfo(ip, *args, **kwargs))
            if results:
                return results
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo


def safe_get(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float,
    max_bytes: int,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    chunk_size: int = 65536,
) -> requests.Response:
    """
    SSRF-hardened HTTP GET:
    - validates the URL and every redirect hop (re-resolving DNS each time),
    - pins the connection to the validated IP (anti DNS-rebinding),
    - disables automatic redirects (each hop is validated manually),
    - streams the body and aborts if it exceeds ``max_bytes``.

    Returns a fully-read ``requests.Response`` (``.content`` is populated).
    Raises :class:`OutboundURLBlocked` for unsafe URLs or oversized responses,
    and propagates ``requests`` exceptions otherwise.
    """
    current_url = url
    for _ in range(max_redirects + 1):
        # Embedded user:pass@ (if any) becomes a Basic Authorization header for
        # THIS hop; the request itself always goes to a credential-free URL.
        # Explicitly passed headers (auth_type/auth_value) take precedence.
        clean_url, userinfo_headers = split_userinfo(current_url)
        request_headers = {**userinfo_headers, **(headers or {})} or None

        hostname, _port, allowed_ips = resolve_public_ips(clean_url)

        with _pinned_dns(hostname, allowed_ips):
            response = requests.get(
                clean_url,
                headers=request_headers,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )

        if response.is_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise OutboundURLBlocked("Redirect without a Location header.")
            current_url = urljoin(current_url, location)
            continue

        # Terminal response: stream the body with a hard size cap.
        try:
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise OutboundURLBlocked(
                        f"Response body exceeds the {max_bytes} byte limit."
                    )
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
        finally:
            response.close()
        return response

    raise OutboundURLBlocked(f"Too many redirects (>{max_redirects}).")

