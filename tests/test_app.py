import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

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
            app.start_helper('browser', 'test.log', '--port', '0')
            self.assertEqual(spawn.call_args.kwargs['env']['MUST_VM_HOME'], tmp)
            self.assertEqual(spawn.call_args.args[0][-3:], ['browser', '--port', '0'])

    def test_crashed_launcher_is_not_running_despite_stale_session(self):
        code = ('import sys,time; from pathlib import Path; import app; '
                'app.STATE=Path(sys.argv[1]); lease=app.lock_vm(); '
                'print("ready",flush=True); time.sleep(30)')
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)):
            session = Path(tmp) / 'session.json'
            session.write_text('{"ssh":"127.0.0.1:22345"}')
            self.assertFalse(app.vm_running())
            child = subprocess.Popen([sys.executable, '-c', code, tmp], cwd=app.ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                self.assertTrue(app.vm_running())
                with self.assertRaisesRegex(ValueError, 'already holds'):
                    app.lock_vm()
                self.assertTrue(app.vm_running())
                child.kill(); child.wait(timeout=5)
                self.assertTrue(session.exists())
                self.assertFalse(app.vm_running())
            finally:
                if child.poll() is None: child.kill(); child.wait(timeout=5)
                child.stdout.close(); child.stderr.close()

    def test_stale_session_starts_new_launcher_and_shuts_down_owned_vm(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)):
            (Path(tmp) / 'session.json').write_text('{"ssh":"127.0.0.1:22345"}')
            launcher = Mock(); launcher.poll.return_value = None
            with patch.object(app, 'start_helper', return_value=launcher) as start, \
                 patch.object(app, 'wait_for_guest') as wait, \
                 patch.object(app, 'guest_check', return_value=True), \
                 patch.object(app, 'exec_guest', return_value=0) as execute, \
                 patch.object(app, 'vpn_ready', return_value=True), redirect_stdout(io.StringIO()):
                with app.ready_vm('10.100.16.13'): pass
            start.assert_called_once_with('run', 'terminal-vm.log')
            wait.assert_called_once_with(launcher)
            self.assertEqual(execute.call_args.args, ('sudo systemctl poweroff --no-block',))
            launcher.wait.assert_called_once_with(timeout=45)

    def test_existing_launcher_is_reused_before_session_is_published(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)), \
             patch.object(app, 'vm_running', return_value=True), \
             patch.object(app, 'start_helper') as start, patch.object(app, 'wait_for_guest') as wait, \
             patch.object(app, 'guest_check', return_value=True), \
             patch.object(app, 'exec_guest', return_value=0) as execute, \
             patch.object(app, 'vpn_ready', return_value=True), redirect_stdout(io.StringIO()):
            with app.ready_vm('10.100.16.13'): pass
            start.assert_not_called()
            wait.assert_called_once_with(None)
            execute.assert_called_once_with('sudo must-vpn start')

    def test_run_discards_stale_ports_under_lock_even_if_gateway_fails(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)), \
             patch.object(app, 'config', return_value={'interface':'WLAN','source':'192.0.2.1'}), \
             patch.object(app, 'validate_adapter'), patch.object(app, 'protect_state'):
            session = Path(tmp) / 'session.json'; session.write_text('{}')
            def fail_start(*args, **kwargs):
                self.assertFalse(session.exists())
                self.assertTrue(app.vm_running())
                raise OSError('gateway fixture')
            with patch.object(app.subprocess, 'Popen', side_effect=fail_start):
                with self.assertRaisesRegex(OSError, 'gateway fixture'): app.run_vm(None)
            self.assertFalse(app.vm_running())
            self.assertFalse(session.exists())

    def test_wait_reports_launcher_exit_without_waiting_full_timeout(self):
        with patch.object(app, 'vm_running', return_value=False), \
             patch.object(app, 'connect_guest') as connect, patch.object(app.time, 'sleep') as sleep, \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'launcher stopped'): app.wait_for_guest(None)
            connect.assert_not_called(); sleep.assert_not_called()

    def test_wait_timeout_reports_last_ssh_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)), \
             patch.object(app, 'vm_running', return_value=True), \
             patch.object(app, 'connect_guest', side_effect=OSError('connection refused')), \
             patch.object(app.time, 'monotonic', side_effect=[0, 1, 4]), \
             patch.object(app.time, 'sleep'), redirect_stdout(io.StringIO()):
            (Path(tmp) / 'session.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Last SSH error: OSError: connection refused'):
                app.wait_for_guest(None, timeout=3)

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

    def test_isolated_browser_cleanup_targets_only_its_profile(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)), \
             patch.object(app.sys, 'platform', 'win32'), patch.object(app.subprocess, 'run') as run:
            app.close_isolated_browser()
            self.assertEqual(run.call_args.kwargs['env']['MUST_VM_BROWSER_PROFILE'], str(Path(tmp) / 'browser-profile'))
            self.assertIn('Get-CimInstance Win32_Process', run.call_args.args[0][3])
            self.assertIn('Stop-Process', run.call_args.args[0][3])

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
                 patch.object(app, 'vm_running', return_value=True), \
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

    def test_terminal_releases_login_bridge_before_opening_shell(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', Path(tmp)):
            (Path(tmp) / 'session.json').write_text('{}')
            browser = Mock()
            browser.poll.return_value = None
            with patch.object(app, 'config', return_value={'interface':'WLAN','source':'192.0.2.1'}), \
                 patch.object(app, 'vm_running', return_value=True), \
                 patch.object(app, 'validate_adapter'), patch.object(app, 'wait_for_guest'), \
                 patch.object(app, 'guest_check', return_value=True), \
                 patch.object(app, 'exec_guest', return_value=0), \
                 patch.object(app, 'vpn_ready', side_effect=[False]*8+[True]), \
                 patch.object(app.time, 'sleep'), \
                 patch.object(app, 'start_helper', return_value=browser) as helper, \
                 patch.object(app, 'close_isolated_browser') as close_browser, \
                 patch.object(app, 'pinned_guest_ssh', return_value=['ssh','guest']), \
                 patch.object(app.subprocess, 'call', side_effect=lambda _: browser.wait.assert_called_once_with(timeout=5) or 0):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(app.terminal(SimpleNamespace(probe=None)), 0)
                helper.assert_called_once_with('browser', 'terminal-browser.log', '--port', '0')
                browser.terminate.assert_called_once_with()
                close_browser.assert_called_once_with()

    def test_vpn_ready_requires_campus_route_on_tunnel(self):
        with patch.object(app, 'guest_check', return_value=False) as check:
            self.assertFalse(app.vpn_ready('10.100.16.13'))
            self.assertIn('ip -4 route get 10.100.16.13', check.call_args.args[0])
            self.assertIn(' dev utun', check.call_args.args[0])


if __name__ == '__main__': unittest.main()
