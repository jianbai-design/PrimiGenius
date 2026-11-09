"""Replace legacy uninstallation with a checked, in-place upgrade."""
import shutil


def prepare(root, output):
    templates = root / 'node_modules/app-builder-lib/templates/nsis'
    protected_templates = output / 'nsis-templates'
    shutil.copytree(templates, protected_templates)
    util = (templates / 'include/installUtil.nsh').read_text(encoding='utf-8')
    function = 'Function uninstallOldVersion\n'
    anchor = '  ${if} $installMode == "CurrentUser"\n'
    if util.count(function) != 1 or util.count(anchor) != 1:
        raise RuntimeError('Legacy uninstall template changed; data-protection hook needs review')
    begin = util.index(anchor, util.index(function))
    end = util.index('\nFunctionEnd', begin)
    # Remove both the copied-uninstaller and in-place fallback, including
    # retries. No installer may invoke code that recursively deletes user data.
    util = util[:begin] + ('  !insertmacro PGProtectLegacyData\n'
                          '  ClearErrors\n  StrCpy $R0 0\n  Return\n') + util[end:]
    util = util.replace('  Var /Global uninstallerFileNameTemp\n', '')
    if 'ExecWait' in util or 'old-uninstaller.exe' in util:
        raise RuntimeError('Unsafe legacy uninstaller invocation remains in NSIS templates')
    util_path = protected_templates / 'include/installUtil.nsh'
    util_path.write_text(util, encoding='utf-8')
    (output / 'installer-hooks.nsh').write_text(
        '!define PG_LEGACY_DATA_PROTECTION\n!include "' + str(root / 'build/installer.nsh') + '"\n', encoding='utf-8')
    return str(protected_templates)
