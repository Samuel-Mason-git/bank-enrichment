#!/bin/bash
# Runs the local pipeline (process.py) inside WSL Ubuntu, since Windows Smart App
# Control blocks pandas' compiled DLLs when run directly on Windows. Used by the
# "Bank Enrichment Collection" scheduled task -- see run_dashboard_wsl.sh for the
# interactive version that also opens the dashboard.
cd /mnt/c/Users/mason/Projects/bank-enrichment
/home/mason/.local/bin/poetry run python src/local_scripts/process.py
