#!/bin/bash
# Runs the pipeline (process.py) once, by hand, inside WSL Ubuntu, since Windows
# Smart App Control blocks pandas' compiled DLLs when run directly on Windows.
# The server's worker runs it every morning anyway; this is for not waiting.
# Set POETRY if Poetry isn't in ~/.local/bin.
cd "$(dirname "$0")"
POETRY="${POETRY:-$HOME/.local/bin/poetry}"
"$POETRY" run python src/pipeline/process.py
