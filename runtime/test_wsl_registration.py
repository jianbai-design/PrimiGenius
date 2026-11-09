"""Registration recovery against isolated files and a deterministic registry double."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from wsl_registration import LXSS, repair_registration


class Key:
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class Registry:
    HKEY_CURRENT_USER = object()
    KEY_READ, KEY_WRITE, REG_SZ, REG_DWORD = 1, 2, 1, 4
    def __init__(self):
        self.nodes = {LXSS: {'DefaultDistribution': ('{other}', 1)},
                      LXSS + r'\{owned}': {'DistributionName': ('owned-machine', 1), 'Version': (2, 4),
                                          'VhdFileName': ('ext4.vhdx', 1), 'Binary': (b'\0\1', 3)},
                      LXSS + r'\{other}': {'DistributionName': ('unrelated', 1), 'Version': (2, 4),
                                          'BasePath': ('C:\\unrelated', 1)}}
        self.changes = []
    def OpenKey(self, parent, path, *args):
        full = path if parent is self.HKEY_CURRENT_USER else parent.path + '\\' + path
        if full not in self.nodes:
            raise FileNotFoundError(full)
        return Key(full)
    def children(self, key):
        prefix = key.path + '\\'
        return [path[len(prefix):] for path in self.nodes if path.startswith(prefix) and '\\' not in path[len(prefix):]]
    def QueryInfoKey(self, key):
        return len(self.children(key)), len(self.nodes[key.path]), 0
    def EnumKey(self, key, index):
        return self.children(key)[index]
    def EnumValue(self, key, index):
        name, (value, kind) = list(self.nodes[key.path].items())[index]
        return name, value, kind
    def QueryValueEx(self, key, name):
        if name not in self.nodes[key.path]:
            raise FileNotFoundError(name)
        return self.nodes[key.path][name]
    def SetValueEx(self, key, name, reserved, kind, value):
        self.changes.append(('set', key.path, name))
        self.nodes[key.path][name] = value, kind
    def DeleteValue(self, key, name):
        self.changes.append(('delete-value', key.path, name))
        del self.nodes[key.path][name]
    def DeleteKey(self, key, child):
        target = key.path + '\\' + child
        if self.children(Key(target)):
            raise OSError('has children')
        self.changes.append(('delete-key', target))
        del self.nodes[target]


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='wsl-registration-test-')
        self.addCleanup(self.temp.cleanup)
        self.install = Path(self.temp.name)/'安装 有空格'
        self.data = self.install/'podman-data'
        self.config = self.install/'podman-config'
        self.data.mkdir(parents=True)
        self.registry = Registry()
        self.original = copy.deepcopy(self.registry.nodes)
        patcher = patch.dict(sys.modules, {'winreg': self.registry})
        patcher.start()
        self.addCleanup(patcher.stop)

    def repair(self):
        return repair_registration('owned-machine', self.data, self.config)

    def disk(self, relative='machine/wsldist/ext4.vhdx'):
        path = self.data/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'original disk, do not modify')
        return path

    def test_existing_disk_repairs_only_base_path_after_durable_backup(self):
        disk = self.disk()
        result = self.repair()
        self.assertEqual(result['action'], 'restore-base-path')
        self.assertEqual(self.registry.nodes[LXSS+r'\{owned}']['BasePath'][0], str(disk.parent.resolve()))
        self.assertEqual(disk.read_bytes(), b'original disk, do not modify')
        snapshot = json.loads(Path(result['archive']).read_text(encoding='utf-8'))
        self.assertNotIn('BasePath', [item[0] for item in snapshot['values']])
        self.assertEqual(snapshot['values'][-1][1], {'bytes_hex': '0001'})
        self.assertEqual(self.registry.nodes[LXSS+r'\{other}'], self.original[LXSS+r'\{other}'])
        self.assertIsNone(self.repair())
        self.assertEqual(len(self.registry.changes), 1)

    def test_orphan_metadata_is_archived_without_removing_other_data(self):
        kept = self.data/'containers'/'keep.bin'
        kept.parent.mkdir()
        kept.write_bytes(b'keep')
        result = self.repair()
        self.assertEqual(result['action'], 'quarantine-orphan')
        self.assertFalse(result['was_default'])
        self.assertTrue(Path(result['archive']).is_file())
        self.assertNotIn(LXSS+r'\{owned}', self.registry.nodes)
        self.assertEqual(kept.read_bytes(), b'keep')
        self.assertEqual(self.registry.nodes[LXSS], self.original[LXSS])
        self.assertEqual(self.registry.nodes[LXSS+r'\{other}'], self.original[LXSS+r'\{other}'])

    def test_valid_path_is_never_replaced_even_if_its_disk_is_missing(self):
        self.registry.nodes[LXSS+r'\{owned}']['BasePath'] = 'C:\\original', 1
        self.assertIsNone(self.repair())
        self.assertEqual(self.registry.changes, [])

    def test_multiple_disks_or_unrecognized_disk_prevent_metadata_removal(self):
        first = self.disk('machine/wsldist/unknown/ext4.vhdx')
        with self.assertRaisesRegex(RuntimeError, 'manual path recovery'):
            self.repair()
        second = self.disk()
        with self.assertRaisesRegex(RuntimeError, 'manual path recovery'):
            self.repair()
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(self.registry.nodes, self.original)

    def test_existing_installer_backup_is_not_bypassed(self):
        backup = self.install.with_name(self.install.name + '.primigenius-preserved')
        backup.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'preserved installation'):
            self.repair()
        self.assertEqual(self.registry.nodes, self.original)

    def test_authorized_new_environment_keeps_backup_and_isolates_diskless_orphan(self):
        backup = self.install.with_name(self.install.name + '.primigenius-preserved')
        backup.mkdir()
        disk = backup / 'ext4.vhdx'
        disk.write_bytes(b'preserved old backup')
        result = repair_registration('owned-machine', self.data, self.config, allow_orphan_replacement=True)
        self.assertEqual(result['action'], 'quarantine-orphan')
        self.assertEqual(disk.read_bytes(), b'preserved old backup')
        self.assertEqual(self.registry.nodes[LXSS+r'\{other}'], self.original[LXSS+r'\{other}'])

    def test_failed_backup_prevents_all_registry_changes(self):
        with patch('wsl_registration.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.repair()
        self.assertEqual(self.registry.nodes, self.original)

    def test_concurrent_registration_change_is_not_overwritten(self):
        def change(_fd):
            self.registry.nodes[LXSS+r'\{owned}']['BasePath'] = 'C:\\finished-installing', 1
        with patch('wsl_registration.os.fsync', side_effect=change):
            with self.assertRaisesRegex(RuntimeError, 'changed while'):
                self.repair()
        self.assertEqual(self.registry.changes, [])
        self.assertIn(LXSS+r'\{owned}', self.registry.nodes)

    def test_only_owned_default_reference_is_cleared(self):
        self.registry.nodes[LXSS]['DefaultDistribution'] = '{owned}', 1
        result = self.repair()
        self.assertTrue(result['was_default'])
        self.assertNotIn('DefaultDistribution', self.registry.nodes[LXSS])
        snapshot = json.loads(Path(result['archive']).read_text())
        self.assertEqual(snapshot['default_distribution'], '{owned}')

    def test_fresh_windows_profile_has_no_registration_to_repair(self):
        self.registry.nodes.clear()
        self.assertIsNone(self.repair())

    def test_duplicate_names_are_retained_for_manual_recovery(self):
        self.registry.nodes[LXSS+r'\{duplicate}'] = copy.deepcopy(self.registry.nodes[LXSS+r'\{owned}'])
        with self.assertRaisesRegex(RuntimeError, 'Multiple WSL registrations'):
            self.repair()
        self.assertEqual(self.registry.changes, [])


@unittest.skipUnless(os.name == 'nt', 'Windows registry test')
class NativeRegistryTests(unittest.TestCase):
    def test_real_registry_repair_is_isolated_and_keeps_disk_bytes(self):
        import winreg
        import wsl_registration
        private = 'Software\\PrimiGenius\\RegistrationRecoveryTest\\' + uuid.uuid4().hex
        self.assertTrue(private.startswith('Software\\PrimiGenius\\RegistrationRecoveryTest\\'))
        with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='wsl-native-test-') as directory:
            install = Path(directory)/'安装 有空格'
            data, config = install/'podman-data', install/'podman-config'
            disk = data/'machine/wsldist/ext4.vhdx'
            disk.parent.mkdir(parents=True)
            disk.write_bytes(b'unchanged native test fixture')
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, private) as root:
                    winreg.SetValueEx(root, 'DefaultDistribution', 0, winreg.REG_SZ, '{other}')
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, private+r'\{owned}') as key:
                    winreg.SetValueEx(key, 'DistributionName', 0, winreg.REG_SZ, 'owned-machine')
                    winreg.SetValueEx(key, 'Version', 0, winreg.REG_DWORD, 2)
                    winreg.SetValueEx(key, 'VhdFileName', 0, winreg.REG_SZ, 'ext4.vhdx')
                with patch.object(wsl_registration, 'LXSS', private):
                    restored = repair_registration('owned-machine', data, config)
                    self.assertEqual(restored['action'], 'restore-base-path')
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, private+r'\{owned}', 0,
                                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
                        self.assertEqual(winreg.QueryValueEx(key, 'BasePath')[0], str(disk.parent))
                        winreg.DeleteValue(key, 'BasePath')
                    self.assertEqual(disk.read_bytes(), b'unchanged native test fixture')
                    disk.unlink()  # Only this test-created byte fixture is removed.
                    orphan = repair_registration('owned-machine', data, config)
                    self.assertEqual(orphan['action'], 'quarantine-orphan')
                    self.assertTrue(Path(orphan['archive']).is_file())
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, private) as root:
                        self.assertEqual(winreg.QueryInfoKey(root)[0], 0)
                        self.assertEqual(winreg.QueryValueEx(root, 'DefaultDistribution')[0], '{other}')
            finally:
                # These are the two exact keys created by this test, never Lxss.
                for key in (private+r'\{owned}', private):
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                    except FileNotFoundError:
                        pass
                parent = private.rsplit('\\', 1)[0]
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, parent) as key:
                        empty = winreg.QueryInfoKey(key)[:2] == (0, 0)
                    if empty:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, parent)
                except FileNotFoundError:
                    pass


if __name__ == '__main__':
    unittest.main()
