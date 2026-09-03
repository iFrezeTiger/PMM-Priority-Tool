; Inno Setup script for PMM Priority Tool.
; Per-user install (no admin rights needed) so pmm_libraries.json stays
; writable next to the exe, matching the app's existing config behavior.
; Build the PyInstaller onedir folder first (dist\PMM_Priority_Tool), then
; compile this with Inno Setup (ISCC.exe installer.iss).

#define MyAppName "PMM Priority Tool"
#define MyAppVersion "1.0.3"
#define MyAppExeName "PMM_Priority_Tool.exe"
#define MyAppSourceDir "dist\PMM_Priority_Tool"

[Setup]
AppId={{6C8F0B9E-6F0F-4C5C-9B36-3C7B9C6C7C10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Installer
OutputBaseFilename=PMM_Priority_Tool_Setup
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Excludes: "pmm_libraries.json"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\pmm_libraries.json"
