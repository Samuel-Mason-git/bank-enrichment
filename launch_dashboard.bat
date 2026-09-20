@echo off
cd /d "%~dp0"
rem Runs pandas/streamlit inside WSL Ubuntu, since Windows Smart App Control
rem blocks pandas' compiled DLLs when run directly on Windows.
title Bank Enrichment Dashboard
start /b "" powershell -NoProfile -WindowStyle Hidden -Command "while (-not (Test-NetConnection -ComputerName localhost -Port 8501 -InformationLevel Quiet -WarningAction SilentlyContinue)) { Start-Sleep -Seconds 1 }; Start-Process 'http://localhost:8501'"
wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/mason/Projects/bank-enrichment/run_dashboard_wsl.sh
