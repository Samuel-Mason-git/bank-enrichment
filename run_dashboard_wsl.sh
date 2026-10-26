#!/bin/bash
# Runs the dashboard inside WSL, avoiding Windows Smart App Control blocking pandas' DLLs.
# Launched by launch_dashboard.bat. Set POETRY if Poetry isn't in ~/.local/bin.
cd "$(dirname "$0")"
POETRY="${POETRY:-$HOME/.local/bin/poetry}"
"$POETRY" run streamlit run src/dashboard/dashboard.py --server.headless true
