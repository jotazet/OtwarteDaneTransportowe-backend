"""Unit tests for the SSRF guard (data_manager.net_security). No real network:
DNS is mocked via socket.getaddrinfo and HTTP via requests.get."""

import socket
from urllib.parse import urlsplit

import pytest

from data_manager.net_security import (
    OutboundURLBlocked,
    assert_safe_outbound_url,
    resolve_public_ips,
    safe_get,
)


def _addrinfo(*ips):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port if isinstance(port, int) else 80))
            for ip in ips
        ]

    return fake_getaddrinfo


def _patch_dns(monkeypatch, *ips):
    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', _addrinfo(*ips))


def _patch_get(monkeypatch, fake_get):
    """Patch requests.get with ``fake_get``, first resolving the host as urllib3 does.

    urllib3 resolves the URL's host as is, a trailing dot included.

    safe_get fails closed when a connection bypasses the DNS pin, so a fake
    that never resolves anything would look like exactly that bypass.
    """

    def connecting_get(url, *args, **kwargs):
        socket.getaddrinfo(urlsplit(url).hostname, 443)
        return fake_get(url, *args, **kwargs)

    monkeypatch.setattr('data_manager.net_security.requests.get', connecting_get)


# ---------------------------------------------------------------------------
# resolve_public_ips / assert_safe_outbound_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'blocked_ip',
    [
        '127.0.0.1',  # loopback
        '10.0.0.5',  # private
        '192.168.1.10',  # private
        '172.16.0.1',  # private
        '169.254.1.1',  # link-local
        '224.0.0.1',  # multicast
        '0.0.0.0',  # unspecified
    ],
)
def test_non_public_ips_are_blocked(monkeypatch, blocked_ip):
    _patch_dns(monkeypatch, blocked_ip)
    with pytest.raises(OutboundURLBlocked):
        assert_safe_outbound_url('https://feeds.example.org/gtfs.zip')


