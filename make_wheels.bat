@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  회사 PC 오프라인 설치용 wheels 폴더 만들기
echo ============================================
if exist wheels rmdir /s /q wheels
if exist "%TEMP%\pa_wheeltest" rmdir /s /q "%TEMP%\pa_wheeltest"

echo.
echo [1/2] 패키지 받는 중... (몇 분 걸립니다)
call venv\Scripts\activate.bat
python -m pip wheel -r requirements.txt -w wheels > make_wheels_log.txt 2>&1
if errorlevel 1 (
  echo   실패했습니다. make_wheels_log.txt 를 확인해 주세요.
  echo STEP1_FAILED >> make_wheels_log.txt
  pause
  exit /b 1
)

echo [2/2] wheels 만으로 설치되는지 확인 중...
python -m venv "%TEMP%\pa_wheeltest"
"%TEMP%\pa_wheeltest\Scripts\python.exe" -m pip install --no-index --find-links wheels -r requirements.txt >> make_wheels_log.txt 2>&1
if errorlevel 1 (
  echo   설치 확인에 실패했습니다. make_wheels_log.txt 를 확인해 주세요.
  echo STEP2_FAILED >> make_wheels_log.txt
  pause
  exit /b 1
)
rmdir /s /q "%TEMP%\pa_wheeltest"
echo ALL_OK >> make_wheels_log.txt

echo.
echo 완료했습니다. 이 창을 닫고 Claude에게 "다 됐어"라고 알려주세요.
pause
