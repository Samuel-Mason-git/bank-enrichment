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

echo Starting the dashboard in WSL ^(%DISTRO%^). Your browser opens when it's ready.
start /b "" powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 300; $i++) { if (Test-NetConnection -ComputerName localhost -Port 8501 -InformationLevel Quiet -WarningAction SilentlyContinue) { Start-Process 'http://localhost:8501'; break }; Start-Sleep -Seconds 1 }"
wsl -d "%DISTRO%" --cd "%~dp0" -- bash scripts/run_dashboard_wsl.sh
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
pause
exit /b 1
