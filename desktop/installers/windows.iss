#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppSource
  #error AppSource is required
#endif
#ifndef ArtifactDir
  #error ArtifactDir is required
#endif
[Setup]
AppId={{7F22CD18-F02C-4FDB-971A-6D97543C6FD2}
AppName=WebCompare
AppVersion={#AppVersion}
AppPublisher=fenghansen
AppPublisherURL=https://github.com/fenghansen/WebCompare
DefaultDirName={localappdata}\Programs\WebCompare
DefaultGroupName=WebCompare
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#ArtifactDir}
OutputBaseFilename=webcompare-desktop-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\WebCompare.exe
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Excludes: "prerequisites\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#AppSource}\prerequisites\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: dontcopy
[InstallDelete]
Type: files; Name: "{app}\_internal\vcruntime140.dll"
Type: files; Name: "{app}\_internal\vcruntime140_1.dll"
Type: files; Name: "{app}\_internal\numpy.libs\msvcp140-*.dll"
[Icons]
Name: "{group}\WebCompare"; Filename: "{app}\WebCompare.exe"
Name: "{autodesktop}\WebCompare"; Filename: "{app}\WebCompare.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\WebCompare.exe"; Description: "Launch WebCompare"; Flags: nowait postinstall skipifsilent; Check: HasWebView2
[Code]
function HasWebView2: Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := ((RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
             (RegQueryStringValue(HKLM32, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
             (RegQueryStringValue(HKLM64, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')));
end;

function InitializeSetup: Boolean;
var
  Release: Cardinal;
  FileVersion: Int64;
  Runtimes: TArrayOfString;
  I: Integer;
begin
  Result := RegQueryDWordValue(HKLM32, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release) and (Release >= 394802);
  if not Result then begin
    MsgBox('WebCompare requires .NET Framework 4.6.2 or later. Install the Windows updates for .NET and run this installer again.', mbError, MB_OK);
    Exit;
  end;
  SetArrayLength(Runtimes, 3);
  Runtimes[0] := 'vcruntime140.dll';
  Runtimes[1] := 'vcruntime140_1.dll';
  Runtimes[2] := 'msvcp140.dll';
  for I := 0 to GetArrayLength(Runtimes) - 1 do begin
    Result := GetPackedVersion(ExpandConstant('{sysnative}\') + Runtimes[I], FileVersion) and
              (ComparePackedVersion(FileVersion, PackVersionComponents(14, 44, 35211, 0)) >= 0);
    if not Result then begin
      SuppressibleMsgBox('Install Microsoft Visual C++ v14 Redistributable (x64), version 14.44.35211 or newer, and run Setup again.' + #13#10 +
                         'Download it from Microsoft at https://aka.ms/vc14/vc_redist.x64.exe', mbError, MB_OK, IDOK);
      Exit;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  Result := '';
  if HasWebView2 then
    Exit;
  WizardForm.StatusLabel.Caption := 'Installing Microsoft Edge WebView2 Runtime for this user...';
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then begin
    Result := 'Could not start Microsoft WebView2 Runtime installation. Error: ' + IntToStr(ResultCode) + '. Download the Evergreen Runtime from https://developer.microsoft.com/microsoft-edge/webview2/ and try again.';
    Exit;
  end;
  for Attempt := 1 to 60 do begin
    if HasWebView2 then
      Exit;
    Sleep(500);
  end;
  Result := 'Microsoft WebView2 Runtime installation did not finish successfully. Exit code: ' + IntToStr(ResultCode) + '. Check the Internet connection, or use the offline Evergreen Standalone Runtime from https://developer.microsoft.com/microsoft-edge/webview2/ and try again.';
end;
