@echo off
REM Unattended mission runner: polls the board every 60s and executes queued missions
REM in headless Claude sessions. Leave this window running; close it to stop the runner.
cd /d "%~dp0"
echo Mission runner starting - queued missions will be executed automatically.
echo Close this window to stop. Logs: runner-logs\
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0mission-runner.ps1"
pause
