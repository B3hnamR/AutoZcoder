"""An owned loopback CONNECT bridge for sidecars without native SOCKS support."""
import ipaddress
import re
import select
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

import socks
MAX_CONNECTIONS = 32
MAX_HEADER = 16 << 10
IDLE_TIMEOUT = 15

def _destination(authority):
    if not authority or any(c in authority for c in '/@?#\\'):
        raise ValueError('invalid CONNECT authority')
    parsed = urlsplit('//' + authority)
    host, port = parsed.hostname, parsed.port or 443
    if not host or not 1 <= port <= 65535 or authority.endswith(':') or parsed.port == 0:
        raise ValueError('invalid CONNECT destination')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', p)
                                      for p in host.rstrip('.').split('.')):
            raise ValueError('invalid CONNECT hostname')
    return host, port

@contextmanager
def sidecar_proxy_route(proxy_url):
    """Keep HTTP/direct routes unchanged; tunnel SOCKS without local target DNS."""
    if not proxy_url:
        yield ''
        return
    try:
        proxy = urlsplit(proxy_url)
        if (not proxy.hostname or not proxy.port or proxy.path not in ('', '/')
                or proxy.query or proxy.fragment or any(c.isspace() for c in proxy_url)):
            raise ValueError()
        kind = {'socks4': socks.SOCKS4, 'socks4a': socks.SOCKS4,
                'socks5': socks.SOCKS5, 'socks5h': socks.SOCKS5}.get(proxy.scheme)
        if proxy.scheme not in ('http', 'https') and kind is None:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ValueError('invalid sidecar proxy route') from None
    if kind is None:
        yield proxy_url
        return

    stop = threading.Event()
    lock = threading.Lock()
    active, workers = set(), set()
    listener = socket.create_server(('127.0.0.1', 0), backlog=MAX_CONNECTIONS)
    listener.settimeout(0.2)

    def close(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def serve(client):
        upstream = None
        established = False
        try:
            deadline, header = time.monotonic() + IDLE_TIMEOUT, bytearray()
            while b'\r\n\r\n' not in header:
                client.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = client.recv(min(4096, MAX_HEADER + 1 - len(header)))
                if not chunk or time.monotonic() >= deadline:
                    return
                header.extend(chunk)
                if len(header) > MAX_HEADER:
                    raise ValueError('CONNECT header too large')
            lines, initial = bytes(header).split(b'\r\n\r\n', 1)
            method, authority, version = lines.split(b'\r\n', 1)[0].decode('ascii').split(' ')
            if method != 'CONNECT' or version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError('CONNECT required')
            target = _destination(authority)
            upstream = socks.socksocket()
            with lock:
                if stop.is_set():
                    return
                active.add(upstream)
            upstream.settimeout(4)
            upstream.set_proxy(kind, proxy.hostname, proxy.port, rdns=True,
                               username=unquote(proxy.username) if proxy.username is not None else None,
                               password=unquote(proxy.password) if proxy.password is not None else None)
            upstream.connect(target)
            client.settimeout(4)
            client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            established = True
            client.setblocking(False)
            upstream.setblocking(False)
            pending = {client: bytearray(), upstream: bytearray(initial)}
            eof, half_closed = set(), set()
            last = time.monotonic()
            while not stop.is_set() and time.monotonic() - last < IDLE_TIMEOUT:
                for source, dest in ((client, upstream), (upstream, client)):
                    if source in eof and dest not in half_closed and not pending[dest]:
                        dest.shutdown(socket.SHUT_WR)
                        half_closed.add(dest)
                if len(eof) == 2 and not any(pending.values()):
                    return
                readable, writable, _ = select.select(
                    [s for s, other in ((client, upstream), (upstream, client))
                     if s not in eof and len(pending[other]) < 64 << 10],
                    [s for s in pending if pending[s]], [], 0.2)
                for sock in writable:
                    try:
                        sent = sock.send(pending[sock])
                    except BlockingIOError:
                        continue
                    if not sent:
                        return
                    del pending[sock][:sent]
                    last = time.monotonic()
                for sock in readable:
                    other = upstream if sock is client else client
                    try:
                        data = sock.recv(min(16384, (64 << 10) - len(pending[other])))
                    except BlockingIOError:
                        continue
                    if not data:
                        eof.add(sock)
                        continue
                    pending[other].extend(data)
                    last = time.monotonic()
        except (OSError, ValueError):
            if not established:
                try:
                    client.settimeout(0.1)
                    client.sendall(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n')
                except OSError:
                    pass
        finally:
            with lock:
                for sock in (client, upstream):
                    if sock is not None:
                        active.discard(sock)
                        close(sock)
                workers.discard(threading.current_thread())
    def accept():
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with lock:
                if stop.is_set() or len(workers) >= MAX_CONNECTIONS:
                    close(client)
                    continue
                active.add(client)
                worker = threading.Thread(target=serve, args=(client,), daemon=True)
                workers.add(worker)
                try:
                    worker.start()
                except RuntimeError:
                    workers.discard(worker)
                    active.discard(client)
                    close(client)
    accepter = threading.Thread(target=accept, daemon=True)
    try:
        accepter.start()
        yield f'http://127.0.0.1:{listener.getsockname()[1]}'
    finally:
        stop.set()
        listener.close()
        deadline = time.monotonic() + 5
        with lock:
            for sock in tuple(active):
                close(sock)
            owned = list(workers)
        for worker in [accepter] + owned:
            if worker.ident is not None:
                worker.join(max(0, deadline - time.monotonic()))
