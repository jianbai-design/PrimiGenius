import io
import os
import sys
import threading
import time
import unittest
from unittest import mock


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app as backend_app
import podman_manager


class _SlowStdout:
    def __init__(self, delay):
        self.delay = delay
        self.calls = 0

    def readline(self):
        self.calls += 1
        if self.calls == 1:
            return b'Copying blob sha256:test\n'
        if self.calls == 2:
            time.sleep(self.delay)
        return b''


class _FakeProcess:
    def __init__(self, delay=0):
        self.started = time.monotonic()
        self.delay = delay
        self.stdout = _SlowStdout(delay) if delay else io.BytesIO(b'Copying blob sha256:test\n')
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        return 0 if (time.monotonic() - self.started) >= self.delay else None

    def wait(self):
        return -9 if self.killed else 0

    def kill(self):
        self.killed = True


class PullStreamTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(podman_manager.PodmanManager)

    def test_pull_enables_retries_and_accepts_quiet_large_layer(self):
        fake = _FakeProcess(delay=0.12)
        captured = {}

        def fake_popen(command, **kwargs):
            captured['command'] = command
            return fake

        with mock.patch.object(podman_manager.subprocess, 'Popen', side_effect=fake_popen):
            lines = list(self.manager.pull_image_stream_wsl(
                'example/image:latest',
                first_output_timeout=1,
                resolve_timeout=1,
                idle_timeout=0,
                total_timeout=1,
            ))

        self.assertFalse(any(line.startswith('ERROR:') for line in lines), lines)
        shell_command = captured['command'][-1]
        self.assertIn('--retry=6', shell_command)
        self.assertIn('--retry-delay=5s', shell_command)
        self.assertFalse(fake.killed)

    def test_explicit_idle_timeout_remains_available_for_diagnostics(self):
        fake = _FakeProcess(delay=1.2)
        with mock.patch.object(podman_manager.subprocess, 'Popen', return_value=fake):
            lines = list(self.manager.pull_image_stream_wsl(
                'example/image:latest',
                first_output_timeout=1,
                resolve_timeout=1,
                idle_timeout=0.04,
                total_timeout=1,
            ))
        self.assertTrue(any('idle timeout' in line for line in lines), lines)
        self.assertTrue(fake.killed)

    def test_direct_pull_can_bypass_configured_registry_mirrors(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured['command'] = command
            return _FakeProcess()

        with mock.patch.object(podman_manager.subprocess, 'Popen', side_effect=fake_popen):
            lines = list(self.manager.pull_image_stream_wsl(
                'docker.io/library/demo:latest',
                bypass_registry_mirrors=True,
            ))

        self.assertFalse(any(line.startswith('ERROR:') for line in lines), lines)
        self.assertIn('CONTAINERS_REGISTRIES_CONF=/dev/null podman pull', captured['command'][-1])

    def test_priority_pull_can_serialize_layer_downloads(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured['command'] = command
            return _FakeProcess()

        with mock.patch.object(podman_manager.subprocess, 'Popen', side_effect=fake_popen):
            lines = list(self.manager.pull_image_stream_wsl(
                'docker.io/rocker/r-ver:4.6.1',
                serialize_layers=True,
            ))

        self.assertFalse(any(line.startswith('ERROR:') for line in lines), lines)
        self.assertIn('CONTAINERS_CONF_OVERRIDE=', captured['command'][-1])
        self.assertIn('image_parallel_copies=1', captured['command'][-1])


class PullSchedulingTests(unittest.TestCase):
    def setUp(self):
        backend_app._REGISTRY_PROBE_CACHE.clear()
        backend_app._PULL_SOURCE_STATS.clear()

    def test_host_style_accelerator_does_not_add_invalid_docker_io_path(self):
        with mock.patch.object(backend_app, '_hidden_docker_accel_prefixes', return_value=['proxy.example']):
            candidates = backend_app._hidden_docker_accel_candidates('rocker/r-ver:4.6.1')

        self.assertEqual(['proxy.example/rocker/r-ver:4.6.1'], candidates)

    def test_tls_unhealthy_registry_is_ranked_after_healthy_registry(self):
        candidates = [
            'bad.example/library/demo:latest',
            'good.example/library/demo:latest',
            'demo:latest',
            'docker.io/library/demo:latest',
        ]
        latencies = {
            'bad.example': None,
            'good.example': 25,
            'registry-1.docker.io': 80,
        }
        with mock.patch.object(backend_app, '_effective_network_mode', return_value='cn'), \
             mock.patch.object(backend_app, '_configured_pull_mirror_hosts', return_value={'bad.example', 'good.example'}), \
             mock.patch.object(backend_app, '_load_custom_mirrors', return_value=[]), \
             mock.patch.object(backend_app, '_probe_registry_latency', side_effect=lambda host, timeout_sec=2.5: latencies.get(host)):
            ordered = backend_app._order_pull_candidates('demo:latest', candidates)

        self.assertLess(ordered.index('good.example/library/demo:latest'), ordered.index('bad.example/library/demo:latest'))
        direct_refs = [ref for ref in ordered if backend_app._pull_registry_host(ref) == 'registry-1.docker.io']
        self.assertEqual(1, len(direct_refs), ordered)

    def test_sources_fall_back_serially(self):
        attempted = []

        def pull_once(_client, ref, **_kwargs):
            attempted.append(ref)
            if ref.startswith('bad.example/'):
                raise RuntimeError('TLS failed')

        with mock.patch.object(backend_app, 'get_docker_client', return_value=object()), \
             mock.patch.object(backend_app, '_build_pull_candidates', return_value=['bad.example/demo:latest', 'good.example/demo:latest']), \
             mock.patch.object(backend_app, '_order_pull_candidates', side_effect=lambda _image, refs: refs), \
             mock.patch.object(backend_app, '_pull_single_ref_with_progress', side_effect=pull_once), \
             mock.patch.object(backend_app, '_retag_to_original_if_needed'), \
             mock.patch.object(backend_app, '_ensure_image_exists_after_pull'), \
             mock.patch.object(backend_app, '_note_pull_candidate_result'), \
             mock.patch.object(backend_app, 'log'):
            backend_app._pull_image_with_progress_locked(object(), 'demo:latest')

        self.assertEqual(['bad.example/demo:latest', 'good.example/demo:latest'], attempted)

    def test_pinned_r_uses_release_archive_after_registry_sources_fail(self):
        with mock.patch.object(backend_app, 'get_docker_client', return_value=object()), \
             mock.patch.object(backend_app, '_build_pull_candidates', return_value=['bad.example/r:4.6.1']), \
             mock.patch.object(backend_app, '_order_pull_candidates', side_effect=lambda _image, refs: refs), \
             mock.patch.object(backend_app, '_pull_single_ref_with_progress', side_effect=RuntimeError('TLS failed')), \
             mock.patch.object(backend_app, '_note_pull_candidate_result'), \
             mock.patch.object(backend_app, '_install_pinned_r_image_archive') as archive_install, \
             mock.patch.object(backend_app, 'log'):
            backend_app._pull_image_with_progress_locked(object(), backend_app.R_DOCKER_IMAGE)

        archive_install.assert_called_once()

    def test_process_lock_prevents_competing_pulls(self):
        state_lock = threading.Lock()
        active = 0
        maximum = 0

        def fake_locked(_client, _image):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1

        with mock.patch.object(backend_app, '_pull_image_with_progress_locked', side_effect=fake_locked):
            threads = [
                threading.Thread(target=backend_app.pull_image_with_progress, args=(object(), f'demo:{i}'))
                for i in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(1, maximum)


class PullEndpointTests(unittest.TestCase):
    def test_archive_only_r_pull_skips_registry_sources(self):
        client = object()
        with backend_app.app.test_request_context(
            '/podman/images/pull',
            method='POST',
            json={'image': backend_app.R_DOCKER_IMAGE, 'archive_only': True},
        ), mock.patch.object(backend_app, 'get_docker_client', return_value=client), \
             mock.patch.object(backend_app, '_install_pinned_r_image_archive') as install_archive, \
             mock.patch.object(backend_app, 'pull_image_with_progress') as pull_registry, \
             mock.patch.object(backend_app, '_image_exists_locally', return_value=True), \
             mock.patch.object(backend_app, '_refresh_images_cache'):
            response = backend_app.podman_pull_image()
            chunks = []
            for chunk in response.response:
                chunks.append(chunk)
                if '"type": "done"' in chunk:
                    break
        install_archive.assert_called_once_with(client)
        pull_registry.assert_not_called()
        self.assertTrue(any('"type": "done"' in chunk for chunk in chunks))

    def test_archive_only_rejects_non_r_image(self):
        with backend_app.app.test_request_context(
            '/podman/images/pull',
            method='POST',
            json={'image': 'ubuntu:22.04', 'archive_only': True},
        ):
            response, status = backend_app.podman_pull_image()
        self.assertEqual(400, status)
        self.assertEqual('error', response.get_json()['status'])

    def test_stream_emits_keepalive_while_pull_is_quiet(self):
        release = threading.Event()

        class _ImmediateCondition:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def wait(self, timeout=None):
                time.sleep(0.001)
                return None

            def notify_all(self):
                return None

        def quiet_pull(_client, _image):
            release.wait(timeout=1)

        clock_value = 0

        def fast_clock():
            nonlocal clock_value
            clock_value += 11
            return clock_value

        try:
            with backend_app.app.test_request_context(
                '/podman/images/pull',
                method='POST',
                json={'image': 'demo:latest'},
            ), mock.patch.object(backend_app, 'get_docker_client', return_value=object()), \
                 mock.patch.object(backend_app, 'pull_image_with_progress', side_effect=quiet_pull), \
                 mock.patch.object(backend_app, '_image_exists_locally', return_value=True), \
                 mock.patch.object(backend_app, '_refresh_images_cache'), \
                 mock.patch.object(backend_app, '_LOG_EVENT_COND', _ImmediateCondition()), \
                 mock.patch.object(backend_app.time, 'monotonic', side_effect=fast_clock):
                response = backend_app.podman_pull_image()
                stream = iter(response.response)
                first_chunk = next(stream)
                release.set()
                completion_chunks = []
                for _ in range(100):
                    chunk = next(stream)
                    completion_chunks.append(chunk)
                    if '"type": "done"' in chunk:
                        break
            self.assertEqual(': keepalive\n\n', first_chunk)
            self.assertTrue(any('"type": "done"' in chunk for chunk in completion_chunks))
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
