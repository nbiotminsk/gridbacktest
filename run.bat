@echo off
rem gridbacktest launcher. Without arguments - menu; with arguments - passed to gbt.py:
rem   run.bat sweep sweep.json
rem   run.bat top results.csv -n 50
rem The file is ASCII-only on purpose: cmd misreads UTF-8 text in .bat files.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title gridbacktest

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

if "%~1"=="" (
  python gbt.py menu
) else (
  python gbt.py %*
)
