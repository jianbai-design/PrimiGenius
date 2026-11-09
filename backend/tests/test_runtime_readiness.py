"""Exercise slow readiness responses without starting or stopping a real engine."""
import http.server
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from embedded_runtime import EmbeddedRuntime
from podman_manager import PodmanManager
import podman_manager


class RuntimeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.delay = 0.7
        self.engine = 'primigenius-libpod'
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(test.delay)
                body = json.dumps({'engine': test.engine, 'version': 'test'}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        manager = types.SimpleNamespace(app_install_dir=str(Path(__file__).resolve().parents[2]), runtime_channel='test')
        self.runtime = EmbeddedRuntime(manager)

        def client(timeout):
            session = requests.Session()
            session.trust_env = False
            session.base_url = 'http://127.0.0.1:' + str(self.server.server_port)
            return session

        self.runtime.api_client = client

    def tearDown(self):
        self.runtime.close_client()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_slow_healthy_engine_recovers_after_an_explicit_short_probe(self):
        self.assertFalse(self.runtime.ping(timeout=0.05))
        self.assertIn('timed out', self.runtime.last_error)
        self.assertTrue(self.runtime.ping(), self.runtime.last_error)
        self.assertEqual(self.runtime.last_error, '')

    def test_manager_does_not_mark_a_slow_healthy_engine_degraded(self):
        manager = object.__new__(PodmanManager)
        manager.runtime = self.runtime
        manager._service_lock = threading.RLock()
        manager._engine_ready_cache = {'value': False, 'time': 0}
        manager._wsl_service_started = False
        manager._connection_state = 'starting'
        with patch.dict(os.environ):
            self.assertTrue(manager.is_engine_ready(), self.runtime.last_error)
        self.assertEqual(manager._connection_state, 'ready')

    def test_start_reuses_a_slow_healthy_engine_without_restarting_it(self):
        self.runtime.stop_bridge = Mock()
        self.runtime.validate = Mock(side_effect=AssertionError('Healthy engine must be reused'))
        self.assertTrue(self.runtime.start(), self.runtime.last_error)
        self.runtime.stop_bridge.assert_not_called()
        self.runtime.validate.assert_not_called()

    def test_new_bridge_waits_for_slow_readiness_within_the_startup_deadline(self):
        original_ping = self.runtime.ping
        first = True

        def readiness(**kwargs):
            nonlocal first
            if first:
                first = False
                return False
            return original_ping(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            manager = self.runtime.manager
            manager.podman_data_dir = directory
            manager.wsl_distro_name = 'test-distro'
            manager.host_path_to_wsl = lambda value: value
            manager._run_wsl = Mock(return_value=types.SimpleNamespace(returncode=0, stdout='podman version test'))
            process = Mock()
            process.poll.return_value = None
            with patch.object(self.runtime, 'ping', side_effect=readiness), \
                    patch.object(self.runtime, 'validate', return_value={'podman_version': 'test'}), \
                    patch('embedded_runtime.subprocess.Popen', return_value=process):
                begin = time.monotonic()
                self.assertTrue(self.runtime.start(timeout=1.5), self.runtime.last_error)
                self.assertLess(time.monotonic() - begin, 2)
            process.terminate.assert_not_called()

    def test_wrong_engine_is_still_rejected(self):
        self.delay = 0
        self.engine = 'unrelated-service'
        self.assertFalse(self.runtime.ping())
        self.assertIn('not the embedded engine', self.runtime.last_error)
        self.assertIsNone(self.runtime.client)

    def test_failed_response_is_closed(self):
        response = Mock()
        response.raise_for_status.side_effect = ValueError('bad response')
        client = Mock()
        client.base_url = 'http://test'
        client.get.return_value = response
        self.runtime.api_client = Mock(return_value=client)
        self.assertFalse(self.runtime.ping())
        response.close.assert_called_once()
        client.close.assert_called_once()


class ConnectionConcurrencyTests(unittest.TestCase):
    def manager(self, ready=False):
        manager = object.__new__(PodmanManager)
        manager._service_lock = threading.RLock()
        manager._api_client_lock = threading.RLock()
        manager._api_client = None
        manager._connection_state = 'ready' if ready else 'starting'
        manager._engine_ready_cache = {'value': ready, 'time': 0}
        manager._service_retry_after = 0
        manager._service_failures = 0
        manager._wsl_service_started = ready
        manager.runtime = Mock()
        manager.runtime.url = 'npipe:////./pipe/test'
        manager.on_state_change = Mock()
        return manager

    def test_status_reads_do_not_wait_for_startup_or_duplicate_a_probe(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                manager = self.manager(ready)
                entered, release = threading.Event(), threading.Event()
                def owner():
                    with manager._service_lock:
                        entered.set()
                        release.wait(3)
                thread = threading.Thread(target=owner)
                thread.start()
                self.assertTrue(entered.wait(2))
                try:
                    start = time.monotonic()
                    for _ in range(100):
                        self.assertEqual(manager.is_engine_ready(), ready)
                    self.assertLess(time.monotonic() - start, .2)
                    manager.runtime.ping.assert_not_called()
                finally:
                    release.set()
                    thread.join(2)

    def test_ready_is_published_once_after_cache_is_updated(self):
        manager = self.manager()
        seen = []
        manager.on_state_change = lambda state: seen.append(manager._engine_ready_cache['value'])
        with patch.dict(os.environ):
            manager._mark_service_ready()
            for _ in range(20):
                self.assertTrue(manager.is_engine_ready())
        self.assertEqual(seen, [True])
        manager.runtime.ping.assert_not_called()

    def test_failure_backoff_suppresses_probes_and_duplicate_starts(self):
        manager = self.manager()
        manager._mark_service_failed()
        for _ in range(20):
            self.assertFalse(manager.is_engine_ready())
            self.assertFalse(manager._start_wsl_podman_service_locked())
        manager.runtime.ping.assert_not_called()
        manager.runtime.start.assert_not_called()

    def test_recovery_reuses_engine_and_does_not_restart_wsl(self):
        manager = self.manager()
        manager.runtime.ping.return_value = True
        with patch.dict(os.environ):
            self.assertTrue(manager._start_wsl_podman_service_locked())
        self.assertEqual(manager._connection_state, 'ready')
        manager.runtime.start.assert_not_called()

    def test_concurrent_healthy_probes_share_one_request(self):
        manager = self.manager(True)
        entered, release = threading.Event(), threading.Event()
        def ping(**kwargs):
            entered.set()
            release.wait(3)
            return True
        manager.runtime.ping.side_effect = ping
        thread = threading.Thread(target=manager.is_engine_ready)
        with patch.dict(os.environ):
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                for _ in range(100):
                    self.assertTrue(manager.is_engine_ready())
            finally:
                release.set()
                thread.join(2)
        manager.runtime.ping.assert_called_once()


class WslStartupTimingTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(PodmanManager)
        self.manager._service_lock = threading.RLock()
        self.manager._wsl_distro_cache = {'value': None, 'time': 0}
        self.manager._engine_ready_cache = {'value': False, 'time': 0}
        self.manager._connection_state = 'starting'
        self.manager.wsl_distro_name = 'test-existing'
        self.manager.runtime = types.SimpleNamespace(last_error='')
        self.manager._is_wsl_distro_registered = Mock(return_value=True)
        self.manager.is_podman_available = Mock(return_value=True)
        self.manager._prepare_wsl_registration = Mock(return_value=True)
        self.manager._start_wsl_podman_service = Mock(return_value=True)
        self.manager.machine_init = Mock()
        self.ok = types.SimpleNamespace(returncode=0, stdout='DISTRO_OK', stderr='')

    def test_slow_start_continues_to_engine_start_without_failure_backoff(self):
        clock = [100.0]
        def run(args, timeout, **kwargs):
            clock[0] += timeout
            if clock[0] < 113:
                raise subprocess.TimeoutExpired(args, timeout)
            return self.ok
        self.manager._run_host_command = Mock(side_effect=run)
        self.manager.is_engine_ready = Mock(return_value=False)
        with patch('podman_manager.time.monotonic', side_effect=lambda: clock[0]), \
                self.assertLogs('podman_manager', level='INFO') as logs:
            self.assertTrue(self.manager._auto_setup_impl()[0])
        self.assertEqual(self.manager._run_host_command.call_count, 3)
        self.manager._start_wsl_podman_service.assert_called_once()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, 'INFO')
        self.assertIn('continuing checks', logs.output[0])
        self.assertEqual(self.manager._wsl_distro_error, '')

    def test_unresponsive_wsl_obeys_total_deadline(self):
        clock, budgets = [100.0], []
        def run(args, timeout, **kwargs):
            budgets.append(timeout)
            clock[0] += timeout
            raise subprocess.TimeoutExpired(args, timeout)
        self.manager._run_host_command = Mock(side_effect=run)
        with patch('podman_manager.time.monotonic', side_effect=lambda: clock[0]), \
                self.assertLogs('podman_manager', level='INFO') as logs:
            self.assertFalse(self.manager._is_wsl_distro_functional())
        self.assertEqual(sum(budgets), 45)
        self.assertEqual(budgets, [6] * 7 + [3])
        self.assertIn('within 45 seconds', self.manager._wsl_distro_error)
        self.assertEqual(len(logs.records), 1)
        self.manager._start_wsl_podman_service.assert_not_called()
        self.manager.machine_init.assert_not_called()

    def test_short_recovery_probe_does_not_inherit_full_startup_budget(self):
        clock = [100.0]
        def run(args, timeout, **kwargs):
            clock[0] += timeout
            raise subprocess.TimeoutExpired(args, timeout)
        self.manager._run_host_command = Mock(side_effect=run)
        with patch('podman_manager.time.monotonic', side_effect=lambda: clock[0]):
            self.assertFalse(self.manager._is_wsl_distro_functional(timeout=6))
        self.assertEqual(clock[0], 106)
        self.manager._run_host_command.assert_called_once()

    def test_missing_disk_fails_immediately_without_cold_start_retries(self):
        self.manager._run_host_command = Mock(return_value=types.SimpleNamespace(returncode=1,
            stdout='', stderr='Wsl/Service/CreateInstance/MountDisk/HCS/ERROR_PATH_NOT_FOUND: ext4.vhdx'))
        self.assertFalse(self.manager._is_wsl_distro_functional())
        self.manager._run_host_command.assert_called_once()
        self.assertTrue(self.manager._wsl_storage_missing)

    def test_concurrent_forced_checks_share_the_same_completed_probe(self):
        entered, release, second_requested = threading.Event(), threading.Event(), threading.Event()
        def run(*args, **kwargs):
            entered.set()
            release.wait(3)
            return self.ok
        self.manager._run_host_command = Mock(side_effect=run)
        monotonic = time.monotonic
        def clock():
            value = monotonic()
            if threading.current_thread().name == 'second-probe':
                second_requested.set()
            return value
        results = []
        def probe():
            results.append(self.manager._is_wsl_distro_functional(force=True))
        first = threading.Thread(target=probe)
        second = threading.Thread(target=probe, name='second-probe')
        with patch('podman_manager.time.monotonic', side_effect=clock):
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            try:
                self.assertTrue(second_requested.wait(2))
                self.assertFalse(self.manager.is_engine_ready())
            finally:
                release.set()
                first.join(3)
                second.join(3)
        self.assertEqual(results, [True, True])
        self.manager._run_host_command.assert_called_once()

    def test_timeout_runner_can_propagate_without_error_or_traceback(self):
        with patch('podman_manager.platform.system', return_value='Windows'), \
                patch('podman_manager.subprocess.run', side_effect=subprocess.TimeoutExpired(['test'], 6)), \
                patch.object(podman_manager.logger, 'error') as error, \
                patch.object(podman_manager.logger, 'warning') as warning:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.manager._run_host_command(['wsl', '--status'], timeout=6, raise_timeout=True)
            error.assert_not_called()
            warning.assert_not_called()
            self.assertIsNone(podman_manager._run_hidden(['test'], timeout=6))
            error.assert_not_called()
            warning.assert_called_once()

    def test_real_subprocess_timeout_then_ready_in_one_startup_window(self):
        calls = []
        def run(_args, timeout, **kwargs):
            script = 'import time; time.sleep(6.2); print("DISTRO_OK")' if not calls else 'print("DISTRO_OK")'
            calls.append(timeout)
            return podman_manager._run_hidden([sys.executable, '-c', script], timeout=timeout, **kwargs)
        self.manager._run_host_command = run
        started = time.monotonic()
        with self.assertLogs('podman_manager', level='INFO') as logs:
            self.assertTrue(self.manager._is_wsl_distro_functional(timeout=8))
        self.assertEqual(len(calls), 2)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(all(record.levelname == 'INFO' for record in logs.records))


if __name__ == '__main__':
    unittest.main()
