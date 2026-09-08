; Сценарий Inno Setup создаёт стандартную установленную версию Shalost FOTUR
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{A8C061E6-7B6E-49A8-966B-93F4DF031B11}
AppName=Shalost FOTUR
AppVersion={#MyAppVersion}
AppPublisher=FOTUR
AppPublisherURL=https://fotur.tech
DefaultDirName={autopf}\Shalost FOTUR
DefaultGroupName=Shalost FOTUR
UninstallDisplayIcon={app}\Shalost-FOTUR.exe
OutputDir=releases
OutputBaseFilename=Shalost-FOTUR-Setup-{#MyAppVersion}
SetupIconFile=assets\shalost-fotur.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\Shalost-FOTUR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Shalost FOTUR"; Filename: "{app}\Shalost-FOTUR.exe"
Name: "{autodesktop}\Shalost FOTUR"; Filename: "{app}\Shalost-FOTUR.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"

[Run]
Filename: "{app}\Shalost-FOTUR.exe"; Description: "Запустить Shalost FOTUR"; Flags: nowait postinstall skipifsilent
