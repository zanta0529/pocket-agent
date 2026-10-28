@echo off
REM Build a distributable zip for non-technical users.
REM Requires venv/ to exist with requirements.txt already installed.
REM Kept ASCII-only on purpose to avoid Big5/950 OEM codepage mangling on zh-TW.

setlocal
cd /d "%~dp0"

echo ============================================================
echo PocketAgent Build Tool
echo ============================================================
echo.
echo Please select a build mode:
echo [1] Legacy PyInstaller (Single executable, may trigger Anti-Virus) [Default]
echo [2] Modern Portable (Bundled Python env, larger size but safe)
echo.
set "CHOICE=1"
set /p CHOICE="Enter your choice (1 or 2, default is 1): "

set PY=venv\Scripts\python.exe
if not exist "%PY%" (
    echo [ERROR] %PY% not found.
    echo Run first:  py -m venv venv
    echo             venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if "%CHOICE%"=="2" goto PORTABLE
goto PYINSTALLER

:PYINSTALLER
echo.
echo ============================================================
echo Starting Legacy PyInstaller Build...
echo ============================================================
echo [1/4] Generating manual...
"%PY%" scripts\generate_manual.py
if errorlevel 1 echo [WARN] Failed to generate manual, continuing build process...

echo [2/4] Checking build tool...
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PY%" -m pip install --quiet pyinstaller
    if errorlevel 1 exit /b 1
)

echo [3/4] Building...
"%PY%" -m PyInstaller --noconfirm --clean build_scripts\PocketAgent.spec
if errorlevel 1 exit /b 1

echo [4/4] Creating zip...
copy /y build_scripts\pocket.bat dist\PocketAgent\ >nul
copy /y build_scripts\pocket.sh dist\PocketAgent\ >nul
copy /y dist\PocketAgent_Manual.md dist\PocketAgent\ >nul
"%PY%" -c "import shutil; shutil.make_archive('dist/PocketAgent','zip','dist','PocketAgent'); print('zip done')"
if errorlevel 1 exit /b 1

echo.
echo Done: dist\PocketAgent.zip
echo Double-click PocketAgent-cli.exe to run
goto END

:PORTABLE
echo.
echo ============================================================
echo Starting Modern Portable Build...
echo ============================================================
"%PY%" build_scripts\build_portable.py
if errorlevel 1 exit /b 1
goto END

:END
endlocal
