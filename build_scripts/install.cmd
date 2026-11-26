@echo off
REM PocketAgent Windows Enterprise CMD Installer (no PowerShell required)
REM Usage: curl -fsSLO https://raw.githubusercontent.com/zanta0529/pocket-agent/main/build_scripts/install.cmd && install.cmd
setlocal EnableDelayedExpansion

set "PA_REPO=%POCKET_AGENT_REPO%"
if "%PA_REPO%"=="" set "PA_REPO=zanta0529/pocket-agent"

set "PA_VERSION=%POCKET_AGENT_VERSION%"
if "%PA_VERSION%"=="" set "PA_VERSION=latest"

set "PA_HOME=%POCKET_AGENT_HOME%"
if "%PA_HOME%"=="" set "PA_HOME=%LOCALAPPDATA%\PocketAgent"

set "PA_UNINSTALL=%POCKET_AGENT_UNINSTALL%"
set "PA_REQ_SUM=%POCKET_AGENT_REQUIRE_CHECKSUM%"

:PARSE_ARGS
if "%~1"=="" goto ARGS_DONE
if /i "%~1"=="--uninstall" (
    set "PA_UNINSTALL=1"
    shift
    goto PARSE_ARGS
)
if /i "%~1"=="--version" (
    if "%~2"=="" (
        echo [ERROR] --version requires a value.
        exit /b 1
    )
    set "PA_VERSION=%~2"
    shift
    shift
    goto PARSE_ARGS
)
if /i "%~1"=="--dir" (
    if "%~2"=="" (
        echo [ERROR] --dir requires a value.
        exit /b 1
    )
    set "PA_HOME=%~2"
    shift
    shift
    goto PARSE_ARGS
)
if /i "%~1"=="--help" goto SHOW_HELP
if /i "%~1"=="-h" goto SHOW_HELP
echo [ERROR] Unknown option: %~1
exit /b 1

:SHOW_HELP
echo PocketAgent Windows CMD Installer
echo Options:
echo   --version ^<tag^>   Install specific release tag ^(default: latest^)
echo   --dir ^<path^>      Install directory ^(default: %%LOCALAPPDATA%%\PocketAgent^)
echo   --uninstall       Remove PocketAgent installation ^(preserves %%USERPROFILE%%\.pocket-agent^)
exit /b 0

:ARGS_DONE
set "PA_BIN=%PA_HOME%\bin"

if "%PA_UNINSTALL%"=="1" goto DO_UNINSTALL

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] curl.exe not found in PATH. Windows 10 1803+ or Windows 11 is required.
    exit /b 1
)
where tar.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] tar.exe not found in PATH. Windows 10 1803+ or Windows 11 is required.
    exit /b 1
)

if /i "%PA_VERSION%"=="latest" (
    set "PA_BASE_URL=https://github.com/%PA_REPO%/releases/latest/download"
) else (
    set "PA_BASE_URL=https://github.com/%PA_REPO%/releases/download/%PA_VERSION%"
)

set "PA_ASSET=PocketAgent-portable.zip"
set "PA_TMP=%TEMP%\pocket-agent-install-%RANDOM%-%RANDOM%"
set "PA_ZIP=%PA_TMP%\%PA_ASSET%"
set "PA_SUMS=%PA_TMP%\SHA256SUMS.txt"
set "PA_EXT=%PA_TMP%\extract"

mkdir "%PA_TMP%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to create temporary directory: %PA_TMP%
    exit /b 1
)
mkdir "%PA_EXT%" >nul 2>&1

echo [INFO] Downloading %PA_ASSET% (%PA_VERSION%) from %PA_REPO%...
curl.exe -fsSL "%PA_BASE_URL%/%PA_ASSET%" -o "%PA_ZIP%"
if errorlevel 1 (
    echo [ERROR] Failed to download %PA_ASSET% from %PA_BASE_URL%.
    goto FAIL_CLEANUP
)

set "HAS_SUMS=0"
curl.exe -fsSL "%PA_BASE_URL%/SHA256SUMS.txt" -o "%PA_SUMS%" >nul 2>&1
if not errorlevel 1 if exist "%PA_SUMS%" set "HAS_SUMS=1"

if "%HAS_SUMS%"=="0" (
    if "%PA_REQ_SUM%"=="1" (
        echo [ERROR] SHA256SUMS.txt is required ^(POCKET_AGENT_REQUIRE_CHECKSUM=1^) but could not be downloaded.
        goto FAIL_CLEANUP
    )
    echo [WARN] SHA256SUMS.txt not found for this release; skipping checksum verification.
    goto DO_EXTRACT
)

set "EXPECTED_HASH="
for /f "usebackq tokens=1,2" %%A in ("%PA_SUMS%") do (
    set "FNAME=%%B"
    if "!FNAME:~0,1!"=="*" set "FNAME=!FNAME:~1!"
    if /i "!FNAME!"=="%PA_ASSET%" set "EXPECTED_HASH=%%A"
)
if "%EXPECTED_HASH%"=="" (
    echo [ERROR] SHA256SUMS.txt does not contain an entry for %PA_ASSET%.
    goto FAIL_CLEANUP
)

