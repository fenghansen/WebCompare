@echo off
setlocal
chcp 65001 >nul
if defined WEBCOMPARE_PYTHON (
    "%WEBCOMPARE_PYTHON%" "%~dp0scripts\install.py" %*
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 "%~dp0scripts\install.py" %*
    ) else (
        python "%~dp0scripts\install.py" %*
    )
)
set "result=%errorlevel%"
if not "%result%"=="0" if not defined CI pause
exit /b %result%
