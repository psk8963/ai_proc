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
#define AppExeName "ProcImpactAgent"

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
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename={#AppExeName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName={#AppName} {#AppVersion}
; 업데이트 설치 시 실행 중이면 닫으라고 안내
CloseApplications=yes

[Languages]
#if FileExists(CompilerPath + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "바로가기:"

[InstallDelete]
; 업데이트할 때 이전 버전 파일을 지우고 새로 깐다(사용자 데이터는 %LOCALAPPDATA%\ProcImpactAgent 에 있어 안전)
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\python\python.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Comment: "서버를 시작하고 브라우저로 화면을 엽니다"
Name: "{group}\설정 파일 열기 (.env)"; Filename: "notepad.exe"; Parameters: """{localappdata}\ProcImpactAgent\.env"""
Name: "{group}\데이터 폴더 열기"; Filename: "{localappdata}\ProcImpactAgent"
Name: "{group}\사용 설명서 (README)"; Filename: "notepad.exe"; Parameters: """{app}\app\README.md"""
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\python.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Tasks: desktopicon

[Dirs]
Name: "{localappdata}\ProcImpactAgent"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Description: "지금 실행"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"
