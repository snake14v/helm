@echo off
title GlassPanel Guardian - external watchdog
REM Runs the watchdog from an EXTERNAL copy (%LOCALAPPDATA%\helm-guardian) so the live watchdog is
REM never the in-repo file a bad fix could edit. Snapshots + logs live there too. Leave this running.
REM
REM CRITICAL: never invoke bare `python` here. On Windows that resolves to the Microsoft Store alias,
REM whose filesystem virtualization SILENTLY redirects the guardian's snapshots/heartbeat/log into
REM %LOCALAPPDATA%\Packages\...\LocalCache\Local\helm-guardian - a shadow folder the dashboard and
REM REVERT-HELM.bat never read. The watchdog looks armed and cannot actually save you. (Hit live 2026-07-10.)
setlocal
set "HELM_DIR=%~dp0"
set "GDIR=%LOCALAPPDATA%\helm-guardian"
if not exist "%GDIR%" mkdir "%GDIR%"
copy /Y "%~dp0guardian.py" "%GDIR%\guardian.py" >nul

REM --- resolve a REAL interpreter (never the WindowsApps shim) ---
set "PY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "C:\Python312\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"
if not defined PY (
  where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  echo [X] No real Python found. Install python.org Python, or run:  py -3 guardian.py watch
  echo     Do NOT use the Microsoft Store Python - it virtualizes the guardian's writes.
  pause & exit /b 1
)

echo Guardian watching GlassPanel at %HELM_DIR%
echo Interpreter: %PY%
echo Snapshots + logs: %GDIR%
echo Close this window to stop watching. GlassPanel keeps running either way.
%PY% "%GDIR%\guardian.py" watch
pause
