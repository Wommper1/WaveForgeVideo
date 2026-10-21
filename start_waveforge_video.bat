@echo off
cd /d "%~dp0"
title WAVEFORGE VIDEO
where py >nul 2>nul
if %errorlevel%==0 (
  py waveforge_video.py
) else (
  python waveforge_video.py
)
pause
