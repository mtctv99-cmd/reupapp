; Inno Setup Script for Sever Dub Studio Pro (Ultra-Lite)
#define MyAppName "Sever Dub Studio Pro"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "Sever Studio"
#define MyAppURL "https://github.com/mtctv99-cmd/sever-studio-releases"
#define MyAppExeName "SeverDubStudioPro.exe"

#ifndef OutputBaseFilename
  #define OutputBaseFilename "SeverDubStudioPro_Setup_v" + MyAppVersion
#endif

[Setup]
AppId={{E8F5A820-A2F7-4E90-B876-88E4B0C729E1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename={#OutputBaseFilename}
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
AppMutex=SeverDubStudioPro_SingleInstance_Mutex
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\SeverDubStudioPro\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
// Kiểm tra Visual C++ 2015-2022 Redistributable (x64)
function IsVCRedistInstalled: Boolean;
var
  Installed: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1);
  if not Result then
    Result := RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1);
end;

// Kiểm tra Microsoft Edge WebView2 Runtime
function IsWebView2Installed: Boolean;
var
  Pv: String;
begin
  Result := RegQueryStringValue(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-F6E3A6305F3D}', 'pv', Pv) and (Pv <> '');
  if not Result then
    Result := RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-F6E3A6305F3D}', 'pv', Pv) and (Pv <> '');
  if not Result then
    Result := RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-F6E3A6305F3D}', 'pv', Pv) and (Pv <> '');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
  begin
    if not IsVCRedistInstalled then
    begin
      if MsgBox('Ung dung yeu cau Microsoft Visual C++ 2015-2022 Redistributable (x64).'#13#10 +
                'Neu chua cai dat, app co the khong the khoi chay.'#13#10#13#10 +
                'Ban co muon tiep tuc cai dat khong?', mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;
    if not IsWebView2Installed then
    begin
      if MsgBox('Ung dung yeu cau Microsoft Edge WebView2 Evergreen Runtime de hien thi giao dien.'#13#10 +
                'Neu may chua co WebView2, app co the tu mo bang trinh duyet mac dinh.'#13#10#13#10 +
                'Ban co muon tiep tuc cai dat khong?', mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;
end;

