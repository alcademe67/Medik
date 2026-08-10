@echo off
REM Double-click this to see your account through TWS.
REM
REM Requires TWS (or IB Gateway) to be open and logged in, with
REM File > Global Configuration > API > Settings > "Enable ActiveX and
REM Socket Clients" checked. Read-only: places nothing, cancels nothing.

setlocal
set REPO=%~dp0
set VENV_DIR=%REPO%venv

cd /d "%REPO%"

if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
)

python examples\tws_status.py

echo.
pause
endlocal
