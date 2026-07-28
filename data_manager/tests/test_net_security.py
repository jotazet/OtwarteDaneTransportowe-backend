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

@pytest.mark.parametrize('blocked_ip', [
    '127.0.0.1',      # loopback
    '10.0.0.5',       # private
    '192.168.1.10',   # private
    '172.16.0.1',     # private
    '169.254.1.1',    # link-local
    '224.0.0.1',      # multicast
    '0.0.0.0',        # unspecified
])
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


@pytest.mark.parametrize('url', [
    'ftp://feeds.example.org/gtfs.zip',   # scheme
    'file:///etc/passwd',                 # scheme
    'https://user:pass@example.org/x',    # userinfo
    'https:///nohost',                    # missing hostname
    '',                                   # empty
])
def test_malformed_or_disallowed_urls_are_blocked(monkeypatch, url):
    _patch_dns(monkeypatch, '93.184.216.34')
    with pytest.raises(OutboundURLBlocked):
        assert_safe_outbound_url(url)


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
