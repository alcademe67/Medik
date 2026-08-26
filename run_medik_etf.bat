@echo off
REM ===================================================================
REM  MEDIK ETF live bot -- the entry point for Windows Task Scheduler.
REM
REM  Point the scheduled task at THIS FILE, not at python.exe. A
REM  scheduled task gets a fresh environment: variables typed with
REM  `set` in a console do not survive into it, so the bot would start
REM  disarmed (or fail to resolve the account) every single morning and
REM  nothing would say so. Setting them here is what makes an
REM  unattended run behave like the one you tested by hand.
REM
REM  Output is redirected to a dated log because Task Scheduler throws
REM  stdout away. Without this you would have no record of a run at all.
REM
REM  SELF-HEALING (added 2026-08-26, owner: "make sure bot is always
REM  connected and running"): the task fires once at 06:45, so a bot
REM  that exits mid-session would otherwise stay down until tomorrow.
REM  This wrapper retries every 5 minutes on the recoverable exit codes
REM  (3 = no TWS, 4 = preflight/quote session, 5 = incoherent state,
REM  or a crash) and stops only on a clean exit 0 (kill switch, not a
REM  trading day, or session ended), on STOP_MEDIK appearing, or after
REM  MAX_RETRIES attempts (~ the whole session at 5-minute spacing).
REM  A gateway that comes alive mid-morning is picked up by the next
REM  retry with no human involved.
REM ===================================================================
setlocal
cd /d "%~dp0"

REM --- account (this login manages two; it must be named) ------------
set IBKR_ACCOUNT=U26953060
set MEDIK_ETF_ACCOUNT=U26953060

REM --- arming: BOTH keys are required to send live orders -----------
set MEDIK_ETF_MODE=live
set MEDIK_ETF_LIVE=true
set LIVE_RISK_ACK=true

REM --- quotes: the TWS socket cannot serve this account's real-time
REM     feed (error 10089, licence boundary -- see CLAUDE.md). Quotes
REM     come from the Client Portal Gateway instead; if it is not
REM     running and logged in at https://localhost:5000 the bot exits
REM     at preflight rather than trading on nothing.
set MEDIK_ETF_QUOTE_SOURCE=cpapi

set MAX_RETRIES=80
set RETRY_WAIT_SEC=300
set ATTEMPT=0

if not exist logs mkdir logs
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set STAMP=%%I
set LOGFILE=logs\medik_etf_%STAMP%.log

:runloop
if exist STOP_MEDIK (
    echo [%TIME%] STOP_MEDIK present -- not starting. >> "%LOGFILE%"
    echo STOP_MEDIK present -- not starting.
    exit /b 0
)

set /a ATTEMPT+=1
echo. >> "%LOGFILE%"
echo ================================================================= >> "%LOGFILE%"
echo attempt %ATTEMPT%/%MAX_RETRIES% started %DATE% %TIME% >> "%LOGFILE%"
echo ================================================================= >> "%LOGFILE%"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" examples\medik_etf_live.py >> "%LOGFILE%" 2>&1
) else (
    python examples\medik_etf_live.py >> "%LOGFILE%" 2>&1
)
set RC=%ERRORLEVEL%

echo attempt %ATTEMPT% finished %DATE% %TIME% exit=%RC% >> "%LOGFILE%"

if "%RC%"=="0" (
    echo clean exit -- done for the day. >> "%LOGFILE%"
    exit /b 0
)

if %ATTEMPT% GEQ %MAX_RETRIES% (
    echo NONZERO EXIT %RC% and retry budget exhausted -- giving up until the next scheduled run. >> "%LOGFILE%"
    exit /b %RC%
)

echo exit %RC% is recoverable -- retrying in %RETRY_WAIT_SEC%s. >> "%LOGFILE%"
ping -n %RETRY_WAIT_SEC% 127.0.0.1 >nul
goto runloop
