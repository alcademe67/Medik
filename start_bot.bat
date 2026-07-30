@echo off
REM Double-click this file to update and start the trading bot.
REM It changes into its own folder, gets the latest code, and runs the engine.
cd /d "%~dp0"

REM Clear any leftover emergency-STOP file so the bot can start.
if exist STOP del STOP

REM Force the fast/aggressive settings regardless of what .env says.
REM (python-dotenv does not override variables already set in the environment.)
set "KLINE_TYPE=1min"
set "TRADE_MODE=momentum"

echo ============================================================
echo   Updating the bot (git pull)...
echo ============================================================
git pull

echo.
echo ============================================================
echo   Starting the trading bot.
echo   Your DASHBOARD opens in your browser AUTOMATICALLY in a few
echo   seconds - you do not need to type anything.
echo   If it doesn't open, start Chrome/Edge and go to this address:
echo         http://localhost:8787
echo   To stop: click the red STOP button on the dashboard,
echo   double-click stop_bot.bat, or close this window.
echo ============================================================
echo.
REM The engine opens the dashboard in the browser itself, right after its
REM built-in web server is ready (see bot/dashboard.py _maybe_open_browser),
REM so there is no early 'connection refused' race.
python -m bot.live_engine

echo.
echo The bot has stopped. Press a key to close this window.
pause >nul
