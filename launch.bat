@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  echo Python 3.9 or newer is required. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

%PYTHON_CMD% -c "import sys; raise SystemExit(sys.version_info ^< (3, 9))" >nul 2>nul
if errorlevel 1 (
  echo Python 3.9 or newer is required.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" %PYTHON_CMD% -m venv .venv
if errorlevel 1 (
  echo Could not create the local Python environment.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo This Python installation does not include Tkinter.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" picklist_app.py
if errorlevel 1 pause