set "ACTUAL_HASH="
set "LINE_IDX=0"
for /f "usebackq delims=" %%L in (`certutil -hashfile "%PA_ZIP%" SHA256`) do (
    set /a LINE_IDX+=1
    if !LINE_IDX! EQU 2 set "ACTUAL_HASH=%%L"
)
set "ACTUAL_HASH=%ACTUAL_HASH: =%"
if "%ACTUAL_HASH%"=="" (
    echo [ERROR] Failed to compute SHA-256 hash using certutil.
    goto FAIL_CLEANUP
)
if /i not "%ACTUAL_HASH%"=="%EXPECTED_HASH%" (
    echo [ERROR] SHA-256 verification failed! Expected %EXPECTED_HASH%, got %ACTUAL_HASH%.
    goto FAIL_CLEANUP
)
echo [INFO] SHA-256 verified (%ACTUAL_HASH%).

:DO_EXTRACT
echo [INFO] Extracting archive...
tar.exe -xf "%PA_ZIP%" -C "%PA_EXT%"
if errorlevel 1 (
    echo [ERROR] Failed to extract %PA_ASSET%.
    goto FAIL_CLEANUP
)

set "PA_PAYLOAD=%PA_EXT%\PocketAgent"
if not exist "%PA_PAYLOAD%\pocket.bat" set "PA_PAYLOAD=%PA_EXT%"
if not exist "%PA_PAYLOAD%\pocket.bat" (
    echo [ERROR] Extracted archive does not contain pocket.bat.
    goto FAIL_CLEANUP
)
if not exist "%PA_PAYLOAD%\python\python.exe" (
    echo [ERROR] Extracted archive does not contain python\python.exe.
    goto FAIL_CLEANUP
)

for %%P in ("%PA_HOME%\..") do if not exist "%%~fP" mkdir "%%~fP" >nul 2>&1

set "PA_BAK=%PA_HOME%.bak.%RANDOM%"
set "MOVED_BAK=0"
if exist "%PA_HOME%" (
    if not exist "%PA_HOME%\pocket.bat" if not exist "%PA_HOME%\app\PORTABLE" (
        echo [ERROR] Target '%PA_HOME%' already exists and is not a PocketAgent directory.
        goto FAIL_CLEANUP
    )
    move "%PA_HOME%" "%PA_BAK%" >nul
    if errorlevel 1 (
        echo [ERROR] Could not move existing installation. Close running PocketAgent instances and retry.
        goto FAIL_CLEANUP
    )
    set "MOVED_BAK=1"
)

move "%PA_PAYLOAD%" "%PA_HOME%" >nul
if errorlevel 1 (
    echo [ERROR] Failed to move extracted files into %PA_HOME%.
    if "%MOVED_BAK%"=="1" move "%PA_BAK%" "%PA_HOME%" >nul 2>&1
    goto FAIL_CLEANUP
)
if "%MOVED_BAK%"=="1" if exist "%PA_BAK%" rmdir /s /q "%PA_BAK%" >nul 2>&1

if not exist "%PA_BIN%" mkdir "%PA_BIN%" >nul 2>&1
(
    echo @echo off
    echo "%%~dp0..\pocket.bat" %%*
) > "%PA_BIN%\pocket-agent.cmd"

call :ADD_USER_PATH "%PA_BIN%"

rmdir /s /q "%PA_TMP%" >nul 2>&1
echo.
echo [OK] PocketAgent installed to "%PA_HOME%".
echo Next steps ^(open a new cmd.exe window if PATH was just updated^):
echo   1. pocket-agent setup
echo   2. pocket-agent
echo To uninstall later: "%PA_HOME%\bin\pocket-agent.cmd" or run install.cmd --uninstall
exit /b 0

:DO_UNINSTALL
if not exist "%PA_HOME%" (
    echo [INFO] PocketAgent is not installed at "%PA_HOME%".
    exit /b 0
)
if not exist "%PA_HOME%\pocket.bat" if not exist "%PA_HOME%\app\PORTABLE" (
    echo [ERROR] Refusing to remove "%PA_HOME%": not a PocketAgent directory.
    exit /b 1
)
rmdir /s /q "%PA_HOME%"
if errorlevel 1 (
    echo [ERROR] Failed to remove "%PA_HOME%". Ensure PocketAgent is not running.
    exit /b 1
)
echo [OK] Uninstalled PocketAgent from "%PA_HOME%".
echo [INFO] User data in "%USERPROFILE%\.pocket-agent" was preserved.
echo [INFO] If "%PA_BIN%" is in your User PATH, you may remove it from Environment Variables.
exit /b 0

:ADD_USER_PATH
set "TARGET_BIN=%~1"
set "CUR_USER_PATH="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul ^| find /i "Path"') do set "CUR_USER_PATH=%%B"
if "%CUR_USER_PATH%"=="" (
    reg add "HKCU\Environment" /v Path /t REG_EXPAND_SZ /d "%TARGET_BIN%" /f >nul
    echo [INFO] Added "%TARGET_BIN%" to User PATH.
    goto :EOF
)
echo ;%CUR_USER_PATH%; | find /i ";%TARGET_BIN%;" >nul
if not errorlevel 1 goto :EOF
reg add "HKCU\Environment" /v Path /t REG_EXPAND_SZ /d "%CUR_USER_PATH%;%TARGET_BIN%" /f >nul
if not errorlevel 1 (
    echo [INFO] Added "%TARGET_BIN%" to User PATH.
) else (
    echo [WARN] Could not update HKCU\Environment Path automatically. Add "%TARGET_BIN%" manually.
)
goto :EOF

:FAIL_CLEANUP
if exist "%PA_TMP%" rmdir /s /q "%PA_TMP%" >nul 2>&1
exit /b 1
