#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.9 or newer is required. Install Python and Tkinter, then try again."
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  echo "Python 3.9 or newer is required."
  exit 1
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import tkinter" >/dev/null 2>&1; then
  echo "Tkinter is required. Install your distribution's python3-tk package, delete .venv, and try again."
  exit 1
fi

exec .venv/bin/python picklist_app.py
