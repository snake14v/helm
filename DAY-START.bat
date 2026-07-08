@echo off
title Day Start
echo.
echo   VAISHAK'S DAY START
echo   ===================
echo   SINGLE PROVIDER:
echo     1. Claude          (Claude Code terminal)
echo     2. Codex           (OpenAI Codex terminal)
echo     3. Antigravity     (Google agy terminal)
echo     4. Gemma           (local chat, offline, zero cloud)
echo   COMBOS:
echo     5. Trio            (Claude + consultants warmed)
echo     6. Full company    (+ Docker / n8n / Crawl4AI + mission runner)
echo.
choice /c 123456 /n /m "  Pick your day [1-6]: "
if errorlevel 6 set P=full
if errorlevel 5 if not defined P set P=trio
if errorlevel 4 if not defined P set P=gemma
if errorlevel 3 if not defined P set P=antigravity
if errorlevel 2 if not defined P set P=codex
if errorlevel 1 if not defined P set P=claude
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0day-launcher.ps1" -Profile %P%
timeout /t 6 >nul
