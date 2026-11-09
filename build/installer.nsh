; electron-builder expands the custom hooks in separate generated sections.
; NSIS cannot see the cross-hook reference during its unused-variable pass,
; although the variable is set in customInit and read in customInstall.
!pragma warning disable 6001
Var PreserveExistingEnvironment
!pragma warning default 6001

; The native finish page replaces Run with reboot choices when required.
!define MUI_FINISHPAGE_REBOOTLATER_DEFAULT
!define MUI_FINISHPAGE_TEXT_REBOOT "PrimiGenius 安装已完成。WSL / 虚拟机平台需要重启 Windows 后才能生效。$\r$\n$\r$\n请先保存正在进行的工作，重启电脑后再打开 PrimiGenius。"

!define PG_INSTALLER_RESOURCE_DIR "${__FILEDIR__}"
!ifdef BUILD_UNINSTALLER
  !include "${PG_INSTALLER_RESOURCE_DIR}\installer-uninstall.nsh"
!endif
!ifndef PG_LEGACY_DATA_PROTECTION
  !error "Build through build_installer.bat: legacy upgrade data protection is required."
!endif
!macro PGProtectLegacyData
  Push $0
  Push $1
  InitPluginsDir
  File /oname=$PLUGINSDIR\primigenius-data.ps1 "${PG_INSTALLER_RESOURCE_DIR}\installer-data.ps1"
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\primigenius-data.ps1" -Mode PrepareUpgrade -InstallDirectory "$installationDir" -TargetDirectory "$INSTDIR"'
  Pop $1
  ${If} $1 != 0
    MessageBox MB_OK|MB_ICONSTOP "无法保护原有插件和数据，升级已停止。请保留原安装目录及相邻的 .primigenius-preserved 目录。$\r$\nUser data protection failed. Upgrade stopped; preserve the installation and its adjacent recovery directory." /SD IDOK
    Abort
  ${EndIf}
  Pop $1
  Pop $0
!macroend

!macro PGRestoreLegacyData
  Push $R0
  InitPluginsDir
  File /oname=$PLUGINSDIR\primigenius-data.ps1 "${PG_INSTALLER_RESOURCE_DIR}\installer-data.ps1"
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\primigenius-data.ps1" -Mode Restore -InstallDirectory "$INSTDIR"'
  Pop $R0
  ${If} $R0 != 0
    MessageBox MB_OK|MB_ICONSTOP "原有数据保留在安装目录旁的 .primigenius-preserved 目录中，恢复未完成，安装已停止。请勿删除该目录。$\r$\nData recovery is incomplete. Setup stopped; do not delete the adjacent .primigenius-preserved directory." /SD IDOK
    Abort
  ${EndIf}
  Pop $R0
!macroend

!macro customRemoveFiles
  !ifmacrodef PGRemoveRuntimeBeforeFiles
    !insertmacro PGRemoveRuntimeBeforeFiles
  !endif
  InitPluginsDir
  File /oname=$PLUGINSDIR\primigenius-data.ps1 "${PG_INSTALLER_RESOURCE_DIR}\installer-data.ps1"
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\primigenius-data.ps1" -Mode RemoveProgram -InstallDirectory "$INSTDIR"'
  Pop $R0
  ${If} $R0 != 0
    Abort "Program file removal failed; user data was preserved."
  ${EndIf}
!macroend
!macro customCheckAppRunning
  InitPluginsDir
  File /oname=$PLUGINSDIR\primigenius-process-check.ps1 "${PG_INSTALLER_RESOURCE_DIR}\installer-process-check.ps1"
  System::Call 'kernel32::GetCurrentProcessId() i .r1'
  PGCheckOwnedProcesses:
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\primigenius-process-check.ps1" -InstallDirectory "$INSTDIR" -InstallerProcessId $1'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "请先退出此安装目录中的 PrimiGenius（包括托盘中的后台任务），然后重试。安装程序不会强制结束任务或其他软件。$\r$\n$\r$\nPlease exit this PrimiGenius installation, including background tasks, then retry. Setup will not force-stop tasks or other applications." /SD IDCANCEL IDRETRY PGCheckOwnedProcesses
    SetErrorLevel 2
    Quit
  ${EndIf}
