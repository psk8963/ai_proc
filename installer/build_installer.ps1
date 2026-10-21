# Pro*C 영향도 분석 에이전트 - Windows 설치 프로그램 만들기
#
# 결과물: build\out\ProcImpactAgent-Setup-<버전>.exe
#   - Python(임베디드 3.14) + 필요한 패키지(wheels 폴더에서, 인터넷 불필요) + 앱 소스를 한 폴더로 묶어
#     Inno Setup 으로 설치 파일을 만든다. 설치 PC 에는 Python 이 없어도 된다.
#   - 관리자 권한 없이 사용자 폴더(%LOCALAPPDATA%\Programs\ProcImpactAgent)에 설치된다.
#
# 필요: Windows, Python 3.14 (python 또는 py -3.14), Inno Setup 6 (ISCC.exe), 인터넷(임베디드 Python 받기 1회)
# 실행: powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
#       GitHub 에 v* 태그를 push 하면 .github\workflows\release.yml 이 같은 스크립트로 릴리스를 만든다.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# ---------- 버전 / 파이썬 ----------
$AppVersion = (Select-String -Path "version.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$Py = if ($env:BUILD_PYTHON) { $env:BUILD_PYTHON } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py -3.14" } else { "python" }
$PyVersion = (Invoke-Expression "$Py -c `"import platform;print(platform.python_version())`"").Trim()
if (-not $PyVersion.StartsWith("3.14.")) { throw "Python 3.14 가 필요합니다 (현재 $PyVersion). wheels 가 cp314 용입니다." }
Write-Host "앱 버전 $AppVersion / Python $PyVersion"

$Build = Join-Path $Root "build"
$Dist = Join-Path $Build "dist"
$Out = Join-Path $Build "out"
if (Test-Path $Dist) { Remove-Item $Dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Dist, $Out, (Join-Path $Build "cache") | Out-Null

# ---------- 1) 임베디드 Python ----------
$EmbedZip = Join-Path $Build "cache\python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path $EmbedZip)) {
    $Url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
    Write-Host "임베디드 Python 받는 중: $Url"
    Invoke-WebRequest -Uri $Url -OutFile $EmbedZip -UseBasicParsing
}
$PyDir = Join-Path $Dist "python"
Expand-Archive -Path $EmbedZip -DestinationPath $PyDir -Force

# python314._pth: site-packages 와 앱 폴더를 import 경로에 넣고 site 를 켠다
$Pth = Get-ChildItem $PyDir -Filter "python*._pth" | Select-Object -First 1
$ZipName = (Get-ChildItem $PyDir -Filter "python3*.zip" | Select-Object -First 1).Name
@($ZipName, ".", "Lib\site-packages", "..\app", "import site") | Set-Content -Path $Pth.FullName -Encoding ascii

# ---------- 2) 패키지 (wheels 폴더에서, 오프라인) ----------
$Site = Join-Path $PyDir "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $Site | Out-Null
Invoke-Expression "$Py -m pip install --no-index --find-links wheels -r requirements.txt --target `"$Site`" --no-compile --disable-pip-version-check"
if ($LASTEXITCODE -ne 0) { throw "패키지 설치 실패" }
# 데스크톱 앱용(트레이 아이콘) 패키지: wheels 에 없으면 인터넷에서 받는다
Invoke-Expression "$Py -m pip install --find-links wheels -r requirements-app.txt --target `"$Site`" --no-compile --disable-pip-version-check"
if ($LASTEXITCODE -ne 0) { throw "앱 패키지(pystray, pillow) 설치 실패" }

# ---------- 3) 앱 파일 ----------
$AppDir = Join-Path $Dist "app"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$Files = @("app.py", "agent_runtime.py", "batch_parser.py", "config.py", "db_runtime.py", "impact_engine.py",
           "proc_parser.py", "prompts.py", "schemas.py", "source_repository.py", "sql_analyzer.py",
           "launcher.py", "desktop.py", "settings_store.py", "version.py", "README.md", ".env.example")
foreach ($f in $Files) { Copy-Item $f -Destination $AppDir }
Copy-Item "static" -Destination $AppDir -Recurse
Copy-Item "assets" -Destination $AppDir -Recurse
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "samples") | Out-Null
Copy-Item "tests\samples\*" -Destination (Join-Path $AppDir "samples") -Recurse

# ---------- 4) 묶은 Python 으로 실제 기동 확인 ----------
$Smoke = Join-Path $Build "smoke-data"
if (Test-Path $Smoke) { Remove-Item $Smoke -Recurse -Force }
$env:PROC_AGENT_DATA_DIR = $Smoke
$env:PROC_AGENT_NO_DOTENV = "1"
& (Join-Path $PyDir "python.exe") -c "import app, oracledb, launcher, desktop, settings_store, version, pystray, PIL; print('smoke ok', version.__version__, oracledb.__version__)"
if ($LASTEXITCODE -ne 0) { throw "묶은 Python 으로 앱을 불러오지 못했습니다" }
Remove-Item Env:PROC_AGENT_DATA_DIR, Env:PROC_AGENT_NO_DOTENV
Remove-Item $Smoke -Recurse -Force -ErrorAction SilentlyContinue

# ---------- 5) Inno Setup ----------
$Iscc = $env:ISCC
if (-not $Iscc) {
    $cands = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
               "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
    $Iscc = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) 를 찾지 못했습니다. https://jrsoftware.org/isdl.php 에서 설치하거나 ISCC 환경 변수에 경로를 지정하세요." }
& $Iscc "/DAppVersion=$AppVersion" "/DDistDir=$Dist" "/DOutDir=$Out" (Join-Path $PSScriptRoot "ai_proc.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 빌드 실패" }
Write-Host "완료: $(Join-Path $Out "ProcImpactAgent-Setup-$AppVersion.exe")"
