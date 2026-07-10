@echo off
title GlassPanel - REVERT to last good snapshot
REM Panic button: full-restore GlassPanel's code from the last good external snapshot, then restart it.
REM Uses the EXTERNAL guardian copy (fate-independent); falls back to the in-repo one.
REM Never bare `python` - the Store alias virtualizes reads/writes and would restore from a shadow folder.
setlocal
set "HELM_DIR=%~dp0"
set "G=%LOCALAPPDATA%\helm-guardian\guardian.py"
if not exist "%G%" set "G=%~dp0guardian.py"

set "PY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "C:\Python312\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"
if not defined PY ( where py >nul 2>&1 && set "PY=py -3" )
if not defined PY ( echo [X] No real Python found (do NOT use the Store Python). & pause & exit /b 1 )

echo Reverting GlassPanel to the last known-good snapshot...
%PY% "%G%" revert latest
echo.
echo Restarting GlassPanel...
wscript "%~dp0start-server-hidden.vbs"
echo Done. Hard-refresh the app (Ctrl+Shift+R) once it is back.
pause
