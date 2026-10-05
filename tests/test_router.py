import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

import app
import local_ssh
import router_app
import routing


class RouterSettingsTests(unittest.TestCase):
    def test_modes_and_domain_boundaries(self):
        data = {'aisc_hosts': ['aisc.must.edu.mo'], 'aisc_networks': ['10.100.16.8/32']}
        ssh = routing.Policy({**data, 'traffic_mode': 'ssh'})
        self.assertEqual(ssh.mode('10.100.16.8', 22), 'vpn')
        self.assertEqual(ssh.mode('aisc.must.edu.mo', 443), 'direct')
        self.assertEqual(ssh.mode('www.must.edu.mo', 443), 'direct')
        self.assertEqual(ssh.mode('ssh.must.edu.mo', 22), 'fallback')
        self.assertNotIn('SOCKS', routing.pac_script(ssh, 1188))
        all_traffic = routing.Policy(data)
        self.assertEqual(all_traffic.mode('aisc.must.edu.mo', 443), 'vpn')
        self.assertEqual(all_traffic.mode('www.must.edu.mo', 443), 'fallback')
        self.assertEqual(all_traffic.mode('example.com', 443), 'direct')
        domains = routing.Policy({**data, 'traffic_mode': 'domains', 'proxy_domains': ['aisc.must.edu.mo']})
        for host in ('aisc.must.edu.mo', 'login.aisc.must.edu.mo'):
            self.assertEqual(domains.mode(host, 443), 'vpn')
        for host in ('aisc.must.edu.mo.evil.example', 'www.must.edu.mo', '10.100.16.8'):
            self.assertEqual(domains.mode(host, 22), 'direct')
        self.assertNotIn('"must.edu.mo"', routing.pac_script(domains, 1188))

    def test_legacy_environment_does_not_select_router_state(self):
        with patch.dict(os.environ, {'MUST_VM_HOME': '/legacy', 'MUST_ROUTER_HOME': ''}):
            self.assertNotEqual(router_app.state_home(), Path('/legacy').resolve())
        with patch.dict(os.environ, {'MUST_ROUTER_HOME': '/router', 'MUST_VM_HOME': '/legacy'}):
            self.assertEqual(router_app.state_home(), Path('/router').resolve())

    def test_children_carry_explicit_isolated_home(self):
        with patch.object(app, 'STATE', Path('/isolated').resolve()):
            command = router_app.self_command('run')
            self.assertEqual(command[-3:], ['--home', str(app.STATE), 'run'])

    @unittest.skipUnless(os.name == 'nt', 'Windows GUI entry point')
    def test_gui_generated_ssh_uses_main_executable(self):
        with patch.object(router_app.sys, 'frozen', True, create=True), \
             patch.object(router_app.sys, 'executable', r'C:\Router\must-router-settings.exe'):
            self.assertEqual(router_app.self_command('ssh-connect')[0], r'C:\Router\must-router.exe')

    def test_import_refuses_running_source_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'legacy'; target = root / 'router'; source.mkdir()
            (source / 'config.json').write_text('{}')
            for name in ('disk.qcow2', 'seed.iso', 'id_ed25519', 'host-key.json', 'qemu'):
                (source / name).write_bytes(b'fixture')
            with routing.server_lease(source):
                # The VM holds run.lock (the server lock is deliberately different).
                with open(source / 'run.lock', 'a+b') as lock:
                    lock.write(b'0'); lock.flush(); routing._lock(lock, True)
                    try:
                        with patch.object(app, 'STATE', target):
                            with self.assertRaisesRegex(ValueError, 'Shut down'):
                                router_app.clone_state(source, source / 'qemu')
                    finally: routing._lock(lock, False)
            self.assertFalse(target.exists())


