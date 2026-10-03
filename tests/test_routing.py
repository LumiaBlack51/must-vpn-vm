from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.request import urlopen

import app
import routing
from guest import vpn_relay


POLICY = {'aisc_hosts': ['aisc.must.edu.mo'], 'aisc_networks': ['10.100.16.13/32']}


@contextmanager
def echo_server():
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()
    errors = []
    def worker():
        try:
            with listener.accept()[0] as client:
                data = bytearray()
                while piece := client.recv(65536): data.extend(piece)
                client.sendall(data)
        except OSError as e: errors.append(e)
    thread = threading.Thread(target=worker, daemon=True); thread.start()
    try: yield listener.getsockname()
    finally:
        listener.close(); thread.join(3)
        if thread.is_alive(): raise AssertionError('Echo thread did not stop.')
        if errors: raise errors[0]


class PolicyTests(unittest.TestCase):
    def test_exact_suffixes_and_explicit_ip_scope(self):
        policy = routing.Policy(POLICY)
        for host in ['aisc.must.edu.mo', 'login.aisc.must.edu.mo', 'AISC.MUST.EDU.MO.', '10.100.16.13']:
            self.assertEqual(policy.mode(host), 'vpn')
        for host in ['must.edu.mo', 'www.must.edu.mo', 'ssh.must.edu.mo']:
            self.assertEqual(policy.mode(host), 'fallback')
        for host in ['notmust.edu.mo', 'must.edu.mo.evil.example', 'example.com', '10.100.16.14']:
            self.assertEqual(policy.mode(host), 'direct')

    def test_fallback_disabled_does_not_disable_forced_vpn(self):
        policy = routing.Policy({**POLICY, 'fallback': False})
        self.assertEqual(policy.mode('www.must.edu.mo'), 'direct')
        self.assertEqual(policy.mode('aisc.must.edu.mo'), 'vpn')

    def test_no_unconfirmed_aisc_addresses_in_defaults(self):
        policy = routing.Policy()
        self.assertEqual(policy.data['aisc_hosts'], [])
        self.assertEqual(policy.mode('10.100.16.13'), 'fallback')

    def test_rejects_urls_shell_syntax_and_invalid_networks(self):
        for host in ['https://aisc.must.edu.mo', 'aisc.must.edu.mo:22', 'a;echo', '-bad.host', 'a\n.host']:
            with self.assertRaises(ValueError): routing.Policy({'aisc_hosts': [host]})
        for data in [{'school_networks': ['::/0']}, {'direct_timeout': 0}, {'fallback': 'on'}]:
            with self.assertRaises(ValueError): routing.Policy(data)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.router = routing.Router(self.state, routing.Policy(POLICY))

    def test_school_success_never_checks_or_uses_vpn(self):
        sock = Mock()
        with patch.object(routing.socket, 'create_connection', return_value=sock) as direct, \
             patch.object(routing, 'server_endpoint') as status, patch.object(routing, 'socks_dial') as vpn:
            self.assertIs(self.router.connect('www.must.edu.mo', 443), sock)
        direct.assert_called_once_with(('www.must.edu.mo', 443), timeout=4)
        status.assert_not_called(); vpn.assert_not_called()

    def test_school_failure_server_off_performs_no_proxy_dial(self):
        with patch.object(routing.socket, 'create_connection', side_effect=socket.gaierror('DNS unavailable')) as direct, \
             patch.object(routing, 'socks_dial') as vpn:
            with self.assertRaises(socket.gaierror): self.router.connect('www.must.edu.mo', 443)
        self.assertEqual(direct.call_count, 1); vpn.assert_not_called()

    def test_school_failure_live_server_falls_back_with_hostname(self):
        sock = Mock()
        with patch.object(routing.socket, 'create_connection', side_effect=TimeoutError()), \
             patch.object(routing, 'server_endpoint', return_value=('127.0.0.1', 1089)), \
             patch.object(routing, 'socks_dial', return_value=sock) as vpn:
            self.assertIs(self.router.connect('ssh.must.edu.mo', 22), sock)
        vpn.assert_called_once_with(('127.0.0.1', 1089), 'ssh.must.edu.mo', 22, 10)

    def test_aisc_off_never_attempts_direct_or_proxy(self):
        with patch.object(routing.socket, 'create_connection') as direct, patch.object(routing, 'socks_dial') as vpn:
            with self.assertRaisesRegex(OSError, 'off'): self.router.connect('10.100.16.13', 22)
        direct.assert_not_called(); vpn.assert_not_called()

    def test_aisc_on_never_attempts_host_dns_or_direct(self):
        sock = Mock()
        with patch.object(routing.socket, 'create_connection') as direct, \
             patch.object(routing, 'server_endpoint', return_value=('127.0.0.1', 1089)), \
             patch.object(routing, 'socks_dial', return_value=sock) as vpn:
            self.assertIs(self.router.connect('aisc.must.edu.mo', 443), sock)
        direct.assert_not_called(); self.assertEqual(vpn.call_args.args[1:3], ('aisc.must.edu.mo', 443))

    def test_unrelated_failures_never_fall_back(self):
        with patch.object(routing.socket, 'create_connection', side_effect=OSError()), \
             patch.object(routing, 'server_endpoint') as status:
            with self.assertRaises(OSError): self.router.connect('example.com', 443)
        status.assert_not_called()

    def test_crash_race_opens_circuit_before_another_attempt(self):
        with patch.object(routing, 'server_endpoint', return_value=('127.0.0.1', 1089)), \
             patch.object(routing, 'socks_dial', side_effect=ConnectionRefusedError()) as vpn:
            for _ in range(2):
                with self.assertRaises(OSError): self.router.connect('aisc.must.edu.mo', 22)
        self.assertEqual(vpn.call_count, 1)

    def test_heartbeat_requires_live_lock_ready_flag_and_freshness(self):
        status = {'host': '127.0.0.1', 'port': 1089, 'ready': True, 'updated': time.time()}
        app.save(self.state / 'server.json', status)
        self.assertIsNone(routing.server_endpoint(self.state))
        with routing.server_lease(self.state):
            self.assertEqual(routing.server_endpoint(self.state), ('127.0.0.1', 1089))
            for update in [{'ready': False}, {'updated': time.time() - 60}, {'updated': time.time() + 60},
                           {'host': '0.0.0.0'}, {'port': 0}]:
                app.save(self.state / 'server.json', {**status, **update})
                self.assertIsNone(routing.server_endpoint(self.state))

    def test_process_crash_releases_lock_even_with_fresh_ready_file(self):
        code = ('import sys,time; from pathlib import Path; import routing; '
                'lease=routing.server_lease(Path(sys.argv[1])); lease.__enter__(); '
                'print("ready",flush=True); time.sleep(30)')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.state)], cwd=app.ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            app.save(self.state / 'server.json', {'host':'127.0.0.1', 'port':1089, 'ready':True, 'updated':time.time()})
            self.assertIsNotNone(routing.server_endpoint(self.state))
            child.kill(); child.wait(timeout=5)
            self.assertIsNone(routing.server_endpoint(self.state))
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=5)
            child.stdout.close(); child.stderr.close()

    def test_real_socks_fallback_transmits_binary_and_half_close(self):
        payload = b'SSH-2.0-fixture\r\n' + bytes(range(256)) * 256
        with echo_server() as address:
            received = []
            def guest_connect(host, port):
                received.append((host, port)); return socket.create_connection(address, timeout=2)
            with routing.server_lease(self.state), routing.socks_listener(0, guest_connect) as port:
                app.save(self.state / 'server.json', {'host':'127.0.0.1', 'port':port, 'ready':True, 'updated':time.time()})
                with patch.object(routing.socket, 'create_connection', wraps=socket.create_connection) as dial:
                    original = dial._mock_wraps
                    dial.side_effect = lambda target, **kw: (_ for _ in ()).throw(ConnectionRefusedError()) \
                        if target[0] == 'ssh.must.edu.mo' else original(target, **kw)
                    client = self.router.connect('ssh.must.edu.mo', 22)
                with client:
                    client.settimeout(3); client.sendall(payload); client.shutdown(socket.SHUT_WR)
                    self.assertEqual(routing.exact(client, len(payload)), payload)
                self.assertEqual(received, [('ssh.must.edu.mo', 22)])

    def test_pac_http_only_routes_configured_scope_and_reports_offline(self):
        policy = routing.Policy(POLICY)
        with routing.pac_server(0, policy, 1088, self.state) as port:
            with urlopen(f'http://127.0.0.1:{port}/proxy.pac', timeout=2) as response:
                script = response.read().decode()
                self.assertIn('"aisc.must.edu.mo"', script)
                self.assertIn('return "DIRECT"', script)
                self.assertIn('SOCKS5 127.0.0.1:1088', script)
                self.assertNotIn('dnsResolve', script)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            with urlopen(f'http://127.0.0.1:{port}/status', timeout=2) as response:
                self.assertFalse(json.load(response)['server_ready'])

    def test_real_local_dispatcher_works_with_no_vm(self):
        payload = b'HTTP/1.1 200 OK\r\n\r\n' + bytes(range(256))
        with echo_server() as address, routing.socks_listener(0, self.router.connect) as port:
            with routing.socks_dial(('127.0.0.1', port), address[0], address[1], 2) as client:
                client.settimeout(3); client.sendall(payload); client.shutdown(socket.SHUT_WR)
                self.assertEqual(routing.exact(client, len(payload)), payload)
        self.assertFalse((self.state / 'server.json').exists())

    def test_real_ssh_proxycommand_cli_preserves_binary_stdio_without_vm(self):
        payload = b'SSH-2.0-fixture\r\n' + bytes(range(256)) * 16
        with echo_server() as address:
            result = subprocess.run([sys.executable, str(app.ROOT / 'app.py'), 'ssh-connect', address[0], str(address[1])],
                input=payload, capture_output=True, timeout=10,
                env={**os.environ, 'MUST_VM_HOME': str(self.state)})
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout, payload)
        self.assertFalse((self.state / 'session.json').exists())


