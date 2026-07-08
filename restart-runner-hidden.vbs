' Kill any hung/existing mission-runner, then relaunch it fresh - all hidden.
' Triggered by the dashboard's "restart runner" / mission self-heal. Idempotent:
' if nothing is running it just starts one; if one is hung it replaces it.
Set sh = CreateObject("WScript.Shell")
base = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
' 1) kill existing runner(s) by command-line match (wait for it to finish)
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -Command ""Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mission-runner\.ps1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }""", 0, True
' 2) relaunch a fresh runner (do not wait)
sh.Run "powershell -ExecutionPolicy Bypass -NoProfile -File """ & base & "\mission-runner.ps1""", 0, False
