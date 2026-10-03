"""MUST VPN VM: isolated QEMU appliance launcher. Never executes a vendor binary on host."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import io
import ipaddress
import json
import logging
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
import routing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography import x509

# Connection retries while the guest boots are expected; the raised exception is
# enough for the caller, without a transport-thread traceback in the terminal.
logging.getLogger('paramiko.transport').setLevel(logging.CRITICAL)

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
RESOURCES = Path(getattr(sys, '_MEIPASS', ROOT))
PINNED_DEB = '298c0bcf6aa923d53337f525affdbc99d61d07bb4119ee10a39eb697eefe32d5'


def state_home():
    explicit = os.environ.get('MUST_VM_HOME')
    if explicit:
        return Path(explicit).expanduser().resolve()
    if sys.platform == 'win32':
        # AppData can be redirected for children of packaged desktop apps.
        # A profile-root directory is shared with ordinary Explorer launches.
        shared = Path.home() / '.must-vpn-vm'
        legacy = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'MUST-VPN-VM'
        if not shared.exists() and legacy.exists():
            return legacy.resolve()
        return shared.resolve()
    return (Path.home() / '.local/share/MUST-VPN-VM').resolve()


STATE = state_home()


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
    if ipaddress.ip_address(args.probe).version != 4:
        raise ValueError('VPN probe must be an IPv4 campus address.')
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
         accelerator=args.accelerator, portal='https://vpn.must.edu.mo/', probe=args.probe))
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


def lock_vm():
    """Hold the launcher lease until this file is closed or the process exits."""
    lockfile = open(STATE / 'run.lock', 'a+b')
    try:
        # Lock before writing: Windows rejects writes into another process's lock.
        routing._lock(lockfile, True)
    except OSError as error:
        lockfile.close()
        raise ValueError('A VM launcher already holds this state directory.') from error
    return lockfile


def vm_running():
    """A session file may survive a crash; only the OS-held lease proves liveness."""
    try:
        lockfile = open(STATE / 'run.lock', 'r+b')
    except FileNotFoundError:
        return False
    with lockfile:
        try: routing._lock(lockfile, True)
        except OSError: return True
        routing._lock(lockfile, False)
    return False


def run_vm(_):
    cfg = config(); validate_adapter(cfg['interface'], cfg['source'])
    protect_state()
    lockfile = lock_vm()
    gateway = ROOT / ('must-gateway.exe' if os.name == 'nt' else 'must-gateway')
    if not gateway.exists(): gateway = ROOT / 'dist' / gateway.name
    children = []
    try:
        # Discard old ports only after exclusively acquiring the launcher lease.
        (STATE / 'session.json').unlink(missing_ok=True)
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
        try: (STATE / 'session.json').unlink(missing_ok=True)
        finally: lockfile.close()


def connect_guest():
    ports = json.loads((STATE / 'session.json').read_text())
    host, port = ports['ssh'].split(':'); address = f'[{host}]:{port}'
    kind, encoded, *_ = json.loads((STATE / 'host-key.json').read_text())['key'].split()
    key = paramiko.Ed25519Key(data=base64.b64decode(encoded))
    client = paramiko.SSHClient(); client.get_host_keys().add(address, kind, key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, int(port), username='vpn', key_filename=str(STATE / 'id_ed25519'), timeout=10,
                       banner_timeout=30, auth_timeout=20, look_for_keys=False, allow_agent=False)
    except BaseException:
        client.close()
        raise
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


def close_isolated_browser(profile='browser-profile'):
    if sys.platform != 'win32': return
    # Chromium reuses an open profile and ignores a new --proxy-server value.
    # Match only processes using this application's dedicated browser profile.
    script = ("$profile=$env:MUST_VM_BROWSER_PROFILE; "
        "$quoted='--user-data-dir=\"'+$profile+'\"'; $plain='--user-data-dir='+$profile; "
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -and ($_.CommandLine.Contains($quoted) -or $_.CommandLine.Contains($plain)) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, 'MUST_VM_BROWSER_PROFILE': str(STATE / profile)})


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
            close_isolated_browser()
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


def self_command(command):
    return [sys.executable, command] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve()), command]


def start_helper(command, log_name, *arguments):
    protect_state()
    with open(STATE / log_name, 'ab') as log:
        return subprocess.Popen([*self_command(command), *arguments], stdout=log, stderr=log,
            env={**os.environ, 'MUST_VM_HOME': str(STATE)},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def stop_helper(child):
    if child is None or child.poll() is not None: return
    child.terminate()
    try: child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill(); child.wait()


def wait_for_guest(launcher, timeout=240):
    deadline = time.monotonic() + timeout
    last_error = 'No current SSH session has been published.'
    print('正在启动隔离虚拟机并等待终端连接...', flush=True)
    while time.monotonic() < deadline:
        if launcher is not None and launcher.poll() is not None:
            raise ValueError(f'VM startup failed; see {STATE / "terminal-vm.log"} and gateway.log.')
        running = vm_running()
        if launcher is None and not running:
            raise ValueError(f'VM launcher stopped while waiting for SSH; see {STATE / "terminal-vm.log"} and gateway.log.')
        if running and (STATE / 'session.json').exists():
            try:
                with connect_guest(): return
            except (OSError, ValueError, EOFError, paramiko.SSHException) as error:
                last_error = f'{type(error).__name__}: {error}'
        time.sleep(3)
    raise ValueError(f'VM did not become ready within {timeout} seconds. Last SSH error: {last_error}; '
                     f'see {STATE / "console.log"} and gateway.log.')


def guest_check(command):
    with connect_guest() as guest:
        _, out, _ = guest.exec_command(command)
        return out.channel.recv_exit_status() == 0


def vpn_ready(target):
    # A utun address alone does not prove that campus resources use the VPN.
    # Check the route to a known campus host before opening the shell.
    return guest_check("systemctl is-active --quiet aTrustDaemon.service must-vpn-core.service && "
        f"ip -4 route get {target} | grep -Eq ' dev utun[0-9]+( |$)'")


def pinned_guest_ssh():
    ssh = shutil.which('ssh.exe' if os.name == 'nt' else 'ssh')
    if not ssh: raise ValueError('OpenSSH client is required to open the interactive guest terminal.')
    ports = json.loads((STATE / 'session.json').read_text())
    host, port = ports['ssh'].split(':')
    if host != '127.0.0.1': raise ValueError('Guest SSH forwarding must be loopback-only.')
    kind, encoded, *_ = json.loads((STATE / 'host-key.json').read_text())['key'].split()
    known_hosts = STATE / 'guest_known_hosts'
    known_hosts.write_text(f'[{host}]:{port} {kind} {encoded}\n', encoding='ascii')
    if os.name != 'nt': known_hosts.chmod(0o600)
    null_config = 'NUL' if os.name == 'nt' else '/dev/null'
    return [ssh, '-F', null_config, '-o', 'StrictHostKeyChecking=yes',
        '-o', f'UserKnownHostsFile={known_hosts}', '-o', 'IdentitiesOnly=yes',
        '-o', 'BatchMode=yes', '-i', str(STATE / 'id_ed25519'), '-p', port,
        '-tt', 'vpn@127.0.0.1']


@contextmanager
def ready_vm(probe, need_ssh=False):
    """Keep an explicitly requested VM alive, and close only one we started."""
    launcher = browser = None
    try:
        if not vm_running():
            launcher = start_helper('run', 'terminal-vm.log')
        wait_for_guest(launcher)
        if not guest_check('test -x /usr/share/sangfor/aTrust/resources/bin/aTrustAgent'):
            print('首次在虚拟机内安装学校 VPN...', flush=True)
            if exec_guest('sudo must-vpn install') != 0: raise ValueError('Guest VPN installation failed.')
        if need_ssh and not guest_check('command -v ssh >/dev/null'):
            print('正在虚拟机内安装 SSH 客户端...', flush=True)
            if exec_guest('sudo apt-get update && sudo apt-get install -y --no-install-recommends openssh-client') != 0:
                raise ValueError('Guest SSH client installation failed.')
        print('正在启动学校 VPN...', flush=True)
        if exec_guest('sudo must-vpn start') != 0: raise ValueError('Guest VPN service failed to start.')
        if not vpn_ready(probe):
            print('等待已保存的登录状态...', flush=True)
            for _ in range(5):
                time.sleep(3)
                if vpn_ready(probe): break
        if not vpn_ready(probe):
            print('需要学校扫码认证；正在打开隔离登录浏览器...', flush=True)
            # This browser has its own proxy configuration, so it need not use
            # the fixed port reserved for manually configured applications.
            browser = start_helper('browser', 'terminal-browser.log', '--port', '0')
        deadline = time.monotonic() + 600
        while not vpn_ready(probe):
            if browser is not None and browser.poll() is not None:
                raise ValueError(f'Login browser bridge stopped; see {STATE / "terminal-browser.log"}.')
            if launcher is not None and launcher.poll() is not None:
                raise ValueError('VM stopped while waiting for VPN authentication.')
            if time.monotonic() >= deadline:
                raise ValueError('VPN was not ready after 10 minutes. Complete login in the isolated browser and retry.')
            time.sleep(3)
        stop_helper(browser)
        if browser is not None: close_isolated_browser()
        browser = None
        yield
    finally:
        stop_helper(browser)
        if browser is not None: close_isolated_browser()
        if launcher is not None and launcher.poll() is None:
            try:
                exec_guest('sudo systemctl poweroff --no-block')
                launcher.wait(timeout=45)
            except (OSError, ValueError, EOFError, paramiko.SSHException, subprocess.TimeoutExpired):
                print('虚拟机未确认关机；请运行 must-vm shutdown。', file=sys.stderr)


def vm_probe(args):
    print(f'VM state: "{STATE}"', flush=True)
    cfg = config(); validate_adapter(cfg['interface'], cfg['source'])
    probe = args.probe or cfg.get('probe', '10.100.16.13')
    if ipaddress.ip_address(probe).version != 4: raise ValueError('VPN probe must be an IPv4 campus address.')
    return probe


def terminal(args):
    probe = vm_probe(args)
    with ready_vm(probe, need_ssh=True):
        print(f'校内地址 {probe} 已走 VPN。现在可直接运行 ssh 用户名@校内主机；输入 exit 退出终端。', flush=True)
        return subprocess.call(pinned_guest_ssh())


def guest_route_ready(guest, probe):
    command = ("systemctl is-active --quiet aTrustDaemon.service must-vpn-core.service && "
        f"ip -4 route get {probe} | grep -Eq ' dev utun[0-9]+( |$)'")
    _, out, _ = guest.exec_command(command, timeout=5)
    try:
        out.channel.settimeout(5)
        out.read()
        return out.channel.recv_exit_status() == 0
    finally: out.channel.close()


def guest_vpn_connect(guest, policy, host, port):
    if not policy.vpn_allowed(host): raise OSError('Target is outside the configured school/AISC scope.')
    if ':' in host:
        raise OSError('Guest VPN supports IPv4 only.')
    # Existing VMs need no seed/image migration: send the helper over pinned SSH.
    # Guest sudo supplies SO_BINDTODEVICE permission, never host privileges.
    script = (RESOURCES / 'guest/vpn_relay.py').read_text(encoding='utf-8')
    channel = None
    try:
        channel = guest.get_transport().open_session(timeout=policy.data['vpn_timeout'])
        channel.settimeout(policy.data['vpn_timeout'])
        channel.exec_command('sudo -n python3 -u -c ' + shlex.quote(script) + ' ' + shlex.quote(host)
                            + f' {port} {policy.data["vpn_timeout"]}')
        if routing.exact(channel, 1) != b'\0': raise OSError('Guest refused the VPN connection.')
        return channel
    except (OSError, paramiko.SSHException, ValueError, EOFError) as e:
        if channel is not None: channel.close()
        raise OSError('Guest VPN connection failed.') from e


def serve(args):
    policy = routing.Policy.load(STATE)
    probe = vm_probe(args)
    protect_state()
    with routing.server_lease(STATE):
        (STATE / 'server.json').unlink(missing_ok=True)
        try:
            with ready_vm(probe), connect_guest() as guest:
                ready = threading.Event()
                def connect(host, port):
                    if not ready.is_set(): raise OSError('VPN is not ready.')
                    return guest_vpn_connect(guest, policy, host, port)
                with routing.socks_listener(args.port, connect) as port:
                    print(f'VPN 服务器：127.0.0.1:{port}。保持此窗口运行；Ctrl+C 停止服务器。', flush=True)
                    try:
                        while guest.get_transport().is_active():
                            try: available = guest_route_ready(guest, probe)
                            except (OSError, EOFError, ValueError, paramiko.SSHException): available = False
                            if available: ready.set()
                            else: ready.clear()
                            save(STATE / 'server.json', {'host': '127.0.0.1', 'port': port,
                                'ready': available, 'updated': time.time()})
                            time.sleep(3)
                        raise ValueError('VM SSH connection stopped; VPN server is offline.')
                    finally:
                        ready.clear()
                        (STATE / 'server.json').unlink(missing_ok=True)
        finally:
            (STATE / 'server.json').unlink(missing_ok=True)


def routing_config(args):
    policy = routing.Policy.load(STATE)
    data = policy.data.copy()
    for key in ('aisc_hosts', 'aisc_networks', 'school_hosts', 'school_networks'):
        value = getattr(args, key)
        if value is not None: data[key] = value
    if args.fallback is not None: data['fallback'] = args.fallback == 'on'
    if args.direct_timeout is not None: data['direct_timeout'] = args.direct_timeout
    policy = routing.Policy(data)
    protect_state(); save(STATE / 'routing.json', policy.data)
    print(json.dumps(policy.data, indent=2, ensure_ascii=False))
    print('分流配置已保存。重启 serve / smart-proxy 后生效。')


def smart_proxy(args):
    policy = routing.Policy.load(STATE)
    router = routing.Router(STATE, policy)
    with routing.socks_listener(args.port, router.connect) as port, \
         routing.pac_server(args.pac_port, policy, port, STATE) as pac_port:
        pac_url = f'http://127.0.0.1:{pac_port}/proxy.pac'
        print(f'本地分流 SOCKS5：127.0.0.1:{port}；浏览器 PAC：{pac_url}', flush=True)
        print('AISC 固定走 VPN；学校连接失败才回退。此入口不会启动虚拟机。', flush=True)
        if args.browser:
            browser = browser_path()
            if not browser: raise ValueError('Install Edge/Chrome/Chromium to open the routed browser.')
            protect_state()
            # Keep the routed profile separate from the all-guest login browser.
            close_isolated_browser('routed-browser-profile')
            subprocess.Popen([browser, f'--user-data-dir={STATE / "routed-browser-profile"}',
                f'--proxy-pac-url={pac_url}', '--disable-external-intent-requests',
                '--disable-quic', '--no-first-run', args.url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while True: time.sleep(1)
        finally:
            if args.browser: close_isolated_browser('routed-browser-profile')


def ssh_connect(args):
    # ProxyCommand stdout must contain only the target's protocol bytes.
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    router = routing.Router(STATE, routing.Policy.load(STATE))
    routing.stdio_bridge(router.connect(args.host, args.port), sys.stdin.buffer, sys.stdout.buffer)


def ssh_config(_):
    policy = routing.Policy.load(STATE)
    hosts = sorted(set(h for key in ('aisc_hosts', 'school_hosts') for h in policy.data[key]))
    patterns = [p for h in hosts for p in ([h] if h.replace('.', '').isdigit() else [h, '*.' + h])]
    patterns += [str(n.network_address) for group in policy.networks.values() for n in group if n.prefixlen == 32]
    if not patterns: raise ValueError('No configured SSH hosts. Configure routing-config first.')
    command = self_command('ssh-connect')
    if os.name == 'nt': command = subprocess.list2cmdline(command)
    else: command = shlex.join(command)
    # Literal percent characters in executable paths are escaped for OpenSSH.
    print('Host ' + ' '.join(sorted(set(patterns))))
    print('    ProxyCommand ' + command.replace('%', '%%') + ' %h %p')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('adapters')
    c = sub.add_parser('configure')
    c.add_argument('--deb', required=True); c.add_argument('--interface', required=True); c.add_argument('--source', required=True)
    c.add_argument('--dns', default='1.1.1.1'); c.add_argument('--base'); c.add_argument('--qemu')
    c.add_argument('--memory', type=int, choices=range(384, 4097), default=768, metavar='384..4096')
    c.add_argument('--probe', default='10.100.16.13', help='Known campus IPv4 host used to verify the VPN route')
    c.add_argument('--accelerator', choices=['auto','tcg', 'whpx'] if os.name == 'nt' else ['auto','tcg', 'kvm'], default='auto')
    sub.add_parser('run')
    for name in ['status', 'install-vpn', 'start-vpn', 'stop-vpn', 'shutdown']: sub.add_parser(name)
    c = sub.add_parser('exec'); c.add_argument('guest_command')
    for name in ['browser', 'proxy']:
        c = sub.add_parser(name); c.add_argument('--port', type=int, default=1088)
    c=sub.add_parser('forward');c.add_argument('target');c.add_argument('--port',type=int,default=2222)
    c=sub.add_parser('terminal');c.add_argument('--probe', help='Override the campus IPv4 route probe')
    c=sub.add_parser('serve', help='Start and keep the VM VPN server running')
    c.add_argument('--probe'); c.add_argument('--port', type=int, default=1089)
    c=sub.add_parser('routing-config', help='Configure AISC-only VPN routing and campus fallback')
    for flag, dest in [('aisc-host','aisc_hosts'), ('aisc-network','aisc_networks'),
                       ('school-host','school_hosts'), ('school-network','school_networks')]:
        c.add_argument('--' + flag, dest=dest, action='append')
    c.add_argument('--fallback', choices=['on','off']); c.add_argument('--direct-timeout', type=float)
    c=sub.add_parser('smart-proxy', help='Local selective proxy; never starts the VM')
    c.add_argument('--port', type=int, default=1088); c.add_argument('--pac-port', type=int, default=8765)
    c.add_argument('--browser', action='store_true'); c.add_argument('--url', default='https://www.must.edu.mo/')
    c=sub.add_parser('ssh-connect', help='Binary stdio connector for OpenSSH ProxyCommand')
    c.add_argument('host'); c.add_argument('port', type=int)
    sub.add_parser('ssh-config', help='Print an SSH config snippet for selective routing')
    sub.add_parser('server-status', help='Read local VPN server readiness without starting or probing it')
    args = p.parse_args()
    if args.command == 'adapters': print(json.dumps(adapters(), indent=2, ensure_ascii=False))
    elif args.command == 'configure': configure(args)
    elif args.command == 'run': run_vm(args)
    elif args.command in ['browser', 'proxy']: tunnel(args)
    elif args.command == 'forward': forward(args)
    elif args.command == 'terminal': return terminal(args)
    elif args.command == 'serve': serve(args)
    elif args.command == 'routing-config': routing_config(args)
    elif args.command == 'smart-proxy': smart_proxy(args)
    elif args.command == 'ssh-connect': ssh_connect(args)
    elif args.command == 'ssh-config': ssh_config(args)
    elif args.command == 'server-status':
        print(json.dumps({'ready': routing.server_endpoint(STATE) is not None}, ensure_ascii=False))
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
