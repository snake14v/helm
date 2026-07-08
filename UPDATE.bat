@echo off
title HELM - Update from GitHub
cd /d "%~dp0"
echo ============================================================
echo    Updating HELM from GitHub (git pull)
echo ============================================================
echo.
where git >nul 2>nul || set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
git pull
echo.
echo Restarting the HELM server (your config.json + state stay put)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 >nul
wscript "%~dp0start-server-hidden.vbs"
echo.
echo ============================================================
echo Updated + restarted. Hard-refresh HELM in your browser (Ctrl+Shift+R).
echo ============================================================
pause
