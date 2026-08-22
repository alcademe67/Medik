@echo off
REM Double-click to download 6 months of 5-minute ETF bars from TWS.
REM
REM Requires TWS open and logged in. Takes about 8-9 minutes: IBKR paces
REM historical requests, so the script waits 11 seconds between them.
REM
REM Safe to re-run. Chunks already downloaded are skipped, so if it dies
REM partway just double-click again and it resumes.

setlocal
set REPO=%~dp0
set VENV_DIR=%REPO%venv
cd /d "%REPO%"

if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
)

echo Downloading 5-minute bars. This takes about 8-9 minutes.
echo.
python examples\fetch_etf_intraday.py --months 6

echo.
pause
endlocal
