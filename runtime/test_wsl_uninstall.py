"""Uninstall ownership and failure handling; never uses real WSL registrations."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
import winreg as native_registry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import wsl_uninstall as uninstall
from runtime.test_wsl_registration import Registry


class WslUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='wsl-uninstall-test-')
        self.addCleanup(self.temp.cleanup)
        self.install = Path(self.temp.name) / '安装 有空格'
        self.config = self.install / 'podman-config' / 'machine-runtime.json'
        self.data = self.install / 'podman-data'
        self.config.parent.mkdir(parents=True)
        self.saved = dict(channel='stable', machine_name='primigenius-stable',
                          wsl_distro_name='podman-primigenius-stable', data_dir=str(self.data))
        self.save()
        self.registry = Registry()
        self.target = uninstall.LXSS + r'\{owned}'
        self.registry.nodes[self.target]['DistributionName'] = ('podman-primigenius-stable', 1)
        self.base = self.data / 'machine' / 'wsldist'
        self.disk = self.base / 'ext4.vhdx'
        self.disk.parent.mkdir(parents=True)
        self.disk.write_bytes(b'owned fake disk')
        self.registry.nodes[self.target]['BasePath'] = (str(self.base), 1)
        self.dev = uninstall.LXSS + r'\{dev}'
        self.dev_disk = self.install / 'podman-data-dev' / 'machine' / 'wsldist' / 'ext4.vhdx'
        self.dev_disk.parent.mkdir(parents=True)
        self.dev_disk.write_bytes(b'development disk must survive')
        self.registry.nodes[self.dev] = dict(DistributionName=('podman-primigenius-dev', 1),
                                             BasePath=(str(self.dev_disk.parent), 1), Version=(2, 4))
        self.others = {key: copy.deepcopy(value) for key, value in self.registry.nodes.items() if key != self.target}
        self.calls = []
        self.patch = patch.dict(sys.modules, {'winreg': self.registry})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save(self):
        self.config.write_text(json.dumps(self.saved), encoding='utf-8')

    def run_wsl(self, command, **kwargs):
        self.assertEqual(command[1:], ['--unregister', self.saved['wsl_distro_name']])
        self.calls.append(command)
        # Like WSL, this runner removes only the explicitly named distribution.
        self.registry.nodes.pop(self.target)
        self.disk.unlink(missing_ok=True)
        return subprocess.CompletedProcess(command, 0)

    def remove(self, runner=None):
        return uninstall.remove_runtime(self.install, run=runner or self.run_wsl)

    def assert_others(self):
        for key, value in self.others.items():
            self.assertEqual(self.registry.nodes[key], value)
        self.assertEqual(self.dev_disk.read_bytes(), b'development disk must survive')

    def test_stable_removes_only_owned_disk_and_archives_selection(self):
        result = self.remove()
        self.assertIn('podman-primigenius-stable', result)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(self.target, self.registry.nodes)
        settings = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertTrue(settings['provisioning_pending'])
        self.assertEqual(settings['data_dir'], self.saved['data_dir'])
        self.assertFalse(self.base.exists())
        archives = list(self.config.parent.glob('registration-recovery/uninstall-*.json'))
        self.assertEqual(len(archives), 2)
        self.assert_others()
        # A subsequent install must be allowed to provision intentionally deleted data.
        from runtime_selection import select_runtime
        entries = [dict(name=value['DistributionName'][0], base=value.get('BasePath', (None,))[0])
                   for key, value in self.registry.nodes.items() if key != uninstall.LXSS]
        self.assertFalse(select_runtime('stable', self.data, self.config, entries)['error'])

    def test_saved_legacy_default_is_supported(self):
        self.saved.update(machine_name='podman-machine-default', wsl_distro_name='podman-machine-default')
        self.save()
        self.registry.nodes[self.target]['DistributionName'] = ('podman-machine-default', 1)
        self.remove()
        self.assert_others()

    def test_numbered_stable_runtime_is_removed_without_touching_old_stable(self):
        self.saved.update(machine_name='primigenius-stable-2', wsl_distro_name='podman-primigenius-stable-2')
        self.save()
        self.registry.nodes[self.target]['DistributionName'] = ('podman-primigenius-stable-2', 1)
        old = uninstall.LXSS + r'\{old-stable}'
        self.registry.nodes[old] = dict(DistributionName=('podman-primigenius-stable', 1),
                                       BasePath=('C:\\old-install\\podman-data\\machine\\wsldist', 1), Version=(2, 4))
        before = copy.deepcopy(self.registry.nodes[old])
        self.remove()
        self.assertEqual(self.registry.nodes[old], before)
        self.assert_others()

    def test_custom_storage_preference_survives_removal_and_reinstall(self):
        from runtime_selection import select_runtime
        self.disk.unlink()
        self.data = Path(self.temp.name) / 'custom-storage'
        self.base = self.data / 'machine/wsldist'
        self.base.mkdir(parents=True)
        self.disk = self.base / 'ext4.vhdx'
        self.disk.write_bytes(b'owned custom-location disk')
        self.saved['data_dir'] = str(self.data)
        self.save()
        self.registry.nodes[self.target]['BasePath'] = (str(self.base), 1)
        self.remove()
        self.assertEqual(json.loads(self.config.read_text())['data_dir'], str(self.data))
        plan = select_runtime('stable', self.data, self.config, [])
        self.assertFalse(plan['error'])
        self.assertEqual(plan['data_dir'], str(self.data))

    def test_old_install_without_selection_can_identify_owned_default(self):
        self.config.unlink()
        self.saved.update(wsl_distro_name='podman-machine-default')
        self.registry.nodes[self.target]['DistributionName'] = ('podman-machine-default', 1)
        self.remove()
        self.assert_others()

    def test_unrelated_podman_default_is_not_adopted(self):
        self.config.unlink()
        self.registry.nodes[self.target]['DistributionName'] = ('podman-machine-default', 1)
        self.registry.nodes[self.target]['BasePath'] = ('C:\\PodmanDesktop\\unrelated', 1)
        before = copy.deepcopy(self.registry.nodes)
        self.remove()
        self.assertEqual(before, self.registry.nodes)
        self.assertFalse(self.calls)
        self.assertTrue(self.disk.exists())

    def test_missing_registration_with_disk_blocks_wrong_user_cleanup(self):
        self.registry.nodes.pop(self.target)
        with self.assertRaisesRegex(RuntimeError, 'Windows user'):
            self.remove()
        self.assertTrue(self.disk.exists())
        self.assertFalse(self.calls)
        self.assertTrue(self.config.is_file())

    def test_retry_after_runtime_was_removed_prepares_reinstall(self):
        self.registry.nodes.pop(self.target)
        self.disk.unlink()
        before = copy.deepcopy(self.registry.nodes)
        self.remove()
        self.assertEqual(before, self.registry.nodes)
        self.assertTrue(json.loads(self.config.read_text(encoding='utf-8'))['provisioning_pending'])
        self.assertFalse(self.calls)

    def test_failed_unregister_preserves_complete_registration_and_program(self):
        program = self.install / 'resources' / 'backend' / 'app.exe'
        program.parent.mkdir(parents=True)
        program.write_bytes(b'program')
        before = copy.deepcopy(self.registry.nodes)
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            self.remove(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
        self.assertEqual(before, self.registry.nodes)
        self.assertTrue(self.disk.exists())
        self.assertTrue(program.exists())
        self.assertTrue(self.config.exists())

    def test_false_success_does_not_remove_selection(self):
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            self.remove(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
        self.assertTrue(self.config.exists())
        self.assertTrue(self.disk.exists())

    def test_pathless_entry_restores_unique_disk_before_native_unregister(self):
        del self.registry.nodes[self.target]['BasePath']
        def run(cmd, **kw):
            self.assertEqual(self.registry.nodes[self.target]['BasePath'][0], str(self.base))
            return self.run_wsl(cmd, **kw)
        self.remove(run)
        self.assert_others()

    def test_pathless_diskless_entry_is_removed_whole_after_backup(self):
        del self.registry.nodes[self.target]['BasePath']
        self.disk.unlink()
        self.remove()
        self.assertNotIn(self.target, self.registry.nodes)
        self.assertFalse(self.calls)
        self.assert_others()

    def test_only_removed_default_reference_is_cleared(self):
        self.registry.nodes[uninstall.LXSS]['DefaultDistribution'] = ('{owned}', 1)
        del self.registry.nodes[self.target]['BasePath']
        self.disk.unlink()
        self.remove()
        self.assertNotIn('DefaultDistribution', self.registry.nodes[uninstall.LXSS])
        self.assertEqual(self.registry.nodes[self.dev], self.others[self.dev])

    def test_native_unregister_cannot_leave_a_dangling_default_reference(self):
        self.registry.nodes[uninstall.LXSS]['DefaultDistribution'] = ('{owned}', 1)
        self.remove()
        self.assertNotIn('DefaultDistribution', self.registry.nodes[uninstall.LXSS])
        self.assertNotIn(self.target, self.registry.nodes)

    def test_ambiguous_pathless_disks_abort_without_registry_changes(self):
        del self.registry.nodes[self.target]['BasePath']
        extra = self.base / 'primigenius-stable' / 'ext4.vhdx'
        extra.parent.mkdir()
        extra.write_bytes(b'other disk')
        before = copy.deepcopy(self.registry.nodes)
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            self.remove()
        self.assertEqual(before, self.registry.nodes)
        self.assertTrue(extra.exists())

    def test_shared_storage_or_selected_dev_is_never_removed(self):
        for field, value in [('BasePath', str(self.dev_disk.parent))]:
            self.registry.nodes[self.target][field] = (value, 1)
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            self.remove()
        self.saved.update(channel='dev', machine_name='primigenius-dev', wsl_distro_name='podman-primigenius-dev')
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'stable/legacy'):
            self.remove()
        self.assert_others()
        self.assertFalse(self.calls)

    def test_another_distribution_sharing_owned_path_blocks_cleanup(self):
        self.registry.nodes[self.dev]['BasePath'] = (str(self.base), 1)
        before = copy.deepcopy(self.registry.nodes)
        with self.assertRaisesRegex(RuntimeError, 'overlaps'):
            self.remove()
        self.assertEqual(before, self.registry.nodes)
        self.assertFalse(self.calls)

    def test_backup_failure_never_runs_wsl(self):
        before = copy.deepcopy(self.registry.nodes)
        with patch.object(uninstall.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.remove()
        self.assertEqual(before, self.registry.nodes)
        self.assertFalse(self.calls)

    def test_registration_race_blocks_cleanup(self):
        original = uninstall.registrations
        count = 0
        def read(registry):
            nonlocal count
            count += 1
            if count == 2:
                self.registry.nodes[self.target]['BasePath'] = ('C:\\changed', 1)
            return original(registry)
        with patch.object(uninstall, 'registrations', side_effect=read):
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                self.remove()
        self.assertTrue(self.disk.exists())
        self.assertFalse(self.calls)

    def test_leftover_runtime_files_are_not_recursively_deleted(self):
        leftover = self.base / 'keep-user-file.txt'
        leftover.write_bytes(b'user data')
        self.remove()
        self.assertEqual(leftover.read_bytes(), b'user data')
        self.assert_others()

    def test_native_registry_in_private_test_namespace(self):
        private = 'Software\\PrimiGenius\\UninstallTests\\' + str(uuid.uuid4())
        created = []
        try:
            # These keys are deliberately outside the real WSL/Lxss tree.
            for path, values in self.registry.nodes.items():
                relative = path[len(uninstall.LXSS):]
                name = private + relative
                with native_registry.CreateKey(native_registry.HKEY_CURRENT_USER, name) as key:
                    created.append(name)
                    for field, (value, kind) in values.items():
                        native_registry.SetValueEx(key, field, 0, kind, value)
            with patch.dict(sys.modules, {'winreg': native_registry}), patch.object(uninstall, 'LXSS', private):
                before, default = uninstall.registrations(native_registry)
                def unregister(command, **kwargs):
                    self.assertEqual(command[1:], ['--unregister', 'podman-primigenius-stable'])
                    native_registry.DeleteKey(native_registry.HKEY_CURRENT_USER, private + r'\{owned}')
                    self.disk.unlink()
                    return subprocess.CompletedProcess(command, 0)
                uninstall.remove_runtime(self.install, run=unregister)
                remaining, after_default = uninstall.registrations(native_registry)
                self.assertEqual(remaining, [entry for entry in before if entry['id'] != '{owned}'])
                self.assertEqual(after_default, default)
                self.assertTrue(self.dev_disk.is_file())
        finally:
            for name in sorted(created, key=len, reverse=True):
                try:
                    native_registry.DeleteKey(native_registry.HKEY_CURRENT_USER, name)
                except FileNotFoundError:
                    pass
            try:
                native_registry.DeleteKey(native_registry.HKEY_CURRENT_USER, r'Software\PrimiGenius\UninstallTests')
            except OSError:
                pass  # Another test may still own a child; never delete it.


class NativeUninstallerTests(unittest.TestCase):
    def setUp(self):
        cache = Path(os.environ['LOCALAPPDATA']) / 'electron-builder/Cache/nsis'
        self.compiler = cache / 'nsis-3.0.4.1/Bin/makensis.exe'
        if not self.compiler.is_file():
            self.skipTest('NSIS compiler unavailable')
        self.plugins = cache / 'nsis-resources-3.4.1/plugins/x86-unicode'
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='uninstall-hook-test-')
        self.addCleanup(self.cleanup)
        self.work = Path(self.temp.name)
        self.install = self.work / '安装 有空格'
        self.backend = self.install / 'resources/backend/app.exe'
        self.backend.parent.mkdir(parents=True)
        self.config = self.install / 'podman-config'
        self.config.mkdir()
        (self.install / 'PrimiGenius.exe').write_bytes(b'program')
        self.disk = self.install / 'podman-data/fake.vhdx'
        self.disk.parent.mkdir()
        self.disk.write_bytes(b'preserved unless verified by backend')

    def cleanup(self):
        # NSIS/Windows can retain the executable mapping briefly after exit.
        # Retry only this test's known TemporaryDirectory, never an install.
        for attempt in range(30):
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(0.1)

    def compile(self, source, destination):
        path = self.work / (destination.stem + '.nsi')
        path.write_text(source, encoding='utf-8')
        result = subprocess.run([str(self.compiler), '/V2', '/INPUTCHARSET', 'UTF8', str(path)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_uninstaller(self, *, keep=False, updated=False, helper_code=0, preview=False):
        marker = self.config / 'cleanup-ran.txt'
        self.compile(fr'''Unicode true
RequestExecutionLevel user
SilentInstall silent
OutFile "{self.backend}"
Section
IfFileExists "{self.install / 'PrimiGenius.exe'}" 0 failed
FileOpen $0 "{marker}" w
FileWrite $0 "backend ran before deletion"
FileClose $0
SetErrorLevel {helper_code}
Quit
failed:
SetErrorLevel 99
SectionEnd
''', self.backend)
        setup = self.work / 'setup.exe'
        uninstaller = self.work / 'uninstall.exe'
        self.compile(fr'''Unicode true
RequestExecutionLevel user
SilentInstall silent
SilentUnInstall {'normal' if preview else 'silent'}
OutFile "{setup}"
!addincludedir "{ROOT / 'node_modules/app-builder-lib/templates/nsis/include'}"
!addplugindir "{self.plugins}"
!include MUI2.nsh
!include LogicLib.nsh
!define BUILD_UNINSTALLER
!define PG_LEGACY_DATA_PROTECTION
!define isUpdated '"{int(updated)}" == "1"'
!include "{ROOT / 'build/installer.nsh'}"
!insertmacro customUnWelcomePage
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"
Function un.onInit
StrCpy $INSTDIR "{self.install}"
!insertmacro customUnInit
FunctionEnd
Section
WriteUninstaller "{uninstaller}"
SectionEnd
Section Uninstall
!insertmacro customRemoveFiles
SectionEnd
''', setup)
        result = subprocess.run([str(setup), '/S'], capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if preview:
            return uninstaller
        # _?= prevents NSIS's asynchronous self-copy; this executable is itself
        # a disposable fixture outside the directory being removed.
        command = f'"{uninstaller}" /S ' + ('/keep-runtime ' if keep else '') + f'_?={self.install}'
        result = subprocess.run(command, capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result, marker

    def test_default_cleanup_completes_before_program_deletion(self):
        result, marker = self.run_uninstaller()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.is_file())
        self.assertFalse(self.backend.exists())
        self.assertFalse((self.install / 'PrimiGenius.exe').exists())
        self.assertTrue(self.disk.is_file())  # The file remover never deletes disks.

    def test_unchecked_option_skips_runtime_helper(self):
        result, marker = self.run_uninstaller(keep=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.backend.exists())
        self.assertTrue(self.disk.is_file())

    def test_update_never_removes_runtime(self):
        result, marker = self.run_uninstaller(updated=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertTrue(self.disk.is_file())

    def test_failed_runtime_cleanup_blocks_program_deletion(self):
        result, marker = self.run_uninstaller(helper_code=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.is_file())
        self.assertTrue(self.backend.is_file())
        self.assertTrue((self.install / 'PrimiGenius.exe').is_file())
        self.assertTrue(self.disk.is_file())


if __name__ == '__main__':
    unittest.main()
