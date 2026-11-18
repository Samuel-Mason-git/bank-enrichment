#!/bin/bash
# Runs the pipeline (process.py) once, by hand, inside WSL, since Windows
# Smart App Control blocks pandas' compiled DLLs when run directly on Windows.
# The server's worker runs it every hour anyway; this is for not waiting.
# Set POETRY if Poetry isn't on your PATH or in ~/.local/bin.
cd "$(dirname "$0")/.." || exit 1
source scripts/find_poetry.sh
"$POETRY" run python src/pipeline/process.py
