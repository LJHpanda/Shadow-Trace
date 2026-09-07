@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

title YingJi Community Setup

echo.
echo ========================================
echo       YingJi Community Edition
echo          Environment Setup
echo ========================================
echo.

set "BOOTSTRAP_PYTHON="
py -3 --version >nul 2>&1
if %errorlevel% equ 0 set "BOOTSTRAP_PYTHON=py -3"

if not defined BOOTSTRAP_PYTHON (
    python --version >nul 2>&1
    if %errorlevel% equ 0 set "BOOTSTRAP_PYTHON=python"
)

if not defined BOOTSTRAP_PYTHON (
    echo [ERROR] Python 3.10 or newer was not found.
    echo Install 64-bit Python from https://www.python.org/downloads/windows/
    echo and enable "Add Python to PATH".
    pause
    exit /b 1
)

%BOOTSTRAP_PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if %errorlevel% neq 0 (
    echo [ERROR] YingJi requires Python 3.10 or newer.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating isolated environment in .venv ...
    %BOOTSTRAP_PYTHON% -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create .venv.
        pause
        exit /b 1
    )
)

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if %errorlevel% neq 0 (
    echo [ERROR] Existing .venv uses an unsupported Python version.
    echo Remove .venv and run setup.bat again.
    pause
    exit /b 1
)
echo [INFO] Updating packaging tools ...
"%VENV_PYTHON%" -m pip install --upgrade pip setuptools
if %errorlevel% neq 0 goto :install_failed

echo [INFO] Installing YingJi dependencies ...
"%VENV_PYTHON%" -m pip install -r requirements.txt
if %errorlevel% neq 0 goto :install_failed

if not exist "downloads" mkdir downloads
if not exist "logs" mkdir logs

echo.
echo ========================================
echo   Setup complete.
echo   Double-click start.bat to run YingJi.
echo ========================================
echo.
pause
exit /b 0

:install_failed
echo.
echo [ERROR] Dependency installation failed.
echo Check the network/proxy settings above and run setup.bat again.
pause
exit /b 1
