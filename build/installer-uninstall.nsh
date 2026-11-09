; Included only when compiling the uninstaller. UAC calls keep HKCU/WSL owned
; by the original user even when another administrator authorizes file removal.
!include nsDialogs.nsh
!include UAC.nsh
!include FileFunc.nsh

Var PGRemoveRuntime
Var PGRuntimeCheckbox

!macro customUnInit
  StrCpy $PGRemoveRuntime 1
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/keep-runtime" $R1
  ${IfNot} ${Errors}
    StrCpy $PGRemoveRuntime 0
  ${EndIf}
  ${If} ${isUpdated}
    StrCpy $PGRemoveRuntime 0
  ${EndIf}
!macroend

!macro customUnWelcomePage
  !insertmacro MUI_UNPAGE_WELCOME
  !insertmacro PGUninstallFunctions
  UninstPage custom un.PGRuntimePage un.PGRuntimePageLeave
!macroend

; electron-builder registers plug-in directories after loading the custom
; include. Expand functions with the pages, when StdUtils/UAC are available.
!macro PGUninstallFunctions
Function un.PGRuntimePage
  ${If} ${isUpdated}
    Abort
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "运行环境清理" "选择是否移除本安装使用的 WSL 发行版"
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}
  ${NSD_CreateCheckbox} 0 8u 100% 28u "移除本安装使用的 WSL 发行版（default 或 stable）"
  Pop $PGRuntimeCheckbox
  ${NSD_SetState} $PGRuntimeCheckbox $PGRemoveRuntime
  ${NSD_CreateLabel} 0 46u 100% 66u "勾选后将删除该发行版内的镜像、容器和 R 环境。取消勾选可保留环境供重新安装后使用。$\r$\n$\r$\n开发版、Ubuntu、其他软件的发行版及 Windows 的 WSL 组件将保留。"
  Pop $0
  nsDialogs::Show
FunctionEnd

Function un.PGRuntimePageLeave
  ${NSD_GetState} $PGRuntimeCheckbox $PGRemoveRuntime
FunctionEnd

Function un.PGRemoveRuntimeAsUser
  ; UAC_SYNCINSTDIR supplies the exact target installation to the user process.
  ; Execute synchronously, before customRemoveFiles removes the backend.
  nsExec::ExecToStack /TIMEOUT=150000 '"$INSTDIR\resources\backend\app.exe" --uninstall-runtime'
  Pop $0
  Pop $1
FunctionEnd

!macroend

!macro PGRemoveRuntimeBeforeFiles
  ${IfNot} ${isUpdated}
  ${AndIf} $PGRemoveRuntime == 1
    DetailPrint "Removing only the WSL distribution owned by this installation..."
    ${If} ${UAC_IsInnerInstance}
      !insertmacro UAC_AsUser_Call Function un.PGRemoveRuntimeAsUser ${UAC_SYNCREGISTERS}|${UAC_SYNCINSTDIR}
    ${Else}
      Call un.PGRemoveRuntimeAsUser
    ${EndIf}
    ${If} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "WSL 发行版清理未完成，程序文件已保留。请查看 $INSTDIR\podman-config\uninstall-result.txt。无法确认归属的环境不会被删除。$\r$\nWSL cleanup did not complete. Program files were preserved; check podman-config\uninstall-result.txt." /SD IDOK
      SetErrorLevel 2
      Abort
    ${EndIf}
  ${EndIf}
!macroend
