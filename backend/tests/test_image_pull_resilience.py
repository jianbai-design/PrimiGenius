import io
import os
import sys
import tempfile
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
        self.manager.wsl_distro_name = podman_manager.WSL_DISTRO_NAME

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

    def test_retired_presets_do_not_hide_new_builtin_fallbacks(self):
        self.assertIs(backend_app.DEFAULT_DOCKERHUB_MIRRORS, podman_manager.DEFAULT_DOCKERHUB_MIRRORS)
        self.assertEqual(len(backend_app.DEFAULT_DOCKERHUB_MIRRORS), len(set(backend_app.DEFAULT_DOCKERHUB_MIRRORS)))
        self.assertFalse(set(backend_app.DEFAULT_DOCKERHUB_MIRRORS) & podman_manager.RETIRED_DOCKERHUB_MIRRORS)
        with tempfile.TemporaryDirectory(prefix='primigenius-mirror-test-') as directory:
            manager = object.__new__(podman_manager.PodmanManager)
            manager.podman_config_dir = directory
            os.mkdir(os.path.join(directory, 'containers'))
            config_path = os.path.join(directory, 'containers', 'containers.conf')
            config = '[engine]\nregistry-mirrors = [\n' + ''.join(
                f'  "https://{host}/",\n' for host in sorted(podman_manager.RETIRED_DOCKERHUB_MIRRORS)
            ) + ']\n'
            with open(config_path, 'w', encoding='utf-8') as handle:
                handle.write(config)
            self.assertEqual([], manager._get_configured_mirrors())
            self.assertEqual(backend_app.DEFAULT_DOCKERHUB_MIRRORS, manager._get_configured_mirrors(include_defaults=True))
            with mock.patch.object(backend_app, '_load_custom_mirrors', return_value=[]), \
                 mock.patch.object(backend_app, '_get_podman_manager', return_value=manager), \
                 mock.patch.object(backend_app, '_effective_network_mode', return_value='cn'), \
                 mock.patch.dict(os.environ, {'PRIMIGENIUS_DOCKER_MIRRORS': ''}):
                candidates = backend_app._build_pull_candidates('alpine:latest')
            for host in ['dockerproxy.net', 'docker.sparkcr.cn', 'docker-pull.ygxz.in']:
                self.assertIn(host + '/library/alpine:latest', candidates)
            self.assertFalse(any(backend_app._pull_registry_host(ref) in podman_manager.RETIRED_DOCKERHUB_MIRRORS for ref in candidates))
            with open(config_path, encoding='utf-8') as handle:
                self.assertEqual(config, handle.read())

    def test_explicit_custom_mirrors_are_not_deleted_by_preset_cleanup(self):
        with mock.patch.object(backend_app, '_load_custom_mirrors', return_value=['dockerproxy.com']), \
             mock.patch.object(backend_app, '_effective_network_mode', return_value='cn'):
            mirrors = backend_app._get_pull_mirrors()
        self.assertEqual('dockerproxy.com', mirrors[0])
        self.assertIn('dockerproxy.net', mirrors)

    def test_domestic_mode_adds_builtin_fallbacks_to_saved_mirrors(self):
        cases = [
            (['https://custom.example/'], '', ['saved.example'], 'custom.example'),
            ([], 'https://env.example/', ['saved.example'], 'env.example'),
            ([], '', ['https://saved.example/'], 'saved.example'),
        ]
        for custom, environment, configured, preferred in cases:
            manager = mock.Mock()
            manager._get_configured_mirrors.return_value = configured
            with self.subTest(preferred=preferred), \
                 mock.patch.object(backend_app, '_load_custom_mirrors', return_value=custom), \
                 mock.patch.object(backend_app, '_get_podman_manager', return_value=manager), \
                 mock.patch.dict(os.environ, {'PRIMIGENIUS_DOCKER_MIRRORS': environment}), \
                 mock.patch.object(backend_app, '_effective_network_mode', return_value='cn') as mode:
                candidates = backend_app._build_pull_candidates('alpine:latest')
                self.assertIn(preferred + '/library/alpine:latest', candidates)
                for mirror in backend_app.DEFAULT_DOCKERHUB_MIRRORS:
                    self.assertIn(mirror + '/library/alpine:latest', candidates)
                self.assertEqual(len(candidates), len(set(candidates)))
                mode.return_value = 'auto'
                self.assertEqual([preferred], backend_app._get_pull_mirrors())

    def test_host_style_accelerator_does_not_add_invalid_docker_io_path(self):
        with mock.patch.object(backend_app, '_hidden_docker_accel_prefixes', return_value=['proxy.example']):
            candidates = backend_app._hidden_docker_accel_candidates('rocker/r-ver:4.6.1')

        self.assertEqual(['proxy.example/rocker/r-ver:4.6.1'], candidates)

    def test_current_accelerator_uses_dockerhub_paths_and_preserves_digest(self):
        self.assertEqual('eo.gh-proxy.cn', backend_app.HIDDEN_DOCKER_ACCEL_PREFIXES[0])
        self.assertIn('gh-proxy.com/docker', backend_app.HIDDEN_DOCKER_ACCEL_PREFIXES)
        digest = 'sha256:' + ('a' * 64)
        cases = [
            ('alpine:3.20', ['eo.gh-proxy.cn/library/alpine:3.20',
                             'gh-proxy.org/docker/docker.io/library/alpine:3.20']),
            ('docker.io/rocker/r-ver@' + digest,
             ['eo.gh-proxy.cn/rocker/r-ver@' + digest,
              'gh-proxy.org/docker/docker.io/rocker/r-ver@' + digest]),
            ('quay.io/biocontainers/seqkit:latest',
             ['gh-proxy.org/docker/quay.io/biocontainers/seqkit:latest']),
        ]
        with mock.patch.object(backend_app, '_effective_network_mode', return_value='cn'), \
             mock.patch.object(backend_app, '_hidden_docker_accel_prefixes',
                               return_value=['eo.gh-proxy.cn', 'gh-proxy.org/docker']), \
             mock.patch.dict(os.environ, {'PRIMIGENIUS_DISABLE_HIDDEN_DOCKER_ACCEL': '0'}):
            for image_ref, expected in cases:
                with self.subTest(image_ref=image_ref):
                    self.assertEqual(expected, backend_app._hidden_docker_accel_candidates(image_ref))

    def test_gh_proxy_com_covers_public_registries_and_preserves_digest(self):
        digest = 'sha256:' + ('b' * 64)
        with mock.patch.object(backend_app, '_effective_network_mode', return_value='cn'), \
             mock.patch.object(backend_app, '_hidden_docker_accel_prefixes', return_value=['gh-proxy.com/docker']), \
             mock.patch.dict(os.environ, {'PRIMIGENIUS_DISABLE_HIDDEN_DOCKER_ACCEL': '0'}):
            for image, expected in [
                ('alpine:3.20', 'docker.io/library/alpine:3.20'),
                ('rocker/r-ver@' + digest, 'docker.io/rocker/r-ver@' + digest),
                ('quay.io/biocontainers/seqkit:latest', 'quay.io/biocontainers/seqkit:latest'),
                ('ghcr.io/owner/image:latest', 'ghcr.io/owner/image:latest'),
                ('mcr.microsoft.com/powershell:latest', 'mcr.microsoft.com/powershell:latest'),
            ]:
                with self.subTest(image=image):
                    self.assertEqual(['gh-proxy.com/docker/' + expected], backend_app._hidden_docker_accel_candidates(image))
            self.assertEqual([], backend_app._hidden_docker_accel_candidates('private.example/team/image:latest'))
            self.assertEqual([], backend_app._hidden_docker_accel_candidates('gh-proxy.com/docker/docker.io/library/alpine:3.20'))

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
             mock.patch.object(backend_app, '_offline_r_environment') as environment, \
             mock.patch.object(backend_app, 'pull_image_with_progress') as pull_registry, \
             mock.patch.object(backend_app, '_image_exists_locally', return_value=True), \
             mock.patch.object(backend_app, '_refresh_images_cache'):
            response = backend_app.podman_pull_image()
            chunks = []
            for chunk in response.response:
                chunks.append(chunk)
                if '"type": "done"' in chunk:
                    break
        environment.return_value.ensure.assert_called_once()
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
