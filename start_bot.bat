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
echo   Watch your PHONE (ntfy) for the startup status.
echo   To stop: double-click stop_bot.bat, or close this window.
echo ============================================================
echo.
python -m bot.live_engine

echo.
echo The bot has stopped. Press a key to close this window.
pause >nul
