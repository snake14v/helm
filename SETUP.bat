@echo off
setlocal EnableDelayedExpansion
title HELM Setup
color 0b
echo.
echo   ==========================================================
echo      HELM - agent mission control  ::  first-time setup
echo   ==========================================================
echo.
echo   This sets the API keys/tokens HELM can use and starts it.
echo   Every key is OPTIONAL - press ENTER to skip any you don't have.
echo   Keys are stored in your Windows user environment (setx), not in any file.
echo.
echo   ----------------------------------------------------------

REM --- prerequisites ---
where python >nul 2>nul || (echo   [!] Python not found. Install Python 3.10+ from python.org, then re-run.& pause & exit /b 1)
echo   [ok] Python found.
where node   >nul 2>nul && echo   [ok] Node found.  || echo   [--] Node not found ^(optional; needed only for some agent CLIs^).
echo.

echo   Enter your keys ^(ENTER = skip^):
echo.

set /p CLAUDETOK=  Claude Code OAuth token (run 'claude setup-token' to get one) :
if not "!CLAUDETOK!"=="" ( setx CLAUDE_CODE_OAUTH_TOKEN "!CLAUDETOK!" >nul & echo      saved CLAUDE_CODE_OAUTH_TOKEN )

set /p ORK=  OpenRouter API key (openrouter.ai/keys - unlocks Kimi/DeepSeek/Qwen free) :
if not "!ORK!"=="" ( setx OPENROUTER_API_KEY "!ORK!" >nul & echo      saved OPENROUTER_API_KEY )

set /p GRK=  Groq API key (console.groq.com/keys) :
if not "!GRK!"=="" ( setx GROQ_API_KEY "!GRK!" >nul & echo      saved GROQ_API_KEY )

set /p MSK=  Moonshot/Kimi API key (platform.moonshot.ai) :
if not "!MSK!"=="" ( setx MOONSHOT_API_KEY "!MSK!" >nul & echo      saved MOONSHOT_API_KEY )

set /p GEMK=  Google AI Studio (Gemini) key (aistudio.google.com/apikey) :
if not "!GEMK!"=="" ( setx GEMINI_API_KEY "!GEMK!" >nul & echo      saved GEMINI_API_KEY )

echo.
echo   ----------------------------------------------------------
echo   Keys saved. NOTE: open a NEW terminal for them to take effect.
echo.
echo   Starting HELM server on http://localhost:8799 ...
start "HELM" /min python "%~dp0server.py"
timeout /t 3 /nobreak >nul
start http://localhost:8799
echo.
echo   Done. HELM is running. Optional next steps:
echo     - Pull a free local model:  ollama pull qwen2.5:3b
echo     - Connect more models:      connect-model.ps1
echo     - Autostart on login:       run start-server-hidden.vbs via Startup folder
echo.
pause
