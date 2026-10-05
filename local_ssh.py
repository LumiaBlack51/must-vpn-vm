"""Import selected guest SSH identities into the router's private state directory."""
import json
import os
from pathlib import Path
import re
import shlex
import uuid

import routing


def safe_alias(alias):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', alias):
        raise ValueError('SSH alias must contain only letters, numbers, dot, dash or underscore.')
    return alias


def import_guest(guest, state, aliases):
    folder = state / 'ssh'
    folder.mkdir(exist_ok=True)
    if os.name != 'nt': folder.chmod(0o700)
    targets = []
    known_hosts = []
    with guest.open_sftp() as sftp:
        home = sftp.normalize('.')
        for alias in aliases:
            safe_alias(alias)
            _, out, err = guest.exec_command('ssh -G ' + shlex.quote(alias), timeout=10)
            fields = {}
            for line in out.read().decode().splitlines():
                key, _, value = line.partition(' ')
                fields.setdefault(key, []).append(value)
            if out.channel.recv_exit_status() != 0:
                raise ValueError(f'Cannot resolve guest SSH alias {alias}.')
            host = routing.hostname(fields.get('hostname', [alias])[0])
            if host.lower() == alias.lower(): raise ValueError(f'Guest SSH alias {alias} has no configured HostName.')
            keys = []
            for index, source in enumerate(fields.get('identityfile', [])):
                source = source.replace('~/', home + '/', 1).replace('%d', home)
                try:
                    with sftp.open(source, 'rb') as file: data = file.read()
                except FileNotFoundError: continue
                destination = folder / f'{alias}-{index}.key'
                destination.write_bytes(data)
                if os.name != 'nt': destination.chmod(0o600)
                keys.append(str(destination))
            if not keys: raise ValueError(f'No readable identity configured for {alias}.')
            port = int(fields.get('port', ['22'])[0])
            key_name = host if port == 22 else f'[{host}]:{port}'
            command = 'ssh-keygen -F ' + shlex.quote(key_name) + ' -f ' + shlex.quote(home + '/.ssh/known_hosts')
            _, out, _ = guest.exec_command(command, timeout=10)
            known = out.read().decode()
            if out.channel.recv_exit_status() != 0:
                raise ValueError(f'No trusted guest host key for {alias}; verify it in the guest first.')
            known_hosts.extend(line for line in known.splitlines() if line and not line.startswith('#'))
            names = [alias]
            if alias.upper() == 'AISC-CPU': names.append('AISCCPU')
            targets.append({'aliases': names, 'hostname': host, 'port': port,
                            'user': fields['user'][0], 'keys': keys})
    known_file = folder / 'known_hosts'
    known_file.write_text('\n'.join(dict.fromkeys(known_hosts)) + '\n', encoding='utf-8')
    inventory = folder / 'targets.json'
    inventory.write_text(json.dumps(targets, indent=2), encoding='utf-8')
    if os.name != 'nt': known_file.chmod(0o600); inventory.chmod(0o600)
    return targets


def write_config(state, command):
    folder = state / 'ssh'
    targets = json.loads((folder / 'targets.json').read_text(encoding='utf-8'))
    lines = ['# Managed by MUST VPN Router; credentials stay in its private state directory.']
    for target in targets:
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+', target['user']):
            raise ValueError('Unsupported SSH username.')
        if not 1 <= int(target['port']) <= 65535: raise ValueError('Invalid SSH port.')
        lines += ['Host ' + ' '.join(safe_alias(a) for a in target['aliases']),
                  '    HostName ' + routing.hostname(target['hostname']), '    User ' + target['user'],
                  '    Port ' + str(target['port']), '    IdentitiesOnly yes',
                  '    StrictHostKeyChecking yes',
                  '    UserKnownHostsFile "' + (folder / 'known_hosts').as_posix() + '"']
        lines += ['    IdentityFile "' + Path(key).as_posix() + '"' for key in target['keys']]
        lines += ['    ProxyCommand ' + command + ' %h %p', '']
    policy = routing.Policy.load(state)
    hosts = set(policy.data['aisc_hosts'] + policy.data['school_hosts'] + policy.data['proxy_domains'])
    patterns = {pattern for host in hosts for pattern in (host, '*.' + host)}
    patterns.update(str(network.network_address) for group in policy.networks.values()
                    for network in group if network.prefixlen == 32)
    if patterns:
        # The final pass sees HostName values from the user's existing aliases.
        # Explicit user ProxyCommand options still take precedence for those aliases.
        lines += ['Match final host ' + ','.join(sorted(patterns)),
                  '    ProxyCommand ' + command + ' %h %p', '']
    # Includes share parser context with their parent file.
    lines += ['Host *', '']
    destination = folder / 'config'
    destination.write_text('\n'.join(lines), encoding='utf-8')
    if os.name != 'nt': destination.chmod(0o600)
    return destination


def install_include(config, user_config):
    """Only the explicit imported aliases are affected; retain an exact backup."""
    user_config.parent.mkdir(parents=True, exist_ok=True)
    content = user_config.read_bytes() if user_config.exists() else b''
    include = ('Include "' + config.as_posix() + '"').encode('utf-8')
    if include in content.splitlines(): return None
    backup = None
    if user_config.exists():
        backup = user_config.with_name('config.before-must-router-' + uuid.uuid4().hex[:8])
        backup.write_bytes(content)
        if os.name != 'nt': backup.chmod(0o600)
    user_config.write_bytes(include + b'\n' + content.removeprefix(b'\xef\xbb\xbf'))
    if os.name != 'nt': user_config.chmod(0o600)
    return backup
