Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

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
Name "BlueHead"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\BlueHead"
InstallDirRegKey HKCU "Software\BlueHead" "InstallDir"
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
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
 ${IfNot} ${RunningX64}
  MessageBox MB_OK|MB_ICONINFORMATION "此安装包适用于 Windows x64。" /SD IDOK
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
 SetOutPath "$INSTDIR"
 File /r "${RELEASE_DIR}\*.*"
 WriteUninstaller "$INSTDIR\Uninstall.exe"
 CreateDirectory "$SMPROGRAMS\BlueHead"
 CreateShortcut "$SMPROGRAMS\BlueHead\BlueHead.lnk" "$INSTDIR\BlueHead.exe"
 CreateShortcut "$SMPROGRAMS\BlueHead\恢复系统蓝牙.lnk" "$INSTDIR\BlueHead.exe" "--recovery"
 CreateShortcut "$SMPROGRAMS\BlueHead\卸载 BlueHead.lnk" "$INSTDIR\Uninstall.exe"
 WriteRegStr HKCU "Software\BlueHead" "InstallDir" "$INSTDIR"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead" "DisplayName" "BlueHead"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead" "DisplayVersion" "${APP_VERSION}"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead" "Publisher" "BlueHead contributors"
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead" "UninstallString" '"$INSTDIR\Uninstall.exe"'
 WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead" "DisplayIcon" "$INSTDIR\BlueHead.exe"
 WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead" "NoModify" 1
 WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead" "NoRepair" 1
SectionEnd

Function un.onInit
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

Section "Uninstall"
 DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "BlueHead"
 DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlueHead"
 DeleteRegKey HKCU "Software\BlueHead"
 Delete "$SMPROGRAMS\BlueHead\BlueHead.lnk"
 Delete "$SMPROGRAMS\BlueHead\恢复系统蓝牙.lnk"
 Delete "$SMPROGRAMS\BlueHead\卸载 BlueHead.lnk"
 RMDir "$SMPROGRAMS\BlueHead"
 ; Remove only files shipped by this build. User-created files are preserved.
 !include "${REMOVE_FILES}"
 Delete "$INSTDIR\Uninstall.exe"
 RMDir "$INSTDIR"
SectionEnd
