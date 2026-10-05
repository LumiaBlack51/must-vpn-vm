"""Single local control window: VPN lifecycle and routing settings."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import subprocess
import threading
import time
import webbrowser

import app
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


def make_server(state, html, save_policy, controller=None):
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
            elif self.path == prefix + 'api/status' and controller is not None:
                self.response(200, controller.status())
            else: self.response(404, {'error':'页面不存在。'})

        def do_POST(self):
            if not self.allowed(write=True): return
            if self.path == prefix + 'api/close':
                if controller is not None and controller.active():
                    self.response(409, {'error':'VPN 正在运行，请先停止 VPN，或直接关闭窗口让 VPN 在后台继续运行。'}); return
                self.response(200, {'ok':True}); self.server.stopping.set(); return
            if self.path in (prefix + 'api/start', prefix + 'api/stop', prefix + 'api/restart') and controller is not None:
                try:
                    # Only fixed actions are accepted. No shell commands or paths
                    # can be supplied by the control page.
                    result = controller.start() if self.path.endswith('/start') else controller.stop(self.path.endswith('/restart'))
                    self.response(200, result)
                except (ValueError, OSError) as error: self.response(400, {'error':str(error)})
                self.close_connection = True
                return
            if self.path != prefix + 'api/settings':
                self.response(404, {'error':'页面不存在。'}); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768: raise ValueError('设置内容过大或为空。')
                fields = json.loads(self.rfile.read(length))
                with self.server.save_lock:
                    policy = form_policy(routing.Policy.load(state), fields)
                    save_policy(policy)
                if controller and controller.active():
                    message = ('已保存。点击上方“重启并应用”使运行中的 VPN 使用新设置。'
                               if controller.status()['restart_required'] else '已保存，运行中的 VPN 已使用此设置。')
                else: message = '已保存，下次启动 VPN 时使用此设置。'
                self.response(200, {'ok':True, 'policy':policy.data, 'message':message})
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


def open_window(url, state):
    browser = app.browser_path()
    if browser:
        subprocess.Popen([browser, f'--app={url}', f'--user-data-dir={state / "control-browser-profile"}',
            '--window-size=1000,940', '--no-first-run', '--disable-extensions'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else: webbrowser.open(url)


def run(state, html, save_policy, open_browser=True, controller=None):
    with open(state / 'ui.lock', 'a+b') as lease:
        try: routing._lock(lease, True)
        except OSError:
            for _ in range(30):
                try:
                    url = json.loads((state / 'ui.json').read_text(encoding='utf-8'))['url']
                    if not url.startswith('http://127.0.0.1:'): raise ValueError('Invalid control window address.')
                    if open_browser: open_window(url, state)
                    print('MUST VPN Router：' + url, flush=True)
                    return 0
                except (OSError, KeyError, json.JSONDecodeError): time.sleep(0.1)
            raise ValueError('Router 主界面正在启动，请稍后重试。') from None
        try:
            with make_server(state, html, save_policy, controller) as server:
                app.save(state / 'ui.json', {'url':server.url})
                print('MUST VPN Router：' + server.url, flush=True)
                if open_browser: open_window(server.url, state)
                while not server.stopping.is_set():
                    if time.monotonic() - server.last_activity >= 120 and not (controller and controller.active()): break
                    server.handle_request()
        finally:
            if controller and controller.active():
                controller.stop()
                # Keep the owner alive until guest shutdown completes.
                while controller.active(): time.sleep(0.2)
            (state / 'ui.json').unlink(missing_ok=True)
    return 0
