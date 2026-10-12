@echo off
title Kiro Batch Studio GUI
echo Cleaning previous GUI instances on port 8080...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8080 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo Starting Kiro Batch Studio GUI...
echo Access Web UI at: http://localhost:8080
python kiro_gui.py %*
if errorlevel 1 pause
