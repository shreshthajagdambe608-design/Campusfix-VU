@echo off
setlocal
cd /d "%~dp0"
title Download Gemma for VU CampusFix
where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama was not found. Install it from https://ollama.com/download/windows and reopen this file.
  pause
  exit /b 1
)
echo Downloading the configured local model: gemma4:e4b
echo This can take several gigabytes and requires internet and disk space.
ollama pull gemma4:e4b
if errorlevel 1 (
  echo Model download failed. Check internet access, disk space, and the model tag supported by your Ollama version.
) else (
  echo Model download complete. Restart VU CampusFix to use image and text analysis.
)
pause
