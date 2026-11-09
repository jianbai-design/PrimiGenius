import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'backend'))
from runtime_logs import LogCursor
from embedded_runtime import OwnedDockerClient, EmbeddedRuntime


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
