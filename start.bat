@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

title YingJi Community Edition
set "PORT=5001"
set "YINGJI_PORT=%PORT%"
set "YINGJI_PID_FILE=%CD%\server.pid"
set "PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo.
    echo [ERROR] The isolated Python environment is missing.
    echo Run setup.bat first.
    echo.
    pause
    exit /b 1
)

if not exist "logs" mkdir logs
if not exist "downloads" mkdir downloads

netstat -ano 2>nul | findstr ":%PORT% " | findstr "LISTENING" >nul
if %errorlevel% equ 0 (
    echo.
    echo [ERROR] Port %PORT% is already in use.
    echo Stop the existing service or choose another PORT in start.bat.
    echo No unrelated process was terminated.
    echo.
    pause
    exit /b 1
)

if exist "stop.flag" del "stop.flag" >nul 2>&1

echo.
echo ========================================
echo       YingJi Community Edition
echo ========================================
echo Project   : %CD%
echo Web       : http://127.0.0.1:%PORT%
echo Downloads : %CD%\downloads
echo Logs      : %CD%\logs
echo.
echo Press Ctrl+C or run stop.bat to stop.
echo.

start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:%PORT%/'"

:restart
if exist "stop.flag" (
    del "stop.flag" >nul 2>&1
    goto :end
)

echo [Monitor] Starting local service. Log: logs\server-stdout.log
"%PYTHON%" -X utf8 server.py 1> "logs\server-stdout.log" 2>&1
set "EXITCODE=%errorlevel%"

if exist "stop.flag" (
    del "stop.flag" >nul 2>&1
    goto :end
)

if %EXITCODE% neq 0 (
    echo [WARN] Service exited with code %EXITCODE%. Restarting in 3 seconds.
    timeout /t 3 /nobreak >nul
    goto :restart
)

:end
if exist "%YINGJI_PID_FILE%" del "%YINGJI_PID_FILE%" >nul 2>&1
echo Service stopped.
pause
exit /b 0
