import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from library_compat import resolve_library


class LibraryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.primary=self.root/'r_libs_r4_6_1-bioc3_23';self.legacy=self.root/'r_libs';self.state=self.root/'state.json'

    def package(self,path,name='demo',built='R 4.6.1; x86_64-pc-linux-gnu; 2026-10-01; unix',version='1.0'):
        folder=path/name;folder.mkdir(parents=True,exist_ok=True)
        (folder/'DESCRIPTION').write_text(f'Package: {name}\nVersion: {version}\n'+(f'Built: {built}\n' if built else ''))
        (folder/'user-data').write_bytes(b'must remain unchanged')

    def resolve(self):
        return resolve_library(str(self.primary),[str(self.legacy)],str(self.state),'4.6.1','3.23')

    def test_populated_current_generation_keeps_its_original_path(self):
        self.package(self.primary);self.package(self.legacy)
        self.assertEqual(self.resolve(),str(self.primary))

    def test_compatible_legacy_library_is_reused_without_moving_or_reinstalling(self):
        self.primary.mkdir();self.package(self.legacy)
        self.package(self.legacy,'BiocVersion',version='3.23.0')
        before={p.relative_to(self.legacy):p.read_bytes() for p in self.legacy.rglob('*') if p.is_file()}
        self.assertEqual(self.resolve(),str(self.legacy))
        self.assertEqual(before,{p.relative_to(self.legacy):p.read_bytes() for p in self.legacy.rglob('*') if p.is_file()})
        self.assertEqual(list(self.primary.iterdir()),[])

    def test_different_r_or_foreign_platform_is_never_loaded_or_deleted(self):
        for built in ('R 4.4.0; x86_64-pc-linux-gnu','R 4.6.1; x86_64-w64-mingw32'):
            self.package(self.legacy,built=built)
            self.assertEqual(self.resolve(),str(self.primary))
            self.assertEqual((self.legacy/'demo/user-data').read_bytes(),b'must remain unchanged')

    def test_wrong_bioconductor_library_remains_untouched(self):
        self.package(self.legacy);self.package(self.legacy,'BiocVersion',version='3.22.0')
        self.assertEqual(self.resolve(),str(self.primary))
        self.assertTrue((self.legacy/'BiocVersion/DESCRIPTION').is_file())

    def test_recorded_data_location_is_preserved(self):
        location=self.root/'用户 自定义包库'
        self.package(location,built='')
        self.state.write_text(json.dumps(dict(library_path=str(location),r_version='4.6.1',bioconductor_version='3.23')))
        self.assertEqual(self.resolve(),str(location))

    def test_unknown_build_never_causes_destructive_migration(self):
        self.package(self.legacy,built='')
        self.assertEqual(self.resolve(),str(self.primary))
        self.assertTrue((self.legacy/'demo/user-data').is_file())

    def test_explicit_old_bioconductor_state_blocks_ambiguous_reuse(self):
        self.package(self.legacy)
        self.state.write_text(json.dumps(dict(r_version='4.6.1',bioconductor_version='3.22')))
        self.assertEqual(self.resolve(),str(self.primary))


if __name__=='__main__':unittest.main()
