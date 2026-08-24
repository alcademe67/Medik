@echo off
REM Read-only market-data diagnostic. Places no orders.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" examples\mktdata_probe.py %*
) else (
    python examples\mktdata_probe.py %*
)
echo.
pause
