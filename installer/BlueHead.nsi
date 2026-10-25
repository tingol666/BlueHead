Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"

!ifndef RELEASE_DIR
 !error "Define RELEASE_DIR as the assembled BlueHead directory"
!endif
!ifndef OUTPUT_FILE
 !error "Define OUTPUT_FILE"
!endif
!ifndef REMOVE_FILES
 !error "Define REMOVE_FILES as the generated exact-file uninstall list"
!endif
!ifndef APP_VERSION
 !error "Define APP_VERSION from the project version"
!endif
Name "BlueHead 2"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\BlueHead-2"
; Major migration deliberately does not reuse the 1.x InstallDir registry key.
; This candidate supports only new/empty destinations, never overlay upgrades.
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
BrandingText "BlueHead · 本机蓝牙音频中心"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "BlueHead"
VIAddVersionKey "FileDescription" "BlueHead ${APP_VERSION} 安装程序"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "BlueHead contributors"
!define MUI_ICON "..\bluehead\assets\bluehead.ico"
!define MUI_UNICON "..\bluehead\assets\bluehead.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\BlueHead.exe"
!define MUI_FINISHPAGE_RUN_TEXT "启动 BlueHead"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE DirectoryLeave
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; Read-only preflight. $0=0 permits a missing/empty directory; otherwise reject.
; Existing files, child directories, inaccessible targets and reparse targets
; are never removed or followed in order to make a destination acceptable.
Function CheckEmptyInstallDir
 Push $1
 Push $3
 StrCpy $3 1
 System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i.r0 ?e'
 Pop $1
 ${If} $0 == -1
  ${If} $1 == 2
  ${OrIf} $1 == 3
   StrCpy $3 0
  ${EndIf}
  Goto guard_done
 ${EndIf}
 IntOp $1 $0 & 0x10
 ${If} $1 == 0
  Goto guard_done
 ${EndIf}
 IntOp $1 $0 & 0x400
 ${If} $1 != 0
  Goto guard_done
 ${EndIf}
 ; Official FileFunc DirState: 0=empty, 1=full, -1=not enumerable.
 ${DirState} "$INSTDIR" $1
 ${If} $1 == 0
  StrCpy $3 0
 ${EndIf}
 guard_done:
 StrCpy $0 $3
 Pop $3
 Pop $1
FunctionEnd

Function DirectoryLeave
 Call CheckEmptyInstallDir
 ${If} $0 != 0
  MessageBox MB_OK|MB_ICONINFORMATION "BlueHead 2 当前仅支持安装到新的空文件夹。所选位置已有文件、子文件夹或无法安全检查，请选择另一个空目录。安装程序不会删除旧版或清理您的文件。" /SD IDOK
  Abort
 ${EndIf}
FunctionEnd

Function .onInit
 ${IfNot} ${RunningX64}
  MessageBox MB_OK|MB_ICONINFORMATION "此安装包适用于 Windows x64。" /SD IDOK
  Abort
 ${EndIf}
 ; net48 uses the Windows runtime rather than a bundled coreclr.
 SetRegView 64
 ReadRegDWORD $0 HKLM "SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" "Release"
 ${If} $0 < 528040
  MessageBox MB_OK|MB_ICONINFORMATION "BlueHead 需要 .NET Framework 4.8 或更高版本。Windows 10 22H2 / Windows 11 已自带；请先完成 Windows 更新。" /SD IDOK
  Abort
 ${EndIf}
 ${If} ${FileExists} "$INSTDIR\app\BlueHead.DriverHelper.exe"
  retry_install:
  nsExec::ExecToStack '"$INSTDIR\app\BlueHead.DriverHelper.exe" --desktop-running'
  Pop $0
  Pop $1
  ${If} $0 == 10
   MessageBox MB_RETRYCANCEL|MB_ICONINFORMATION "请先从系统托盘退出 BlueHead，再继续安装。" /SD IDCANCEL IDRETRY retry_install
   Abort
  ${EndIf}
 ${EndIf}
FunctionEnd

