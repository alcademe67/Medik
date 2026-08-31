' Launches the Medik ETF run fully hidden (no console window, no focus stealing).
' Used by the "MEDIK ETF AUTO TRADER" scheduled task.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "C:\Users\Administrator\Medik"
shell.Run """C:\Users\Administrator\Medik\run_medik_etf.bat""", 0, False
Set shell = Nothing
