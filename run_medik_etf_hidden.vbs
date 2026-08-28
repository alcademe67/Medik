' Launch the MEDIK ETF bot with NO console window.
' The bot writes everything to logs\medik_etf_YYYY-MM-DD.log, so the
' console the scheduled task used to show was always empty -- this
' wrapper runs the same run_medik_etf.bat with the window hidden.
' Point the "MEDIK ETF AUTO TRADER" task at THIS file via wscript.exe.
CreateObject("WScript.Shell").Run """C:\Users\Administrator\Medik\run_medik_etf.bat""", 0, False
