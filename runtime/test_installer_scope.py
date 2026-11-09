"""Execute the actual installer guard with a simulated Windows process inventory."""
import base64
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class InstallerScopeTests(unittest.TestCase):
    def check(self, inventory, expected):
        source = (ROOT / 'build/installer-process-check.ps1').read_text(encoding='utf-8')
        source = source[source.index("$ErrorActionPreference"):]
        harness = r"""$InstallDirectory = 'C:\Apps\PrimiGenius'
$InstallerProcessId = 999
function Get-CimInstance { INVENTORY }
function Get-Process {
    $p = [pscustomobject]@{}
    $p | Add-Member -MemberType ScriptMethod -Name CloseMainWindow -Value { $true }
    return $p
}
""".replace('INVENTORY', inventory) + source
        encoded = base64.b64encode(harness.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=15)
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)

    def test_unrelated_python_and_sibling_installation_are_ignored(self):
        self.check(r"[pscustomobject]@{ProcessId=1;ExecutablePath='C:\Python\python.exe'}; "
                   r"[pscustomobject]@{ProcessId=2;ExecutablePath='C:\Apps\PrimiGenius-other\app.exe'}", 0)

    def test_owned_backend_blocks_replacement_without_being_killed(self):
        self.check(r"[pscustomobject]@{ProcessId=3;ExecutablePath='C:\Apps\PrimiGenius\resources\backend\app.exe'}", 2)

    def test_owned_main_requests_normal_close_and_requires_recheck(self):
        self.check(r"[pscustomobject]@{ProcessId=4;ExecutablePath='c:\apps\primigenius\PrimiGenius.exe'}", 2)

    def test_installer_does_not_block_itself(self):
        self.check(r"[pscustomobject]@{ProcessId=999;ExecutablePath='C:\Apps\PrimiGenius\setup.exe'}", 0)

    def test_inventory_failure_is_not_treated_as_safe_to_replace(self):
        self.check("throw 'Access denied'", 3)


if __name__ == '__main__':
    unittest.main()