!macroend

!macro customInit
  ; Change only the fresh-install default. Keep registered upgrade paths and
  ; the explicit /D override selected by electron-builder's initMultiUser.
  ReadRegStr $0 HKLM "${INSTALL_REGISTRY_KEY}" InstallLocation
  ${If} $0 == ""
    ReadRegStr $0 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
  ${EndIf}
  ${StdUtils.GetParameter} $R0 "D" ""
  ${If} $0 == ""
  ${AndIf} $R0 == ""
    ; All-users shell context resolves LocalAppData to ProgramData. Switch
    ; briefly so the default belongs to the installing Windows user.
    SetShellVarContext current
    StrCpy $INSTDIR "$LOCALAPPDATA\${APP_FILENAME}"
    SetShellVarContext all
  ${EndIf}

  ; customInit runs before application files are replaced, so it can also
  ; recognize a manual in-place upgrade (not only electron-updater's
  ; --updated invocation).
  StrCpy $PreserveExistingEnvironment "0"
  IfFileExists "$INSTDIR\podman-config\machine-runtime.json" MarkExistingEnvironment
  IfFileExists "$INSTDIR\podman-data\*.*" MarkExistingEnvironment
  Goto ExistingEnvironmentChecked
  MarkExistingEnvironment:
  StrCpy $PreserveExistingEnvironment "1"
  ExistingEnvironmentChecked:

!macroend

!macro customUnInstallCheck
  Push $R1
  StrCpy $R1 0
  ${If} ${Errors}
    StrCpy $R1 1
  ${EndIf}
  !insertmacro PGRestoreLegacyData
  ${If} $R0 != 0
  ${OrIf} $R1 != 0
    Abort "Previous uninstaller failed; original data restored."
  ${EndIf}
  Pop $R1
  ClearErrors
!macroend

!macro customUnInstallCheckCurrentUser
  !insertmacro customUnInstallCheck
!macroend

