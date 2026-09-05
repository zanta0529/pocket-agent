@echo off
REM Build a distributable zip for non-technical users.
REM Requires venv/ to exist with requirements.txt already installed.
REM
REM Kept ASCII-only on purpose: cmd.exe parses .bat using the OEM codepage
REM (Big5 / 950 on zh-TW), so UTF-8 Chinese here gets mangled into garbage
REM commands. Chinese docs live in README.md instead.
REM
REM Also .bat and not .ps1: corporate Group Policy commonly blocks
REM PowerShell script execution.
setlocal
cd /d %~dp0

set PY=venv\Scripts\python.exe
if not exist "%PY%" (
    echo [ERROR] %PY% not found.
    echo Run first:  py -m venv venv
    echo             venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

echo [1/3] Installing build tool...
"%PY%" -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 exit /b 1

echo [2/3] Building (takes 2-5 minutes)...
"%PY%" -m PyInstaller --noconfirm --clean PocketAgent.spec
if errorlevel 1 exit /b 1

echo [3/3] Creating zip...
"%PY%" -c "import shutil; shutil.make_archive('dist/PocketAgent','zip','dist','PocketAgent'); print('zip done')"
if errorlevel 1 exit /b 1

echo.
echo Done: dist\PocketAgent.zip
echo Put it on a shared drive; colleagues unzip and double-click PocketAgent.exe
endlocal
