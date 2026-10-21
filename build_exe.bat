@echo off
rem Builds dist\WaveForgeVideo\WaveForgeVideo.exe (needs Python and: pip install pyinstaller)
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --windowed --name WaveForgeVideo waveforge_video.py
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo Built dist\WaveForgeVideo\WaveForgeVideo.exe
