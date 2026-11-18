#!/bin/bash
# Runs the dashboard inside WSL, avoiding Windows Smart App Control blocking pandas' DLLs.
# Launched by launch_dashboard.bat. Set POETRY if Poetry isn't on your PATH or in ~/.local/bin.
cd "$(dirname "$0")/.." || exit 1
source scripts/find_poetry.sh
"$POETRY" run streamlit run src/dashboard/dashboard.py --server.headless true
