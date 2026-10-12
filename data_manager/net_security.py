from __future__ import annotations

import atexit
import os
import socket
import threading
import time
from contextlib import contextmanager
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import requests

DEFAULT_MAX_REDIRECTS = 5

# Bodies up to this size stay in RAM; anything larger spills to a temp file.
# Feed downloads are capped at MAX_FEED_FILE_SIZE_BYTES (200 MB by default) and
# used to be buffered twice in memory (a list of chunks PLUS the joined bytes),
# putting a ~400 MB spike on the worker for every fetch.
SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024


class OutboundURLBlocked(ValueError):
    """Raised when an outbound URL is considered unsafe (SSRF protection)."""


class ResponseTooLarge(OutboundURLBlocked):
    """The response body exceeded the caller's ``max_bytes`` cap."""

    def __init__(self, max_bytes: int):
        super().__init__(f"Response body exceeds the {max_bytes} byte limit.")
        self.max_bytes = max_bytes


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


def _request_origin(url: str) -> tuple[str, str, int]:
    """(scheme, hostname, port) of a URL — the unit auth headers are scoped to."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


def _may_forward_auth(origin: tuple[str, str, int], target: tuple[str, str, int]) -> bool:
    """Whether credentials for ``origin`` may still be sent to ``target``.

    ``requests`` strips the Authorization header when a redirect crosses to a
    different host; ``safe_get`` follows redirects manually and must do the
    same, or a feed URL that 302s to an attacker-controlled host would hand
    over the feed's stored API key / bearer token.

    Same host is required. A same-host http→https upgrade keeps the header;
    an https→http downgrade drops it (the credential would go out in clear).
    """
    if origin[1] != target[1] or origin[2] != target[2]:
        return False
    return not (origin[0] == "https" and target[0] != "https")


# ---------------------------------------------------------------------------
# DNS pinning
# ---------------------------------------------------------------------------


@contextmanager
def _pinned_dns(hostname: str, allowed_ips: set[str]):
    """
    Temporarily force ``hostname`` to resolve ONLY to ``allowed_ips`` (already
    validated as public). This closes the TOCTOU/DNS-rebinding gap between the
    validation lookup and the actual connection, while leaving TLS SNI and
    certificate verification intact (the request still carries the real hostname).

    The pin is scoped to the installing THREAD: ``socket.getaddrinfo`` is a
    process-global, so without the thread check one task's pin would rewrite
    every other concurrent connection's DNS — a cross-request hijack under any
    threaded pool. Patches chain (each captures the previous resolver), so
    nesting inside one thread is safe.

    Supported Celery pool remains prefork; the thread guard is what keeps this
    correct if that ever changes.
    """
    real_getaddrinfo = socket.getaddrinfo
    owner_thread = threading.get_ident()

    def patched(host, *args, **kwargs):
        if host == hostname and threading.get_ident() == owner_thread:
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


# ---------------------------------------------------------------------------
# Incomplete TLS chains (AIA completion)
# ---------------------------------------------------------------------------

# Per-process cache: hostname -> path of a CA bundle extended with the host's
# AIA-fetched intermediates. Realtime loops hit the same hosts every few
# seconds; the chain must not be re-fetched each time.
_AIA_BUNDLE_CACHE: dict[str, str] = {}
_AIA_INTERMEDIATE_MAX_BYTES = 100_000


def _purge_aia_bundles() -> None:
    """Remove the generated CA bundles (they are NamedTemporaryFile(delete=False))."""
    for path in list(_AIA_BUNDLE_CACHE.values()):
        try:
            os.unlink(path)
        except OSError:
            pass
    _AIA_BUNDLE_CACHE.clear()


atexit.register(_purge_aia_bundles)


def _ca_bundle_with_aia_intermediates(hostname: str, port: int) -> str | None:
    """Best-effort fix for servers that omit their intermediate certificate.

    Reads the server's leaf certificate, follows its Authority Information
    Access (CA Issuers) URLs — exactly what browsers do — and writes a CA
    bundle of ``certifi`` roots + the fetched intermediates. Returns the
    bundle path, or None when the chain cannot be completed (caller should
    re-raise the original SSL error). The intermediates are UNTRUSTED input:
    they only help verification succeed if they genuinely link the leaf to a
    trusted root, so this cannot make a forged certificate pass.
    """
    cached = _AIA_BUNDLE_CACHE.get(hostname)
    if cached:
        return cached

    import ssl
    import tempfile

    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        # Unpinned on purpose: this leaf is untrusted input used ONLY to read
        # its AIA extension, and each issuer URL below is SSRF-validated and
        # pinned on its own. The request that actually gets verified is pinned
        # by the caller.
        leaf_pem = ssl.get_server_certificate((hostname, port), timeout=10)
        leaf = x509.load_pem_x509_certificate(leaf_pem.encode())
        aia = leaf.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
        issuer_urls = [
            desc.access_location.value
            for desc in aia
            if desc.access_method == x509.oid.AuthorityInformationAccessOID.CA_ISSUERS
        ]
    except Exception:
        return None

    intermediate_pems: list[str] = []
    for issuer_url in issuer_urls[:3]:
        try:
            # Through safe_get, so the size cap is enforced WHILE streaming and
            # the connection is DNS-pinned. A plain requests.get() here used to
            # buffer the whole body before checking its length — an untrusted
            # certificate could name an issuer URL serving gigabytes and OOM
            # the worker.
            fetched = safe_get(
                issuer_url,
                timeout=10,
                max_bytes=_AIA_INTERMEDIATE_MAX_BYTES,
                total_timeout=30,
            )
            if fetched.status_code >= 400:
                continue
            try:
                cert = x509.load_der_x509_certificate(fetched.content)
            except ValueError:
                cert = x509.load_pem_x509_certificate(fetched.content)
            intermediate_pems.append(cert.public_bytes(serialization.Encoding.PEM).decode())
        except Exception:
            continue

    if not intermediate_pems:
        return None

    with open(certifi.where(), encoding='utf-8') as fh:
        roots = fh.read()
    bundle = tempfile.NamedTemporaryFile(
        'w', suffix='.pem', prefix=f'ca-aia-{hostname}-', delete=False
    )
    bundle.write(roots + '\n' + '\n'.join(intermediate_pems))
    bundle.close()
    _AIA_BUNDLE_CACHE[hostname] = bundle.name
    return bundle.name


# ---------------------------------------------------------------------------
# Hardened GET
# ---------------------------------------------------------------------------


class _Deadline:
    """Total wall-clock budget for a fetch.

    ``requests``' ``timeout`` is per socket read, so a server trickling bytes
    can keep a connection alive far past it. Feed fetches run under a Celery
    soft time limit; without a total budget the task is killed mid-download and
    the fetch-failure bookkeeping (FeedFetchError, retry schedule) never runs.
    """

    def __init__(self, total: float | None):
        self._expires_at = None if total is None else time.monotonic() + total

    def check(self) -> None:
        if self._expires_at is not None and time.monotonic() > self._expires_at:
            raise requests.exceptions.Timeout('Total download time budget exceeded.')


def _drain_to_spool(response, *, max_bytes: int, chunk_size: int, deadline: _Deadline):
    """Stream a response body into a spooled temp file, enforcing ``max_bytes``.

    Returns the file positioned at 0. Small bodies never touch the disk
    (SpooledTemporaryFile keeps them in RAM up to SPOOL_MAX_MEMORY_BYTES).
    """
    import tempfile

    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_MEMORY_BYTES, mode='w+b')
    try:
        total = 0
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            deadline.check()
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(max_bytes)
            spool.write(chunk)
        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise
    finally:
        response.close()


@contextmanager
def safe_download(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float,
    max_bytes: int,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    chunk_size: int = 65536,
    total_timeout: float | None = None,
):
    """SSRF-hardened GET that streams the body to a spooled temp file.

    Yields ``(response, body_file)``; ``body_file`` is positioned at 0 and is
    closed on exit. The response body is NOT loaded into ``response.content``
    — use this for feed-sized downloads and :func:`safe_get` for small ones.

    Guarantees, per hop:
    - the URL and every redirect target is re-validated and re-resolved,
    - the connection is pinned to the validated IPs (anti DNS-rebinding),
    - redirects are followed manually so each hop can be checked,
    - credentials are dropped when a redirect leaves the original origin,
    - the body is size-capped WHILE streaming, never after the fact.
    """
    deadline = _Deadline(total_timeout)
    origin = _request_origin(split_userinfo(url)[0])
    current_url = url

    for _ in range(max_redirects + 1):
        deadline.check()
        # Embedded user:pass@ (if any) becomes a Basic Authorization header for
        # THIS hop; the request itself always goes to a credential-free URL.
        # Explicitly passed headers (auth_type/auth_value) take precedence.
        clean_url, userinfo_headers = split_userinfo(current_url)

        if _may_forward_auth(origin, _request_origin(clean_url)):
            request_headers = {**userinfo_headers, **(headers or {})} or None
        else:
            # Off-origin hop: the caller's credentials stay behind. Userinfo
            # embedded in THIS hop's URL is still honoured — it was addressed
            # to this host explicitly.
            request_headers = dict(userinfo_headers) or None

        hostname, port, allowed_ips = resolve_public_ips(clean_url)

        response = None
        verify_error = None
        with _pinned_dns(hostname, allowed_ips):
            try:
                response = requests.get(
                    clean_url,
                    headers=request_headers,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.SSLError as exc:
                # Misconfigured upstream: server does not send its intermediate
                # certificate (browsers hide this by AIA-fetching it). Complete
                # the chain ourselves and retry ONCE — full verification stays
                # in force, the chain must still anchor at a trusted root.
                if "certificate verify failed" not in str(exc).lower():
                    raise
                verify_error = exc

        if response is None:
            # Deliberately outside the pin above: completing the chain talks to
            # the CA's repository, a different host that needs its own DNS
            # resolution and its own pin.
            bundle = _ca_bundle_with_aia_intermediates(hostname, port)
            if not bundle:
                raise verify_error
            with _pinned_dns(hostname, allowed_ips):
                response = requests.get(
                    clean_url,
                    headers=request_headers,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                    verify=bundle,
                )

        if response.is_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise OutboundURLBlocked("Redirect without a Location header.")
            current_url = urljoin(current_url, location)
            continue

        body = _drain_to_spool(
            response, max_bytes=max_bytes, chunk_size=chunk_size, deadline=deadline
        )
        try:
            yield response, body
        finally:
            body.close()
        return

    raise OutboundURLBlocked(f"Too many redirects (>{max_redirects}).")


def safe_get(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float,
    max_bytes: int,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    chunk_size: int = 65536,
    total_timeout: float | None = None,
) -> requests.Response:
    """:func:`safe_download` with the body materialised into ``response.content``.

    Convenience for small responses (link checks, certificate fetches). For
    feed-sized downloads prefer :func:`safe_download`, which never holds the
    whole body in memory.
    """
    with safe_download(
        url,
        headers=headers,
        timeout=timeout,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        chunk_size=chunk_size,
        total_timeout=total_timeout,
    ) as (response, body):
        response._content = body.read()
        response._content_consumed = True
        return response
