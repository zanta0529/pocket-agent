@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    set "RUN_PY=%VENV_PY%"
    goto :RUN
)

echo ============================================================
echo [INFO] Virtual environment not found. Detecting Python...
echo ============================================================

set "SYS_PY="
python --version >nul 2>&1
if not errorlevel 1 (
    set "SYS_PY=python"
    goto :CHECK_VENV_TOOL
)

py --version >nul 2>&1
if not errorlevel 1 (
    set "SYS_PY=py"
    goto :CHECK_VENV_TOOL
)

echo [ERROR] Neither 'python' nor 'py' was found in your PATH.
echo Please install Python 3.11+ and ensure it is added to your PATH.
echo ============================================================
pause
exit /b 1

:CHECK_VENV_TOOL
echo [INFO] Found Python runtime: %SYS_PY%
echo [INFO] Detecting virtual environment capabilities...

REM 1. Standard library venv
%SYS_PY% -c "import venv" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Detected standard library module: venv
    set "CREATE_CMD=%SYS_PY% -m venv venv"
    goto :DO_CREATE
)

REM 2. virtualenv module
%SYS_PY% -c "import virtualenv" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Built-in venv not found [Embeddable Python detected].
    echo [INFO] Detected 'virtualenv' module.
    set "CREATE_CMD=%SYS_PY% -m virtualenv venv"
    goto :DO_CREATE
)

REM 3. virtualenv executable
virtualenv --version >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Built-in venv not found. Detected 'virtualenv' executable.
    set "CREATE_CMD=virtualenv venv"
    goto :DO_CREATE
)

REM 4. uv executable
uv --version >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Built-in venv not found. Detected 'uv' executable.
    set "CREATE_CMD=uv venv venv"
    goto :DO_CREATE
)

echo [WARNING] No venv, virtualenv, or uv found to create an isolated environment.
echo [INFO] Falling back to system Python directly: %SYS_PY%
set "RUN_PY=%SYS_PY%"
goto :INSTALL_DEPS

:DO_CREATE
echo [INFO] Creating virtual environment: %CREATE_CMD%
%CREATE_CMD%
if errorlevel 1 (
    echo [WARNING] Failed to create virtual environment via: %CREATE_CMD%
    echo [INFO] Falling back to using system Python directly: %SYS_PY%
    set "RUN_PY=%SYS_PY%"
    goto :INSTALL_DEPS
)

if not exist "%VENV_PY%" (
    echo [WARNING] %VENV_PY% was not created.
    echo [INFO] Falling back to using system Python directly: %SYS_PY%
    set "RUN_PY=%SYS_PY%"
    goto :INSTALL_DEPS
)

set "RUN_PY=%VENV_PY%"
echo [INFO] Upgrading pip...
"%RUN_PY%" -m pip install --upgrade pip

:INSTALL_DEPS
echo [INFO] Installing dependencies from requirements.txt ...
"%RUN_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)

echo [INFO] Environment setup completed!
echo ============================================================

:RUN
"%RUN_PY%" main.py %*
if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] Application terminated unexpectedly [Exit Code: %errorlevel%].
    echo Check agent.log or the error message above for details.
    echo ============================================================
    pause
)
endlocal



