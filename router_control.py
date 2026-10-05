"""Lifecycle owned by the unified local Router window, independent of legacy VM."""
import threading

import app
import routing


class Controller:
    def __init__(self, state, runner):
        self.state, self.runner = state, runner
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.restart_requested = False
        self.error = ''
        self.active_policy = None

    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def status(self):
        with self.lock:
            active = self.active()
            ready = routing.server_endpoint(self.state) is not None
            stopping = active and self.stop_event.is_set()
            phase = ('stopping' if stopping else 'ready' if ready else 'starting' if active
                     else 'error' if self.error else 'stopped')
            return {'phase':phase, 'ready':ready and not stopping, 'managed':active,
                    'error':self.error, 'vm_running':app.vm_running(),
                    'restart_required':active and self.active_policy != routing.Policy.load(self.state).data}

    def start(self):
        with self.lock:
            if self.active(): return self.status()
            if routing.server_endpoint(self.state) is not None:
                raise ValueError('Router 已在终端运行，请先在该终端按 Ctrl+C 停止，再从此窗口启动。')
            if not (self.state / 'config.json').exists():
                raise ValueError('尚未配置 Router 虚拟机，请先按使用说明完成初始化。')
            self.error = ''; self.restart_requested = False
            self.stop_event = threading.Event()
            self.active_policy = routing.Policy.load(self.state).data
            self.thread = threading.Thread(target=self._run, name='router-vpn', daemon=False)
            self.thread.start()
            return self.status()

    def stop(self, restart=False):
        with self.lock:
            if not self.active():
                if restart: return self.start()
                return self.status()
            self.restart_requested = restart
            self.stop_event.set()
            return self.status()

    def _run(self):
        while True:
            failure = ''
            try: self.runner(self.stop_event)
            except app.ServiceStopped: pass
            except Exception as error: failure = str(error) or type(error).__name__
            with self.lock:
                self.error = failure
                if self.restart_requested and not failure:
                    self.restart_requested = False
                    self.stop_event = threading.Event()
                    self.active_policy = routing.Policy.load(self.state).data
                    continue
                self.thread = None
                return
