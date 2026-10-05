import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import app
import router_control
import routing


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        app.save(self.state / 'config.json', {})
        self.state_patch = patch.object(app, 'STATE', self.state)
        self.state_patch.start(); self.addCleanup(self.state_patch.stop)

    def test_one_worker_repeated_start_stop_and_restart_with_new_policy(self):
        started, restarted = threading.Event(), threading.Event()
        policies = []
        def runner(stop):
            policies.append(routing.Policy.load(self.state).data['traffic_mode'])
            (started if len(policies) == 1 else restarted).set()
            if not stop.wait(5): raise AssertionError('worker was not stopped')
            raise app.ServiceStopped()
        controller = router_control.Controller(self.state, runner)
        try:
            self.assertEqual(controller.status()['phase'], 'stopped')
            controller.start(); self.assertTrue(started.wait(2))
            thread = controller.thread
            controller.start(); self.assertIs(controller.thread, thread)
            app.save(self.state / 'routing.json', routing.Policy({'traffic_mode':'ssh'}).data)
            self.assertTrue(controller.status()['restart_required'])
            controller.stop(restart=True); self.assertTrue(restarted.wait(2))
            self.assertEqual(policies, ['all', 'ssh'])
            self.assertFalse(controller.status()['restart_required'])
            controller.stop(); thread.join(3)
            self.assertEqual(controller.status()['phase'], 'stopped')
        finally:
            controller.stop()
            if controller.thread: controller.thread.join(3)

    def test_failure_visible_and_retry_available(self):
        def fail(_): raise ValueError('adapter unavailable')
        controller = router_control.Controller(self.state, fail)
        controller.start()
        with controller.lock: thread = controller.thread
        if thread: thread.join(3)
        self.assertEqual(controller.status()['phase'], 'error')
        self.assertEqual(controller.status()['error'], 'adapter unavailable')
        self.assertFalse(controller.active())

    def test_cancel_during_boot_waits_for_guest_then_shuts_down_without_vpn_login(self):
        event = threading.Event()
        launcher = Mock(); launcher.poll.return_value = None
        with patch.object(app, 'vm_running', return_value=False), \
             patch.object(app, 'start_helper', return_value=launcher), \
             patch.object(app, 'wait_for_guest', side_effect=lambda _: event.set()), \
             patch.object(app, 'exec_guest', return_value=0) as execute, \
             patch.object(app, 'guest_check') as check, redirect_stdout(io.StringIO()):
            with self.assertRaises(app.ServiceStopped):
                with app.ready_vm('10.100.16.13', stop_event=event): self.fail('must not become ready')
        execute.assert_called_once_with('sudo systemctl poweroff --no-block')
        launcher.wait.assert_called_once_with(timeout=45)
        check.assert_not_called()

    def test_cancellation_does_not_power_off_separately_started_vm(self):
        event = threading.Event()
        with patch.object(app, 'vm_running', return_value=True), \
             patch.object(app, 'wait_for_guest', side_effect=lambda _: event.set()), \
             patch.object(app, 'exec_guest') as execute:
            with self.assertRaises(app.ServiceStopped):
                with app.ready_vm('10.100.16.13', stop_event=event): pass
        execute.assert_not_called()


if __name__ == '__main__': unittest.main()
