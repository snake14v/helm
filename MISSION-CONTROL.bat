@echo off
REM Start Mission Control server + open as a windowed Windows app (Edge app mode - no browser chrome).
cd /d "%~dp0"
start "MissionControlSrv" /min python server.py
timeout /t 2 /nobreak >nul
where msedge >nul 2>nul
if %errorlevel%==0 (
  start msedge --app=http://localhost:8799
) else if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
  start "" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" --app=http://localhost:8799
) else (
  start http://localhost:8799
)