class ProtocolProbeTests(unittest.TestCase):
    def test_tcp_ack_without_ssh_banner_falls_back_only_when_ready(self):
        probe = MagicMock(); probe.__enter__.return_value = probe
        probe.recv.side_effect = TimeoutError('TUN acknowledged TCP but target did not reply')
        for ready in (None, ('127.0.0.1', 1189)):
            with patch.object(routing.socket, 'create_connection', return_value=probe), \
                 patch.object(routing, 'server_endpoint', return_value=ready), \
                 patch.object(routing, 'socks_dial') as vpn:
                router = routing.Router(Path('.'), routing.Policy())
                if ready:
                    self.assertIs(router.connect('ssh.must.edu.mo', 22), vpn.return_value)
                    vpn.assert_called_once()
                else:
                    with self.assertRaises(TimeoutError): router.connect('ssh.must.edu.mo', 22)
                    vpn.assert_not_called()

    def test_successful_ssh_probe_is_separate_from_real_exchange(self):
        probe = MagicMock(); probe.__enter__.return_value = probe
        probe.recv.side_effect = [b'Notice\r\nSSH-2.', b'0-OpenSSH\r\n']
        actual = Mock()
        with patch.object(routing.socket, 'create_connection', side_effect=[probe, actual]) as connect, \
             patch.object(routing, 'server_endpoint') as status:
            self.assertIs(routing.Router(Path('.'), routing.Policy()).connect('ssh.must.edu.mo', 22), actual)
        self.assertEqual(connect.call_count, 2)
        actual.sendall.assert_not_called(); status.assert_not_called()
        probe.__exit__.assert_called_once()

    def test_tls_handshake_failure_falls_back_without_user_request_replay(self):
        probe = MagicMock(); probe.__enter__.return_value = probe
        context = Mock(); context.wrap_socket.side_effect = TimeoutError('No TLS handshake')
        with patch.object(routing.socket, 'create_connection', return_value=probe), \
             patch.object(routing.ssl, 'create_default_context', return_value=context), \
             patch.object(routing, 'server_endpoint', return_value=('127.0.0.1', 1189)), \
             patch.object(routing, 'socks_dial') as vpn:
            routing.Router(Path('.'), routing.Policy()).connect('www.must.edu.mo', 443)
        probe.sendall.assert_not_called(); vpn.assert_called_once()


class NativeSshTests(unittest.TestCase):
    def test_include_preserves_config_backup_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'user/config'; config.parent.mkdir()
            original = b'Host existing\r\n    HostName old.example\r\n'
            config.write_bytes(original); managed = Path(tmp) / 'router/ssh/config'
            backup = local_ssh.install_include(managed, config)
            self.assertEqual(backup.read_bytes(), original)
            self.assertTrue(config.read_bytes().endswith(original))
            first = config.read_bytes()
            self.assertIsNone(local_ssh.install_include(managed, config))
            self.assertEqual(config.read_bytes(), first)

    def test_hostkey_verification_and_no_global_host_wildcard(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp); (state / 'ssh').mkdir()
            (state / 'ssh/targets.json').write_text(json.dumps([{'aliases':['AISC-CPU','AISCCPU'],
                'hostname':'10.100.16.8','user':'fixture','port':22,'keys':[str(state/'ssh/test.key')]}]))
            content = local_ssh.write_config(state, 'must-router ssh-connect').read_text()
            self.assertIn('StrictHostKeyChecking yes', content)
            self.assertIn('Host AISC-CPU AISCCPU', content)
            self.assertIn('ProxyCommand must-router ssh-connect %h %p', content)
            self.assertIn('Match final host ', content)
            self.assertTrue(content.endswith('Host *\n'))
            self.assertNotIn('Host *\n    ProxyCommand', content)

    def test_final_match_routes_resolved_school_alias_without_changing_unrelated_hosts(self):
        import shutil
        import subprocess
        ssh = shutil.which('ssh')
        if not ssh: self.skipTest('OpenSSH unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'private'
            with patch.object(app, 'STATE', state): app.protect_state()
            (state / 'ssh').mkdir()
            if os.name == 'nt':
                # Reproduce the ACL inherited from Python's Windows temp dirs.
                subprocess.run(['icacls.exe', str(state / 'ssh'), '/grant', '*S-1-3-4:(OI)(CI)F'],
                               check=True, stdout=subprocess.DEVNULL)
            (state / 'ssh/targets.json').write_text('[]')
            config = local_ssh.write_config(state, 'must-router ssh-connect')
            user = state / 'user-config'
            user.write_text('Include "' + config.as_posix() + '"\n'
                'Host campus-alias\n    HostName ssh.must.edu.mo\n'
                'Host unrelated\n    HostName example.com\n')
            for alias, routed in [('campus-alias', True), ('unrelated', False)]:
                result = subprocess.run([ssh, '-G', '-F', str(user), alias], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('proxycommand must-router ssh-connect' in result.stdout, routed)


if __name__ == '__main__': unittest.main()
