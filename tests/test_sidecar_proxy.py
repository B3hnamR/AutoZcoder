"""Local-only SOCKS bridge routing, byte forwarding, and ownership tests."""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import sidecar_proxy as bridge


@pytest.fixture
def upstream(monkeypatch):
    created, records, peers, threads = [], [], [], []

    class Tunnel:
        def __init__(self):
            self.sock, peer = socket.socketpair()
            self.closed = False
            self.record = {}
            created.append(self)
            peers.append(peer)

        def set_proxy(self, *args, **kwargs):
            self.record.update(args=args, kwargs=kwargs)

        def connect(self, target):
            self.record['target'] = target
            records.append(self.record)
            peer = peers[created.index(self)]

            def echo():
                try:
                    while data := peer.recv(16384):
                        peer.sendall(data)
                except OSError:
                    pass
                finally:
                    peer.close()

            worker = threading.Thread(target=echo, daemon=True)
            threads.append(worker)
            worker.start()

        def close(self):
            self.closed = True
            self.sock.close()

        def __getattr__(self, key):
            return getattr(self.sock, key)

    monkeypatch.setattr(bridge.socks, 'socksocket', Tunnel)
    # A target resolution would prove an accidental direct/local-DNS path.
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **k:
                        real(host, *a, **k) if host == '127.0.0.1' else
                        pytest.fail('unexpected destination DNS'))
    yield created, records
    for peer in peers:
        peer.close()
    for worker in threads:
        worker.join(1)


def client(route, request=b'CONNECT target.invalid:443 HTTP/1.1\r\nHost: target.invalid\r\n\r\n'):
    parsed = urlsplit(route)
    sock = socket.create_connection(('127.0.0.1', parsed.port), timeout=2)
    sock.sendall(request)
    reply = bytearray()
    while b'\r\n\r\n' not in reply:
        part = sock.recv(4096)
        if not part:
            break
        reply.extend(part)
    return sock, bytes(reply)


@pytest.mark.parametrize('scheme,kind', [('socks4', bridge.socks.SOCKS4),
                                        ('socks4a', bridge.socks.SOCKS4),
                                        ('socks5', bridge.socks.SOCKS5),
                                        ('socks5h', bridge.socks.SOCKS5)])
def test_socks_destination_remote_dns_auth_and_unchanged_bytes(upstream, scheme, kind):
    created, records = upstream
    with bridge.sidecar_proxy_route(f'{scheme}://u%40ser:p%3Ass@192.0.2.1:1080') as route:
        assert route.startswith('http://127.0.0.1:')
        sock, reply = client(route)
        with sock:
            assert reply == b'HTTP/1.1 200 Connection Established\r\n\r\n'
            payload = b'\x16\x03\x01\x00\x08\x00opaque\xff'
            sock.sendall(payload)
            assert sock.recv(4096) == payload
        assert records == [{'args': (kind, '192.0.2.1', 1080),
                            'kwargs': {'rdns': True, 'username': 'u@ser', 'password': 'p:ss'},
                            'target': ('target.invalid', 443)}]
    assert all(s.closed for s in created)


@pytest.mark.parametrize('authority,target', [('target.invalid', ('target.invalid', 443)),
                                               ('[2001:db8::1]:8443', ('2001:db8::1', 8443))])
def test_connect_authority_defaults_and_ipv6(upstream, authority, target):
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        sock, reply = client(route, f'CONNECT {authority} HTTP/1.1\r\n\r\n'.encode())
        sock.close()
        assert b'200' in reply and upstream[1][0]['target'] == target


def test_tunnel_flushes_large_payload_before_half_close(upstream):
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        sock, _ = client(route)
        with sock:
            payload = bytes(range(256)) * 2048
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
            received = bytearray()
            while chunk := sock.recv(16384):
                received.extend(chunk)
            assert received == payload


@pytest.mark.parametrize('wire_request', [b'GET / HTTP/1.1\r\n\r\n',
    b'CONNECT user:secret@target.invalid:443 HTTP/1.1\r\n\r\n',
    b'CONNECT https://target.invalid/ HTTP/1.1\r\n\r\n',
    b'CONNECT target.invalid:0 HTTP/1.1\r\n\r\n',
    b'CONNECT target.invalid:99999 HTTP/1.1\r\n\r\n',
    b'CONNECT target.invalid:443 HTTP/1.1\r\nX: ' + b'x' * (16 << 10) + b'\r\n\r\n'])
