import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


@pytest.fixture
def local_server(monkeypatch):
    """Serve a body over real HTTP on 127.0.0.1 and return its URL.

    ``start(length, chunks, headers=None, delay=0)`` answers every GET with
    ``Content-Length: length`` and writes ``chunks`` with ``delay`` seconds
    between them; send fewer bytes than ``length`` for a truncated body. The
    SSRF check rejects loopback, so it is bypassed for this server only.
    """
    servers = []

    def start(length, chunks, headers=None, delay=0):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(length))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    for chunk in chunks:
                        self.wfile.write(chunk)
                        time.sleep(delay)
                except OSError:
                    pass  # the client hung up
                self.close_connection = True

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        port = server.server_address[1]
        monkeypatch.setenv('no_proxy', '127.0.0.1')  # requests would send it to HTTP_PROXY
        monkeypatch.setattr(
            'data_manager.net_security.resolve_public_ips',
            lambda url: ('127.0.0.1', port, {'127.0.0.1'}),
        )
        return f'http://127.0.0.1:{port}/feed'

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
