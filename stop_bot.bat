@echo off
REM Double-click this file to stop the trading bot safely.
REM It drops a STOP file that the engine checks; the bot halts on its next tick.
cd /d "%~dp0"

type nul > STOP

echo ============================================================
echo   STOP signal sent.
echo   The bot will halt within about a minute (on its next check).
echo   To trade again later, just run start_bot.bat (it clears STOP).
echo ============================================================
pause >nul