class GuestGateTests(unittest.TestCase):
    def test_guest_non_vpn_route_refused_before_open_channel(self):
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.100.16.13', 0))]
        with patch.object(vpn_relay.socket, 'getaddrinfo', return_value=resolved), \
             patch.object(vpn_relay.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='10.100.16.13 dev eth0 ')), \
             patch.object(vpn_relay.socket, 'socket') as make:
            with self.assertRaisesRegex(OSError, 'no guest VPN route'):
                vpn_relay.connect_vpn('aisc.must.edu.mo', 22, 10)
            make.assert_not_called()

    def test_guest_connection_uses_exact_vpn_checked_ip(self):
        sock = Mock()
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.100.16.13', 0))]
        with patch.object(vpn_relay.socket, 'getaddrinfo', return_value=resolved), \
             patch.object(vpn_relay.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='10.100.16.13 dev utun7 src 10.1.1.1')), \
             patch.object(vpn_relay.socket, 'SO_BINDTODEVICE', 25, create=True), \
             patch.object(vpn_relay.socket, 'socket', return_value=sock):
            self.assertIs(vpn_relay.connect_vpn('aisc.must.edu.mo', 443, 10), sock)
        sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, 25, b'utun7\0')
        sock.connect.assert_called_once_with(('10.100.16.13', 443))

    def test_device_disappearing_between_route_and_bind_never_falls_back(self):
        sock = Mock(); sock.setsockopt.side_effect = OSError('VPN device disappeared')
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.100.16.13', 0))]
        with patch.object(vpn_relay.socket, 'getaddrinfo', return_value=resolved), \
             patch.object(vpn_relay.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='10.100.16.13 dev utun7 ')), \
             patch.object(vpn_relay.socket, 'SO_BINDTODEVICE', 25, create=True), \
             patch.object(vpn_relay.socket, 'socket', return_value=sock):
            with self.assertRaises(OSError): vpn_relay.connect_vpn('aisc.must.edu.mo', 22, 10)
        sock.connect.assert_not_called(); sock.close.assert_called_once()

    def test_host_consumes_only_guest_success_marker(self):
        guest = Mock(); channel = guest.get_transport().open_session.return_value
        channel.recv.return_value = b'\0'
        self.assertIs(app.guest_vpn_connect(guest, routing.Policy(POLICY), 'aisc.must.edu.mo', 443), channel)
        channel.recv.assert_called_once_with(1)
        self.assertIn('sudo -n python3 -u -c', channel.exec_command.call_args.args[0])
        self.assertTrue(channel.exec_command.call_args.args[0].endswith('aisc.must.edu.mo 443 10.0'))
        channel.close.assert_not_called()

    def test_failed_guest_connection_closes_exec_channel(self):
        guest = Mock(); channel = guest.get_transport().open_session.return_value
        channel.recv.return_value = b''
        with self.assertRaises(OSError): app.guest_vpn_connect(guest, routing.Policy(POLICY), 'aisc.must.edu.mo', 443)
        channel.close.assert_called_once()

    def test_outside_scope_never_contacts_guest(self):
        guest = Mock()
        with self.assertRaises(OSError): app.guest_vpn_connect(guest, routing.Policy(POLICY), 'example.com', 443)
        guest.exec_command.assert_not_called()

    def test_server_removes_readiness_before_owned_vm_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)):
            state = Path(tmp); guest = MagicMock()
            guest.get_transport().is_active.return_value = True
            @contextmanager
            def vm(*_):
                try: yield
                finally: self.assertFalse((state / 'server.json').exists())
            with patch.object(app, 'vm_probe', return_value='10.100.16.13'), patch.object(app, 'protect_state'), \
                 patch.object(app, 'ready_vm', side_effect=vm), patch.object(app, 'connect_guest', return_value=guest), \
                 patch.object(app, 'guest_route_ready', return_value=True), \
                 patch.object(app.time, 'sleep', side_effect=KeyboardInterrupt), redirect_stdout(io.StringIO()):
                guest.__enter__.return_value = guest
                with self.assertRaises(KeyboardInterrupt): app.serve(SimpleNamespace(port=0))
            self.assertIsNone(routing.server_endpoint(state))


if __name__ == '__main__': unittest.main()