def test_public_ip_is_allowed(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    hostname, port, ips = resolve_public_ips('https://feeds.example.org/gtfs.zip')
    assert hostname == 'feeds.example.org'
    assert port == 443
    assert ips == {'93.184.216.34'}


def test_any_private_ip_in_mixed_resolution_blocks(monkeypatch):
    # DNS-rebinding style: one public and one private A record.
    _patch_dns(monkeypatch, '93.184.216.34', '10.0.0.5')
    with pytest.raises(OutboundURLBlocked):
        assert_safe_outbound_url('https://feeds.example.org/gtfs.zip')


@pytest.mark.parametrize(
    'url',
    [
        'ftp://feeds.example.org/gtfs.zip',  # scheme
        'file:///etc/passwd',  # scheme
        'https:///nohost',  # missing hostname
        '',  # empty
    ],
)
def test_malformed_or_disallowed_urls_are_blocked(monkeypatch, url):
    _patch_dns(monkeypatch, '93.184.216.34')
    with pytest.raises(OutboundURLBlocked):
        assert_safe_outbound_url(url)


# ---------------------------------------------------------------------------
# Userinfo URLs (intentionally-public credentials)
# ---------------------------------------------------------------------------


def test_split_userinfo_decodes_and_builds_basic_header():
    import base64

    from data_manager.net_security import split_userinfo

    clean, headers = split_userinfo('https://kd_sample:Hm5mEuPC%3F1h7@kd.example.org/gtfs/feed.zip')
    assert clean == 'https://kd.example.org/gtfs/feed.zip'
    expected = base64.b64encode(b'kd_sample:Hm5mEuPC?1h7').decode()
    assert headers == {'Authorization': f'Basic {expected}'}

    # No credentials -> passthrough.
    assert split_userinfo('https://example.org/x') == ('https://example.org/x', {})


def test_userinfo_url_validates_against_real_host(monkeypatch):
    # `https://trusted.com@evil-host/` must be validated against evil-host.
    def fake_dns(host, port, *args, **kwargs):
        ip = '10.0.0.5' if host == 'internal.example.org' else '93.184.216.34'
        return _addrinfo(ip)(host, port)

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', fake_dns)
    with pytest.raises(OutboundURLBlocked):
        assert_safe_outbound_url('https://feeds.example.org@internal.example.org/x')
    assert_safe_outbound_url('https://user:pass@feeds.example.org/x')  # public host: OK


def test_safe_get_converts_userinfo_to_basic_auth(monkeypatch):
    import base64

    _patch_dns(monkeypatch, '93.184.216.34')
    captured = {}

    def fake_get(url, headers=None, **kwargs):
        captured['url'] = url
        captured['headers'] = headers
        return _FakeResponse(body=b'PK\x03\x04data')

    _patch_get(monkeypatch, fake_get)
    response = safe_get(
        'https://kd_sample:Hm5mEuPC%3F1h7@feeds.example.org/feed.zip',
        timeout=5,
        max_bytes=1024,
    )
    assert response.content == b'PK\x03\x04data'
    assert '@' not in captured['url']  # credentials never travel in the URL
    expected = base64.b64encode(b'kd_sample:Hm5mEuPC?1h7').decode()
    assert captured['headers']['Authorization'] == f'Basic {expected}'


def test_safe_get_explicit_auth_header_wins_over_userinfo(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    captured = {}

    def fake_get(url, headers=None, **kwargs):
        captured['headers'] = headers
        return _FakeResponse(body=b'ok')

    _patch_get(monkeypatch, fake_get)
    safe_get(
        'https://user:pass@feeds.example.org/feed.zip',
        headers={'Authorization': 'Bearer explicit-token'},
        timeout=5,
        max_bytes=1024,
    )
    assert captured['headers']['Authorization'] == 'Bearer explicit-token'


def test_dns_failure_is_blocked(monkeypatch):
    def failing(host, *args, **kwargs):
        raise socket.gaierror('NXDOMAIN')

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', failing)
    with pytest.raises(OutboundURLBlocked):
        assert_safe_outbound_url('https://nxdomain.example.org/feed')


# ---------------------------------------------------------------------------
# safe_get — redirects and size cap
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, body=b'', redirect_to=None, chunks=None):
        self._body = body
        self._chunks = chunks
        self.is_redirect = redirect_to is not None
        self.headers = {'Location': redirect_to} if redirect_to else {}
        self.status_code = 302 if redirect_to else 200
        self.closed = False

    def iter_content(self, chunk_size=65536):
        if self._chunks is not None:
            yield from self._chunks
        else:
            yield self._body

    @property
    def content(self):
        # safe_get populates _content like requests.Response does.
        return self._content

    def close(self):
        self.closed = True


def test_safe_get_returns_body(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    _patch_get(monkeypatch, lambda *a, **kw: _FakeResponse(body=b'PK\x03\x04data'))
    response = safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)
    assert response.content == b'PK\x03\x04data'


def test_safe_get_blocks_redirect_to_private_host(monkeypatch):
    responses = iter([_FakeResponse(redirect_to='https://internal.example.org/secret')])

    def fake_dns(host, port, *args, **kwargs):
        ip = '93.184.216.34' if host in ('feeds.example.org', '93.184.216.34') else '10.0.0.5'
        return _addrinfo(ip)(host, port)

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', fake_dns)
    _patch_get(monkeypatch, lambda *a, **kw: next(responses))
    with pytest.raises(OutboundURLBlocked):
        safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)


def test_safe_get_blocks_redirect_without_location(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    response = _FakeResponse(redirect_to='x')
    response.headers = {}
    _patch_get(monkeypatch, lambda *a, **kw: response)
    with pytest.raises(OutboundURLBlocked):
        safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)


def test_safe_get_blocks_redirect_loop(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    _patch_get(
        monkeypatch, lambda *a, **kw: _FakeResponse(redirect_to='https://feeds.example.org/loop')
    )
    with pytest.raises(OutboundURLBlocked, match='redirect'):
        safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)


def test_safe_get_aborts_oversized_body(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    _patch_get(monkeypatch, lambda *a, **kw: _FakeResponse(chunks=[b'a' * 600, b'b' * 600]))
    with pytest.raises(OutboundURLBlocked, match='byte limit'):
        safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1000)


# ---------------------------------------------------------------------------
# Incomplete server chains (AIA completion)
# ---------------------------------------------------------------------------