Section "BlueHead" Main
 ; Check again immediately before writing, including silent /D= installation.
 Call DirectoryLeave
 SetOutPath "$INSTDIR"
 File /r "${RELEASE_DIR}\*.*"
 WriteUninstaller "$INSTDIR\Uninstall.exe"
 CreateDirectory "$SMPROGRAMS\BlueHead2"
 CreateShortcut "$SMPROGRAMS\BlueHead2\BlueHead 2.lnk" "$INSTDIR\BlueHead.exe"
 CreateShortcut "$SMPROGRAMS\BlueHead2\恢复系统蓝牙.lnk" "$INSTDIR\BlueHead.exe" "--recovery"
 CreateShortcut "$SMPROGRAMS\BlueHead2\卸载 BlueHead 2.lnk" "$INSTDIR\Uninstall.exe"
 WriteRegStr HKCU "Software\BlueHead2" "InstallDir" "$INSTDIR"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "DisplayName" "BlueHead 2"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "DisplayVersion" "${APP_VERSION}"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "InstallLocation" "$INSTDIR"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "InstallLocation" "$INSTDIR"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "Publisher" "BlueHead contributors"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "UninstallString" '"$INSTDIR\Uninstall.exe"'
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "DisplayIcon" "$INSTDIR\BlueHead.exe"
 WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "NoModify" 1
 WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2" "NoRepair" 1
SectionEnd

Function un.onInit
 SetRegView 64
 SetRegView 64
 ${If} ${FileExists} "$INSTDIR\app\BlueHead.DriverHelper.exe"
  retry_close:
  nsExec::ExecToStack '"$INSTDIR\app\BlueHead.DriverHelper.exe" --desktop-running'
  Pop $0
  Pop $1
  ${If} $0 == 10
   MessageBox MB_RETRYCANCEL|MB_ICONINFORMATION "请先从系统托盘退出 BlueHead，再继续卸载。" /SD IDCANCEL IDRETRY retry_close
   Abort
  ${EndIf}
  nsExec::ExecToStack '"$INSTDIR\app\BlueHead.DriverHelper.exe" --needs-restore'
  Pop $0
  Pop $1
  ${If} $0 == 10
   MessageBox MB_YESNO|MB_ICONQUESTION "卸载前需要将专用蓝牙适配器恢复给 Windows。接下来会请求管理员授权，是否继续？" /SD IDNO IDYES restore_driver
   Abort
   restore_driver:
   nsExec::ExecToStack '"$INSTDIR\app\BlueHead.DriverHelper.exe" --restore-all-elevate'
   Pop $0
   Pop $1
   ${If} $0 != 0
    MessageBox MB_OK|MB_ICONINFORMATION "蓝牙驱动尚未恢复，卸载已取消。请使用 BlueHead 的恢复工具处理后再重试。" /SD IDOK
    Abort
   ${EndIf}
  ${ElseIf} $0 != 0
   MessageBox MB_OK|MB_ICONINFORMATION "无法检查蓝牙恢复状态，卸载已取消。" /SD IDOK
   Abort
  ${EndIf}
 ${EndIf}
FunctionEnd

; The Run value is shared with older/portable editions. Only an exact command
; owned by this installation may be removed; never delete another copy's entry.
Function un.IsOwnedAutostart
 StrCmp $0 '"$INSTDIR\app\BlueHead.exe" --tray' owned_autostart
 StrCmp $0 '"$INSTDIR\BlueHead.exe" --tray' owned_autostart
 StrCpy $0 0
 Return
 owned_autostart:
 StrCpy $0 1
FunctionEnd

Section "Uninstall"
 ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "BlueHead"
 Call un.IsOwnedAutostart
 ${If} $0 == 1
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "BlueHead"
 ${EndIf}
 ; A different 2.x folder may now own these entries. Preserve its registration.
 ReadRegStr $0 HKCU "Software\BlueHead2" "InstallDir"
 ${If} $0 == $INSTDIR
 ; A different 2.x folder may now own these entries. Preserve its registration.
 ReadRegStr $0 HKCU "Software\BlueHead2" "InstallDir"
 ${If} $0 == $INSTDIR
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead2"
  DeleteRegKey HKCU "Software\BlueHead2"
  Delete "$SMPROGRAMS\BlueHead2\BlueHead 2.lnk"
  Delete "$SMPROGRAMS\BlueHead2\恢复系统蓝牙.lnk"
  Delete "$SMPROGRAMS\BlueHead2\卸载 BlueHead 2.lnk"
  RMDir "$SMPROGRAMS\BlueHead2"
 ${EndIf}
 ${EndIf}
 ; Remove only files shipped by this build. User-created files are preserved.
 !include "${REMOVE_FILES}"
 Delete "$INSTDIR\Uninstall.exe"
 RMDir "$INSTDIR"
SectionEnd
