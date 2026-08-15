@echo off
REM Launches the background trading service. Meant to be run by Windows
REM Task Scheduler (see docs\SETUP_WINDOWS.md), but you can also double-click it
REM to test.
REM
REM Assumes a venv at %REPO%\venv (adjust VENV_DIR if yours lives elsewhere)
REM and that .env is configured at the repo root.

setlocal
set REPO=%~dp0..
set VENV_DIR=%REPO%\venv

cd /d "%REPO%"

if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
)

python service\supervisor.py
set EXITCODE=%ERRORLEVEL%

endlocal & exit /b %EXITCODE%
