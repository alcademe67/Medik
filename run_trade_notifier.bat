@echo off
cd /d C:\Users\Administrator\Medik
:loop
if exist STOP_MEDIK goto :eof
if exist ".venv\Scripts\python.exe" ( ".venv\Scripts\python.exe" ops\trade_notifier.py ) else ( python ops\trade_notifier.py )
timeout /t 15 /nobreak >nul
goto loop
