@echo off
REM ===================================================================
REM  MEDIK connection supervisor -- watchdog for the Client Portal
REM  Gateway (auto-restart on crash, keep-alive, human-needed alerts).
REM  Point a LOGON scheduled task at this file so the gateway is always
REM  being healed, well before the bot fires at 06:45.
REM
REM  This wrapper is itself self-healing: the supervisor loops forever,
REM  so if it ever crashes this restarts it after RETRY_WAIT_SEC. It
REM  stops only on a clean exit (STOP_MEDIK) -- the same kill switch the
REM  bot honours -- so "stop everything" stays a single gesture.
REM ===================================================================
setlocal
cd /d "%~dp0"

set MEDIK_ETF_ACCOUNT=U26953060
set RETRY_WAIT_SEC=30

if not exist logs mkdir logs
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set STAMP=%%I
set LOGFILE=logs\medik_supervisor_%STAMP%.log

:runloop
if exist STOP_MEDIK (
    echo [%TIME%] STOP_MEDIK present -- supervisor not starting. >> "%LOGFILE%"
    exit /b 0
)

echo ================================================================= >> "%LOGFILE%"
echo supervisor start %DATE% %TIME% >> "%LOGFILE%"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" ops\medik_supervisor.py >> "%LOGFILE%" 2>&1
) else (
    python ops\medik_supervisor.py >> "%LOGFILE%" 2>&1
)
set RC=%ERRORLEVEL%
echo supervisor exited %DATE% %TIME% rc=%RC% >> "%LOGFILE%"

if "%RC%"=="0" exit /b 0
if exist STOP_MEDIK exit /b 0
echo supervisor died (rc=%RC%) -- restarting in %RETRY_WAIT_SEC%s >> "%LOGFILE%"
ping -n %RETRY_WAIT_SEC% 127.0.0.1 >nul
goto runloop
