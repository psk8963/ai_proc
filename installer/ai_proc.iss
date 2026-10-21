; Pro*C 영향도 분석 에이전트 - Inno Setup 스크립트
; build_installer.ps1 이 /DAppVersion /DDistDir /DOutDir 를 넘겨서 컴파일한다.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\build\dist"
#endif
#ifndef OutDir
  #define OutDir "..\build\out"
#endif
#define AppName "Pro*C 영향도 분석 에이전트"
; 폴더·바로가기 이름에는 * 를 쓸 수 없어서 따로 둔다 (Windows 파일 이름 규칙)
#define ShortName "ProC 영향도 분석 에이전트"
#define AppExeName "ProcImpactAgent"
#define AppPort "8001"

[Setup]
AppId={{199F9D88-32F3-47F1-BDBF-88CE0C03E4D2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=psk8963
AppPublisherURL=https://github.com/psk8963/ai_proc
VersionInfoVersion={#AppVersion}
; 관리자 권한 없이 사용자 폴더에 설치 (회사 PC 에서 권한 요청 없이 설치 가능)
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppExeName}
DefaultGroupName={#ShortName}
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename={#AppExeName}-Setup-{#AppVersion}
SetupIconFile={#DistDir}\app\assets\app.ico
UninstallDisplayIcon={app}\app\assets\app.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName={#AppName} {#AppVersion}
CloseApplications=yes
RestartApplications=no

[Languages]
#if FileExists(CompilerPath + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "바로가기:"
Name: "autostart"; Description: "Windows 시작할 때 트레이에서 자동 실행"; GroupDescription: "실행:"; Flags: unchecked

[InstallDelete]
; 업데이트할 때 이전 버전 파일을 지우고 새로 깐다(사용자 데이터는 %LOCALAPPDATA%\ProcImpactAgent 에 있어 안전)
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 앱 실행: 콘솔 창 없이(pythonw) 전용 앱 창 + 트레이 아이콘
Name: "{group}\{#ShortName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\desktop.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\app.ico"; Comment: "Pro*C 영향도 분석 에이전트"
Name: "{group}\문제 해결 (콘솔 모드)"; Filename: "{app}\python\python.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\app.ico"; Comment: "서버 로그를 콘솔 창에 보여주며 실행합니다"
Name: "{group}\설정 파일 열기 (.env)"; Filename: "notepad.exe"; Parameters: """{localappdata}\ProcImpactAgent\.env"""
Name: "{group}\데이터 폴더 열기"; Filename: "{localappdata}\ProcImpactAgent"
Name: "{group}\{#ShortName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#ShortName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\desktop.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\app.ico"; Tasks: desktopicon
Name: "{userstartup}\{#ShortName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\desktop.py"" --background"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\app.ico"; Tasks: autostart

[Dirs]
Name: "{localappdata}\ProcImpactAgent"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\desktop.py"""; WorkingDir: "{app}\app"; Description: "지금 실행"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Code]
{ 설치·제거 전에 실행 중인 앱을 정상 종료시킨다 (트레이에 떠 있으면 파일이 잠겨 있으므로) }
procedure StopRunningApp();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 3 http://127.0.0.1:{#AppPort}/api/app/quit | Out-Null; Start-Sleep -Seconds 3 } catch {}"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningApp();
  Result := True;
end;
