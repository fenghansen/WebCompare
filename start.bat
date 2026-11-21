@echo off
setlocal
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Please run install.bat first.
    if not defined CI pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m webcompare.cli %*
set "result=%errorlevel%"
if not "%result%"=="0" if not defined CI pause
exit /b %result%
