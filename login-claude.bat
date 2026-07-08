@echo off
title HELM - Refresh Claude headless token
echo ============================================================
echo    Generating a headless Claude OAuth token (valid ~1 year)
echo ============================================================
echo.
echo This is what the unattended mission-runner uses to auth.
echo A browser may open to authorize; then a token (sk-ant-oat...) is printed.
echo.
set "CLAUDE_EXE="
for /f "delims=" %%f in ('dir /b /s "%APPDATA%\Claude\claude-code\*\claude.exe" 2^>nul') do set "CLAUDE_EXE=%%f"
if not defined CLAUDE_EXE (
  echo claude.exe not found under %%APPDATA%%\Claude - is Claude Code installed?
  pause
  exit /b
)
"%CLAUDE_EXE%" setup-token
echo.
echo ------------------------------------------------------------
echo Copy the printed sk-ant-oat... token, then run THIS (one line):
echo    setx CLAUDE_CODE_OAUTH_TOKEN "PASTE_TOKEN_HERE"
echo The headless runner reads that variable. Close this window after.
echo ------------------------------------------------------------
pause
