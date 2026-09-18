@echo off
setlocal
cd /d "%~dp0"

rem Reuse the local environment even if Python is no longer on PATH.
if exist ".venv\Scripts\python.exe" goto check_environment
if exist ".venv" goto invalid_environment

set "PYTHON_CMD="
rem Test actual interpreters: a command can exist but be broken or too old.
py -3 -c "import sys; sys.exit(not sys.version_info >= (3, 9))" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if defined PYTHON_CMD goto create_environment
python -c "import sys; sys.exit(not sys.version_info >= (3, 9))" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"
if defined PYTHON_CMD goto create_environment
python3 -c "import sys; sys.exit(not sys.version_info >= (3, 9))" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python3"
if defined PYTHON_CMD goto create_environment

echo Could not find a working Python 3.9 or newer.
echo Install Python from https://www.python.org/downloads/
echo Include Tcl/Tk support and enable the Python launcher or add Python to PATH.
echo Python detection details:
py -3 --version
python --version
python3 --version
pause
exit /b 1

:create_environment
echo Creating the local Python environment using %PYTHON_CMD%...
%PYTHON_CMD% -m venv .venv
if errorlevel 1 (
  echo Could not create the local Python environment.
  pause
  exit /b 1
)

:check_environment
".venv\Scripts\python.exe" -c "import sys; print('Python:', sys.version); print('Environment:', sys.executable); sys.exit(not sys.version_info >= (3, 9))"
if errorlevel 1 goto invalid_environment

".venv\Scripts\python.exe" -c "import tkinter"
if errorlevel 1 (
  echo This Python installation does not include Tkinter.
  echo Install Python with Tcl/Tk support, rename .venv to .venv-old, then run this launcher again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" picklist_app.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" pause
exit /b %APP_EXIT_CODE%

:invalid_environment
echo The existing .venv is incomplete, unusable, or uses Python older than 3.9.
echo Virtual environments cannot be copied between computers or operating systems.
echo Rename .venv to .venv-old, then run this launcher again to create a fresh environment.
pause
exit /b 1
