"""Use a temporary PAC for browsers that follow GNOME desktop proxy settings."""
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import sys

import app

SCHEMA = 'org.gnome.system.proxy'


def supported():
    return (sys.platform.startswith('linux')
            and 'GNOME' in os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
            and shutil.which('gsettings') is not None)


def setting(action, key, *values):
    try:
        return subprocess.check_output(['gsettings', action, SCHEMA, key, *values],
                                       text=True, stderr=subprocess.PIPE, timeout=5).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('无法更新 GNOME 代理设置。请检查桌面网络设置。') from error


def restore(backup):
    if not backup.exists():
        return
    previous = json.loads(backup.read_text(encoding='utf-8'))
    if setting('get', 'autoconfig-url') == previous['url']:
        mode = setting('get', 'mode')
        if mode == "'auto'":
            setting('set', 'mode', 'none')
        if mode in ("'none'", "'auto'"):
            setting('set', 'autoconfig-url', previous['previous_url'])
    backup.unlink()


@contextmanager
def use_pac(state, url, enabled):
    backup = state / 'desktop-proxy.json'
    # A prior process may have exited abruptly while it owned the PAC.
    if not enabled and not backup.exists():
        yield
        return
    if not supported():
        raise ValueError('普通浏览器接入需要 GNOME 桌面及 gsettings。')
    restore(backup)
    if not enabled:
        yield
        return
    if setting('get', 'mode') != "'none'":
        raise ValueError('GNOME 已配置系统代理。请取消“接入普通浏览器”，或停用现有系统代理后重试。')
    app.save(backup, {'url': "'" + url + "'", 'previous_url': setting('get', 'autoconfig-url')})
    try:
        setting('set', 'autoconfig-url', url)
        setting('set', 'mode', 'auto')
        yield
    finally:
        restore(backup)
