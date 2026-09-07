@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

title YingJi Stop
set "PID_FILE=%CD%\server.pid"

echo. > "stop.flag"

if not exist "%PID_FILE%" (
    echo [INFO] No YingJi server PID file was found.
    echo If the service is already stopped, no further action is needed.
    timeout /t 3 >nul
    exit /b 0
)

set /p SERVER_PID=<"%PID_FILE%"
powershell.exe -NoProfile -Command "$raw=(Get-Content -LiteralPath '%PID_FILE%' -Raw).Trim(); $n=0; if (-not [int]::TryParse($raw,[ref]$n)) { exit 3 }; $p=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $n); if ($null -eq $p) { exit 4 }; if ($p.CommandLine -notmatch 'server\.py') { exit 5 }; $expected=[IO.Path]::GetFullPath('%CD%\.venv\Scripts\python.exe'); if (-not [String]::Equals([IO.Path]::GetFullPath($p.ExecutablePath),$expected,[StringComparison]::OrdinalIgnoreCase)) { exit 6 }; exit 0"

if %errorlevel% equ 0 (
    echo [INFO] Stopping YingJi process %SERVER_PID% and its child processes ...
    taskkill /F /T /PID %SERVER_PID% >nul 2>&1
    if %errorlevel% equ 0 (
        echo [OK] YingJi stopped.
        del "%PID_FILE%" >nul 2>&1
        timeout /t 2 >nul
        exit /b 0
    )
)

echo [WARN] The PID file is stale or does not belong to YingJi.
echo No process was terminated. Remove server.pid after confirming YingJi is stopped.
timeout /t 4 >nul
exit /b 1
