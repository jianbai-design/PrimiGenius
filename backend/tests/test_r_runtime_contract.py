import contextlib
import inspect
import json
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


class RRuntimeContractTests(unittest.TestCase):
    def test_pinned_image_system_library_packages_are_runtime_provided(self):
        expected = {
            'base', 'boot', 'class', 'cluster', 'codetools', 'compiler',
            'datasets', 'foreign', 'graphics', 'grDevices', 'grid',
            'KernSmooth', 'lattice', 'MASS', 'Matrix', 'methods', 'mgcv',
            'nlme', 'nnet', 'parallel', 'rpart', 'spatial', 'splines', 'stats',
            'stats4', 'survival', 'tcltk', 'tools', 'utils',
        }
        self.assertEqual(expected, set(backend_app._R_IMAGE_LIBRARY_PACKAGES))
        self.assertTrue(backend_app._is_r_image_package('lattice'))
        self.assertTrue(backend_app._is_r_image_package('LATTICE'))

    def test_host_dependency_check_accepts_image_packages_without_r_libs_copy(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(backend_app, 'R_LIBS_DIR', root), \
             mock.patch.object(backend_app, '_r_libs_package_set_lower', {}):
            status = backend_app._host_r_dependency_status(['lattice', 'Matrix'])
        self.assertTrue(status['ready'])
        self.assertEqual([], status['missing'])
        self.assertEqual(['Matrix', 'lattice'], status['chain'])

    def test_image_packages_are_listed_but_not_reinstalled(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(backend_app, 'R_LIBS_DIR', root), \
             mock.patch.object(backend_app, '_r_packages_cache', {'packages': None, 'time': 0}), \
             backend_app.app.test_request_context('/r/list-packages'):
            response = backend_app.r_list_packages().get_json()
        lattice = next(item for item in response['packages'] if item['package'] == 'lattice')
        self.assertTrue(lattice['builtin'])

        with backend_app.app.test_request_context(
            '/r/install-packages', method='POST', json={'packages': ['lattice', 'Matrix']}
        ), mock.patch.object(backend_app.threading, 'Thread') as thread:
            response = backend_app.r_install_packages().get_json()
        thread.assert_not_called()
        job = backend_app.INSTALL_JOBS.pop(response['job_id'])
        self.assertEqual('done', job['status'])
        self.assertEqual(0, job['exit_code'])

    def test_r_script_mount_sanitizes_unsafe_filename_prefix(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(backend_app.tempfile, 'gettempdir', return_value=root):
            host_path, _, container_path = backend_app._create_r_script_mount(
                'cat("ok")', prefix='r_preflight_edgeR|limma:*?'
            )
            script_name = os.path.basename(host_path)
            self.assertNotRegex(script_name, r'[<>:"/\\|?*]')
            self.assertTrue(script_name.startswith('r_preflight_edgeR_limma_'))
            self.assertEqual(f'/r_job_script/{script_name}', container_path)
            with open(host_path, 'r', encoding='utf-8') as handle:
                self.assertEqual('cat("ok")\n', handle.read())

    def test_runtime_is_version_and_digest_pinned(self):
        self.assertEqual('4.6.1', backend_app.R_VERSION)
        self.assertEqual('3.23', backend_app.BIOCONDUCTOR_VERSION)
        self.assertIn(':4.6.1@sha256:', backend_app.R_DOCKER_IMAGE)
        self.assertNotIn(':latest', backend_app.R_DOCKER_IMAGE)
        self.assertIn('r4.6.1-bioc3.23', backend_app.R_CUSTOM_IMAGE)

    def test_release_archive_contract_matches_exported_image(self):
        self.assertEqual(397702823, backend_app.R_IMAGE_ARCHIVE_SIZE)
        self.assertEqual(64, len(backend_app.R_IMAGE_ARCHIVE_SHA256))
        self.assertIn('/PrimiGenius-images/releases/download/r-4.6.1/', backend_app.R_IMAGE_ARCHIVE_URL)
        self.assertTrue(backend_app.R_IMAGE_ARCHIVE_NAME.endswith('.tar.gz'))

    def test_cn_archive_download_uses_plugin_accelerators_before_github(self):
        with mock.patch.object(backend_app, '_effective_network_mode', return_value='cn'):
            urls = backend_app._r_image_archive_urls()
        self.assertTrue(urls[0].startswith(backend_app.R_IMAGE_ARCHIVE_ACCELERATORS[0]))
        self.assertEqual(backend_app.R_IMAGE_ARCHIVE_URL, urls[-1])

    def test_archive_download_prefers_fastest_verified_probe(self):
        urls = ['https://slow.example/image.tar.gz', 'https://fast.example/image.tar.gz', 'https://bad.example/image.tar.gz']
        latency = {urls[0]: 300, urls[1]: 40, urls[2]: None}
        with mock.patch.object(backend_app, '_r_image_archive_urls', return_value=urls), \
             mock.patch.object(backend_app, '_probe_r_image_archive_url', side_effect=lambda url: latency[url]):
            ordered = backend_app._ordered_r_image_archive_urls()
        self.assertEqual([urls[1], urls[0], urls[2]], ordered)

    def test_archive_probe_reads_only_a_small_gzip_range(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'\x1f\x8b' + (b'x' * 1024)
        with mock.patch.object(backend_app.urllib.request, 'urlopen', return_value=response) as urlopen, \
             mock.patch.object(backend_app.time, 'perf_counter', side_effect=[1.0, 1.05]):
            latency = backend_app._probe_r_image_archive_url('https://fast.example/image.tar.gz')
        self.assertEqual(50, latency)
        request_obj = urlopen.call_args.args[0]
        self.assertEqual('bytes=0-65535', request_obj.get_header('Range'))
        response.__enter__.return_value.read.assert_called_once_with(64 * 1024)

    def test_verified_archive_import_checks_image_id_and_removes_download(self):
        manager = mock.Mock()
        manager._run_wsl.return_value = mock.Mock(returncode=0, stdout='Loaded image', stderr='')
        manager.host_path_to_wsl.return_value = '/tmp/r-image.tar'
        image = mock.Mock(id=backend_app.R_DOCKER_IMAGE_CONFIG_DIGEST)
        client = mock.Mock()
        client.images.get.return_value = image
        with tempfile.TemporaryDirectory() as root:
            archive_path = os.path.join(root, backend_app.R_IMAGE_ARCHIVE_NAME)
            with open(archive_path, 'wb') as stream:
                stream.write(b'test')
            manager.podman_data_dir = root
            with mock.patch.object(backend_app, '_get_podman_manager', return_value=manager), \
                 mock.patch.object(backend_app, '_download_r_image_archive', return_value=archive_path), \
                 mock.patch.object(backend_app, 'get_docker_client', return_value=client):
                backend_app._install_pinned_r_image_archive(client)
            self.assertFalse(os.path.exists(archive_path))
        manager._run_wsl.assert_called_once()
        client.images.get.assert_called_once_with(backend_app.R_DOCKER_IMAGE_TAGGED)
        backend_app._image_exists_cache.pop(backend_app.R_DOCKER_IMAGE, None)

    def test_custom_recipe_carries_runtime_contract_labels(self):
        recipe = backend_app._r_custom_recipe_text()
        self.assertIn(f'FROM {backend_app.R_DOCKER_IMAGE}', recipe)
        self.assertIn('org.primigenius.r-version="4.6.1"', recipe)
        self.assertIn('org.primigenius.bioconductor-version="3.23"', recipe)
        self.assertIn(backend_app.R_DOCKER_IMAGE_DIGEST, recipe)
        self.assertIn(f'org.primigenius.recipe-sha256="{backend_app.R_CUSTOM_RECIPE_SHA256}"', recipe)

    def test_custom_recipe_exposes_apt_progress_and_avoids_recommends(self):
        recipe = backend_app._r_custom_recipe_text()
        self.assertIn('ARG APT_MIRROR=', recipe)
        self.assertIn('[APT] Refreshing Ubuntu package indexes', recipe)
        self.assertIn('s#https?://', recipe)
        self.assertIn('--no-install-recommends', recipe)
        self.assertNotIn('update -qq', recipe)
        self.assertNotIn('2>/dev/null', recipe)

    def test_apt_mirrors_are_ordered_by_live_probe_with_fallbacks(self):
        latency = {'tuna': 90, 'ustc': 20, 'official': 120}

        def fake_probe(name, url, timeout=4.0):
            if name == 'aliyun':
                raise RuntimeError('offline')
            return {'name': name, 'url': url, 'latency_ms': latency[name]}

        backend_app._R_APT_MIRROR_CACHE['items'] = None
        backend_app._R_APT_MIRROR_CACHE['time'] = 0
        with mock.patch.object(backend_app, '_effective_network_mode', return_value='auto'), \
             mock.patch.object(backend_app, '_probe_r_apt_mirror', side_effect=fake_probe):
            ordered = backend_app._ordered_r_apt_mirrors(force=True)
        self.assertEqual('https://mirrors.ustc.edu.cn/ubuntu', ordered[0])
        self.assertIn('https://mirrors.aliyun.com/ubuntu', ordered)

    def test_custom_image_build_retries_with_next_apt_mirror(self):
        labels = {
            'org.primigenius.r-version': backend_app.R_VERSION,
            'org.primigenius.bioconductor-version': backend_app.BIOCONDUCTOR_VERSION,
            'org.primigenius.base-digest': backend_app.R_DOCKER_IMAGE_DIGEST,
            'org.primigenius.recipe-version': str(backend_app.R_CUSTOM_RECIPE_VERSION),
            'org.primigenius.recipe-sha256': backend_app.R_CUSTOM_RECIPE_SHA256,
        }
        built_image = mock.Mock(attrs={'Config': {'Labels': labels}})
        client = mock.Mock()
        client.images.get.side_effect = [backend_app.docker.errors.ImageNotFound('missing'), built_image]
        client.api.build.side_effect = [
            [{'error': 'first mirror failed'}],
            [{'stream': 'build complete'}],
        ]
        validation_container = mock.Mock(id='validation-container')
        validation_container.wait.return_value = {'StatusCode': 0}
        client.containers.run.return_value = validation_container
        backend_app._image_exists_cache.pop(backend_app.R_CUSTOM_IMAGE, None)
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(backend_app, 'APP_INSTALL_DIR', root), \
             mock.patch.object(backend_app, '_ensure_pinned_r_base_image', return_value=True), \
             mock.patch.object(backend_app, '_ordered_r_apt_mirrors', return_value=['https://bad.example/ubuntu', 'https://good.example/ubuntu']):
            self.assertTrue(backend_app._ensure_custom_r_image(client))
        self.assertEqual(2, client.api.build.call_count)
        self.assertEqual(
            {'APT_MIRROR': 'https://good.example/ubuntu'},
            client.api.build.call_args.kwargs['buildargs'],
        )
        self.assertTrue(client.api.build.call_args.kwargs['forcerm'])
        validation_container.remove.assert_called_once_with(force=True)
        backend_app._image_exists_cache.pop(backend_app.R_CUSTOM_IMAGE, None)

    def test_digest_is_preserved_in_mirror_pull_candidates(self):
        with mock.patch.object(backend_app, '_load_custom_mirrors', return_value=['mirror.example']), \
             mock.patch.object(backend_app, '_hidden_docker_accel_candidates', return_value=[]):
            candidates = backend_app._build_pull_candidates(backend_app.R_DOCKER_IMAGE)
        self.assertTrue(candidates)
        self.assertTrue(all(backend_app.R_DOCKER_IMAGE_DIGEST in item for item in candidates))

    def test_tag_and_digest_are_parsed_independently(self):
        name, tag, digest = backend_app._parse_image_ref(backend_app.R_DOCKER_IMAGE)
        self.assertEqual('rocker/r-ver', name)
        self.assertEqual('4.6.1', tag)
        self.assertEqual(backend_app.R_DOCKER_IMAGE_DIGEST.removeprefix('sha256:'), digest.removeprefix('sha256:'))

    def test_digest_verified_mirror_pull_gets_stable_local_tag(self):
        image = mock.Mock()
        client = mock.Mock()
        client.images.get.return_value = image
        mirror_ref = 'mirror.example/rocker/r-ver:4.6.1@' + backend_app.R_DOCKER_IMAGE_DIGEST
        manager = mock.Mock()
        with mock.patch.object(backend_app, '_get_podman_manager', return_value=manager):
            backend_app._retag_to_original_if_needed(client, mirror_ref, backend_app.R_DOCKER_IMAGE)
        image.tag.assert_called_once_with('docker.io/rocker/r-ver', tag='4.6.1')
        manager._run_wsl.assert_called_once()

    def test_unqualified_r_runtime_image_is_upgraded_to_pinned_ref(self):
        self.assertEqual(backend_app.R_DOCKER_IMAGE, backend_app._resolve_system_image_ref('rocker/r-ver'))
        self.assertEqual(backend_app.R_DOCKER_IMAGE, backend_app._resolve_system_image_ref('rocker/r-ver:latest'))
        self.assertEqual(backend_app.R_DOCKER_IMAGE, backend_app._resolve_system_image_ref('rocker/r-ver:4.6.1'))

    def test_explicit_other_r_version_is_not_rewritten(self):
        self.assertEqual('rocker/r-ver:4.5.2', backend_app._resolve_system_image_ref('rocker/r-ver:4.5.2'))

    def test_priority_search_candidate_carries_pinned_digest(self):
        candidates = backend_app._build_priority_candidates('rocker/r-ver')
        self.assertEqual(backend_app.R_DOCKER_IMAGE, candidates[0]['name'])

    def test_pinned_r_pull_uses_shorter_per_source_retry_policy(self):
        manager = mock.Mock()
        manager.pull_image_stream_wsl.return_value = ['Writing manifest to image destination']
        with mock.patch.object(backend_app, '_get_podman_manager', return_value=manager):
            backend_app._pull_single_ref_with_progress(
                mock.Mock(),
                backend_app.R_DOCKER_IMAGE,
                display_name=backend_app.R_DOCKER_IMAGE,
            )
        manager.pull_image_stream_wsl.assert_called_once_with(
            'docker.io/rocker/r-ver@' + backend_app.R_DOCKER_IMAGE_DIGEST,
            cancel_event=None,
            retry_count=2,
            total_timeout=1800,
            bypass_registry_mirrors=True,
            serialize_layers=True,
        )

    def test_pinned_r_pull_prefers_faster_verified_source(self):
        fast = 'fast.example/rocker/r-ver:4.6.1@' + backend_app.R_DOCKER_IMAGE_DIGEST
        slow = 'slow.example/rocker/r-ver:4.6.1@' + backend_app.R_DOCKER_IMAGE_DIGEST
        latency = {
            'registry-1.docker.io': 200,
            'fast.example': 20,
            'slow.example': 100,
        }
        with mock.patch.object(backend_app, '_effective_network_mode', return_value='auto'), \
             mock.patch.object(backend_app, '_configured_pull_mirror_hosts', return_value={'fast.example', 'slow.example'}), \
             mock.patch.object(backend_app, '_load_custom_mirrors', return_value=[]), \
             mock.patch.object(backend_app, '_probe_registry_latency', side_effect=lambda host, timeout_sec=2.5: latency[host]), \
             mock.patch.object(backend_app, '_PULL_SOURCE_STATS', {}):
            ordered = backend_app._order_pull_candidates(
                backend_app.R_DOCKER_IMAGE,
                [backend_app.R_DOCKER_IMAGE, fast, slow],
            )
        self.assertEqual(fast, ordered[0])

    def test_manual_image_pull_does_not_build_custom_r_image(self):
        source = inspect.getsource(backend_app.podman_pull_image)
        self.assertNotIn('_ensure_custom_r_image', source)

    def test_missing_image_aliases_use_one_wsl_process(self):
        client = mock.Mock()
        client.images.get.side_effect = backend_app.docker.errors.ImageNotFound('missing')
        manager = mock.Mock()
        manager._run_wsl.return_value = mock.Mock(returncode=1)
        with mock.patch.object(backend_app, '_get_podman_manager', return_value=manager):
            self.assertFalse(backend_app._image_exists_locally(client, backend_app.R_DOCKER_IMAGE))
        manager._run_wsl.assert_called_once()
        self.assertGreater(manager._run_wsl.call_args.args[0].count('podman image exists'), 1)

    def test_r_install_dialog_uses_direct_archive_fallback_and_refreshes(self):
        renderer_path = os.path.abspath(os.path.join(BACKEND_DIR, '..', 'src', 'renderer.js'))
        with open(renderer_path, 'r', encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('class="dlg-backup"', source)
        self.assertIn('archive_only: true', source)
        self.assertIn("await loadPackages(true)", source)
        self.assertIn('let activeRImageInstallDialog = null', source)
        self.assertIn('r-image-install-dialog-minimized', source)
        self.assertIn("const message = downloading", source)
        self.assertIn('窗口已最小化', source)
        self.assertIn('activeRImageInstallDialog.updateLanguage();', source)
        self.assertIn('install-dialog click handler survives language changes', source)

    def test_r_archive_uses_plugin_release_accelerators(self):
        main_path = os.path.abspath(os.path.join(BACKEND_DIR, '..', 'main.js'))
        with open(main_path, 'r', encoding='utf-8') as handle:
            main_source = handle.read()
        for prefix in backend_app.R_IMAGE_ARCHIVE_ACCELERATORS:
            self.assertIn(f"prefix: '{prefix}'", main_source)

    def test_first_r_use_still_prepares_custom_image(self):
        self.assertIn('_ensure_custom_r_image', inspect.getsource(backend_app._run_rscript_docker))
        self.assertIn('_ensure_custom_r_image', inspect.getsource(backend_app._run_r_install_job_locked))

    def test_accelerator_digest_alias_is_recognized_as_pinned_r_runtime(self):
        alias = 'docker.gh-proxy.cn/rocker/r-ver@' + backend_app.R_DOCKER_IMAGE_DIGEST
        self.assertTrue(backend_app._is_pinned_r_runtime_local_ref(alias))

    def test_local_r_image_name_is_canonical_and_duplicate_alias_is_hidden(self):
        alias = 'docker.gh-proxy.cn/rocker/r-ver@' + backend_app.R_DOCKER_IMAGE_DIGEST
        canonical = 'docker.io/rocker/r-ver:4.6.1'
        parsed = backend_app._parse_images([{
            'Id': 'sha256:' + ('a' * 64),
            'RepoTags': [alias, canonical],
            'Size': 970900000,
            'Created': int(time.time()),
        }])
        self.assertEqual(1, len(parsed))
        self.assertEqual('rocker/r-ver:4.6.1', parsed[0]['display_name'])
        self.assertEqual(backend_app.R_DOCKER_IMAGE_TAGGED, parsed[0]['canonical_ref'])
        self.assertTrue(parsed[0]['is_system_r_image'])

    def test_legacy_latest_image_is_not_mislabeled_as_pinned_runtime(self):
        self.assertFalse(backend_app._is_pinned_r_runtime_local_ref('rocker/r-ver:latest'))

    def test_deleting_pinned_r_base_also_removes_all_custom_r_images(self):
        custom_current = mock.Mock(
            id='sha256:custom-current',
            tags=[backend_app.R_CUSTOM_IMAGE],
            attrs={},
        )
        custom_legacy = mock.Mock(
            id='sha256:custom-legacy',
            tags=['localhost/primigenius-r-base:install-v2'],
            attrs={},
        )
        dangling_build = mock.Mock(
            id='sha256:custom-dangling',
            tags=[],
            attrs={'Config': {'Labels': {
                'org.primigenius.r-version': backend_app.R_VERSION,
                'org.primigenius.base-digest': backend_app.R_DOCKER_IMAGE_DIGEST,
            }}},
        )
        unrelated = mock.Mock(
            id='sha256:unrelated',
            tags=['quay.io/example/tool:1.0'],
            attrs={},
        )
        client = mock.Mock()
        client.images.list.return_value = [custom_current, custom_legacy, dangling_build, unrelated]
        with backend_app.app.test_request_context(
            '/podman/images/remove',
            method='POST',
            json={'image_id': 'sha256:r-base', 'repo': backend_app.R_DOCKER_IMAGE_TAGGED},
        ), mock.patch.object(backend_app, 'get_docker_client', return_value=client), \
             mock.patch.object(backend_app.threading, 'Thread'):
            response = backend_app.podman_remove_image()
        self.assertEqual('success', response.get_json()['status'])
        removed = [call.kwargs['image'] for call in client.images.remove.call_args_list]
        self.assertEqual(
            ['sha256:custom-current', 'sha256:custom-legacy', 'sha256:custom-dangling', 'sha256:r-base'],
            removed,
        )
        client.images.list.assert_called_once_with(all=True)

    def test_deleting_an_unrelated_image_does_not_scan_or_remove_custom_r_images(self):
        client = mock.Mock()
        with backend_app.app.test_request_context(
            '/podman/images/remove',
            method='POST',
            json={'image_id': 'sha256:tool', 'repo': 'quay.io/example/tool:1.0'},
        ), mock.patch.object(backend_app, 'get_docker_client', return_value=client), \
             mock.patch.object(backend_app.threading, 'Thread'):
            response = backend_app.podman_remove_image()
        self.assertEqual('success', response.get_json()['status'])
        client.images.list.assert_not_called()
        client.images.remove.assert_called_once_with(image='sha256:tool', force=True)

    def test_r_plugin_config_is_overridden_by_system_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_dir = os.path.join(root, 'r', 'demo')
            os.makedirs(plugin_dir)
            with open(os.path.join(plugin_dir, 'config.json'), 'w', encoding='utf-8') as handle:
                json.dump({
                    'id': 'demo',
                    'type': 'R_panel',
                    'docker_image': 'rocker/r-ver:latest',
                    'parameters': [],
                }, handle)
            backend_app.invalidate_plugin_cache()
            with mock.patch.object(backend_app, 'PLUGINS_DIR', root):
                loaded = backend_app.load_all_plugins()['demo']
            backend_app.invalidate_plugin_cache()
        self.assertEqual('rocker/r-ver:latest', loaded['configured_docker_image'])
        self.assertEqual(backend_app.R_DOCKER_IMAGE, loaded['docker_image'])

    def test_all_supported_r_plugin_type_variants_use_system_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            for index, plugin_type in enumerate(('R', 'R_panel', 'r_panel', 'r-script')):
                plugin_dir = os.path.join(root, 'r', f'demo_{index}')
                os.makedirs(plugin_dir)
                with open(os.path.join(plugin_dir, 'config.json'), 'w', encoding='utf-8') as handle:
                    json.dump({
                        'id': f'demo_{index}',
                        'type': plugin_type,
                        'docker_image': 'rocker/r-ver:latest',
                        'parameters': [],
                    }, handle)
            backend_app.invalidate_plugin_cache()
            with mock.patch.object(backend_app, 'PLUGINS_DIR', root):
                loaded = backend_app.load_all_plugins()
            backend_app.invalidate_plugin_cache()
        self.assertEqual(4, len(loaded))
        self.assertTrue(all(item['docker_image'] == backend_app.R_DOCKER_IMAGE for item in loaded.values()))

    def test_failed_validation_preserves_legacy_library(self):
        class Images:
            def get(self, _name):
                labels = {
                    'org.primigenius.r-version': backend_app.R_VERSION,
                    'org.primigenius.bioconductor-version': backend_app.BIOCONDUCTOR_VERSION,
                    'org.primigenius.base-digest': backend_app.R_DOCKER_IMAGE_DIGEST,
                    'org.primigenius.recipe-version': str(backend_app.R_CUSTOM_RECIPE_VERSION),
                    'org.primigenius.recipe-sha256': backend_app.R_CUSTOM_RECIPE_SHA256,
                }
                return mock.Mock(attrs={'Config': {'Labels': labels}})

        client = mock.Mock(images=Images())
        client.containers.run.side_effect = RuntimeError('validation failed')
        with tempfile.TemporaryDirectory() as root:
            legacy = os.path.join(root, 'r_libs')
            current = os.path.join(root, 'r_libs_current')
            os.makedirs(legacy)
            os.makedirs(current)
            with mock.patch.object(backend_app, 'R_LEGACY_LIBS_DIR', legacy), \
                 mock.patch.object(backend_app, 'R_LEGACY_BACKEND_LIBS_DIR', legacy), \
                 mock.patch.object(backend_app, 'R_LIBS_DIR', current), \
                 mock.patch.object(backend_app, '_image_exists_locally', return_value=True), \
                 mock.patch.object(backend_app, '_load_r_runtime_state', return_value={}):
                backend_app._complete_r_runtime_migration(client, lock_held=True)
            self.assertTrue(os.path.isdir(legacy))

    def test_successful_validation_retires_legacy_library(self):
        class Images:
            def __init__(self):
                self.removed = []

            def get(self, _name):
                labels = {
                    'org.primigenius.r-version': backend_app.R_VERSION,
                    'org.primigenius.bioconductor-version': backend_app.BIOCONDUCTOR_VERSION,
                    'org.primigenius.base-digest': backend_app.R_DOCKER_IMAGE_DIGEST,
                    'org.primigenius.recipe-version': str(backend_app.R_CUSTOM_RECIPE_VERSION),
                    'org.primigenius.recipe-sha256': backend_app.R_CUSTOM_RECIPE_SHA256,
                }
                return mock.Mock(attrs={'Config': {'Labels': labels}})

            def remove(self, image, force=False, noprune=True):
                self.removed.append(image)

        images = Images()
        client = mock.Mock(images=images)
        validation_container = mock.Mock()
        validation_container.wait.return_value = {'StatusCode': 0}
        client.containers.run.return_value = validation_container
        with tempfile.TemporaryDirectory() as root:
            legacy = os.path.join(root, 'install', 'r_libs')
            backend_legacy = os.path.join(root, 'backend', 'r_libs')
            current = os.path.join(root, 'r_libs_current')
            os.makedirs(legacy)
            os.makedirs(backend_legacy)
            os.makedirs(current)
            with open(os.path.join(legacy, 'old-package'), 'w', encoding='utf-8') as handle:
                handle.write('legacy')
            with open(os.path.join(backend_legacy, 'old-package'), 'w', encoding='utf-8') as handle:
                handle.write('legacy')
            with mock.patch.object(backend_app, 'R_LEGACY_LIBS_DIR', legacy), \
                 mock.patch.object(backend_app, 'R_LEGACY_BACKEND_LIBS_DIR', backend_legacy), \
                 mock.patch.object(backend_app, 'R_LIBS_DIR', current), \
                 mock.patch.object(backend_app, '_image_exists_locally', return_value=True), \
                 mock.patch.object(backend_app, '_load_r_runtime_state', return_value={
                     'migration_complete': True,
                     'cleanup_complete': True,
                     'library_generation': backend_app.R_LIBRARY_GENERATION,
                 }), \
                 mock.patch.object(backend_app, '_save_r_runtime_state') as save_state:
                backend_app._complete_r_runtime_migration(client, lock_held=True)
            self.assertFalse(os.path.exists(legacy))
            self.assertFalse(os.path.exists(backend_legacy))
            self.assertIn(backend_app.R_LEGACY_DOCKER_IMAGE, images.removed)
            self.assertTrue(save_state.call_args.kwargs['migration_complete'])
            self.assertEqual(2, len(save_state.call_args.kwargs['cleanup']['legacy_libraries_removed']))
            self.assertTrue(client.containers.run.call_args.kwargs['detach'])
            self.assertEqual({'type': 'json-file'}, client.containers.run.call_args.kwargs['log_config'])
            self.assertTrue(client.containers.run.call_args.kwargs['mounts'][0]['ReadOnly'])


class RRepositorySelectionTests(unittest.TestCase):
    def setUp(self):
        backend_app._R_REPO_AUTO_CACHE['selection'] = None
        backend_app._R_REPO_AUTO_CACHE['time'] = 0

    def test_auto_selects_fastest_complete_bundle(self):
        def fake_probe(name, cran, bioc, timeout=4.0):
            latency = {'tuna': 90, 'ustc': 25, 'nju': 50, 'zju': 70, 'global': 120}[name]
            return {'name': name, 'cran': cran, 'bioc': bioc, 'latency_ms': latency}

        with mock.patch.object(backend_app, '_probe_r_repo_bundle', side_effect=fake_probe):
            cran, bioc = backend_app._select_auto_r_repo_bundle(force=True)
        self.assertEqual('https://mirrors.ustc.edu.cn/CRAN', cran)
        self.assertEqual('https://mirrors.ustc.edu.cn/bioc', bioc)

    def test_auto_candidates_have_complete_supported_bundles(self):
        bundles = {name: (cran, bioc) for name, cran, bioc in backend_app.R_REPOSITORY_BUNDLES}
        self.assertNotIn('aliyun', bundles)
        self.assertEqual(
            ('https://mirrors.nju.edu.cn/CRAN', 'https://mirrors.nju.edu.cn/bioconductor'),
            bundles['nju'],
        )
        self.assertEqual(
            ('https://mirrors.zju.edu.cn/CRAN', 'https://mirrors.zju.edu.cn/bioconductor'),
            bundles['zju'],
        )

    def test_auto_falls_back_to_official_bundle(self):
        with mock.patch.object(backend_app, '_probe_r_repo_bundle', side_effect=RuntimeError('offline')), \
             mock.patch.object(backend_app, 'log'):
            cran, bioc = backend_app._select_auto_r_repo_bundle(force=True)
        self.assertEqual('https://cloud.r-project.org', cran)
        self.assertEqual('https://bioconductor.org', bioc)


class RPackageLockTests(unittest.TestCase):
    def test_writer_waits_for_active_reader(self):
        lock = backend_app._RPackageRWLock()
        reader_entered = threading.Event()
        release_reader = threading.Event()
        writer_entered = threading.Event()

        def reader():
            with lock.read():
                reader_entered.set()
                release_reader.wait(timeout=2)

        def writer():
            with lock.write():
                writer_entered.set()

        with mock.patch.object(backend_app, '_r_cross_process_lock', side_effect=lambda _exclusive: contextlib.nullcontext()):
            reader_thread = threading.Thread(target=reader)
            writer_thread = threading.Thread(target=writer)
            reader_thread.start()
            self.assertTrue(reader_entered.wait(timeout=1))
            writer_thread.start()
            time.sleep(0.05)
            self.assertFalse(writer_entered.is_set())
            release_reader.set()
            reader_thread.join(timeout=2)
            writer_thread.join(timeout=2)
        self.assertTrue(writer_entered.is_set())
        self.assertFalse(reader_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())

    def test_writers_are_serialized(self):
        lock = backend_app._RPackageRWLock()
        state_lock = threading.Lock()
        active = 0
        maximum = 0

        def writer():
            nonlocal active, maximum
            with lock.write():
                with state_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with state_lock:
                    active -= 1

        threads = [threading.Thread(target=writer) for _ in range(3)]
        with mock.patch.object(backend_app, '_r_cross_process_lock', side_effect=lambda _exclusive: contextlib.nullcontext()):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
        self.assertEqual(1, maximum)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_failed_or_cancelled_reader_releases_lock(self):
        lock = backend_app._RPackageRWLock()
        with mock.patch.object(backend_app, '_r_cross_process_lock', side_effect=lambda _exclusive: contextlib.nullcontext()):
            with self.assertRaises(RuntimeError):
                with lock.read():
                    raise RuntimeError('analysis failed')
            with self.assertRaises(KeyboardInterrupt):
                with lock.read():
                    raise KeyboardInterrupt()
            with lock.write():
                pass

    def test_failed_writer_releases_lock_for_next_analysis(self):
        lock = backend_app._RPackageRWLock()
        with mock.patch.object(backend_app, '_r_cross_process_lock', side_effect=lambda _exclusive: contextlib.nullcontext()):
            with self.assertRaises(RuntimeError):
                with lock.write():
                    raise RuntimeError('repair failed')
            with lock.read():
                pass


if __name__ == '__main__':
    unittest.main()
