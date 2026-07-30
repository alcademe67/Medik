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
echo   Your DASHBOARD will open in the browser: http://localhost:8787
echo   (If it doesn't, open that address yourself.)
echo   To stop: click the red STOP button on the dashboard,
echo   double-click stop_bot.bat, or close this window.
echo ============================================================
echo.
REM Open the dashboard in the default browser. The page keeps retrying until
REM the engine's built-in server is up (a second or two), so order is fine.
start "" http://localhost:8787
python -m bot.live_engine

echo.
echo The bot has stopped. Press a key to close this window.
pause >nul
