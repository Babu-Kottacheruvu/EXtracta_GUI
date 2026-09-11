; Inno Setup script -> produces a proper Setup.exe (Start Menu shortcut,
; uninstaller, Program Files install) around the PyInstaller build.
;
; 1. Install Inno Setup: https://jrsoftware.org/isinfo.php
; 2. Build the client first: .\build_client.ps1  (produces dist\EXtracta.exe)
; 3. Open this file in Inno Setup and click Compile (or: ISCC installer.iss)
; Output: Output\EXtracta-Setup.exe

[Setup]
AppName=EXtracta
AppVersion=1.0
DefaultDirName={autopf}\EXtracta
DefaultGroupName=EXtracta
OutputDir=Output
OutputBaseFilename=EXtracta-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64

[Files]
Source: "dist\EXtracta.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\EXtracta"; Filename: "{app}\EXtracta.exe"
Name: "{autodesktop}\EXtracta"; Filename: "{app}\EXtracta.exe"

[Run]
Filename: "{app}\EXtracta.exe"; Description: "Launch EXtracta"; Flags: nowait postinstall skipifsilent
