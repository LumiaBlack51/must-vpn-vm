"""Windowless Windows settings launcher, compatible with Router 0.3.0."""
import ctypes
import sys

import router_app


if __name__ == '__main__':
    try:
        sys.argv = [sys.argv[0], 'settings', *sys.argv[1:]]
        sys.exit(router_app.main())
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, '无法打开设置：' + str(error), 'MUST VPN Router 设置', 0x10)
        sys.exit(1)
