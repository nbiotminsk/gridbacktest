@echo off
rem gridbacktest GUI launcher: gui.bat
rem The file is ASCII-only on purpose: cmd misreads UTF-8 text in .bat files.
rem The window is started with pythonw (no console), and this console closes right away.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title gridbacktest GUI

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3 from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
python -c "import requests" >nul 2>nul
if errorlevel 1 (
  echo Installing requests...
  python -m pip install --user requests
)

where pythonw >nul 2>nul
if errorlevel 1 (
  rem no pythonw - start with the console as before
  python gui.py
  if errorlevel 1 pause
  exit /b
)
start "" pythonw gui.py
