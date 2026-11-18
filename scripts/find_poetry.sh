#!/bin/bash
# Sourced by the run_*_wsl.sh scripts: finds Poetry and makes sure the project's packages are
# installed, or says what to do and stops. Set POETRY to use a Poetry somewhere else.

if [ -z "$POETRY" ]; then
    if command -v poetry >/dev/null 2>&1; then
        POETRY="$(command -v poetry)"
    else
        POETRY="$HOME/.local/bin/poetry"
    fi
fi

if [ ! -x "$POETRY" ] && ! command -v "$POETRY" >/dev/null 2>&1; then
    echo "Poetry isn't installed in WSL. Install it there, then try again:"
    echo "    sudo apt install -y python3 python3-venv pipx && pipx install poetry"
    echo "(Or set POETRY to where yours is.)"
    exit 1
fi

if ! "$POETRY" run python -c "import streamlit" >/dev/null 2>&1; then
    echo "Installing the project's packages (first run only, takes a few minutes)..."
    "$POETRY" install --no-interaction || { echo "poetry install failed: see the error above."; exit 1; }
fi
