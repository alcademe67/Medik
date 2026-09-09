' Launches the MEDIK connection supervisor fully hidden (no console window).
' Used by the Startup-folder entry so the gateway watchdog is always running.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "C:\Users\Administrator\Medik"
shell.Run """C:\Users\Administrator\Medik\run_medik_supervisor.bat""", 0, False
Set shell = Nothing
