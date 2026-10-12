"""Unit tests for the SSRF guard (data_manager.net_security). No real network:
DNS is mocked via socket.getaddrinfo and HTTP via requests.get."""

import socket

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

    monkeypatch.setattr('data_manager.net_security.requests.get', fake_get)
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

    monkeypatch.setattr('data_manager.net_security.requests.get', fake_get)
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
    monkeypatch.setattr(
        'data_manager.net_security.requests.get',
        lambda *a, **kw: _FakeResponse(body=b'PK\x03\x04data'),
    )
    response = safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)
    assert response.content == b'PK\x03\x04data'


def test_safe_get_blocks_redirect_to_private_host(monkeypatch):
    responses = iter([_FakeResponse(redirect_to='https://internal.example.org/secret')])

    def fake_dns(host, port, *args, **kwargs):
        ip = '93.184.216.34' if host in ('feeds.example.org', '93.184.216.34') else '10.0.0.5'
        return _addrinfo(ip)(host, port)

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', fake_dns)
    monkeypatch.setattr('data_manager.net_security.requests.get', lambda *a, **kw: next(responses))
    with pytest.raises(OutboundURLBlocked):
        safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)


def test_safe_get_blocks_redirect_without_location(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    response = _FakeResponse(redirect_to='x')
    response.headers = {}
    monkeypatch.setattr('data_manager.net_security.requests.get', lambda *a, **kw: response)
    with pytest.raises(OutboundURLBlocked):
        safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)


def test_safe_get_blocks_redirect_loop(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    monkeypatch.setattr(
        'data_manager.net_security.requests.get',
        lambda *a, **kw: _FakeResponse(redirect_to='https://feeds.example.org/loop'),
    )
    with pytest.raises(OutboundURLBlocked, match='redirect'):
        safe_get('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1024)


def test_safe_get_aborts_oversized_body(monkeypatch):
    _patch_dns(monkeypatch, '93.184.216.34')
    monkeypatch.setattr(
        'data_manager.net_security.requests.get',
        lambda *a, **kw: _FakeResponse(chunks=[b'a' * 600, b'b' * 600]),
    )
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

    monkeypatch.setattr('data_manager.net_security.requests.get', fake_get)
    monkeypatch.setattr(
        'data_manager.net_security._ca_bundle_with_aia_intermediates',
        lambda host, port: '/tmp/fake-bundle.pem',
    )
    response = safe_get('https://broken-chain.example.org/f.zip', timeout=5, max_bytes=1024)
    assert response.content == b'PK\x03\x04ok'
    assert calls == [True, '/tmp/fake-bundle.pem']  # retried once with the bundle


def test_other_ssl_errors_are_not_retried(monkeypatch):
    import requests as requests_module

    _patch_dns(monkeypatch, '93.184.216.34')

    def fake_get(url, **kwargs):
        raise requests_module.exceptions.SSLError('handshake failure')

    monkeypatch.setattr('data_manager.net_security.requests.get', fake_get)
    bundle_mock_called = []
    monkeypatch.setattr(
        'data_manager.net_security._ca_bundle_with_aia_intermediates',
        lambda host, port: bundle_mock_called.append(1) or '/tmp/x',
    )
    with pytest.raises(requests_module.exceptions.SSLError):
        safe_get('https://feeds.example.org/f.zip', timeout=5, max_bytes=1024)
    assert not bundle_mock_called  # AIA path only for verify failures


def test_verify_failure_propagates_when_chain_cannot_be_completed(monkeypatch):
    import requests as requests_module

    _patch_dns(monkeypatch, '93.184.216.34')

    def fake_get(url, **kwargs):
        raise requests_module.exceptions.SSLError('certificate verify failed')

    monkeypatch.setattr('data_manager.net_security.requests.get', fake_get)
    monkeypatch.setattr(
        'data_manager.net_security._ca_bundle_with_aia_intermediates',
        lambda host, port: None,
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

    monkeypatch.setattr('data_manager.net_security.requests.get', fake_get)
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
    monkeypatch.setattr(
        'data_manager.net_security.requests.get',
        lambda *a, **kw: _FakeResponse(chunks=chunks),
    )

    with safe_download('https://feeds.example.org/gtfs.zip', timeout=5, max_bytes=1 << 20) as (
        _response,
        body,
    ):
        assert body.read() == b'x' * 16384
        body.seek(0)
        assert body.read(4) == b'xxxx'  # rewindable, i.e. a real stream


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

    monkeypatch.setattr(
        'data_manager.net_security.requests.get',
        lambda *a, **kw: _FakeResponse(chunks=endless_chunks()),
    )
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