def test_verify_failure_retries_with_aia_bundle(monkeypatch):
    import requests as requests_module

    _patch_dns(monkeypatch, '93.184.216.34')
    calls = []

    def fake_get(url, verify=True, **kwargs):
        calls.append(verify)
        if len(calls) == 1:
            raise requests_module.exceptions.SSLError(
                'certificate verify failed: unable to get local issuer certificate'
            )
        return _FakeResponse(body=b'PK\x03\x04ok')

    _patch_get(monkeypatch, fake_get)
    monkeypatch.setattr(
        'data_manager.net_security._ca_bundle_with_aia_intermediates',
        lambda host, port, allowed_ips: '/tmp/fake-bundle.pem',
    )
    response = safe_get('https://broken-chain.example.org/f.zip', timeout=5, max_bytes=1024)
    assert response.content == b'PK\x03\x04ok'
    assert calls == [True, '/tmp/fake-bundle.pem']  # retried once with the bundle


def test_other_ssl_errors_are_not_retried(monkeypatch):
    import requests as requests_module

    _patch_dns(monkeypatch, '93.184.216.34')

    def fake_get(url, **kwargs):
        raise requests_module.exceptions.SSLError('handshake failure')

    _patch_get(monkeypatch, fake_get)
    bundle_mock_called = []
    monkeypatch.setattr(
        'data_manager.net_security._ca_bundle_with_aia_intermediates',
        lambda host, port, allowed_ips: bundle_mock_called.append(1) or '/tmp/x',
    )
    with pytest.raises(requests_module.exceptions.SSLError):
        safe_get('https://feeds.example.org/f.zip', timeout=5, max_bytes=1024)
    assert not bundle_mock_called  # AIA path only for verify failures


def test_verify_failure_propagates_when_chain_cannot_be_completed(monkeypatch):
    import requests as requests_module

    _patch_dns(monkeypatch, '93.184.216.34')

    def fake_get(url, **kwargs):
        raise requests_module.exceptions.SSLError('certificate verify failed')

    _patch_get(monkeypatch, fake_get)
    monkeypatch.setattr(
        'data_manager.net_security._ca_bundle_with_aia_intermediates',
        lambda host, port, allowed_ips: None,
    )
    with pytest.raises(requests_module.exceptions.SSLError):
        safe_get('https://feeds.example.org/f.zip', timeout=5, max_bytes=1024)


# ---------------------------------------------------------------------------
# Credentials must not survive a redirect off the original origin
# ---------------------------------------------------------------------------


def _multi_host_dns(monkeypatch, mapping: dict):
    """Resolve each hostname to its own public IP.

    A literal IP resolves to itself, mirroring real getaddrinfo — _pinned_dns
    relies on exactly that to re-resolve the addresses it pinned.
    """

    def fake(host, port, *args, **kwargs):
        if host[0].isdigit():
            return _addrinfo(host)(host, port)
        return _addrinfo(mapping.get(host, '93.184.216.34'))(host, port)

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', fake)


def _recording_get(monkeypatch, responses):
    """Patch requests.get, recording the headers sent on each hop."""
    sent = []
    queue = iter(responses)

    def fake_get(url, headers=None, **kwargs):
        sent.append((url, dict(headers or {})))
        return next(queue)

    _patch_get(monkeypatch, fake_get)
    return sent


def test_auth_header_is_dropped_on_cross_host_redirect(monkeypatch):
    """A feed that 302s elsewhere must not hand over its stored API key.

    safe_get follows redirects by hand, and used to replay the caller's headers
    verbatim on every hop — so any upstream able to redirect could harvest the
    feed's decrypted auth_value. ``requests`` strips auth across hosts; so do we.
    """
    _multi_host_dns(
        monkeypatch,
        {
            'feeds.example.org': '93.184.216.34',
            'evil.example.com': '1.1.1.1',
        },
    )
    sent = _recording_get(
        monkeypatch,
        [
            _FakeResponse(redirect_to='https://evil.example.com/collect'),
            _FakeResponse(body=b'PK\x03\x04payload'),
        ],
    )

    safe_get(
        'https://feeds.example.org/gtfs.zip',
        headers={'X-API-Key': 'super-secret'},
        timeout=5,
        max_bytes=1024,
    )

    first_url, first_headers = sent[0]
    second_url, second_headers = sent[1]
    assert first_headers.get('X-API-Key') == 'super-secret'
    assert 'evil.example.com' in second_url
    assert 'X-API-Key' not in second_headers, second_headers


