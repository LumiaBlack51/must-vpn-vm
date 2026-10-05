"""Windowless entry to the unified Router application."""
import ctypes
import sys

import router_app


if __name__ == '__main__':
    try:
        router_app.app.STATE = router_app.state_home()
        router_app.app.protect_state()
        log = open(router_app.app.STATE / 'router-ui.log', 'a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = log
        sys.argv = [sys.argv[0], 'ui', *sys.argv[1:]]
        sys.exit(router_app.main())
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, '无法打开 Router：' + str(error), 'MUST VPN Router', 0x10)
        sys.exit(1)
