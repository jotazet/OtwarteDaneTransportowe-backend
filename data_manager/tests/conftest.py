import socket

import pytest


@pytest.fixture
def fake_dns(monkeypatch):
    """Resolve SSRF-checked URLs without real DNS.

    Every host resolves to a public address; map a host to another IP in the
    returned dict, e.g. ``fake_dns['intranet.example.org'] = '10.0.0.5'``.
    """
    ips = {}

    def getaddrinfo(host, port, *args, **kwargs):
        ip = ips.get(host, '93.184.216.34')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

    monkeypatch.setattr('data_manager.net_security.socket.getaddrinfo', getaddrinfo)
    return ips
