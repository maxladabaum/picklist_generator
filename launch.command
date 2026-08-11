#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "Python 3.9 or newer is required. Install it from python.org, then run this launcher again." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  osascript -e 'display dialog "Python 3.9 or newer is required. Install the current Python from python.org, then run this launcher again." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import tkinter" >/dev/null 2>&1; then
  osascript -e 'display dialog "This Python installation does not include Tkinter. Install the current Python 3 from python.org, then delete .venv and try again." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

exec .venv/bin/python picklist_app.py
