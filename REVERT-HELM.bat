@echo off
title HELM - REVERT to last good snapshot
REM Panic button: full-restore HELM's code from the last good external snapshot, then restart it.
REM Uses the EXTERNAL guardian copy (fate-independent); falls back to the in-repo one.
set "HELM_DIR=%~dp0"
set "G=%LOCALAPPDATA%\helm-guardian\guardian.py"
if not exist "%G%" set "G=%~dp0guardian.py"
echo Reverting HELM to the last known-good snapshot...
python "%G%" revert latest
echo.
echo Restarting HELM...
wscript "%~dp0start-server-hidden.vbs"
echo Done. Hard-refresh the app (Ctrl+Shift+R) once it is back.
pause
