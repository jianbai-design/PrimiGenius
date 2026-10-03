; electron-builder expands the custom hooks in separate generated sections.
; NSIS cannot see the cross-hook reference during its unused-variable pass,
; although the variable is set in customInit and read in customInstall.
!pragma warning disable 6001
Var PreserveExistingEnvironment
!pragma warning default 6001

; The native finish page replaces Run with reboot choices when required.
!define MUI_FINISHPAGE_REBOOTLATER_DEFAULT
!define MUI_FINISHPAGE_TEXT_REBOOT "PrimiGenius 安装已完成。WSL / 虚拟机平台需要重启 Windows 后才能生效。$\r$\n$\r$\n请先保存正在进行的工作，重启电脑后再打开 PrimiGenius。"

!macro customCheckAppRunning
  DetailPrint "Closing PrimiGenius..."
  nsExec::Exec 'taskkill /F /IM "PrimiGenius.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "app.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "node.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "python.exe" >nul 2>&1'
  Pop $0
  Sleep 3000
!macroend

!macro preInit
  nsExec::Exec 'taskkill /F /IM "PrimiGenius.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "app.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "node.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "python.exe" >nul 2>&1'
  Pop $0
  Sleep 2000
!macroend

!macro customInit
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

  nsExec::Exec 'taskkill /F /IM "PrimiGenius.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "app.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "node.exe" >nul 2>&1'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "python.exe" >nul 2>&1'
  Pop $0
  Sleep 2000
!macroend

!macro customUnInstallCheck
  ClearErrors
  StrCpy $R0 0
!macroend

!macro customInstall
  DetailPrint "=== PrimiGenius Environment Setup ==="

  ; An application update must preserve the already working WSL/Podman
  ; environment. Re-running DISM/MSI during every update is slow and can leave
  ; an existing user in a pending-reboot state for no reason.
  ${If} ${isUpdated}
    DetailPrint "Existing installation detected; preserving WSL, Podman Machine, and all user data."
    Goto EnvironmentSetupDone
  ${EndIf}
  ${If} $PreserveExistingEnvironment == "1"
    DetailPrint "Existing Podman environment detected; preserving WSL, Podman Machine, and all user data."
    Goto EnvironmentSetupDone
  ${EndIf}

  ; === Step 1: Enable WSL feature ===
  DetailPrint "[1/4] Enabling WSL (Microsoft-Windows-Subsystem-Linux)..."
  nsExec::ExecToLog 'dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart'
  Pop $1
  DetailPrint "[1/4] WSL feature done (exit: $1)"
  ${If} $1 == 3010
    SetRebootFlag true
  ${EndIf}

  ; === Step 2: Enable VirtualMachinePlatform ===
  DetailPrint "[2/4] Enabling VirtualMachinePlatform..."
  nsExec::ExecToLog 'dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart'
  Pop $1
  DetailPrint "[2/4] VirtualMachinePlatform done (exit: $1)"
  ${If} $1 == 3010
    SetRebootFlag true
  ${EndIf}

  ; === Step 3: Check if WSL2 is already installed ===
  DetailPrint "[3/4] Checking if WSL2 is already installed..."
  nsExec::ExecToStack 'wsl --version'
  Pop $1
  Pop $2
  StrCpy $3 $2 3
  ${If} $3 == "WSL"
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
  ${EndIf}
  Goto WSLInstallDone

  InstallWSLFullRoot:
  DetailPrint "[3/4] Installing WSL 2.7.3 (full) from $INSTDIR\wsl.2.7.3.0.x64.msi..."
  nsExec::ExecToLog 'msiexec /i "$INSTDIR\wsl.2.7.3.0.x64.msi" /quiet /norestart'
  Pop $1
  DetailPrint "[3/4] WSL2 installed (exit: $1)"
  ${If} $1 == 3010
    SetRebootFlag true
  ${EndIf}

  WSLInstallDone:

  ; === Step 4: Set WSL default version to 2 ===
  DetailPrint "[4/4] Setting WSL default version to 2..."
  nsExec::ExecToLog 'wsl --set-default-version 2'
  Pop $1

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
!macroend

!macro customUnInstall
  ${if} ${isUpdated}
    Goto SkipOptionalCleanupPrompts
  ${endif}

  IfSilent SkipOptionalCleanupPrompts 0

  DetailPrint "Stopping Podman Machine..."
  nsExec::Exec '"$INSTDIR\podman\podman.exe" machine stop' /TIMEOUT=30000
  Pop $0
  nsExec::Exec '"$INSTDIR\resources\podman\podman.exe" machine stop' /TIMEOUT=30000
  Pop $0

  DetailPrint "Shutting down WSL..."
  nsExec::ExecToLog 'wsl --shutdown'

  DetailPrint "Removing Podman Machine..."
  nsExec::Exec '"$INSTDIR\podman\podman.exe" machine rm -f' /TIMEOUT=60000
  Pop $0
  nsExec::Exec '"$INSTDIR\resources\podman\podman.exe" machine rm -f' /TIMEOUT=60000
  Pop $0

  DetailPrint "Cleaning Podman data..."
  RMDir /r "$INSTDIR\podman-data"
  RMDir /r "$INSTDIR\podman-config"
  RMDir /r "$INSTDIR\podman"
  RMDir /r "$INSTDIR\resources\podman"

  SkipOptionalCleanupPrompts:
!macroend
