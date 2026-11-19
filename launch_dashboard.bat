@echo off
setlocal
cd /d "%~dp0"
rem Runs the dashboard inside WSL, since Windows Smart App Control blocks
rem pandas' compiled DLLs when run directly on Windows.
rem Uses your default WSL distro (skipping Docker Desktop's own). To pick one,
rem set BANK_WSL_DISTRO, e.g.  setx BANK_WSL_DISTRO Ubuntu-24.04
title Bank Enrichment Dashboard

where wsl >nul 2>nul
if errorlevel 1 (
    echo WSL isn't installed. The dashboard runs inside it on Windows.
    echo Install it from an administrator PowerShell, then restart:
    echo     wsl --install -d Ubuntu
    echo Or run it without WSL, if Smart App Control is off:
    echo     poetry run streamlit run src/dashboard/dashboard.py
    goto :failed
)

set "DISTRO=%BANK_WSL_DISTRO%"
if not defined DISTRO (
    for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "$env:WSL_UTF8='1'; wsl -l -q 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -notlike 'docker-desktop*' } | Select-Object -First 1"`) do set "DISTRO=%%d"
)
if not defined DISTRO (
    echo WSL is installed but has no Linux distro to run the dashboard in.
    echo Install one from PowerShell, then double-click this file again:
    echo     wsl --install -d Ubuntu
    goto :failed
)

rem Check the name before using it, so a wrong name says what's installed
rem instead of a bare "no distribution" error.
powershell -NoProfile -Command "$env:WSL_UTF8='1'; $names = wsl -l -q | ForEach-Object { $_.Trim() }; if ($names -contains $env:DISTRO) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo Couldn't find the WSL distro "%DISTRO%". Installed distros are:
    wsl -l -q
    echo Set BANK_WSL_DISTRO to one of those names, e.g.  setx BANK_WSL_DISTRO Ubuntu-24.04
    goto :failed
)

echo Starting the dashboard in WSL ^(%DISTRO%^). Your browser opens when it's ready.
rem Opens the browser once the port answers. A plain socket check, so it's quick to notice.
start /b "" powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 300; $i++) { try { $c = New-Object System.Net.Sockets.TcpClient('localhost', 8501); $c.Close(); Start-Process 'http://localhost:8501'; break } catch { Start-Sleep -Milliseconds 500 } }"
wsl -d "%DISTRO%" --cd "%~dp0" -- bash scripts/run_dashboard_wsl.sh
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
pause
exit /b 1
