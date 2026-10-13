import contextlib
import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app as runtime


class RBuildEnvironmentTests(unittest.TestCase):
    def test_upgrade_rejects_old_image_without_changing_library(self):
        labels = {
            'org.primigenius.r-version': runtime.R_VERSION,
            'org.primigenius.bioconductor-version': runtime.BIOCONDUCTOR_VERSION,
            'org.primigenius.base-digest': runtime.R_DOCKER_IMAGE_DIGEST,
            'org.primigenius.recipe-version': '4', 'org.primigenius.recipe-sha256': 'old',
        }
        self.assertFalse(runtime._custom_r_image_matches_contract(mock.Mock(attrs={'Config': {'Labels': labels}})))
        self.assertEqual('r4.6.1-bioc3.23', runtime.R_LIBRARY_GENERATION)
        self.assertIn('rustc-1.89', runtime.R_SYSTEM_DEPS)
        self.assertIn('cargo-1.89', runtime.R_SYSTEM_DEPS)
        self.assertIn(runtime._R_RUST_CHECK, runtime._r_custom_recipe_text())

    def test_fallback_preserves_shell_arguments_and_failure(self):
        client = mock.Mock()
        args = ['Rscript', '-e', "cat(\"it's safe; $HOME\")"]
        with mock.patch.object(runtime, '_get_r_image', return_value=runtime.R_DOCKER_IMAGE), \
             mock.patch.object(runtime, '_ensure_custom_r_image', return_value=False), \
             mock.patch.object(runtime, '_stream_container_logs_realtime'), \
             mock.patch.object(runtime, '_robust_container_wait', return_value={'StatusCode': 1}):
            code, _ = runtime._run_r_container_with_realtime_logs(client, args, [], {}, install_system_deps=True)
        command = client.containers.run.call_args.kwargs['command'][2]
        prefix = runtime._r_system_deps_prefix() + 'stdbuf -oL -eL '
        self.assertTrue(command.startswith(prefix))
        self.assertEqual(args, shlex.split(command[len(prefix):]))
        self.assertEqual(1, code)
        client.containers.run.return_value.remove.assert_called_once()

    def test_install_preflight_and_analysis_share_fallback(self):
        client = mock.Mock()
        job = {'status': 'pending', 'log': []}
        with tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as patches:
            for name, value in (
                ('get_docker_client', client), ('_get_r_image', runtime.R_DOCKER_IMAGE),
                ('_ensure_pinned_r_base_image', True), ('_ensure_custom_r_image', False),
                ('_resolve_r_repo_bundle', ('https://example.test/CRAN', 'https://example.test/bioc')),
                ('_robust_container_wait', {'StatusCode': 1}),
                ('_host_r_dependency_status', {'ready': False, 'missing': ['demo']}),
                ('_has_cached_r_preflight', False), ('_complete_r_runtime_migration', None),
                ('_stream_container_logs_realtime', None), ('_enqueue_job_log', None),
            ):
                patches.enter_context(mock.patch.object(runtime, name, return_value=value))
            patches.enter_context(mock.patch.object(runtime, 'INSTALL_JOBS', {'test': job}))
            patches.enter_context(mock.patch.object(runtime.tempfile, 'gettempdir', return_value=root))
            patches.enter_context(mock.patch.object(runtime, 'R_PACKAGE_LOCK_FILE', os.path.join(root, 'package.lock')))
            runtime._run_r_install_job_locked('test', ['demo'], skip_repair=True)
            self.assertEqual('failed', job['status'])
            self.assertTrue(client.containers.run.call_args.kwargs['command'][2].startswith(runtime._r_system_deps_prefix()))
            script = os.path.join(root, 'analysis.R')
            with open(script, 'w', encoding='utf-8') as handle:
                handle.write('cat("ok")')
            for required in (['demo'], None):
                client.containers.run.reset_mock()
                result = runtime._run_rscript_docker(script, {}, root, root, required_packages=required)
                self.assertEqual('error' if required else 'failed', result['status'])
                self.assertEqual(1, client.containers.run.call_count)
                self.assertTrue(client.containers.run.call_args.kwargs['command'][2].startswith(runtime._r_system_deps_prefix()))
            self.assertEqual(['analysis.R', 'package.lock', 'primigenius_rscripts'], sorted(os.listdir(root)))
            self.assertEqual([], os.listdir(os.path.join(root, 'primigenius_rscripts')))


if __name__ == '__main__':
    unittest.main()
