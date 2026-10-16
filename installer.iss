; Inno Setup script for notify-relay. Built by build.ps1 after PyInstaller.
; Per-user install: no admin prompt, nothing outside the user's profile.

#define AppName "notify-relay"
#define AppVersion "0.1"
#define Exe "notify-relay.exe"

[Setup]
AppId={{3cb2f112-6199-492e-a744-853ea4a39443}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
; DCompositionCreateDevice3 needs Windows 10 1809 (build 17763).
MinVersion=10.0.17763
OutputDir=dist
OutputBaseFilename={#AppName}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#Exe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "dist\notify-relay\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#Exe}"

[Run]
Filename: "{app}\{#Exe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall

[UninstallDelete]
; Never banners_backup.json: the app deletes it after a successful restore, so if it
; is still here the restore failed and the record is the only way back.
Type: files; Name: "{userappdata}\notify-relay\settings.json"
Type: files; Name: "{userappdata}\notify-relay\settings.json.tmp"
Type: files; Name: "{userappdata}\notify-relay\show-settings"
Type: dirifempty; Name: "{userappdata}\notify-relay"

[Code]
function RelayRunning(): Boolean;
var
  rc: Integer;
begin
  // find exits 0 only when the image name is in tasklist's output, in any language.
  Exec(ExpandConstant('{cmd}'), '/c tasklist /fi "imagename eq {#Exe}" /nh | find /i "{#Exe}"',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
  Result := rc = 0;
end;

procedure StopRelay();
var
  rc, i: Integer;
begin
  Exec('taskkill.exe', '/f /im {#Exe}', '', SW_HIDE, ewWaitUntilTerminated, rc);
  for i := 1 to 50 do
  begin
    if not RelayRunning() then
      Exit;
    Sleep(200);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRelay();      // an upgrade must not find its own files locked
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // 1. Stop the relay, so the restore below is not taken for a second launch.
    StopRelay();
    // 2. Put Windows' own popups back exactly as they were before the install.
    if (not Exec(ExpandConstant('{app}\{#Exe}'), '--banners restore', '', SW_HIDE,
                 ewWaitUntilTerminated, rc)) or (rc <> 0) then
      SuppressibleMsgBox('Windows'' own notification popups may still be turned off for ' +
        'some apps. To fix it, install notify-relay again and uninstall it once more, or ' +
        'turn them back on per app in Settings > System > Notifications.',
        mbInformation, MB_OK, IDOK);
    // 3. Autostart.
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run',
                   '{#AppName}');
  end;
end;
