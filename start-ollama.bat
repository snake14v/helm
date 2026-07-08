@echo off
title HELM - Start Ollama (local Gemma / free models)
echo Starting the local Ollama server (serves Gemma + your pulled models)...
echo Leave this window open while you want local models available.
echo (If it says "address already in use", Ollama is already running - you're good.)
echo.
ollama serve
pause
