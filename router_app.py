"""MUST VPN Router: independent installation, VM state, and native SSH integration."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import app
import desktop_proxy
import local_ssh
import routing

VERSION = '0.3.3'
DEFAULT_PORT = 1189
PROXY_PORT = 1188
PAC_PORT = 18765
WEB_URL = 'https://aisc.must.edu.mo/auth/public/auth?callbackUrl=https%3A%2F%2Faisc.must.edu.mo%2Fapi%2Fauth%2Fcallback'


def state_home():
    explicit = os.environ.get('MUST_ROUTER_HOME')
    if explicit: return Path(explicit).expanduser().resolve()
    if sys.platform == 'win32': return (Path.home() / '.must-vpn-router').resolve()
    return (Path.home() / '.local/share/MUST-VPN-Router').resolve()


def self_command(command):
    if getattr(sys, 'frozen', False):
        # The optional Windows GUI launcher lives beside the main executable.
        entry = [str(Path(sys.executable).with_name('must-router.exe'))] if os.name == 'nt' else [sys.executable]
    else: entry = [sys.executable, str(Path(__file__).resolve())]
    return entry + ['--home', str(app.STATE), command]


def use_existing_vm(source):
    source = source.expanduser().resolve()
    if source == app.STATE or app.STATE in source.parents or source in app.STATE.parents:
        raise ValueError('Router and VM data must use separate directories.')
    if app.LINKED_VM_STATE == source:
        return
    if app.LINKED_VM_STATE is not None or (app.STATE / 'config.json').exists():
        raise ValueError('Router already has a VM configured.')
    for name in ('config.json', 'disk.qcow2', 'seed.iso', 'id_ed25519', 'host-key.json'):
        if not (source / name).is_file():
            raise ValueError(f'Source VM is missing {name}.')
    app.protect_state()
    app.save(app.STATE / 'vm-link.json', {'state': str(source)})
    print(f'Router will use the existing VM at {source}.')


def clone_state(source, qemu=None):
    source = source.expanduser().resolve()
    if source == app.STATE or source in app.STATE.parents or app.STATE in source.parents:
        raise ValueError('The new state directory must be separate from the source.')
    if app.LINKED_VM_STATE is not None or (app.STATE / 'disk.qcow2').exists() or (app.STATE / 'config.json').exists():
        raise ValueError('Router state already exists; existing data is preserved.')
    cfg = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    own_qemu = qemu or (app.ROOT / 'runtime/qemu/qemu-system-x86_64.exe' if os.name == 'nt'
                         else Path(shutil.which('qemu-system-x86_64') or '/usr/bin/qemu-system-x86_64'))
    own_qemu = Path(own_qemu).resolve()
    if not own_qemu.is_file(): raise ValueError('Router QEMU is missing; install the router package or provide --qemu.')
    required = ['disk.qcow2', 'seed.iso', 'id_ed25519', 'host-key.json']
    for name in required:
        if not (source / name).is_file(): raise ValueError(f'Source VM is missing {name}.')
    with open(source / 'run.lock', 'a+b') as lease:
        try: routing._lock(lease, True)
        except OSError as error: raise ValueError('Shut down the source VM before importing its disk.') from error
        try:
            app.protect_state()
            for name in required:
                shutil.copyfile(source / name, app.STATE / name)
                if os.name != 'nt': (app.STATE / name).chmod(0o600)
            cfg['qemu'] = str(own_qemu)
            app.save(app.STATE / 'config.json', cfg)
            app.save(app.STATE / 'routing.json', routing.Policy().data)
        finally: routing._lock(lease, False)
    print(f'Independent VM copied to {app.STATE}. Source VM remains separate.')


def settings(arguments):
    if not arguments or arguments in (['--gui'], ['--no-browser']):
        import router_settings
        from router_control import Controller
        app.protect_state()
        def save_policy(policy):
            app.save(app.STATE / 'routing.json', policy.data)
            if (app.STATE / 'ssh/targets.json').exists():
                local_ssh.write_config(app.STATE, proxy_command())
        return router_settings.run(app.STATE, app.RESOURCES / 'router_settings.html', save_policy,
                                   open_browser=arguments != ['--no-browser'],
                                   controller=Controller(app.STATE, lambda event: serve([], event)),
                                   open_web=open_web)
    if arguments == ['--show']:
        print(json.dumps(routing.Policy.load(app.STATE).data, indent=2, ensure_ascii=False))
        return 0
    if arguments:
        sys.argv = [sys.argv[0], 'routing-config', *arguments]
        result = app.main()
        if (app.STATE / 'ssh/targets.json').exists():
            local_ssh.write_config(app.STATE, proxy_command())
        return result


def proxy_command():
    return ssh_proxy_command('ssh-connect')


def ssh_proxy_command(name):
    command = self_command(name)
    text = subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command)
    return text.replace('%', '%%')


def serve(arguments, stop_event=None):
    parser = argparse.ArgumentParser(prog='must-router serve')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--probe')
    options = parser.parse_args(arguments)
    options.stop_event = stop_event
    app.protect_state()
    policy = routing.Policy.load(app.STATE)
    router = routing.Router(app.STATE, policy)
    pac_url = f'http://127.0.0.1:{PAC_PORT}/proxy.pac?{time.time_ns()}'
    with routing.socks_listener(PROXY_PORT, router.connect), \
         routing.pac_server(PAC_PORT, policy, PROXY_PORT, app.STATE), \
         desktop_proxy.use_pac(app.STATE, pac_url, policy.web_enabled and policy.data['desktop_web']):
        print(f'本地分流 SOCKS5：127.0.0.1:{PROXY_PORT}；PAC：http://127.0.0.1:{PAC_PORT}/proxy.pac', flush=True)
        app.serve(options)
    return 0


def open_web():
    browser = app.browser_path()
    if not browser:
        raise ValueError('请安装 Chromium、Edge、Chrome 或 Brave。')
    app.protect_state()
    app.close_isolated_browser('routed-browser-profile')
    subprocess.Popen([browser, f'--user-data-dir={app.STATE / "routed-browser-profile"}',
        f'--proxy-pac-url=http://127.0.0.1:{PAC_PORT}/proxy.pac', '--disable-quic',
        '--disable-external-intent-requests', '--no-first-run', WEB_URL])



def main():
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument('--home', type=Path)
    global_args, args = global_parser.parse_known_args()
    app.STATE = (global_args.home or state_home()).expanduser().resolve()
    link = app.STATE / 'vm-link.json'
    app.LINKED_VM_STATE = Path(json.loads(link.read_text(encoding='utf-8'))['state']) if link.exists() else None
    app.self_command = self_command
    # These defaults belong only to this entry point; must-vm keeps its own state.
    routing.DEFAULTS.update(aisc_hosts=['aisc.must.edu.mo'],
        aisc_networks=['10.100.16.13/32', '10.100.16.8/32', '172.16.130.167/32'])
    if not args or args[0] in ('-h', '--help'):
        print('MUST VPN Router ' + VERSION)
        print('init --from-state PATH    关闭原虚拟机后创建独立副本')
        print('reuse-vm --from-state PATH  复用已有虚拟机')
        print('serve                    启动虚拟机、VPN 及本地分流入口')
        print('ui / settings            打开主界面：VPN 启停、状态和连接设置')
        print('settings --show          在终端查看设置；也支持 --mode / --domain / --fallback')
        print('import-ssh [AISC AISC-CPU] 导入来宾的指定 SSH 配置及密钥到私有目录')
        print('ssh-setup [--install]     生成 SSH 配置；--install 添加独立 Include')
        print('ssh AISC [command...]     用本机 OpenSSH 连接，无需全局 SSH 配置')
        print('web                      打开使用分流规则的独立浏览器')
        print('其他命令：configure / adapters / run / status / server-status / shutdown / ssh-connect')
        return 0
    command, arguments = args[0], args[1:]
    if command == 'reuse-vm':
        parser = argparse.ArgumentParser(prog='must-router reuse-vm')
        parser.add_argument('--from-state', required=True, type=Path)
        use_existing_vm(parser.parse_args(arguments).from_state)
        return 0
    if command == 'init':
        parser = argparse.ArgumentParser(prog='must-router init')
        parser.add_argument('--from-state', required=True, type=Path); parser.add_argument('--qemu', type=Path)
        options = parser.parse_args(arguments); clone_state(options.from_state, options.qemu)
        return 0
    if command in ('ui', 'settings'): return settings(arguments)
    if command == 'import-ssh':
        aliases = arguments or ['AISC', 'AISC-CPU']
        app.protect_state()
        with app.connect_guest() as guest: targets = local_ssh.import_guest(guest, app.STATE, aliases)
        # Reapply Windows ACLs after importing SSH files into the private directory.
        app.protect_state()
        config = local_ssh.write_config(app.STATE, proxy_command())
        print(f'Imported {len(targets)} SSH targets into {config}. Private keys are local only.')
        return 0
    if command == 'ssh-setup':
        parser = argparse.ArgumentParser(prog='must-router ssh-setup'); parser.add_argument('--install', action='store_true')
        parser.add_argument('--existing-alias', help='Use the credentials and HostName from an existing OpenSSH alias')
        options = parser.parse_args(arguments)
        if options.existing_alias:
            app.protect_state()
            config = local_ssh.write_existing_alias(
                app.STATE, options.existing_alias, proxy_command(), ssh_proxy_command('ssh-enabled'))
        else:
            config = local_ssh.write_config(app.STATE, proxy_command())
        if options.install:
            backup = local_ssh.install_include(config, Path.home() / '.ssh/config')
            print('Native SSH aliases enabled: ' + (options.existing_alias or 'AISC / AISC-CPU / AISCCPU'))
            if backup: print('Previous SSH config backup:', backup)
        else: print('Include "' + config.as_posix() + '"')
        return 0
    if command == 'ssh':
        if not arguments: raise ValueError('Usage: must-router ssh AISC [remote command]')
        ssh = shutil.which('ssh.exe' if os.name == 'nt' else 'ssh')
        if not ssh: raise ValueError('Native OpenSSH client is required.')
        config = local_ssh.write_config(app.STATE, proxy_command())
        return subprocess.call([ssh, '-F', str(config), *arguments])
    if command == 'ssh-enabled':
        return 0 if routing.Policy.load(app.STATE).aisc_ssh_enabled else 1
    if command == 'web':
        open_web()
        return 0
    if command == 'serve':
        return serve(arguments)
    if command in ('browser', 'proxy', 'smart-proxy') and '--port' not in arguments:
        arguments += ['--port', str(PROXY_PORT if command == 'smart-proxy' else 0)]
    if command == 'smart-proxy' and '--pac-port' not in arguments: arguments += ['--pac-port', str(PAC_PORT)]
    sys.argv = [sys.argv[0], command, *arguments]
    return app.main()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    try: sys.exit(main())
    except KeyboardInterrupt: print('\nStopped.', file=sys.stderr); sys.exit(130)
    except (OSError, ValueError, subprocess.SubprocessError, app.paramiko.SSHException) as error:
        print(f'Error: {error}', file=sys.stderr); sys.exit(1)
