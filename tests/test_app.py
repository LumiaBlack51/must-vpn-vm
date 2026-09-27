import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import app


class AppTests(unittest.TestCase):
    def test_windows_state_uses_profile_directory_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app.sys, 'platform', 'win32'), \
             patch.object(app.Path, 'home', return_value=Path(tmp)), \
             patch.dict(app.os.environ, {'LOCALAPPDATA': str(Path(tmp) / 'redirected')}, clear=True):
            shared = Path(tmp) / '.must-vpn-vm'
            self.assertEqual(app.state_home(), shared.resolve())
            legacy = Path(tmp) / 'redirected/MUST-VPN-VM'
            legacy.mkdir(parents=True)
            self.assertEqual(app.state_home(), legacy.resolve())
            shared.mkdir()
            self.assertEqual(app.state_home(), shared.resolve())
            with patch.dict(app.os.environ, {'MUST_VM_HOME': str(legacy)}):
                self.assertEqual(app.state_home(), legacy.resolve())

    def test_helper_inherits_selected_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)), \
             patch.object(app, 'protect_state'), patch.object(app.subprocess, 'Popen') as spawn:
            app.start_helper('run', 'test.log')
            self.assertEqual(spawn.call_args.kwargs['env']['MUST_VM_HOME'], tmp)

    def test_missing_config_reports_actual_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)):
            with self.assertRaises(FileNotFoundError) as error:
                app.terminal(SimpleNamespace(probe=None))
            self.assertEqual(Path(error.exception.filename), Path(tmp) / 'config.json')

    def test_browser_blocks_external_application_launch(self):
        args=app.browser_args('browser',1088,'https://vpn.must.edu.mo/','fixture-key')
        self.assertIn('--disable-external-intent-requests',args)
        self.assertIn('--proxy-bypass-list=<-loopback>',args)
        self.assertIn('--disable-quic',args)
        self.assertIn('--ignore-certificate-errors-spki-list=fixture-key',args)
        self.assertNotIn('--ignore-certificate-errors',args)

    def test_qemu_has_only_explicit_gateway(self):
        args = app.qemu_args(dict(qemu='qemu', accelerator='tcg', memory=768), '127.0.0.1:1234')
        self.assertIn('-nodefaults', args)
        self.assertIn('tcg,tb-size=32,thread=single',args)
        self.assertIn('socket,id=direct,connect=127.0.0.1:1234', args)
        self.assertFalse(any('user,id=' in a or 'hostfwd' in a for a in args))
        self.assertIn('none', args)

    def test_virtual_and_changed_adapters_fail_closed(self):
        with patch.object(app, 'adapters', return_value=[dict(name='Wi-Fi', ip='192.0.2.10')]):
            app.validate_adapter('Wi-Fi', '192.0.2.10')
            for name, ip in [('Clash', '192.0.2.10'), ('Wi-Fi', '192.0.2.11')]:
                with self.assertRaises(ValueError): app.validate_adapter(name, ip)

    def test_seed_contains_no_automatic_vpn_install(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)):
            deb = Path(tmp) / 'fake.deb'; deb.write_bytes(b'fixture')
            app.create_seed(deb, '192.0.2.53')
            iso = app.pycdlib.PyCdlib(); iso.open(str(Path(tmp) / 'seed.iso'))
            out = io.BytesIO(); iso.get_file_from_iso_fp(out, rr_path='/user-data'); iso.close()
            user = json.loads(out.getvalue().decode().split('\n', 1)[1])
            self.assertNotIn('aTrust', json.dumps(user['runcmd']))
            self.assertNotIn('packages', user)
            self.assertFalse(user['ssh_pwauth'])
            self.assertFalse((Path(tmp) / 'guest_host_key').exists())

    def test_socks_preserves_hostname_for_guest_dns(self):
        calls = []
        class Transport:
            def open_channel(self, kind, dst, src, timeout):
                calls.append(dst); raise app.paramiko.SSHException('fixture')
        a,b = socket.socketpair()
        thread = threading.Thread(target=app.socks_client, args=(a, Transport())); thread.start()
        b.sendall(b'\x05\x01\x00'); self.assertEqual(app.exact(b, 2), b'\x05\x00')
        host=b'intranet.must.edu.mo'
        b.sendall(b'\x05\x01\x00\x03'+bytes([len(host)])+host+b'\x01\xbb')
        self.assertEqual(app.exact(b, 2), b'\x05\x01')
        b.close(); thread.join(2)
        self.assertEqual(calls, [('intranet.must.edu.mo',443)])

    def test_no_auth_method_is_rejected(self):
        a,b = socket.socketpair(); t=threading.Thread(target=app.socks_client,args=(a,None));t.start()
        b.sendall(b'\x05\x01\x02'); self.assertEqual(app.exact(b,2),b'\x05\xff'); b.close(); t.join(2)

    def test_interactive_guest_ssh_uses_pinned_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)), patch.object(app.shutil, 'which', return_value='ssh'):
            state = Path(tmp)
            (state / 'session.json').write_text(json.dumps({'ssh': '127.0.0.1:22345'}))
            (state / 'host-key.json').write_text(json.dumps({'key': 'ssh-ed25519 Zml4dHVyZQ=='}))
            command = app.pinned_guest_ssh()
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('BatchMode=yes', command)
            self.assertIn('vpn@127.0.0.1', command)
            self.assertIn('-tt', command)
            self.assertEqual((state / 'guest_known_hosts').read_text(), '[127.0.0.1]:22345 ssh-ed25519 Zml4dHVyZQ==\n')

    def test_terminal_opens_shell_only_after_tunnel_route(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)):
            state = Path(tmp)
            (state / 'config.json').write_text('{}')
            (state / 'session.json').write_text('{}')
            with patch.object(app, 'config', return_value={'interface':'WLAN','source':'192.0.2.1'}), \
                 patch.object(app, 'validate_adapter'), patch.object(app, 'wait_for_guest'), \
                 patch.object(app, 'guest_check', return_value=True), \
                 patch.object(app, 'exec_guest', return_value=0), \
                 patch.object(app, 'vpn_ready', side_effect=[False,True,True,True]), \
                 patch.object(app.time, 'sleep'), patch.object(app, 'start_helper') as helper, \
                 patch.object(app, 'pinned_guest_ssh', return_value=['ssh','guest']), \
                 patch.object(app.subprocess, 'call', return_value=0) as shell:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(app.terminal(SimpleNamespace(probe=None)), 0)
                helper.assert_not_called()
                shell.assert_called_once_with(['ssh','guest'])

    def test_vpn_ready_requires_campus_route_on_tunnel(self):
        with patch.object(app, 'guest_check', return_value=False) as check:
            self.assertFalse(app.vpn_ready('10.100.16.13'))
            self.assertIn('ip -4 route get 10.100.16.13', check.call_args.args[0])
            self.assertIn(' dev utun', check.call_args.args[0])


if __name__ == '__main__': unittest.main()
