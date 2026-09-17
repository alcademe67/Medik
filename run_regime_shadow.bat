@echo off
cd /d C:\Users\Administrator\Medik
if exist STOP_MEDIK goto :eof
if exist ".venv\Scripts\python.exe" ( ".venv\Scripts\python.exe" ops\regime_shadow.py ) else ( python ops\regime_shadow.py )