def test_userinfo_credentials_are_dropped_on_cross_host_redirect(monkeypatch):
    """user:pass@ in the original URL must not follow the redirect either."""
    _multi_host_dns(
        monkeypatch,
        {
            'feeds.example.org': '93.184.216.34',
            'evil.example.com': '1.1.1.1',
        },
    )
    sent = _recording_get(
        monkeypatch,
        [
            _FakeResponse(redirect_to='https://evil.example.com/collect'),
            _FakeResponse(body=b'ok'),
        ],
    )

    safe_get('https://user:pw@feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)

    assert 'Authorization' in sent[0][1]
    assert 'Authorization' not in sent[1][1], sent[1][1]


def test_auth_header_survives_same_host_redirect(monkeypatch):
    """Redirects within one origin are ordinary; the credential must be kept."""
    _multi_host_dns(monkeypatch, {'feeds.example.org': '93.184.216.34'})
    sent = _recording_get(
        monkeypatch,
        [
            _FakeResponse(redirect_to='https://feeds.example.org/gtfs/latest.zip'),
            _FakeResponse(body=b'PK\x03\x04payload'),
        ],
    )

    safe_get(
        'https://feeds.example.org/gtfs.zip',
        headers={'X-API-Key': 'super-secret'},
        timeout=5,
        max_bytes=1024,
    )

    assert sent[1][1].get('X-API-Key') == 'super-secret'


def test_auth_header_is_dropped_on_https_to_http_downgrade(monkeypatch):
    """Same host, but the credential would go out in clear text."""
    _multi_host_dns(monkeypatch, {'feeds.example.org': '93.184.216.34'})
    sent = _recording_get(
        monkeypatch,
        [
            _FakeResponse(redirect_to='http://feeds.example.org/gtfs.zip'),
            _FakeResponse(body=b'ok'),
        ],
    )

    safe_get(
        'https://feeds.example.org/gtfs.zip',
        headers={'Authorization': 'Bearer tok'},
        timeout=5,
        max_bytes=1024,
    )

    assert 'Authorization' not in sent[1][1], sent[1][1]


# ---------------------------------------------------------------------------
# Streaming and size caps
# ---------------------------------------------------------------------------


def test_safe_download_streams_without_materialising_the_body(monkeypatch):
    from data_manager.net_security import safe_download

    _patch_dns(monkeypatch, '93.184.216.34')
    chunks = [b'x' * 1024] * 16
    _patch_get(monkeypatch, lambda *a, **kw: _FakeResponse(chunks=chunks))

    with safe_download('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1 << 20) as (
        _response,
        body,
    ):
        assert body.read() == b'x' * 16384
        body.seek(0)
        assert body.read(4) == b'xxxx'  # rewindable, i.e. a real stream


def test_safe_download_can_write_straight_into_a_file(monkeypatch, tmp_path):
    from data_manager.net_security import safe_download

    _patch_dns(monkeypatch, '93.184.216.34')
    _patch_get(monkeypatch, lambda *a, **kw: _FakeResponse(chunks=[b'ab', b'cd']))
    target = tmp_path / 'region.osm.pbf.tmp'

    with target.open('w+b') as file:
        with safe_download(
            'https://download.example.org/poland.osm.pbf', timeout=5, max_bytes=1 << 20, into=file
        ) as (_response, body):
            assert body is file
        assert not file.closed  # the caller owns it

    assert target.read_bytes() == b'abcd'


def test_aia_intermediate_fetch_is_size_capped(monkeypatch, tmp_path):
    """The issuer URL comes from an UNTRUSTED server certificate.

    The old code did requests.get() and only checked len(response.content)
    afterwards, so an issuer URL serving gigabytes was fully buffered first.
    The fetch goes through safe_get now, which aborts mid-stream.
    """
    import data_manager.net_security as ns

    _patch_dns(monkeypatch, '93.184.216.34')
    monkeypatch.setattr(ns, '_AIA_BUNDLE_CACHE', {})

    served = {'bytes': 0}

    def endless_chunks():
        while True:
            served['bytes'] += 8192
            if served['bytes'] > 50 * 1024 * 1024:  # fuse: the cap must bite first
                raise AssertionError('size cap did not stop the download')
            yield b'\x00' * 8192

    _patch_get(monkeypatch, lambda *a, **kw: _FakeResponse(chunks=endless_chunks()))
    with pytest.raises(OutboundURLBlocked, match='byte limit'):
        ns.safe_get(
            'https://ca.example.org/intermediate.der',
            timeout=5,
            max_bytes=ns._AIA_INTERMEDIATE_MAX_BYTES,
        )
    assert served['bytes'] <= ns._AIA_INTERMEDIATE_MAX_BYTES + 8192


# ---------------------------------------------------------------------------
# DNS pinning is scoped to the installing thread
# ---------------------------------------------------------------------------


def test_dns_pin_does_not_leak_into_other_threads(monkeypatch):
    """socket.getaddrinfo is process-global.

    Without the thread check, one task's pin rewrote every other concurrent
    connection's DNS — a cross-request hijack under any threaded pool.
    """
    import socket as socket_module
    import threading

    from data_manager.net_security import _pinned_dns

    def fake(host, port, *args, **kwargs):
        # Literal IPs resolve to themselves, as the real resolver does.
        return _addrinfo(host if host[0].isdigit() else '8.8.4.4')(host, port)

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', fake)

    seen = {}
    started = threading.Event()
    release = threading.Event()

    def other_thread():
        started.wait(timeout=5)
        seen['ip'] = socket_module.getaddrinfo('feeds.example.org', 443)[0][4][0]
        release.set()

    worker = threading.Thread(target=other_thread)
    worker.start()
    with _pinned_dns('feeds.example.org', {'104.16.0.1'}):
        assert socket_module.getaddrinfo('feeds.example.org', 443)[0][4][0] == '104.16.0.1'
        started.set()
        release.wait(timeout=5)
    worker.join(timeout=5)

    assert seen['ip'] == '8.8.4.4', 'the pin leaked into another thread'


# ---------------------------------------------------------------------------
# Hostname normalisation: validate, pin and request one ASCII name
# ---------------------------------------------------------------------------

IDN_URL = 'https://łódź.example.org/gtfs.zip'
IDN_ASCII_HOST = 'xn--d-uga0v4h.example.org'  # what requests/urllib3 resolve


def _once_public_dns(monkeypatch, name):
    """``name`` resolves to a public IP once, then to a private one.

    Other names always resolve to the public IP; literal IPs to themselves, as
    the real resolver does. Returns the list of non-literal names looked up.
    """
    lookups = []

    def fake(host, port, *args, **kwargs):
        if host[0].isdigit():
            return _addrinfo(host)(host, port)
        lookups.append(host)
        rebound = host == name and lookups.count(name) > 1
        return _addrinfo('10.0.0.5' if rebound else '93.184.216.34')(host, port)

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', fake)
    return lookups


def test_idn_hostname_is_validated_in_its_ascii_form(monkeypatch):
    lookups = _once_public_dns(monkeypatch, IDN_ASCII_HOST)
    assert resolve_public_ips(IDN_URL) == (IDN_ASCII_HOST, 443, {'93.184.216.34'})
    assert lookups == [IDN_ASCII_HOST]


@pytest.mark.parametrize(
    ('url', 'name'),
    [(IDN_URL, IDN_ASCII_HOST), ('https://feeds.example.org./x', 'feeds.example.org.')],
    ids=['idn', 'trailing-dot'],
)
def test_request_goes_through_the_pin(monkeypatch, url, name):
    lookups = _once_public_dns(monkeypatch, name)
    sent = []

    def fake_get(url, headers=None, **kwargs):
        host = urlsplit(url).hostname  # what urllib3 resolves
        sent.append((host, socket.getaddrinfo(host, 443)[0][4][0], headers))
        return _FakeResponse(body=b'ok')

    monkeypatch.setattr('data_manager.net_security.requests.get', fake_get)
    response = safe_get(url, headers={'X-Api-Key': 'k'}, timeout=5, max_bytes=1024)

    assert response.content == b'ok'
    # The validated IP, not the rebound one; same origin, so the key is kept.
    assert sent == [(name, '93.184.216.34', {'X-Api-Key': 'k'})]
    assert lookups == [name]  # one real lookup: validation only


def test_idn_redirect_target_is_normalised_and_pinned(monkeypatch):
    lookups = _once_public_dns(monkeypatch, IDN_ASCII_HOST)
    sent = _recording_get(
        monkeypatch, [_FakeResponse(redirect_to=IDN_URL), _FakeResponse(body=b'ok')]
    )

    assert safe_get('https://feeds.example.org/x', timeout=5, max_bytes=1024).content == b'ok'
    assert urlsplit(sent[1][0]).hostname == IDN_ASCII_HOST
    assert lookups == ['feeds.example.org', IDN_ASCII_HOST]  # validation only, per hop


def test_pin_refuses_other_names_before_connecting(monkeypatch):
    """Fail closed: a name spelled differently gets no unvalidated lookup."""
    from data_manager.net_security import _pinned_dns

    lookups = _once_public_dns(monkeypatch, IDN_ASCII_HOST)
    with _pinned_dns(IDN_ASCII_HOST, {'93.184.216.34'}):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo('łódź.example.org', 443)
        assert socket.getaddrinfo('93.184.216.34', 443)[0][4][0] == '93.184.216.34'
    assert lookups == []


def test_response_without_a_pinned_lookup_is_refused(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    response = _FakeResponse(body=b'ok')
    monkeypatch.setattr('data_manager.net_security.requests.get', lambda *a, **kw: response)
    with pytest.raises(OutboundURLBlocked, match='validated address'):
        safe_get(IDN_URL, timeout=5, max_bytes=1024)
    assert response.closed


def test_invalid_idn_hostname_is_blocked():
    with pytest.raises(OutboundURLBlocked):
        assert_safe_outbound_url('https://ł' + 'a' * 70 + '.example.org/x')  # label > 63


def test_aia_leaf_certificate_fetch_is_pinned(monkeypatch):
    import ssl

    from data_manager.net_security import _ca_bundle_with_aia_intermediates

    lookups = _once_public_dns(monkeypatch, IDN_ASCII_HOST)
    handshakes = []

    def fake_get_server_certificate(addr, timeout=None):
        handshakes.append(socket.getaddrinfo(*addr)[0][4][0])
        raise ssl.SSLError('stop here')

    monkeypatch.setattr(ssl, 'get_server_certificate', fake_get_server_certificate)
    monkeypatch.setattr('data_manager.net_security._AIA_BUNDLE_CACHE', {})
    assert _ca_bundle_with_aia_intermediates(IDN_ASCII_HOST, 443, {'93.184.216.34'}) is None
    assert handshakes == ['93.184.216.34']
    assert lookups == []


@pytest.mark.parametrize(
    'url',
    [
        'http://ŁÓDŹ.Example.org./x',
        'http://feeds.example.org/x',
        'http://93.184.216.34/x',
        'http://[2606:2800:220:1::1]/x',
    ],
    ids=['idn-upper-trailing-dot', 'ascii', 'ipv4-literal', 'ipv6-literal'],
)
def test_pin_answers_the_lookup_real_requests_makes(monkeypatch, url):
    """The real requests/urllib3 stack, DNS mocked and a local server.

    Validation lookups (no family/type arguments) see a public IP; connect
    lookups (urllib3 passes family and type) are routed to the local server.
    The fetch succeeds only if urllib3's lookup hit the pin: any other name
    is refused, and a response without a pinned lookup is discarded.
    """
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_getaddrinfo = socket.getaddrinfo
    connect_lookups = []

    def fake(host, port, *args, **kwargs):
        if not args:  # validation
            ip = host if host[0].isdigit() else '93.184.216.34'
            return _addrinfo(ip)(host, port)
        connect_lookups.append(host)
        return real_getaddrinfo('127.0.0.1', server.server_port, *args, **kwargs)

    monkeypatch.setenv('NO_PROXY', '*')
    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', fake)
    try:
        response = safe_get(url, timeout=5, max_bytes=1024)
    finally:
        server.shutdown()
        server.server_close()

    assert response.content == b'ok'
    # urllib3 asked for the URL's name; only the pin's own lookup of the
    # validated IP got through to the resolver.
    assert connect_lookups == ['2606:2800:220:1::1' if '[' in url else '93.184.216.34']
