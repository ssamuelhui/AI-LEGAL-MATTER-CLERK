; Matter Clerk -- Windows installer (Phase 3 Session 5)
;
; Per-user install, no administrator rights required. Corporate laptops
; frequently prohibit admin installs; a per-user install works everywhere and
; never raises a UAC prompt.
;
; Build with installer\build_installer.ps1, which runs ISCC against this file
; after build_windows.ps1 has produced dist\MatterClerk\.

#define AppName        "Matter Clerk"
#define AppVersion     "1.0.3"
#define AppPublisher   "Samuel"
#define AppExeName     "MatterClerk.exe"
#define SourceDir      "..\dist\MatterClerk"

[Setup]
AppId={{7B3F2A64-5C1E-4E86-9A2D-3F5C8B1D0E47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}

; Per-user install. PrivilegesRequired=lowest keeps UAC out of the picture
; entirely; the commandline override exists so a future corporate/IT rollout
; can force a machine-wide install without editing this script.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline

; {localappdata}\Programs\MatterClerk -- deliberately NOT the data directory,
; which is {localappdata}\MatterClerk. Application code and privileged client
; data stay separate, so an uninstall can remove one without touching the other.
DefaultDirName={localappdata}\Programs\MatterClerk
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

LicenseFile=license.txt
OutputDir=output
OutputBaseFilename=MatterClerk-Setup
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Registers the uninstaller in Settings > Apps (Add/Remove Programs).
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; GroupDescription: "Shortcuts:"
Name: "desktopicon";   Description: "Create a &desktop shortcut";    GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The whole PyInstaller onedir output, including _internal\ with the vendored
; Tesseract and Poppler binaries, the ONNX model and the tiktoken cache.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startmenuicon
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; A plain launch. The launcher itself detects a missing .env and shows the
; first-run wizard, so the installer does not need to decide which mode to use
; -- one code path covers both a first install and a reinstall over an
; existing, already-configured one.
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller's onedir output is fully enumerated in [Files], but Python writes
; __pycache__ directories beside bundled modules at run time. Those are not
; tracked by the installer and would otherwise leave {app} behind.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
Type: dirifempty;     Name: "{app}"

[Code]
// Uninstall data handling.
//
// The UninstallDelete section is static, so the keep-data-unless-asked
// behaviour has to live here. This runs in InitializeUninstall -- BEFORE
// anything is removed -- rather than as a checkbox on the uninstall progress
// form, which renders only after the user has already committed to
// uninstalling. A mis-click there would destroy privileged client material
// with no undo.
//
// Defaults are stacked toward keeping data: MB_DEFBUTTON2 makes No the
// default on both prompts, so Enter, Space, or a reflexive click preserves
// the matters. The destructive path has to be chosen twice, deliberately.
//
// NB: line comments, not a braced block. Inno's section scanner is
// line-based and runs before Pascal comments are parsed, so any line whose
// first non-space character is [ is read as a section tag -- even inside a
// { } comment. That is what broke the first compile of this file.

function GetDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\MatterClerk');
end;

function InitializeUninstall(): Boolean;
var
  DataDir: String;
begin
  Result := True;

  DataDir := GetDataDir();
  if not DirExists(DataDir) then
    Exit;

  if MsgBox(
       'Also delete your matter data?' + #13#10#13#10 +
       'Matter Clerk will be removed either way. This question is only about ' +
       'your saved work:' + #13#10#13#10 +
       DataDir + #13#10#13#10 +
       'That folder holds all your matters, uploaded documents, the search ' +
       'index and the audit log.' + #13#10#13#10 +
       'Choose No to keep it (recommended).',
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    if MsgBox(
         'Permanently delete all matter data?' + #13#10#13#10 +
         'This cannot be undone. Everything in' + #13#10 +
         DataDir + #13#10 +
         'will be erased, including client documents and the audit log.',
         mbError, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      if not DelTree(DataDir, True, True, True) then
        MsgBox('Some files in' + #13#10 + DataDir + #13#10 +
               'could not be removed. They may be in use. Delete the folder ' +
               'manually after Matter Clerk has closed.',
               mbError, MB_OK);
    end;
  end;
end;
