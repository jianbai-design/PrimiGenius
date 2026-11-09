"""Execute data protection against disposable installation trees, never real installs."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import os

ROOT = Path(__file__).resolve().parents[1]


class InstallerDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='installer-data-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '安装 有空格'
        self.root.mkdir()
        self.expected = {'plugins/r/custom/config.json': b'custom plugin',
                         'r_libs_4/pkg/lib.so': b'compiled package', 'outputs/result.txt': b'result',
                         'podman-data/disk.vhdx': b'virtual disk', '.runtime.json': b'configuration',
                         'podman-config/machine-runtime.json': b'legacy runtime identity',
                         'logs/state.sqlite3': b'previous task and file token history',
                         'r_libs_r4_6_1-bioc3_23/custom/DESCRIPTION': b'Package: custom\nVersion: 1.0\n'}
        for name, content in self.expected.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        (self.root/'PrimiGenius.exe').write_bytes(b'program')
        (self.root/'resources').mkdir()
        (self.root/'resources/app.asar').write_bytes(b'program archive')

    def run_helper(self, mode, success=True, target=None):
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', str(ROOT/'build/installer-data.ps1'), '-Mode', mode, '-InstallDirectory', str(self.root)]
        if target:
            command += ['-TargetDirectory', str(target)]
        r = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(r.returncode == 0, success, r.stdout+r.stderr)

    def assert_data(self):
        for name, content in self.expected.items():
            self.assertEqual((self.root/name).read_bytes(), content)

    def test_build_wrapper_refuses_unprotected_invocation(self):
        result = subprocess.run(['node', str(ROOT/'build/electron-builder-wrapper.js'), '--win'],
                                capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected NSIS templates are required', result.stderr)

    def test_build_wrapper_refuses_original_destructive_templates(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='installer-work-') as directory:
            templates = Path(directory)/'nsis-templates'
            (templates/'include').mkdir(parents=True)
            shutil.copyfile(ROOT/'node_modules/app-builder-lib/templates/nsis/include/installUtil.nsh',
                            templates/'include/installUtil.nsh')
            result = subprocess.run(['node', str(ROOT/'build/electron-builder-wrapper.js'),
                                     '--pg-nsis-templates', str(templates), '--win'],
                                    capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Unsafe NSIS templates', result.stderr)

    def test_legacy_recursive_uninstall_then_restore(self):
        self.run_helper('Protect', target=self.root)
        self.assertFalse((self.root/'plugins').exists())
        shutil.rmtree(self.root)  # Simulate the actual old uninstaller.
        self.run_helper('Restore')
        self.assert_data()
        self.assertFalse(Path(str(self.root)+'.primigenius-preserved').exists())

    def test_new_uninstaller_preserves_all_nonprogram_entries(self):
        self.run_helper('RemoveProgram')
        self.assert_data()
        self.assertFalse((self.root/'resources').exists())

    def test_interrupted_upgrade_can_resume(self):
        self.run_helper('Protect')
        self.run_helper('Protect')
        self.run_helper('Restore')
        self.assert_data()

    def test_in_place_upgrade_leaves_locked_user_files_in_place(self):
        import ctypes
        handle = ctypes.windll.kernel32.CreateFileW(
            str(self.root/'podman-data/disk.vhdx'), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(handle, -1)
        try:
            self.run_helper('PrepareUpgrade', target=self.root)
            self.assert_data()
            self.assertFalse(Path(str(self.root)+'.primigenius-preserved').exists())
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def test_restore_conflict_never_overwrites_either_plugin(self):
        self.run_helper('Protect')
        (self.root/'plugins').mkdir()
        (self.root/'plugins/new.txt').write_bytes(b'new')
        self.run_helper('Restore', success=False)
        self.assertEqual((self.root/'plugins/new.txt').read_bytes(), b'new')
        backup = Path(str(self.root)+'.primigenius-preserved')
        self.assertEqual((backup/'data/plugins/r/custom/config.json').read_bytes(), b'custom plugin')

    def test_upgrade_to_another_location_is_blocked_before_deletion(self):
        self.run_helper('Protect', success=False, target=self.root.parent/'different')
        self.assert_data()

    def test_real_nsis_in_place_upgrade_skips_legacy_delete(self):
        compiler = Path(os.environ['LOCALAPPDATA'])/'electron-builder/Cache/nsis/nsis-3.0.4.1/Bin/makensis.exe'
        if not compiler.is_file():
            self.skipTest('Local NSIS compiler unavailable')
        work = Path(self.temp.name)
        # This root was created above, under this test's TemporaryDirectory.
        self.assertEqual(self.root.parent, work)
        old = work/'old.exe'
        from build.installer_templates import prepare
        templates = Path(prepare(ROOT, work))
        util = (templates/'include/installUtil.nsh').read_text(encoding='utf-8')
        start = util.index('  !insertmacro PGProtectLegacyData')
        guard = util[start:util.index('FunctionEnd', start)]
        self.assertNotIn('ExecWait', util)
        self.assertNotIn('old-uninstaller.exe', util)
        old_source = work/'old.nsi'
        old_source.write_text(f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
OutFile "{old}"
Section
RMDir /r "{self.root}"
SectionEnd
''', encoding='utf-8')
        new = work/'upgrade.exe'
        new_source = work/'upgrade.nsi'
        new_source.write_text(fr'''Unicode true
RequestExecutionLevel user
SilentInstall silent
OutFile "{new}"
!include LogicLib.nsh
!define PG_LEGACY_DATA_PROTECTION
!include "{ROOT / 'build/installer.nsh'}"
Var installationDir
Function Upgrade
StrCpy $INSTDIR "{self.root}"
StrCpy $installationDir "{self.root}"
{guard}
ExecWait '"{old}" /S' $R0
FunctionEnd
Section
Call Upgrade
!insertmacro customUnInstallCheck
FileOpen $0 "$INSTDIR\PrimiGenius.exe" w
FileWrite $0 "new application payload"
FileClose $0
FileOpen $0 "$INSTDIR\resources\app.asar" w
FileWrite $0 "new renderer payload"
FileClose $0
SectionEnd
''', encoding='utf-8')
        for source in (old_source, new_source):
            result = subprocess.run([str(compiler), '/V2', '/INPUTCHARSET', 'UTF8', str(source)],
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        result = subprocess.run([str(new), '/S'], capture_output=True, timeout=45,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assert_data()
        self.assertEqual((self.root/'PrimiGenius.exe').read_bytes(), b'new application payload')
        self.assertEqual((self.root/'resources/app.asar').read_bytes(), b'new renderer payload')


    def test_real_new_uninstaller_keeps_plugins_libraries_and_virtual_disk(self):
        compiler = Path(os.environ['LOCALAPPDATA'])/'electron-builder/Cache/nsis/nsis-3.0.4.1/Bin/makensis.exe'
        if not compiler.is_file():
            self.skipTest('Local NSIS compiler unavailable')
        work = Path(self.temp.name)
        source = work/'uninstall-test.nsi'
        executable = work/'setup.exe'
        uninstaller = work/'uninstall.exe'
        source.write_text(fr'''Unicode true
RequestExecutionLevel user
SilentInstall silent
SilentUnInstall silent
OutFile "{executable}"
!include LogicLib.nsh
!define PG_LEGACY_DATA_PROTECTION
!include "{ROOT / 'build/installer.nsh'}"
Function un.onInit
StrCpy $INSTDIR "{self.root}"
FunctionEnd
Section
WriteUninstaller "{uninstaller}"
SectionEnd
Section Uninstall
!insertmacro customRemoveFiles
SectionEnd
''', encoding='utf-8')
        commands = ([str(compiler), '/V2', '/INPUTCHARSET', 'UTF8', str(source)],
                    [str(executable), '/S'], f'"{uninstaller}" /S _?={self.root}')
        for command in commands:
            result = subprocess.run(command, capture_output=True, timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assert_data()
        self.assertFalse((self.root/'PrimiGenius.exe').exists())
        self.assertFalse((self.root/'resources').exists())


if __name__ == '__main__':
    unittest.main()
