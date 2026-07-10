@echo off
title HELM Guardian - external watchdog
REM Runs the watchdog from an EXTERNAL copy (%LOCALAPPDATA%\helm-guardian) so the live watchdog is
REM never the in-repo file a bad fix could edit. Snapshots + logs live there too. Leave this running.
set "HELM_DIR=%~dp0"
set "GDIR=%LOCALAPPDATA%\helm-guardian"
if not exist "%GDIR%" mkdir "%GDIR%"
copy /Y "%~dp0guardian.py" "%GDIR%\guardian.py" >nul
echo Guardian watching HELM at %HELM_DIR%
echo Snapshots + logs: %GDIR%
echo Close this window to stop watching. HELM keeps running either way.
python "%GDIR%\guardian.py" watch
pause
