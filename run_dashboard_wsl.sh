#!/bin/bash
# Runs the dashboard inside WSL, avoiding Windows Smart App Control blocking pandas' DLLs.
# Launched by launch_dashboard.bat via: wsl -d Ubuntu-24.04 -- bash run_dashboard_wsl.sh
cd /mnt/c/Users/mason/Projects/bank-enrichment
POETRY=/home/mason/.local/bin/poetry
"$POETRY" run streamlit run src/dashboard/dashboard.py --server.headless true
