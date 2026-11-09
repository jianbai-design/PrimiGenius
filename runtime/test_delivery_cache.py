import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'build'))
import runtime_delivery as delivery


class DeliveryCacheTests(unittest.TestCase):
    def source(self,root):
        source=root/'source';source.mkdir()
        binaries={'primigenius-engine':b'engine','primigenius-bridge-linux':b'bridge','podman-linux':b'cli','primigenius-bridge.exe':b'windows bridge'}
        for name,data in binaries.items():(source/name).write_bytes(data)
        with tarfile.open(source/'rootfs.tar.gz','w:gz') as archive:
            for name,data in [('usr/libexec/primigenius/engine',b'engine'),('usr/libexec/primigenius/bridge',b'bridge'),('usr/bin/podman',b'cli'),('etc/os-release',b'test distro')]:
                member=tarfile.TarInfo(name);member.size=len(data);archive.addfile(member,io.BytesIO(data))
        manifest=dict(schema_version=1,engine='primigenius-libpod',files={path.name:delivery.sha(path) for path in source.iterdir()})
        (source/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return source,manifest

    def test_cache_reuses_verified_delivery_and_never_modifies_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source,manifest=self.source(root)
            first=delivery.prepare(source,root/'first',cache=root/'cache')
            with patch.object(delivery,'_prepare',side_effect=AssertionError('cache should avoid recompression')):
                second=delivery.prepare(source,root/'second',cache=root/'cache')
            self.assertFalse(first['cache_hit']);self.assertTrue(second['cache_hit'])
            self.assertEqual(delivery.sha(root/'first/rootfs.tar.gz'),delivery.sha(root/'second/rootfs.tar.gz'))
            self.assertEqual(manifest['files'],{name:delivery.sha(source/name) for name in manifest['files']})

    def test_cache_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source,_=self.source(root)
            delivery.prepare(source,root/'first',cache=root/'cache')
            binary=next((root/'cache').glob('*/files/primigenius-engine'));binary.write_bytes(b'broken')
            with self.assertRaisesRegex(RuntimeError,'Invalid cached runtime'):
                delivery.prepare(source,root/'second',cache=root/'cache')
            self.assertFalse((root/'second').exists())

    def test_release_checks_remain_mandatory_on_cache_hit(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source,_=self.source(root)
            delivery.prepare(source,root/'first',cache=root/'cache')
            manifest=json.loads((root/'first/manifest.json').read_text())
            acceptance=dict(artifacts=manifest['files'],checks={name:True for name in ('containers','plugins','gui','installer','rollback','performance')})
            acceptance['checks']['installer']=False
            (source/'acceptance.json').write_text(json.dumps(acceptance))
            with self.assertRaisesRegex(RuntimeError,'Release acceptance'):
                delivery.prepare(source,root/'second',release=True,cache=root/'cache')
            self.assertFalse((root/'second').exists())


if __name__=='__main__':unittest.main()
