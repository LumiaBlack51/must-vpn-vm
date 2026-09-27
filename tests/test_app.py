import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import app


class AppTests(unittest.TestCase):
    def test_qemu_has_only_explicit_gateway(self):
        args = app.qemu_args(dict(qemu='qemu', accelerator='tcg', memory=768), '127.0.0.1:1234')
        self.assertIn('-nodefaults', args)
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


if __name__ == '__main__': unittest.main()
