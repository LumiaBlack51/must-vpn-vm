"""MUST VPN Router: independent installation, VM state, and native SSH integration."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import app
import local_ssh
import routing

VERSION = '0.3.1'
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


def clone_state(source, qemu=None):
    source = source.expanduser().resolve()
    if source == app.STATE or source in app.STATE.parents or app.STATE in source.parents:
        raise ValueError('The new state directory must be separate from the source.')
    if (app.STATE / 'disk.qcow2').exists() or (app.STATE / 'config.json').exists():
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
        app.protect_state()
        def save_policy(policy):
            app.save(app.STATE / 'routing.json', policy.data)
            if (app.STATE / 'ssh/targets.json').exists():
                local_ssh.write_config(app.STATE, proxy_command())
        return router_settings.run(app.STATE, app.RESOURCES / 'router_settings.html', save_policy,
                                   open_browser=arguments != ['--no-browser'])
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
    command = self_command('ssh-connect')
    text = subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command)
    return text.replace('%', '%%')


def main():
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument('--home', type=Path)
    global_args, args = global_parser.parse_known_args()
    app.STATE = (global_args.home or state_home()).expanduser().resolve()
    app.self_command = self_command
    # These defaults belong only to this entry point; must-vm keeps its own state.
    routing.DEFAULTS.update(aisc_hosts=['aisc.must.edu.mo'],
        aisc_networks=['10.100.16.13/32', '10.100.16.8/32', '172.16.130.167/32'])
    if not args or args[0] in ('-h', '--help'):
        print('MUST VPN Router ' + VERSION)
        print('init --from-state PATH    关闭原虚拟机后创建独立副本')
        print('serve                    启动独立虚拟机、VPN 及本地分流入口')
        print('settings                 打开图形设置页面（无需启动虚拟机）')
        print('settings --show          在终端查看设置；也支持 --mode / --domain / --fallback')
        print('import-ssh [AISC AISC-CPU] 导入来宾的指定 SSH 配置及密钥到私有目录')
        print('ssh-setup [--install]     生成 SSH 配置；--install 添加独立 Include')
        print('ssh AISC [command...]     用本机 OpenSSH 连接，无需全局 SSH 配置')
        print('web                      打开使用分流规则的独立浏览器')
        print('其他命令：configure / adapters / run / status / server-status / shutdown / ssh-connect')
        return 0
    command, arguments = args[0], args[1:]
    if command == 'init':
        parser = argparse.ArgumentParser(prog='must-router init')
        parser.add_argument('--from-state', required=True, type=Path); parser.add_argument('--qemu', type=Path)
        options = parser.parse_args(arguments); clone_state(options.from_state, options.qemu)
        return 0
    if command == 'settings': return settings(arguments)
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
        options = parser.parse_args(arguments)
        config = local_ssh.write_config(app.STATE, proxy_command())
        if options.install:
            backup = local_ssh.install_include(config, Path.home() / '.ssh/config')
            print('Native SSH aliases enabled: ssh AISC / ssh AISC-CPU / ssh AISCCPU')
            if backup: print('Previous SSH config backup:', backup)
        else: print('Include "' + config.as_posix() + '"')
        return 0
    if command == 'ssh':
        if not arguments: raise ValueError('Usage: must-router ssh AISC [remote command]')
        ssh = shutil.which('ssh.exe' if os.name == 'nt' else 'ssh')
        if not ssh: raise ValueError('Native OpenSSH client is required.')
        config = local_ssh.write_config(app.STATE, proxy_command())
        return subprocess.call([ssh, '-F', str(config), *arguments])
    if command == 'web':
        browser = app.browser_path()
        if not browser: raise ValueError('Edge/Chrome/Chromium is required.')
        app.protect_state(); app.close_isolated_browser('routed-browser-profile')
        subprocess.Popen([browser, f'--user-data-dir={app.STATE / "routed-browser-profile"}',
            f'--proxy-pac-url=http://127.0.0.1:{PAC_PORT}/proxy.pac', '--disable-quic',
            '--disable-external-intent-requests', '--no-first-run', WEB_URL])
        return 0
    if command == 'serve':
        if '--port' not in arguments: arguments += ['--port', str(DEFAULT_PORT)]
        app.protect_state()
        policy = routing.Policy.load(app.STATE)
        router = routing.Router(app.STATE, policy)
        # Own both listeners in this process: bind errors fail startup, and a
        # process crash cannot leave a detached proxy holding the fixed ports.
        with routing.socks_listener(PROXY_PORT, router.connect), \
             routing.pac_server(PAC_PORT, policy, PROXY_PORT, app.STATE):
            sys.argv = [sys.argv[0], command, *arguments]
            print(f'本地分流 SOCKS5：127.0.0.1:{PROXY_PORT}；PAC：http://127.0.0.1:{PAC_PORT}/proxy.pac', flush=True)
            return app.main()
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