!macro customInstall
  DetailPrint "=== PrimiGenius Environment Setup ==="

  !ifdef PG_JAVA_DIRECT_DELIVERY
    ; The same complete JRE is now shipped directly to the legacy runtime path.
    ; Remove only the old archive after the new payload has been installed.
    IfFileExists "$INSTDIR\java\jre\bin\java.exe" 0 JavaArchiveCleanupDone
    IfFileExists "$INSTDIR\java\jre\bin\javaw.exe" 0 JavaArchiveCleanupDone
    IfFileExists "$INSTDIR\java\jre\bin\server\jvm.dll" 0 JavaArchiveCleanupDone
    IfFileExists "$INSTDIR\java\jre\lib\rt.jar" 0 JavaArchiveCleanupDone
    Delete "$INSTDIR\resources\jre8.zip"
    JavaArchiveCleanupDone:
  !endif

  ; An application update must preserve the already working WSL/runtime
  ; environment. Re-running DISM/MSI during every update is slow and can leave
  ; an existing user in a pending-reboot state for no reason.
  ${If} ${isUpdated}
    DetailPrint "Existing installation detected; preserving WSL, the runtime, and all user data."
    Goto EnvironmentSetupDone
  ${EndIf}
  ${If} $PreserveExistingEnvironment == "1"
    DetailPrint "Existing runtime environment detected; preserving WSL, the runtime, and all user data."
    Goto EnvironmentSetupDone
  ${EndIf}

  ; A working system needs no DISM or MSI work. First-run import remains app-owned.
  nsExec::ExecToStack 'wsl --status'
  Pop $1
  Pop $2
  ${If} $1 == 0
    DetailPrint "WSL is available; reusing installed system components."
    Goto CreateRuntimeDirectories
  ${EndIf}

  ; === Step 1: Enable WSL feature ===
  DetailPrint "[1/4] Enabling WSL (Microsoft-Windows-Subsystem-Linux)..."
  nsExec::ExecToLog 'dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart'
  Pop $1
  DetailPrint "[1/4] WSL feature done (exit: $1)"
  ${If} $1 == 3010
    SetRebootFlag true
  ${ElseIf} $1 != 0
    DetailPrint "Environment setup needs attention (exit: $1)."
    MessageBox MB_OK|MB_ICONEXCLAMATION "运行环境准备未完成（错误码 $1）。请保留安装日志；首次启动会重新检查并显示恢复步骤。$\r$\nEnvironment setup is incomplete (code $1). Keep the installer log; the app will check again on first launch." /SD IDOK
    Goto EnvironmentSetupDone
  ${EndIf}

  ; === Step 2: Enable VirtualMachinePlatform ===
  DetailPrint "[2/4] Enabling VirtualMachinePlatform..."
  nsExec::ExecToLog 'dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart'
  Pop $1
  DetailPrint "[2/4] VirtualMachinePlatform done (exit: $1)"
  ${If} $1 == 3010
    SetRebootFlag true
  ${ElseIf} $1 != 0
    DetailPrint "Environment setup needs attention (exit: $1)."
    MessageBox MB_OK|MB_ICONEXCLAMATION "运行环境准备未完成（错误码 $1）。请保留安装日志；首次启动会重新检查并显示恢复步骤。$\r$\nEnvironment setup is incomplete (code $1). Keep the installer log; the app will check again on first launch." /SD IDOK
    Goto EnvironmentSetupDone
  ${EndIf}

  ; === Step 3: Check if WSL2 is already installed ===
  DetailPrint "[3/4] Checking if WSL2 is already installed..."
  nsExec::ExecToStack 'wsl --version'
  Pop $1
  Pop $2
  ${If} $1 == 0
    DetailPrint "[3/4] WSL2 is already installed on this system, skipping MSI installation."
    Goto WSLInstallDone
  ${EndIf}

  ; === Step 3b: Install WSL2 (full installation from built MSI) ===
  DetailPrint "[3/4] WSL2 not found, installing from bundled MSI..."
  IfFileExists "$INSTDIR\resources\wsl.2.7.3.0.x64.msi" InstallWSLFull 0
  IfFileExists "$INSTDIR\wsl.2.7.3.0.x64.msi" InstallWSLFullRoot 0
  DetailPrint "[3/4] WSL2 MSI not found, will install via wsl --install on first launch"
  Goto WSLInstallDone

  InstallWSLFull:
  DetailPrint "[3/4] Installing WSL 2.7.3 (full) from $INSTDIR\resources\wsl.2.7.3.0.x64.msi..."
  nsExec::ExecToLog 'msiexec /i "$INSTDIR\resources\wsl.2.7.3.0.x64.msi" /quiet /norestart'
  Pop $1
  DetailPrint "[3/4] WSL2 installed (exit: $1)"
  ${If} $1 == 3010
    SetRebootFlag true
  ${ElseIf} $1 != 0
    DetailPrint "Environment setup needs attention (exit: $1)."
    MessageBox MB_OK|MB_ICONEXCLAMATION "运行环境准备未完成（错误码 $1）。请保留安装日志；首次启动会重新检查并显示恢复步骤。$\r$\nEnvironment setup is incomplete (code $1). Keep the installer log; the app will check again on first launch." /SD IDOK
    Goto EnvironmentSetupDone
  ${EndIf}
  Goto WSLInstallDone

  InstallWSLFullRoot:
  DetailPrint "[3/4] Installing WSL 2.7.3 (full) from $INSTDIR\wsl.2.7.3.0.x64.msi..."
  nsExec::ExecToLog 'msiexec /i "$INSTDIR\wsl.2.7.3.0.x64.msi" /quiet /norestart'
  Pop $1
  DetailPrint "[3/4] WSL2 installed (exit: $1)"
  ${If} $1 == 3010
    SetRebootFlag true
  ${ElseIf} $1 != 0
    DetailPrint "Environment setup needs attention (exit: $1)."
    MessageBox MB_OK|MB_ICONEXCLAMATION "运行环境准备未完成（错误码 $1）。请保留安装日志；首次启动会重新检查并显示恢复步骤。$\r$\nEnvironment setup is incomplete (code $1). Keep the installer log; the app will check again on first launch." /SD IDOK
    Goto EnvironmentSetupDone
  ${EndIf}

  WSLInstallDone:

  ; Dedicated environments are imported with --version 2 by the backend.
  ; Do not change the user's global WSL default.

  CreateRuntimeDirectories:
  ; === Create data directories ===
  CreateDirectory "$INSTDIR\podman-data"
  CreateDirectory "$INSTDIR\podman-config"
  CreateDirectory "$INSTDIR\plugins"
  CreateDirectory "$INSTDIR\plugins\r"
  CreateDirectory "$INSTDIR\plugins\linux"
  CreateDirectory "$INSTDIR\r_libs"
  CreateDirectory "$INSTDIR\outputs"

  DetailPrint "=== Environment Setup Complete ==="

  EnvironmentSetupDone:
  ; Only the checksum-verified, non-executable archive staging folder is writable.
  ; Keep the manifest and application binaries protected on all-users installs.
  IfFileExists "$INSTDIR\resources\r-environment\stage\environment.tar.gz" RArchivePermissions 0
  IfFileExists "$INSTDIR\resources\r-environment\stage\environment.tar.xz" 0 RArchivePermissionsDone
  RArchivePermissions:
  nsExec::ExecToLog '"$SYSDIR\icacls.exe" "$INSTDIR\resources\r-environment\stage" /grant "*S-1-5-32-545:(OI)(CI)M"'
  Pop $0
  ${If} $0 != 0
    DetailPrint "R archive staging permissions could not be set; automatic cleanup may require repair."
  ${EndIf}
  RArchivePermissionsDone:
  IfRebootFlag ROfflineDeferred 0
  DetailPrint "Installing the bundled R environment for the current user (offline)..."
  Delete "$INSTDIR\resources\r-environment\stage\install-result.txt"
  ${StdUtils.ExecShellAsUser} $0 "$INSTDIR\resources\backend\app.exe" "open" "--prepare-offline-r"
  ${If} $0 == "error"
    Goto ROfflineFailed
  ${EndIf}
  File /oname=$PLUGINSDIR\primigenius-r-wait.ps1 "${PG_INSTALLER_RESOURCE_DIR}\installer-r-wait.ps1"
  nsExec::ExecToLog /TIMEOUT=1860000 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\primigenius-r-wait.ps1" -ResultFile "$INSTDIR\resources\r-environment\stage\install-result.txt"'
  Pop $0
  ${If} $0 != 0
    Goto ROfflineFailed
  ${EndIf}
  DetailPrint "Offline R environment verified and ready."
  Goto ROfflineDone
  ROfflineDeferred:
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\RunOnce" "PrimiGeniusOfflineR" '$\"$INSTDIR\resources\backend\app.exe$\" --prepare-offline-r'
  DetailPrint "Windows restart required; offline R setup will resume after sign-in."
  Goto ROfflineDone
  ROfflineFailed:
  MessageBox MB_OK|MB_ICONEXCLAMATION "R 运行环境准备失败。请查看安装详细日志及 resources\r-environment\stage\install-result.txt。若提示 ext4.vhdx 或 WSL 存储路径不存在，应先从旧安装目录或备份恢复数据；重复安装不能恢复已删除的虚拟磁盘。请勿重置环境或删除 .primigenius-preserved 备份目录。$\r$\nR environment preparation failed. Check the installation details and install-result.txt. If the WSL disk or storage path is missing, restore the original data from backup first. Reinstalling cannot recover a deleted virtual disk. Do not reset the runtime or delete recovery backups." /SD IDOK
  SetErrorLevel 2
  ROfflineDone:
!macroend

!macro customUnInstall
  ; Runtime cleanup already completed (or was explicitly skipped) before files
  ; were removed. Never invoke a deleted backend from this final hook.
!macroend
