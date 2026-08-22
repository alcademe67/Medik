@echo off
REM Double-click to backtest V2 at several account sizes.
REM Run fetch_data.bat FIRST — this reads the cache it creates.

setlocal
set REPO=%~dp0
set VENV_DIR=%REPO%venv
cd /d "%REPO%"

if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
)

for %%E in (290 500 1000 2500 5000) do (
    echo.
    echo ==================================================================
    echo   V2  equity %%E
    echo ==================================================================
    python backtest\medik_etf_bt.py --v2 --equity=%%E
)

echo.
echo ==================================================================
echo   V1 at 500 for comparison
echo ==================================================================
python backtest\medik_etf_bt.py --v1 --equity=500

echo.
pause
endlocal
