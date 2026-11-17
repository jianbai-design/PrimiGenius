import pathlib
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'backend'))
from runtime_logs import LogCursor
from embedded_runtime import OwnedDockerClient, EmbeddedRuntime
from runtime_migration import MigrationError, migrate_existing, _pinned_cli


class RuntimeMigrationTest(unittest.TestCase):
    def test_patch_releases_use_actual_store_checks_in_both_directions(self):
        for version in ('5.8.2', '5.8.4', '5.8.6', '5.8.8'):
            for backend in ('sqlite', 'boltdb'):
                with self.subTest(version=version, backend=backend):
                    run = Mock(side_effect=[SimpleNamespace(returncode=0,
                        stdout=json.dumps({'host': {'databaseBackend': backend}}),
                        stderr='level=debug msg="Using static dir /custom data/中文"'),
                        SimpleNamespace(returncode=0, stdout='migration complete', stderr='')])
                    runtime = SimpleNamespace(manager=SimpleNamespace(_run_wsl=run))
                    with patch('runtime_migration._pinned_cli', return_value=('/archive', 'usr/bin/podman', 'a'*64)):
                        self.assertEqual('migration complete', migrate_existing(runtime, version, {'podman_version':'5.8.5'}))
                    self.assertIn('/custom data/中文', run.call_args.args[0])
                    self.assertIn('bolt_state.db' if backend == 'boltdb' else 'db.sql', run.call_args.args[0])

    def test_unknown_release_series_and_invalid_versions_do_not_touch_storage(self):
        for version in ('5.9.0', '6.0.0', '5.8.8-custom', '', 'not a version'):
            runtime = SimpleNamespace(manager=SimpleNamespace(_run_wsl=Mock()))
            with self.assertRaises(MigrationError), patch('runtime_migration._pinned_cli') as archive:
                migrate_existing(runtime, version, {'podman_version':'5.8.5'})
            runtime.manager._run_wsl.assert_not_called()
            archive.assert_not_called()

    def test_missing_or_unverified_database_path_is_preserved(self):
        runtime = SimpleNamespace(manager=SimpleNamespace(_run_wsl=Mock(return_value=SimpleNamespace(
            returncode=0, stdout='{"host":{"databaseBackend":"sqlite"}}', stderr=''))))
        with patch('runtime_migration._pinned_cli', return_value=('/archive', 'usr/bin/podman', 'a'*64)):
            with self.assertRaisesRegex(MigrationError, 'location could not be verified'):
                migrate_existing(runtime, '5.8.2', {'podman_version':'5.8.5'})
        self.assertEqual(1, runtime.manager._run_wsl.call_count)

    def test_only_verified_regular_cli_is_read_without_extracting_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / 'rootfs.tar.gz'
            data = b'fixed Linux CLI'
            with tarfile.open(archive, 'w:gz') as output:
                member = tarfile.TarInfo('./usr/bin/podman'); member.size=len(data)
                output.addfile(member, io.BytesIO(data))
            manifest = {'files': {'rootfs.tar.gz': hashlib.sha256(archive.read_bytes()).hexdigest()}}
            runtime = SimpleNamespace(bundle=directory, manager=SimpleNamespace(host_path_to_wsl=lambda p:p))
            self.assertEqual((str(archive), './usr/bin/podman', hashlib.sha256(data).hexdigest()), _pinned_cli(runtime, manifest))
            self.assertEqual(['rootfs.tar.gz'], [p.name for p in pathlib.Path(directory).iterdir()])
            manifest['files']['rootfs.tar.gz']='0'*64
            with self.assertRaisesRegex(MigrationError, 'checksum'):
                _pinned_cli(runtime, manifest)


class RuntimeBundleLocationTest(unittest.TestCase):
    def test_release_bundle_and_packaged_resources_keep_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manager = SimpleNamespace(app_install_dir=directory, runtime_channel='test')
            for relative in ('runtime/dist', 'runtime/release', 'resources/runtime'):
                bundle = root / relative
                bundle.mkdir(parents=True)
                (bundle / 'manifest.json').write_text('{}')
                self.assertEqual(str(bundle), EmbeddedRuntime(manager).bundle)


class MountCompatibilityTest(unittest.TestCase):
    def test_drive_mounts_preserve_readonly_and_do_not_mutate_callers(self):
        api = Mock()
        host = {'Mounts': [{'Type': 'bind', 'Source': r'E:\中文 空格\packages', 'Target': '/r_libs', 'ReadOnly': True},
                           {'Type': 'volume', 'Source': 'named-data', 'Target': '/data'}],
                'Binds': [r'D:\input files:/input:ro', '/tmp/linux:/linux:rw', 'named:/named:ro']}
        with patch('docker.DockerClient.__init__', lambda obj, **kw: setattr(obj, 'api', api)), patch('docker.APIClient'):
            client = OwnedDockerClient(owner='test', base_url='npipe:////./pipe/test')
            create = api.create_container
            # The wrapper closes over the original mocked endpoint.
            create(image='image', host_config=host)
            sent = api.mock_calls[-1].kwargs['host_config']
        self.assertEqual(sent['Mounts'][0]['Source'], '/mnt/e/中文 空格/packages')
        self.assertTrue(sent['Mounts'][0]['ReadOnly'])
        self.assertEqual(sent['Mounts'][1], host['Mounts'][1])
        self.assertEqual(sent['Binds'], ['/mnt/d/input files:/input:ro', '/tmp/linux:/linux:rw', 'named:/named:ro'])
        self.assertEqual(host['Mounts'][0]['Source'], r'E:\中文 空格\packages')


class Records:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class LogResumeTest(unittest.TestCase):
    def test_repeated_lines_utf8_boundaries_and_interrupted_record(self):
        record = '2026-10-17T12:00:00.123456789Z 中文重复\n'.encode()
        first = Records([record[:32], record[32:], record, record[:34], OSError('EOF')])
        calls = []

        class Container:
            stream = first

            def logs(self, **kwargs):
                calls.append(kwargs)
                return self.stream

        container = Container()
        cursor, output = LogCursor(), []
        with self.assertRaises(OSError):
            cursor.read(container, output.append)
        self.assertTrue(first.closed)
        container.stream = Records([record * 3, b'2026-10-17T12:00:01.000000001Z tail\n'])
        cursor.read(container, output.append)
        self.assertEqual(output, ['中文重复'] * 3 + ['tail'])
        self.assertIsInstance(calls[-1]['since'], int)
        self.assertEqual(cursor.occurrence, 1)
        self.assertTrue(container.stream.closed)


if __name__ == '__main__':
    unittest.main()
