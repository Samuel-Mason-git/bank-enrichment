@echo off
cd /d "%~dp0"
poetry run python -m streamlit run src\local_scripts\dashboard.py
