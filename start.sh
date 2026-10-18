#!/usr/bin/env bash
# Linux / macOS launcher
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
    echo "Python 3.10+ is required. Install it from https://www.python.org/downloads/"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating Python environment..."
    "$PY" -m venv .venv || {
        echo "Could not create a virtual environment."
        echo "On Debian/Ubuntu run: sudo apt install python3-venv"
        exit 1
    }
fi

.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python run.py
