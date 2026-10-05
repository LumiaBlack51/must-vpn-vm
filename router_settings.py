"""Loopback-only graphical settings; opening settings never starts the VM."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import subprocess
import threading
import time
import webbrowser

import routing


def form_policy(current, fields):
    allowed = {'traffic_mode', 'proxy_domains', 'fallback', 'direct_timeout', 'ssh_ports'}
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise ValueError('设置内容无效，请重新打开设置页面。')
    data = {**current.data, **fields}
    if data['traffic_mode'] not in ('ssh', 'all', 'domains'):
        raise ValueError('请选择有效的代理模式。')
    domains = data['proxy_domains']
    if not isinstance(domains, list) or len(domains) > 128:
        raise ValueError('域名最多可填写 128 个，每行一个。')
    try: data['proxy_domains'] = list(dict.fromkeys(routing.hostname(h.strip()) for h in domains if h.strip()))
    except (ValueError, AttributeError):
        raise ValueError('请输入裸域名，例如 aisc.must.edu.mo，不要包含 https://、路径或 *.。') from None
    if data['traffic_mode'] == 'domains' and not data['proxy_domains']:
        raise ValueError('指定域名模式至少需要填写一个域名。')
    try:
        timeout = float(data['direct_timeout'])
        if not 0.1 <= timeout <= 60: raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('直连等待时间应在 0.1 到 60 秒之间。') from None
    ports = data['ssh_ports']
    if not isinstance(ports, list) or not ports or not all(type(p) is int and 1 <= p <= 65535 for p in ports):
        raise ValueError('SSH 端口应为 1 到 65535 的整数，多个端口用逗号分隔。')
    if not isinstance(data['fallback'], bool): raise ValueError('请选择是否启用学校连接回退。')
    return routing.Policy(data)


def make_server(state, html, save_policy):
    token = secrets.token_urlsafe(32)
    prefix = '/' + token + '/'

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(5)

        def log_message(self, *_): pass

        def response(self, status, body, kind='application/json; charset=utf-8'):
            if not isinstance(body, bytes): body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers(); self.wfile.write(body)

        def allowed(self, write=False):
            if self.headers.get('Host') != self.server.host_header or not self.path.startswith(prefix):
                self.response(403, {'error':'此设置页面只能在本机使用。'}); return False
            if write and (self.headers.get('Origin') != self.server.origin or
                          self.headers.get('X-Router-Settings') != token or
                          self.headers.get('Content-Type') != 'application/json'):
                self.response(403, {'error':'请求未通过验证，请重新打开设置页面。'}); return False
            self.server.last_activity = time.monotonic()
            return True

        def do_GET(self):
            if not self.allowed(): return
            if self.path == prefix:
                self.response(200, html.read_bytes(), 'text/html; charset=utf-8')
            elif self.path == prefix + 'api/settings':
                try:
                    self.response(200, {'policy':routing.Policy.load(state).data,
                                        'server_ready':routing.server_endpoint(state) is not None})
                except (OSError, ValueError): self.response(500, {'error':'无法读取设置，请检查程序数据目录。'})
            elif self.path == prefix + 'api/ping': self.response(200, {'ok':True})
            else: self.response(404, {'error':'页面不存在。'})

        def do_POST(self):
            if not self.allowed(write=True): return
            if self.path == prefix + 'api/close':
                self.response(200, {'ok':True}); self.server.stopping.set(); return
            if self.path != prefix + 'api/settings':
                self.response(404, {'error':'页面不存在。'}); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768: raise ValueError('设置内容过大或为空。')
                fields = json.loads(self.rfile.read(length))
                with self.server.save_lock:
                    policy = form_policy(routing.Policy.load(state), fields)
                    save_policy(policy)
                self.response(200, {'ok':True, 'policy':policy.data,
                    'message':'已保存。请重启 MUST VPN Router，并重新打开分流浏览器，使所有设置生效。'})
            except (ValueError, TypeError, UnicodeError) as error:
                self.response(400, {'error':str(error)})
            except (OSError, subprocess.SubprocessError):
                self.response(500, {'error':'保存失败，请检查数据目录权限后重试。'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.host_header = f'127.0.0.1:{server.server_address[1]}'
    server.origin = 'http://' + server.host_header
    server.url = server.origin + prefix
    server.last_activity = time.monotonic()
    server.stopping = threading.Event()
    server.save_lock = threading.Lock()
    server.timeout = 1
    return server


def run(state, html, save_policy, open_browser=True):
    with make_server(state, html, save_policy) as server:
        print('MUST VPN Router 设置：' + server.url, flush=True)
        print('关闭页面后，设置服务将在两分钟内自动退出。', flush=True)
        if open_browser: webbrowser.open(server.url)
        while not server.stopping.is_set() and time.monotonic() - server.last_activity < 120:
            server.handle_request()
    return 0