def test_reject_invalid_requests_without_socks_connect(upstream, wire_request):
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        sock, reply = client(route, wire_request)
        sock.close()
        assert reply.startswith(b'HTTP/1.1 502')
        assert upstream[1] == []


def test_shutdown_closes_active_tunnels_and_idle_headers(upstream):
    start = time.monotonic()
    before = set(threading.enumerate())
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        tunnel, _ = client(route)
        idle = socket.create_connection(('127.0.0.1', urlsplit(route).port), timeout=2)
        idle.sendall(b'CONNECT')
    assert time.monotonic() - start < 5
    for sock in (tunnel, idle):
        with sock:
            try:
                assert sock.recv(4096) == b''
            except ConnectionResetError:
                pass
    assert all(s.closed for s in upstream[0])
    assert not [t for t in threading.enumerate() if t not in before and t.name.startswith('Thread-')]


def test_concurrency_limit_rejects_excess_without_new_worker(upstream, monkeypatch):
    monkeypatch.setattr(bridge, 'MAX_CONNECTIONS', 2)
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        first, _ = client(route)
        second, _ = client(route)
        extra = socket.create_connection(('127.0.0.1', urlsplit(route).port), timeout=2)
        with extra:
            assert extra.recv(1024) == b''
        assert len(upstream[0]) == 2
        first.close()
        second.close()


@pytest.mark.parametrize('route', ['', 'http://user:pw@127.0.0.1:80', 'https://proxy.invalid:443'])
def test_direct_and_http_routes_are_preserved(route):
    with bridge.sidecar_proxy_route(route) as selected:
        assert selected == route


def test_socks_failure_is_http_error_never_direct(upstream, monkeypatch):
    def fail(*a):
        raise OSError('offline selected proxy failure')
    monkeypatch.setattr(bridge.socks, 'socksocket', fail)
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        sock, reply = client(route)
        sock.close()
        assert reply.startswith(b'HTTP/1.1 502')


def test_partial_header_has_absolute_deadline(upstream, monkeypatch):
    monkeypatch.setattr(bridge, 'IDLE_TIMEOUT', 0.1)
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        sock = socket.create_connection(('127.0.0.1', urlsplit(route).port), timeout=2)
        with sock:
            sock.sendall(b'CONNECT target.invalid:443 HTTP/1.1\r\n')
            assert sock.recv(4096).startswith(b'HTTP/1.1 502')
        assert upstream[1] == []


def test_shutdown_interrupts_owned_upstream_connect(upstream, monkeypatch):
    made, entered = threading.Event(), threading.Event()
    original = bridge.socks.socksocket

    def blocked_socket():
        tunnel = original()
        made.set()

        def connect(target):
            entered.set()
            tunnel.sock.recv(1)  # closing the owned socket must wake this

        tunnel.connect = connect
        return tunnel

    monkeypatch.setattr(bridge.socks, 'socksocket', blocked_socket)
    start = time.monotonic()
    with bridge.sidecar_proxy_route('socks5://192.0.2.1:1080') as route:
        sock = socket.create_connection(('127.0.0.1', urlsplit(route).port), timeout=2)
        sock.sendall(b'CONNECT target.invalid:443 HTTP/1.1\r\n\r\n')
        assert made.wait(1) and entered.wait(1)
    sock.close()
    assert time.monotonic() - start < 5 and upstream[0][0].closed


def test_claim_sidecar_bridge_scope_preserves_parent_route(upstream, monkeypatch):
    import claim_start as cs
    monkeypatch.setenv('ZAI_CAPTCHA_CMD', 'offline-sidecar')
    monkeypatch.delenv('ZAI_CAPTCHA_PARAM', raising=False)
    selected = 'socks5://u:secret@192.0.2.1:1080'
    monkeypatch.setenv('ZAI_PROXY', selected)
    monkeypatch.setattr(cs, '_captcha_config', lambda *_: {})
    routes = []

    def run(argv, env):
        routes.append(env['ZAI_CAPTCHA_PROXY'])
        assert os.environ['ZAI_PROXY'] == selected and env['ZAI_PROXY'] == selected
        sock, reply = client(routes[-1])
        sock.close()
        assert b'200' in reply
        return subprocess.CompletedProcess(argv, 0, 'V' * 40 + ' sgp\n', '')

    monkeypatch.setattr(cs, '_run_sidecar', run)
    assert cs._captcha_token(lambda *_: None) == ('V' * 40, 'sgp')
    assert os.environ['ZAI_PROXY'] == selected
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', urlsplit(routes[0]).port), timeout=0.2)
