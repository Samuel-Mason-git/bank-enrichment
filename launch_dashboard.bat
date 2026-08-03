@echo off
cd /d "%~dp0"
poetry run python src\local_scripts\process.py
poetry run python -m streamlit run src\local_scripts\dashboard.py
