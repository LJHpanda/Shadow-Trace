Unicode True
!include "MUI2.nsh"

!ifndef MyAppVersion
  !define MyAppVersion "0.0.0-dev"
!endif
!ifndef SourceDir
  !error "SourceDir must be provided with /DSourceDir=..."
!endif
!ifndef OutputFile
  !error "OutputFile must be provided with /DOutputFile=..."
!endif

!define APP_NAME "YingJi"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\YingJi"

Name "${APP_NAME}"
OutFile "${OutputFile}"
InstallDir "$LOCALAPPDATA\Programs\YingJi"
InstallDirRegKey HKCU "Software\YingJi" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "YingJi"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${SourceDir}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "YingJi" SEC_MAIN
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r "${SourceDir}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\YingJi" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${MyAppVersion}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "YingJi"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\YingJi.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  CreateDirectory "$SMPROGRAMS\YingJi"
  CreateShortcut "$SMPROGRAMS\YingJi\YingJi.lnk" "$INSTDIR\YingJi.exe"
  CreateShortcut "$SMPROGRAMS\YingJi\Uninstall YingJi.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$SMPROGRAMS\YingJi\YingJi.lnk"
  Delete "$SMPROGRAMS\YingJi\Uninstall YingJi.lnk"
  RMDir "$SMPROGRAMS\YingJi"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "Software\YingJi"
  RMDir /r "$INSTDIR"
SectionEnd
