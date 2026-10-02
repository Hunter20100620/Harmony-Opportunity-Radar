@echo off
REM Harmony Opportunity Radar - quick run launcher.
REM Double-click to open the interactive menu, or pass an action, e.g. run.bat gui
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set RC=%errorlevel%
if not "%RC%"=="0" (
    echo.
    echo Failed with exit code %RC%.
    pause
)
exit /b %RC%