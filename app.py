"""MUST VPN VM: isolated QEMU appliance launcher. Never executes a vendor binary on host."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import select
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

import paramiko
import pycdlib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography import x509

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
RESOURCES = Path(getattr(sys, '_MEIPASS', ROOT))
STATE = Path(os.environ.get('MUST_VM_HOME', str(Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'MUST-VPN-VM')))
PINNED_DEB = '298c0bcf6aa923d53337f525affdbc99d61d07bb4119ee10a39eb697eefe32d5'


def digest(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def config():
    return json.loads((STATE / 'config.json').read_text(encoding='utf-8'))


def protect_state():
    STATE.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # Restrict VM keys and VPN session data to this Windows account.
        sid = json.loads(subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
            '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value | ConvertTo-Json'], text=True))
        subprocess.run(['icacls.exe', str(STATE), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], check=True, stdout=subprocess.DEVNULL)
    else:
        STATE.chmod(0o700)


def adapters():
    if os.name == 'nt':
        command = ('@(Get-NetAdapter -Physical | Where-Object Status -eq Up | ForEach-Object { '
            '$a=$_; Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 | '
            'Where-Object { $_.IPAddress -notlike "169.254.*" } | ForEach-Object { '
            '[pscustomobject]@{name=$a.Name;index=$a.ifIndex;ip=$_.IPAddress;description=$a.InterfaceDescription} } }) | ConvertTo-Json -Compress')
        raw = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command', '[Console]::OutputEncoding=[Text.Encoding]::UTF8;'+command])
        data = json.loads(raw.decode('utf-8-sig') or '[]')
        return data if isinstance(data, list) else [data]
    data = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'up']))
    return [dict(name=a['ifname'], index=a['ifindex'], ip=i['local']) for a in data
        if (Path('/sys/class/net') / a['ifname'] / 'device').exists()
        for i in a.get('addr_info', []) if i['scope'] == 'global']


def validate_adapter(name, ip):
    if not any(a['name'] == name and a['ip'] == ip for a in adapters()):
        raise ValueError('Select an UP physical adapter and its current IPv4 address (see adapters).')


def new_key(path):
    key = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
    if os.name != 'nt':
        path.chmod(0o600)
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def create_seed(deb, dns):
    public = new_key(STATE / 'id_ed25519')
    host_public = new_key(STATE / 'guest_host_key')
    script = (RESOURCES / 'guest/must-vpn').read_text(encoding='utf-8').replace('\r\n', '\n')
    user = {
        'hostname': 'must-vpn', 'manage_etc_hosts': True,
        'users': [{'name': 'vpn', 'shell': '/bin/bash', 'lock_passwd': True,
                   'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'ssh_authorized_keys': [public]}],
        'disable_root': True, 'ssh_pwauth': False,
        'ssh_keys': {'ed25519_private': (STATE / 'guest_host_key').read_text(), 'ed25519_public': host_public},
        'write_files': [
            {'path': '/etc/must-vm', 'content': 'MUSTVPN-Isolated\n', 'permissions': '0444'},
            {'path': '/usr/local/sbin/must-vpn', 'content': script, 'permissions': '0755'},
            {'path': '/etc/ssh/sshd_config.d/10-must.conf', 'content': 'PasswordAuthentication no\nPermitRootLogin no\nAllowTcpForwarding yes\nGatewayPorts no\n'},
            {'path': '/etc/sysctl.d/90-must.conf', 'content': 'net.ipv6.conf.all.disable_ipv6=1\nnet.ipv6.conf.default.disable_ipv6=1\n'},
        ],
        # No apt, vendor installation, vendor startup, desktop or browser at first boot.
        'runcmd': [['sysctl', '--system'], ['systemctl', 'restart', 'ssh'], ['systemctl', 'set-default', 'multi-user.target']],
        'growpart': {'mode': 'auto', 'devices': ['/'], 'ignore_growroot_disabled': False},
    }
    network = {'version': 2, 'ethernets': {'uplink': {'match': {'macaddress': '52:54:00:4d:55:53'},
        'set-name': 'eth0', 'dhcp4': False, 'dhcp6': False, 'accept-ra': False,
        'addresses': ['10.77.0.2/24'], 'routes': [{'to': 'default', 'via': '10.77.0.1'}],
        'nameservers': {'addresses': [dns]}}}}
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge='1.09', vol_ident='cidata')
    entries = {'user-data': '#cloud-config\n'+json.dumps(user), 'meta-data': json.dumps({'instance-id': str(uuid.uuid4()), 'local-hostname': 'must-vpn'}),
               'network-config': json.dumps(network), 'vendor.sha256': f'{PINNED_DEB}  vendor.deb\n'}
    streams = []
    for index, (name, content) in enumerate(entries.items()):
        b = content.encode(); stream = io.BytesIO(b); streams.append(stream)
        iso.add_fp(stream, len(b), iso_path=f'/FILE{index};1', rr_name=name, joliet_path='/'+name)
    iso.add_file(str(deb), iso_path='/VENDOR.DEB;1', rr_name='vendor.deb', joliet_path='/vendor.deb')
    iso.write(str(STATE / 'seed.iso')); iso.close()
    save(STATE / 'host-key.json', {'key': host_public})
    # Only the guest requires this private host key, now sealed in the ISO.
    (STATE / 'guest_host_key').unlink()


def configure(args):
    if (STATE / 'config.json').exists() or (STATE / 'disk.qcow2').exists():
        raise ValueError('Existing VM state preserved. Use a different MUST_VM_HOME for a new VM.')
    validate_adapter(args.interface, args.source)
    if not ipaddress.ip_address(args.dns).version == 4:
        raise ValueError('DNS must be a reachable IPv4 address on the physical network.')
    deb = Path(args.deb).resolve()
    if digest(deb) != PINNED_DEB:
        raise ValueError('DEB SHA-256 does not match the inspected school package.')
    base = Path(args.base or ROOT / 'runtime/base.qcow2').resolve()
    qemu = Path(args.qemu or ROOT / 'runtime/qemu/qemu-system-x86_64.exe').resolve() if os.name == 'nt' else Path(args.qemu or shutil.which('qemu-system-x86_64') or '/usr/bin/qemu-system-x86_64')
    image_tool = qemu.with_name('qemu-img.exe' if os.name == 'nt' else 'qemu-img')
    for p in (base, qemu, image_tool):
        if not p.is_file(): raise ValueError(f'Missing runtime: {p}')
    lock = json.loads((RESOURCES / 'runtime-lock.json').read_text())
    if digest(base) != lock['ubuntu']['sha256']:
        raise ValueError('Base image SHA-256 mismatch.')
    protect_state()
    # Self-contained per-user disk avoids backing-chain path changes during MSI upgrades.
    subprocess.run([str(image_tool), 'convert', '-f', 'qcow2', '-O', 'qcow2', str(base), str(STATE / 'disk.qcow2')], check=True)
    subprocess.run([str(image_tool), 'resize', str(STATE / 'disk.qcow2'), '6G'], check=True)
    create_seed(deb, args.dns)
    save(STATE / 'config.json', dict(interface=args.interface, source=args.source, qemu=str(qemu), memory=args.memory,
         accelerator=args.accelerator, portal='https://vpn.must.edu.mo/'))
    print('VM prepared. No VPN installed or started. Run: must-vm run')


def qemu_args(cfg, ethernet):
    def escape(p): return str(p).replace(',', ',,')
    accelerator = cfg['accelerator']
    if accelerator == 'auto':
        acceleration = ['-accel', 'whpx' if os.name == 'nt' else 'kvm', '-accel', 'tcg,tb-size=32,thread=single']
    else:
        acceleration = ['-accel', 'tcg,tb-size=32,thread=single' if accelerator == 'tcg' else accelerator]
    return [cfg['qemu'], '-name', 'MUSTVPN-Isolated', '-machine', 'q35', *acceleration,
        '-m', str(cfg['memory']), '-smp', '1', '-nodefaults', '-display', 'none', '-monitor', 'none',
        '-smbios', 'type=1,manufacturer=MUST-VM,product=MUSTVPN-Isolated,serial=ds=nocloud',
        '-drive', f'file={escape(STATE / "disk.qcow2")},if=virtio,format=qcow2,discard=unmap',
        '-drive', f'file={escape(STATE / "seed.iso")},if=virtio,format=raw,readonly=on',
        '-netdev', f'socket,id=direct,connect={ethernet}',
        '-device', 'virtio-net-pci,netdev=direct,mac=52:54:00:4d:55:53',
        '-serial', f'file:{STATE / "console.log"}']


def run_vm(_):
    cfg = config(); validate_adapter(cfg['interface'], cfg['source'])
    protect_state()
    # OS-held advisory lock, released even if the launcher crashes.
    lockfile = open(STATE / 'run.lock', 'a+b')
    lockfile.seek(0); lockfile.write(b'0'); lockfile.flush(); lockfile.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise ValueError('A VM launcher already holds this state directory.')
    gateway = ROOT / ('must-gateway.exe' if os.name == 'nt' else 'must-gateway')
    if not gateway.exists(): gateway = ROOT / 'dist' / gateway.name
    children = []
    try:
        with open(STATE / 'gateway.log', 'ab') as log:
            gw = subprocess.Popen([str(gateway), '--interface', cfg['interface'], '--source', cfg['source']], stdout=subprocess.PIPE, stderr=log)
            children.append(gw)
            line = gw.stdout.readline()
            if not line: raise ValueError('Gateway failed: see gateway.log')
            ports = json.loads(line)
            vm = subprocess.Popen(qemu_args(cfg, ports['ethernet']), stdout=log, stderr=log)
            children.append(vm)
            save(STATE / 'session.json', ports)
            print('VM running. In another terminal use: must-vm status / install-vpn / start-vpn / browser')
            print('Ctrl+C stops this VM and its gateway; host networking is unchanged.')
            while vm.poll() is None and gw.poll() is None: time.sleep(0.5)
            if vm.poll() is None and gw.poll() is not None:
                try: vm.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise ValueError('Gateway stopped; VM will be stopped without using a fallback network.')
            if vm.poll() not in (None, 0):
                raise ValueError('VM or gateway exited; see gateway.log and console.log.')
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=8)
                except subprocess.TimeoutExpired: child.kill(); child.wait()
        (STATE / 'session.json').unlink(missing_ok=True)
        lockfile.close()


def connect_guest():
    ports = json.loads((STATE / 'session.json').read_text())
    host, port = ports['ssh'].split(':'); address = f'[{host}]:{port}'
    kind, encoded, *_ = json.loads((STATE / 'host-key.json').read_text())['key'].split()
    key = paramiko.Ed25519Key(data=base64.b64decode(encoded))
    client = paramiko.SSHClient(); client.get_host_keys().add(address, kind, key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(host, int(port), username='vpn', key_filename=str(STATE / 'id_ed25519'), timeout=10,
                   banner_timeout=30, auth_timeout=20, look_for_keys=False, allow_agent=False)
    return client


def exec_guest(command):
    with connect_guest() as client:
        _, stdout, stderr = client.exec_command(command, get_pty=True)
        for line in stdout: print(line.replace('\r', ''), end='')
        return stdout.channel.recv_exit_status()


def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        piece = sock.recv(count-len(data))
        if not piece: raise EOFError('closed')
        data.extend(piece)
    return bytes(data)


def copy_channel(client, channel, transport):
    client.settimeout(None)
    while transport.is_active():
        ready, _, _ = select.select([client, channel], [], [], 60)
        for source in ready:
            data = source.recv(65536)
            if not data: return
            (channel if source is client else client).sendall(data)


def socks_client(client, transport):
    channel = None
    try:
        client.settimeout(15)
        version, methods = exact(client, 2)
        if version != 5 or 0 not in exact(client, methods):
            client.sendall(b'\x05\xff'); return
        client.sendall(b'\x05\x00')
        version, cmd, reserved, atyp = exact(client, 4)
        if version != 5 or cmd != 1 or reserved != 0: raise ValueError('CONNECT only')
        if atyp == 1: host = socket.inet_ntoa(exact(client, 4))
        elif atyp == 3: host = exact(client, exact(client, 1)[0]).decode('ascii')
        else: raise ValueError('IPv4/domain only')
        port = struct.unpack('!H', exact(client, 2))[0]
        # Guest resolves domain names and routes local API requests, never the host resolver.
        channel = transport.open_channel('direct-tcpip', (host, port), client.getpeername(), timeout=15)
        client.sendall(b'\x05\x00\x00\x01'+b'\x00'*6)
        copy_channel(client, channel, transport)
    except (OSError, EOFError, ValueError, paramiko.SSHException):
        try: client.sendall(b'\x05\x01\x00\x01'+b'\x00'*6)
        except OSError: pass
    finally:
        if channel: channel.close()
        client.close()


def forward(args):
    host, port = args.target.rsplit(':',1)
    port = int(port)
    if not host or not 1 <= port <= 65535: raise ValueError('Expected target hostname:port')
    with connect_guest() as guest, socket.socket() as listener:
        listener.bind(('127.0.0.1',args.port));listener.listen(16);listener.settimeout(1)
        print(f'127.0.0.1:{listener.getsockname()[1]} -> guest -> {host}:{port}. Ctrl+C stops forwarding.')
        slots=threading.BoundedSemaphore(64)
        def worker(client):
            try:
                with guest.get_transport().open_channel('direct-tcpip',(host,port),client.getpeername(),timeout=15) as channel:
                    copy_channel(client,channel,guest.get_transport())
            except (OSError,paramiko.SSHException): pass
            finally: client.close();slots.release()
        while guest.get_transport().is_active():
            try: client,_=listener.accept()
            except socket.timeout: continue
            if not slots.acquire(blocking=False):client.close();continue
            threading.Thread(target=worker,args=(client,),daemon=True).start()


def browser_path():
    candidates = ([Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
                   Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe'] if os.name == 'nt'
                  else [Path(p) for name in ('chromium', 'chromium-browser', 'google-chrome') if (p := shutil.which(name))])
    return next((str(p) for p in candidates if p.is_file()), None)


def browser_args(browser, port, portal, spki):
    return [browser, f'--user-data-dir={STATE / "browser-profile"}',
        f'--proxy-server=socks5://127.0.0.1:{port}', '--proxy-bypass-list=<-loopback>',
        '--disable-external-intent-requests', '--disable-background-networking',
        f'--ignore-certificate-errors-spki-list={spki}',
        '--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1', '--no-first-run', portal]


def guest_api_spki(guest):
    # Retrieve the local API certificate over the already pinned SSH connection.
    # Trust only its public key in this browser process, never all certificates.
    script = ('import socket,ssl,base64; '
        'ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE; '
        's=ctx.wrap_socket(socket.create_connection(("127.0.0.1",54630),timeout=10),server_hostname="localhost"); '
        'print(base64.b64encode(s.getpeercert(binary_form=True)).decode());s.close()')
    _, out, err = guest.exec_command('python3 -c '+shlex.quote(script))
    data = out.read()
    if out.channel.recv_exit_status() != 0:
        raise ValueError('Guest authentication API is unavailable. Run start-vpn first.')
    cert = x509.load_der_x509_certificate(base64.b64decode(data))
    public = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(public).digest()).decode()


def tunnel(args):
    with connect_guest() as guest, socket.socket() as listener:
        listener.bind(('127.0.0.1', args.port)); listener.listen(32); listener.settimeout(1)
        port = listener.getsockname()[1]
        print(f'SOCKS5: 127.0.0.1:{port}; use socks5h for guest DNS. Ctrl+C closes the tunnel.')
        if args.command == 'browser':
            browser = browser_path()
            if not browser: raise ValueError('Install Edge/Chrome/Chromium to open the isolated login browser.')
            subprocess.Popen(browser_args(browser, port, config()['portal'], guest_api_spki(guest)),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        slots = threading.BoundedSemaphore(128)
        def worker(c):
            try: socks_client(c, guest.get_transport())
            finally: slots.release()
        while guest.get_transport().is_active():
            try: client, _ = listener.accept()
            except socket.timeout: continue
            if not slots.acquire(blocking=False): client.close(); continue
            threading.Thread(target=worker, args=(client,), daemon=True).start()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('adapters')
    c = sub.add_parser('configure')
    c.add_argument('--deb', required=True); c.add_argument('--interface', required=True); c.add_argument('--source', required=True)
    c.add_argument('--dns', default='1.1.1.1'); c.add_argument('--base'); c.add_argument('--qemu')
    c.add_argument('--memory', type=int, choices=range(384, 4097), default=768, metavar='384..4096')
    c.add_argument('--accelerator', choices=['auto','tcg', 'whpx'] if os.name == 'nt' else ['auto','tcg', 'kvm'], default='auto')
    sub.add_parser('run')
    for name in ['status', 'install-vpn', 'start-vpn', 'stop-vpn', 'shutdown']: sub.add_parser(name)
    c = sub.add_parser('exec'); c.add_argument('guest_command')
    for name in ['browser', 'proxy']:
        c = sub.add_parser(name); c.add_argument('--port', type=int, default=1088)
    c=sub.add_parser('forward');c.add_argument('target');c.add_argument('--port',type=int,default=2222)
    args = p.parse_args()
    if args.command == 'adapters': print(json.dumps(adapters(), indent=2, ensure_ascii=False))
    elif args.command == 'configure': configure(args)
    elif args.command == 'run': run_vm(args)
    elif args.command in ['browser', 'proxy']: tunnel(args)
    elif args.command == 'forward': forward(args)
    else:
        commands = {'status': 'sudo must-vpn status', 'install-vpn': 'sudo must-vpn install',
                    'start-vpn': 'sudo must-vpn start', 'stop-vpn': 'sudo must-vpn stop', 'shutdown': 'sudo systemctl poweroff --no-block'}
        return exec_guest(args.guest_command if args.command == 'exec' else commands[args.command])
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    try: sys.exit(main())
    except KeyboardInterrupt: print('\nStopped.'); sys.exit(130)
    except (OSError, ValueError, subprocess.SubprocessError, paramiko.SSHException) as e:
        print(f'Error: {e}', file=sys.stderr); sys.exit(1)
