' Stop all mission-runner processes (the "pause background fleets" lever). Hidden.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -Command ""Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mission-runner\.ps1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }""", 0, True
