"""Selective TCP routing. The local dispatcher never starts a VM or VPN."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import socket
import ssl
import struct
import threading
import time


DEFAULTS = {
    'aisc_hosts': [],
    'aisc_networks': [],
    'school_hosts': ['must.edu.mo'],
    'school_networks': ['10.100.16.13/32'],
    'fallback': True,
    'direct_timeout': 4,
    'vpn_timeout': 10,
    'traffic_mode': 'all',
    'proxy_domains': [],
    'ssh_ports': [22],
}
HEARTBEAT_TTL = 12


def hostname(value):
    value = value.rstrip('.').lower()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        value = value.encode('idna').decode('ascii')
        if len(value) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p)
                                     for p in value.split('.')):
            raise ValueError('Expected a hostname or IP address, without a URL or port.')
        return value


class Policy:
    def __init__(self, data=None):
        self.data = {**DEFAULTS, **(data or {})}
        for key in ('aisc_hosts', 'school_hosts', 'proxy_domains'):
            if not isinstance(self.data[key], list): raise ValueError(f'{key} must be a list')
            self.data[key] = [hostname(h) for h in self.data[key]]
        self.networks = {}
        for key in ('aisc_networks', 'school_networks'):
            self.networks[key] = [ipaddress.ip_network(n, strict=False) for n in self.data[key]]
            if any(n.version != 4 for n in self.networks[key]):
                raise ValueError('Guest VPN networks must be IPv4.')
            self.data[key] = [str(n) for n in self.networks[key]]
        if not isinstance(self.data['fallback'], bool): raise ValueError('fallback must be true or false')
        if self.data['traffic_mode'] not in ('ssh', 'all', 'domains'):
            raise ValueError('traffic_mode must be ssh, all, or domains.')
        if not isinstance(self.data['ssh_ports'], list) or not all(
                isinstance(p, int) and 1 <= p <= 65535 for p in self.data['ssh_ports']):
            raise ValueError('ssh_ports must be a list of TCP port numbers.')
        for key in ('direct_timeout', 'vpn_timeout'):
            if not 0.1 <= float(self.data[key]) <= 60: raise ValueError(f'{key} must be between 0.1 and 60')
            self.data[key] = float(self.data[key])

    @classmethod
    def load(cls, state):
        path = state / 'routing.json'
        return cls(json.loads(path.read_text(encoding='utf-8')) if path.exists() else None)

    def matches(self, host, group):
        try: ip = ipaddress.ip_address(host)
        except ValueError: ip = None
        if ip is not None:
            return host in self.data[group + '_hosts'] or any(ip in n for n in self.networks[group + '_networks'])
        return any(host == h or host.endswith('.' + h) for h in self.data[group + '_hosts'])

    def mode(self, host, port=22):
        host = hostname(host)
        if self.data['traffic_mode'] == 'ssh' and port not in self.data['ssh_ports']: return 'direct'
        if self.data['traffic_mode'] == 'domains':
            return 'vpn' if any(host == h or host.endswith('.' + h) for h in self.data['proxy_domains']) else 'direct'
        if self.matches(host, 'aisc'): return 'vpn'
        if self.matches(host, 'school') and self.data['fallback']: return 'fallback'
        return 'direct'

    def vpn_allowed(self, host, port=22):
        return self.mode(host, port) in ('vpn', 'fallback')


def _lock(file, acquire):
    file.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file, (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


@contextmanager
def server_lease(state):
    with open(state / 'server.lock', 'a+b') as file:
        if file.seek(0, 2) == 0: file.write(b'0'); file.flush()
        try: _lock(file, True)
        except OSError as e: raise ValueError('A VPN server is already running for this state directory.') from e
        try: yield
        finally: _lock(file, False)


def lease_held(state):
    try:
        with open(state / 'server.lock', 'r+b') as file:
            try: _lock(file, True)
            except OSError: return True
            _lock(file, False)
    except OSError: pass
    return False


def server_endpoint(state):
    """No network probes when the server is stopped, stale, or unauthenticated."""
    if not lease_held(state): return None
    try:
        status = json.loads((state / 'server.json').read_text(encoding='utf-8'))
        age = time.time() - status['updated']
        if status['ready'] is True and 0 <= age <= HEARTBEAT_TTL and status['host'] == '127.0.0.1':
            port = status['port']
            if isinstance(port, int) and 1 <= port <= 65535: return status['host'], port
    except (OSError, ValueError, KeyError, TypeError): pass
    return None


def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        piece = sock.recv(count - len(data))
        if not piece: raise OSError('Connection closed during SOCKS handshake.')
        data.extend(piece)
    return bytes(data)


def address_read(sock, atyp):
    if atyp == 1: return socket.inet_ntop(socket.AF_INET, exact(sock, 4))
    if atyp == 4: return socket.inet_ntop(socket.AF_INET6, exact(sock, 16))
    if atyp == 3: return hostname(exact(sock, exact(sock, 1)[0]).decode('ascii'))
    raise ValueError('Unsupported SOCKS address type.')


def socks_dial(endpoint, host, port, timeout):
    sock = socket.create_connection(endpoint, timeout=timeout)
    try:
        sock.sendall(b'\x05\x01\x00')
        if exact(sock, 2) != b'\x05\x00': raise OSError('VPN server rejected SOCKS authentication.')
        name = hostname(host).encode('ascii')
        sock.sendall(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + struct.pack('!H', port))
        version, reply, reserved, atyp = exact(sock, 4)
        if version != 5 or reply != 0 or reserved != 0: raise OSError('VPN server could not connect to the target.')
        address_read(sock, atyp); exact(sock, 2)
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


class Router:
    def __init__(self, state, policy):
        self.state, self.policy = state, policy
        self._retry_after = 0
        self._lock = threading.Lock()

    def vpn(self, host, port):
        with self._lock:
            if time.monotonic() < self._retry_after: raise OSError('VPN server temporarily unavailable; retry later.')
        endpoint = server_endpoint(self.state)
        if endpoint is None: raise OSError('VPN server is off or VPN is not ready. Start the VPN server when needed.')
        try: return socks_dial(endpoint, host, port, self.policy.data['vpn_timeout'])
        except OSError:
            # Brief circuit breaker for a server crash between heartbeat and connect.
            with self._lock: self._retry_after = time.monotonic() + 5
            raise

    def connect(self, host, port):
        host = hostname(host)
        if not 1 <= port <= 65535: raise ValueError('Port must be between 1 and 65535.')
        mode = self.policy.mode(host, port)
        if mode == 'vpn': return self.vpn(host, port)
        try:
            if mode == 'fallback' and port in self.policy.data['ssh_ports']:
                check_ssh_direct(host, port, self.policy.data['direct_timeout'])
            elif mode == 'fallback' and port in (80, 443):
                check_web_direct(host, port, self.policy.data['direct_timeout'])
            sock = socket.create_connection((host, port), timeout=self.policy.data['direct_timeout'])
            sock.settimeout(None)
            return sock
        except OSError:
            if mode != 'fallback' or server_endpoint(self.state) is None: raise
            return self.vpn(host, port)


def check_web_direct(host, port, timeout):
    """Check reachability without replaying the user's HTTP request or credentials."""
    with socket.create_connection((host, port), timeout=timeout) as probe:
        if port == 443:
            with ssl.create_default_context().wrap_socket(probe, server_hostname=host): return
        probe.sendall(f'HEAD / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode('ascii'))
        if not exact(probe, 5).startswith(b'HTTP/'):
            raise OSError('Direct endpoint did not identify as HTTP.')


def check_ssh_direct(host, port, timeout):
    """Some TUN proxies acknowledge TCP before they can reach the SSH server.

    Probe identification on a separate connection. Never inject this probe's
    client version into the real SSH exchange (it is covered by the session hash).
    """
    with socket.create_connection((host, port), timeout=timeout) as probe:
        deadline = time.monotonic() + timeout
        probe.sendall(b'SSH-2.0-MUSTVPN_RouteProbe\r\n')
        buffer = bytearray()
        while len(buffer) < 8192:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError('Direct SSH identification timed out.')
            probe.settimeout(remaining)
            data = probe.recv(1024)
            if not data: raise OSError('Direct SSH closed before identification.')
            buffer.extend(data)
            for line in buffer.split(b'\n')[:-1]:
                if line.startswith((b'SSH-2.0-', b'SSH-1.99-')): return
        raise OSError('Direct endpoint did not identify as SSH.')


def shutdown_write(stream):
    try:
        if hasattr(stream, 'shutdown_write'): stream.shutdown_write()
        else: stream.shutdown(socket.SHUT_WR)
    except OSError: pass


def relay(left, right, stop=None):
    # Preserve half-close for SSH/SCP and HTTP peers that reply after request EOF.
    left.settimeout(None); right.settimeout(None)
    reading = [left, right]
    while reading and (stop is None or not stop.is_set()):
        ready, _, _ = select.select(reading, [], [], 1)
        for source in ready:
            dest = right if source is left else left
            data = source.recv(65536)
            if not data:
                reading.remove(source); shutdown_write(dest)
                if source is right: return
            else: dest.sendall(data)


def socks_client(client, connect, stop=None):
    remote = None
    connected = False
    try:
        client.settimeout(15)
        version, count = exact(client, 2)
        if version != 5 or 0 not in exact(client, count):
            client.sendall(b'\x05\xff'); return
        client.sendall(b'\x05\x00')
        version, command, reserved, atyp = exact(client, 4)
        if version != 5 or command != 1 or reserved != 0: raise ValueError('SOCKS CONNECT only.')
        host, port = address_read(client, atyp), struct.unpack('!H', exact(client, 2))[0]
        if port == 0: raise ValueError('Invalid target port.')
        remote = connect(host, port)
        client.sendall(b'\x05\x00\x00\x01' + b'\x00' * 6)
        connected = True
        relay(client, remote, stop)
    except (OSError, ValueError, UnicodeError):
        if not connected:
            try: client.sendall(b'\x05\x01\x00\x01' + b'\x00' * 6)
            except OSError: pass
    finally:
        if remote is not None: remote.close()
        client.close()


@contextmanager
def socks_listener(port, connect):
    stop = threading.Event()
    clients = set()
    mutex = threading.Lock()
    slots = threading.BoundedSemaphore(128)
    listener = socket.socket()
    try:
        listener.bind(('127.0.0.1', port)); listener.listen(32); listener.settimeout(0.5)
    except BaseException:
        listener.close(); raise

    def worker(client):
        try: socks_client(client, connect, stop)
        finally:
            with mutex: clients.discard(client)
            slots.release()

    def accept():
        while not stop.is_set():
            try: client, _ = listener.accept()
            except socket.timeout: continue
            except OSError: break
            if not slots.acquire(blocking=False): client.close(); continue
            with mutex: clients.add(client)
            threading.Thread(target=worker, args=(client,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True); thread.start()
    try: yield listener.getsockname()[1]
    finally:
        stop.set(); listener.close(); thread.join(2)
        with mutex:
            for client in clients:
                try: client.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                client.close()


def pac_script(policy, port):
    # Only campus/AISC requests visit the dispatcher. No host DNS lookup in PAC.
    if policy.data['traffic_mode'] == 'ssh':
        return 'function FindProxyForURL(url, host) { return "DIRECT"; }\n'
    domains_only = policy.data['traffic_mode'] == 'domains'
    hosts = policy.data['proxy_domains'] if domains_only else policy.data['aisc_hosts'] + policy.data['school_hosts']
    networks = [] if domains_only else [n for group in policy.networks.values() for n in group]
    return '''function FindProxyForURL(url, host) {
  host = host.toLowerCase().replace(/\\.$/, "");
  var groups = %s;
  for (var i = 0; i < groups.length; i++) {
    var h = groups[i];
    if (host === h || host.slice(-(h.length + 1)) === "." + h)
      return "SOCKS5 127.0.0.1:%d";
  }
  var networks = %s;
  if (/^\\d+\\.\\d+\\.\\d+\\.\\d+$/.test(host)) {
    for (var j = 0; j < networks.length; j++)
      if (isInNet(host, networks[j][0], networks[j][1])) return "SOCKS5 127.0.0.1:%d";
  }
  return "DIRECT";
}
''' % (json.dumps(hosts), port,
       json.dumps([[str(n.network_address), str(n.netmask)] for n in networks]), port)


@contextmanager
def pac_server(port, policy, socks_port, state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/proxy.pac':
                body, kind = pac_script(policy, socks_port).encode(), 'application/x-ns-proxy-autoconfig'
            elif self.path == '/status':
                body = json.dumps({'server_ready': server_endpoint(state) is not None, 'policy': policy.data}).encode()
                kind = 'application/json'
            else:
                self.send_error(404); return
            self.send_response(200); self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *_): pass

        def setup(self):
            super().setup(); self.connection.settimeout(5)

    http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True)
    thread.start()
    try: yield http.server_address[1]
    finally: http.shutdown(); http.server_close(); thread.join(2)


def stdio_bridge(sock, source, dest):
    failures = []
    def upload():
        try:
            while data := source.read1(65536): sock.sendall(data)
            shutdown_write(sock)
        except OSError as e:
            failures.append(e)
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
    threading.Thread(target=upload, daemon=True).start()
    try:
        while data := sock.recv(65536): dest.write(data); dest.flush()
        if failures: raise failures[0]
    finally: sock.close()
