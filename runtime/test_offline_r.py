"""Offline deployment failure/compatibility contracts; no engine or network needed."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import docker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from offline_r import OfflineR


class OfflineRTests(unittest.TestCase):
    def test_notifications_do_not_break_environment_preparation(self):
        seen = []
        self.environment.on_state_change = lambda state: seen.append(state.copy())
        self.environment.ensure()
        self.assertEqual([item['phase'] for item in seen], ['checking', 'verifying-archive', 'importing', 'validating', 'ready'])
        self.assertTrue(seen[-1]['ready'])
        self.assertEqual(seen[-1]['image_id'], self.identity)
        self.environment.on_state_change = Mock(side_effect=OSError('closed desktop pipe'))
        self.environment._set('ready', 'ready')
        self.assertTrue(self.environment.snapshot()['ready'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        (self.bundle / 'stage').mkdir(parents=True)
        self.archive = self.bundle / 'stage/environment.tar.gz'
        self.archive.write_bytes(b'test OCI fixture')
        self.identity = 'sha256:' + '1'*64
        self.manifest = dict(schema=1, recipe_sha256='recipe', image_id=self.identity,
            archive='environment.tar.gz', archive_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            archive_bytes=self.archive.stat().st_size, image_bytes=100, required_free_bytes=200)
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest))
        self.client = Mock()
        self.images = {}
        def get(ref):
            if ref not in self.images:
                raise docker.errors.ImageNotFound('missing')
            return self.images[ref]
        self.client.images.get.side_effect = get
        def load(stream):
            self.assertEqual(b'test OCI fixture', stream.read())
            self.images[self.identity] = SimpleNamespace(id=self.identity, valid=True)
            yield {'stream':'imported'}
        self.client.api.load_image.side_effect = load
        self.validate, self.space = Mock(), Mock()
        self.environment = OfflineR(self.bundle, self.root/'state', 'recipe', 'legacy',
            lambda image: image.valid, self.validate, lambda:self.client, self.space, Mock(), consume_archive=True)

    def test_import_and_atomic_activation_then_archive_removed(self):
        self.assertEqual(self.identity, self.environment.ensure())
        self.assertTrue(self.environment.snapshot()['ready'])
        self.assertEqual(self.identity, json.loads((self.root/'state/active.json').read_text())['image_id'])
        self.assertFalse(self.archive.exists())
        self.client.api.load_image.assert_called_once()
        self.client.close.assert_called_once()

    def use_xz_bundle(self):
        import lzma
        target = self.archive.with_suffix('.xz')
        target.write_bytes(lzma.compress(self.archive.read_bytes()))
        self.archive.unlink()
        self.archive = target
        self.manifest['archive'] = target.name
        self.manifest['archive_bytes'] = target.stat().st_size
        self.manifest['archive_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_xz_bundle_keeps_image_identity_and_cleanup_contract(self):
        self.use_xz_bundle()
        self.test_import_and_atomic_activation_then_archive_removed()

    def test_archive_names_cannot_escape_staging_or_select_unknown_formats(self):
        from offline_r import read_manifest
        for name in ('../environment.tar.xz', '/environment.tar.gz', 'environment.zip'):
            with self.subTest(name=name):
                self.manifest['archive'] = name
                path = self.bundle / 'manifest.json'
                path.write_text(json.dumps(self.manifest))
                with self.assertRaisesRegex(RuntimeError, 'archive name'):
                    read_manifest(path, 'recipe')

    def test_existing_environment_reused_without_import_or_disk_reservation(self):
        self.images['legacy'] = SimpleNamespace(id='old-valid', valid=True)
        self.assertEqual('old-valid', self.environment.ensure())
        self.client.api.load_image.assert_not_called()
        self.space.assert_not_called()
        self.client.images.remove.assert_not_called()

    def test_invalid_existing_environment_does_not_prevent_offline_replacement(self):
        self.images['legacy'] = SimpleNamespace(id='bad-old', valid=True)
        self.validate.side_effect = lambda client, ref: (_ for _ in ()).throw(RuntimeError('missing compiler')) if ref=='bad-old' else None
        self.assertEqual(self.identity, self.environment.ensure())
        self.assertIn('legacy', self.images)

    def test_corruption_never_imports_or_discards_repair_resource(self):
        self.archive.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            self.environment.ensure()
        self.client.api.load_image.assert_not_called()
        self.assertTrue(self.archive.exists())

    def test_insufficient_space_leaves_previous_state_untouched(self):
        (self.root/'state').mkdir()
        state = self.root/'state/active.json'
        state.write_text('{"image_id":"previous"}')
        self.space.side_effect = RuntimeError('disk full')
        with self.assertRaisesRegex(RuntimeError, 'disk full'):
            self.environment.ensure()
        self.assertEqual('{"image_id":"previous"}', state.read_text())
        self.client.api.load_image.assert_not_called()

    def test_failed_validation_can_resume_without_reimporting(self):
        self.validate.side_effect = RuntimeError('old package incompatible')
        with self.assertRaises(RuntimeError):
            self.environment.ensure()
        self.assertFalse((self.root/'state/active.json').exists())
        self.assertTrue(self.archive.exists())
        self.validate.side_effect = None
        self.assertEqual(self.identity, self.environment.ensure())
        self.client.api.load_image.assert_called_once()

    def test_concurrent_requests_share_one_preparation(self):
        self.validate.side_effect = lambda *a: time.sleep(.02)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            result = list(pool.map(lambda _:self.environment.ensure(), range(24)))
        self.assertEqual([self.identity]*24, result)
        self.client.api.load_image.assert_called_once()
        self.validate.assert_called_once()

    def test_missing_archive_requires_offline_repair_not_download(self):
        self.archive.unlink()
        with self.assertRaisesRegex(RuntimeError, 'installer containing the matching R environment'):
            self.environment.ensure()
        self.client.api.load_image.assert_not_called()
        self.assertEqual('attention', self.environment.snapshot()['state'])

    def test_next_launch_only_inspects_verified_identity_without_import_or_validation(self):
        self.environment.ensure()
        self.validate.reset_mock()
        self.client.api.load_image.reset_mock()
        reopened = OfflineR(self.bundle, self.root/'state', 'recipe', 'legacy',
            lambda image: image.valid, self.validate, lambda:self.client, self.space, Mock())
        self.assertEqual(self.identity, reopened.ensure())
        self.validate.assert_not_called()
        self.client.api.load_image.assert_not_called()

    def test_delete_requires_confirmation_and_refuses_used_image(self):
        image = Mock(id=self.identity, valid=True, attrs={'Size': 100})
        self.images[self.identity] = image
        with self.assertRaises(ValueError):
            self.environment.remove(self.identity)
        self.client.containers.list.return_value = [object()]
        with self.assertRaisesRegex(RuntimeError, 'still used'):
            self.environment.remove(self.identity, confirmed=True)
        self.client.images.remove.assert_not_called()

    def test_export_failure_keeps_image(self):
        self.images[self.identity] = Mock(id=self.identity, valid=True, attrs={'Size': 100})
        self.images[self.identity].save.side_effect = RuntimeError('export interrupted')
        self.client.containers.list.return_value = []
        with self.assertRaisesRegex(RuntimeError, 'export interrupted'):
            self.environment.remove(self.identity, confirmed=True)
        self.client.images.remove.assert_not_called()

    def test_delete_then_restore_uses_local_recovery_and_retags(self):
        import gzip
        image = Mock(id=self.identity, valid=True, attrs={'Size': 100})
        image.save.return_value = iter([b'offline-image-tar'])
        self.images[self.identity] = image
        self.client.containers.list.return_value = []
        self.client.images.remove.side_effect = lambda *a, **kw: self.images.pop(self.identity)
        self.environment.image_id = self.identity
        self.environment.remove(self.identity, confirmed=True)
        self.assertIsNone(self.environment.image_id)
        self.assertEqual('removed', self.environment.snapshot()['phase'])
        self.archive.unlink()
        def restore(stream):
            self.assertEqual(gzip.decompress(stream.read()), b'offline-image-tar')
            self.images[self.identity] = image
            yield {'stream': 'restored'}
        self.client.api.load_image.side_effect = restore
        self.assertEqual(self.identity, self.environment.ensure())
        self.client.api.tag.assert_called_with(self.identity, repository='legacy', tag='latest')
        self.client.images.remove.assert_called_once_with(self.identity, force=False, noprune=True)

    def test_xz_bundle_can_restore_an_existing_gzip_recovery_archive(self):
        self.use_xz_bundle()
        self.test_delete_then_restore_uses_local_recovery_and_retags()

    def test_xz_transport_never_advertises_the_compressed_length(self):
        import requests
        from offline_r import image_archive_stream
        self.use_xz_bundle()
        with image_archive_stream(self.archive) as stream:
            request = requests.Request('POST', 'http://localhost/images/load', data=stream).prepare()
            self.assertNotIn('Content-Length', request.headers)
            self.assertEqual('chunked', request.headers['Transfer-Encoding'])
            self.assertEqual(b'test OCI fixture', b''.join(request.body))


if __name__ == '__main__':
    unittest.main()
