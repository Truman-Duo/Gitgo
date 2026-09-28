#ifndef StageDir
  #error StageDir must point at the staged terminal release
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifndef ProductName
  #define ProductName "Gitgo"
#endif
#ifndef ProductId
  #define ProductId "gitgo-terminal"
#endif
#ifndef PrimaryCommand
  #define PrimaryCommand "gitgo"
#endif

[Setup]
AppId={{A36173A9-1621-40B6-9918-7E4FA277CE62}
AppName={#ProductName}
AppVersion=0.1.0
DefaultDirName={localappdata}\Programs\{#ProductId}
DefaultGroupName={#ProductName}
OutputDir={#OutputDir}
OutputBaseFilename={#PrimaryCommand}-setup
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayIcon={app}\{#PrimaryCommand}.exe
WizardStyle=modern

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#ProductName}"; Filename: "{app}\{#PrimaryCommand}.exe"
Name: "{group}\Uninstall {#ProductName}"; Filename: "{uninstallexe}"

[Registry]
; App Paths helps Windows shells find the product even before a newly changed
; PATH is inherited by an already-open terminal.  The ordinary terminal
; command is provided by the exact user PATH entry managed in [Code].
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\{#PrimaryCommand}.exe"; ValueType: string; ValueName: ""; ValueData: "{app}\{#PrimaryCommand}.exe"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\{#PrimaryCommand}.exe"; ValueType: string; ValueName: "Path"; ValueData: "{app}"; Flags: uninsdeletekey

[UninstallDelete]
; Configuration and encrypted provider credentials are application-owned.
; Runtime/session databases are intentionally retained, and project paths or
; project-local .gitgo/.git/gitgo metadata are never traversed.
Type: files; Name: "{userprofile}\.gitgo\config.json"
Type: files; Name: "{userprofile}\.gitgo\commit-config.json"
Type: files; Name: "{userprofile}\.gitgo\provider_secrets.json"

[Code]
const
  EnvironmentKey = 'Environment';

function NormalizePathEntry(Value: String): String;
begin
  Result := Lowercase(RemoveBackslashUnlessRoot(Trim(Value)));
end;

function ContainsPathEntry(PathValue, Entry: String): Boolean;
var
  Remaining, Item: String;
  Separator: Integer;
begin
  Result := False;
  Remaining := PathValue;
  while Remaining <> '' do begin
    Separator := Pos(';', Remaining);
    if Separator = 0 then begin
      Item := Remaining;
      Remaining := '';
    end else begin
      Item := Copy(Remaining, 1, Separator - 1);
      Delete(Remaining, 1, Separator);
    end;
    if NormalizePathEntry(Item) = NormalizePathEntry(Entry) then begin
      Result := True;
      Exit;
    end;
  end;
end;

procedure AddUserPathEntry(Entry: String);
var
  PathValue: String;
begin
  if not RegQueryStringValue(HKCU, EnvironmentKey, 'Path', PathValue) then
    PathValue := '';
  if ContainsPathEntry(PathValue, Entry) then
    Exit;
  if (PathValue <> '') and (PathValue[Length(PathValue)] <> ';') then
    PathValue := PathValue + ';';
  if not RegWriteExpandStringValue(HKCU, EnvironmentKey, 'Path', PathValue + Entry) then
    RaiseException('Unable to add the terminal command to the current user PATH.');
end;

procedure RemoveUserPathEntry(Entry: String);
var
  PathValue, Remaining, Item, Updated: String;
  Separator: Integer;
begin
  if not RegQueryStringValue(HKCU, EnvironmentKey, 'Path', PathValue) then
    Exit;
  Remaining := PathValue;
  Updated := '';
  while Remaining <> '' do begin
    Separator := Pos(';', Remaining);
    if Separator = 0 then begin
      Item := Remaining;
      Remaining := '';
    end else begin
      Item := Copy(Remaining, 1, Separator - 1);
      Delete(Remaining, 1, Separator);
    end;
    if (Trim(Item) <> '') and
       (NormalizePathEntry(Item) <> NormalizePathEntry(Entry)) then begin
      if Updated <> '' then Updated := Updated + ';';
      Updated := Updated + Item;
    end;
  end;
  RegWriteExpandStringValue(HKCU, EnvironmentKey, 'Path', Updated);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddUserPathEntry(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveUserPathEntry(ExpandConstant('{app}'));
end;
